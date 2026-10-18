"""Подключение к одному серверу MCP (официальный Python SDK): фоновая задача держит сессию, раз в 30 секунд
проверяет связь, при обрыве переподключается с растущей паузой (2 с … 2 мин) и сообщает статус.

stdio — сервер запускается программой на этом ПК (npx, uvx, docker, python…); его вывод ошибок пишется в
<data>/logs/mcp-<id>.log, хвост журнала попадает в текст ошибки. http — Streamable HTTP по адресу.
Вся жизнь сессии (вход и выход из контекстов SDK) проходит в одной задаче — так требуют области отмены anyio.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from jarvis import __version__
from jarvis.mcp.config import McpServerConfig
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

INIT_TIMEOUT = 90.0  # первый запуск npx/uvx скачивает пакет
LIST_TIMEOUT = 30.0
PING_S = 30.0
PING_TIMEOUT = 20.0
BACKOFF_START = 2.0
BACKOFF_MAX = 120.0
_DEAD_CODES = (types.CONNECTION_CLOSED, 408)  # соединение закрыто / ответа нет

_INSTALL_HINTS = {
    "npx": "установите Node.js (nodejs.org)",
    "npm": "установите Node.js (nodejs.org)",
    "node": "установите Node.js (nodejs.org)",
    "uvx": "установите uv (docs.astral.sh/uv)",
    "uv": "установите uv (docs.astral.sh/uv)",
    "docker": "установите Docker Desktop",
    "dnx": "установите .NET 10 SDK",
}


def resolve_command(command: str) -> str:
    """npx → полный путь (на Windows — npx.cmd): так процесс запускается без оболочки."""
    if os.path.isabs(command) or os.sep in command or (os.altsep and os.altsep in command):
        return command
    return shutil.which(command) or command


def _leaf(error: BaseException) -> BaseException:
    """Из группы исключений anyio — первое настоящее."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def describe_error(error: BaseException, config: McpServerConfig) -> str:
    leaf = _leaf(error)
    base = os.path.splitext(os.path.basename(config.command))[0].lower()
    if isinstance(leaf, FileNotFoundError):
        hint = _INSTALL_HINTS.get(base)
        return f"Не найдена программа «{config.command}»" + (f" — {hint}" if hint else "")
    if isinstance(leaf, PermissionError):
        return f"Нет прав на запуск «{config.command}»"
    if isinstance(leaf, httpx.HTTPStatusError):
        status = leaf.response.status_code
        if status in (401, 403):
            return f"Сервер MCP отклонил доступ ({status}) — проверьте ключ в заголовках"
        return f"Сервер MCP ответил ошибкой {status}"
    if isinstance(leaf, httpx.ConnectError | httpx.ConnectTimeout):
        return "Нет связи с сервером MCP — проверьте адрес"
    if isinstance(leaf, TimeoutError):
        return "Сервер MCP не ответил вовремя"
    if isinstance(leaf, McpError):
        if leaf.error.code == types.CONNECTION_CLOSED:
            return "Сервер MCP закрыл соединение"
        return f"Сервер MCP: {leaf.error.message}"
    text = str(leaf) or leaf.__class__.__name__
    return f"Ошибка сервера MCP: {text}"[:300]


def _log_tail(path: Path, limit: int = 200) -> str:
    """Последняя содержательная строка журнала сервера («npm ERR! 404 Not Found …»)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [line.strip() for line in tail.splitlines() if line.strip() and not line.startswith("──")]
    return lines[-1][:limit] if lines else ""


def result_text(result: types.CallToolResult) -> str:
    """Ответ инструмента → текст: текстовые части; картинки — «[изображение]»."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.ImageContent):
            parts.append("[изображение]")
        elif isinstance(block, types.AudioContent):
            parts.append("[аудио]")
        elif isinstance(block, types.EmbeddedResource):
            resource = block.resource
            if isinstance(resource, types.TextResourceContents):
                parts.append(resource.text)
            else:
                parts.append(f"[файл {resource.uri}]")
        elif isinstance(block, types.ResourceLink):
            parts.append(f"[ресурс {block.uri}]")
    if not parts and result.structuredContent is not None:
        parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
    return "\n".join(parts).strip() or "(пустой ответ)"


async def list_all_tools(session: ClientSession) -> list[types.Tool]:
    tools: list[types.Tool] = []
    cursor: str | None = None
    for _ in range(50):
        if cursor:
            result = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
        else:
            result = await session.list_tools()
        tools.extend(result.tools)
        cursor = result.nextCursor
        if not cursor:
            break
    return tools


class McpConnection:
    """Связь с одним сервером: статус (connecting / connected / error / disabled), инструменты, вызовы."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        on_change: Callable[["McpConnection"], None],
        log_path: Path,
    ) -> None:
        self.config = config
        self.status = "connecting" if config.enabled else "disabled"
        self.error = ""
        self.tools: list[types.Tool] = []
        self.session: ClientSession | None = None
        self.log_path = log_path
        self._on_change = on_change
        self._task: asyncio.Task[None] | None = None
        self._signal = asyncio.Event()
        self._tools_dirty = False
        self._broken = False

    # --- жизненный цикл --------------------------------------------------------------------------------
    def start(self) -> None:
        if not self.config.enabled:
            self._set("disabled", "")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())
            self._task.set_name(f"mcp-{self.config.id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=8.0)
        self.session = None

    def _set(self, status: str, error: str, *, force: bool = False) -> None:
        changed = (status, error) != (self.status, self.error)
        self.status, self.error = status, error
        if changed or force or status == "connected":
            try:
                self._on_change(self)
            except Exception:
                log.exception("обработчик статуса MCP упал")

    async def _run(self) -> None:
        delay = BACKOFF_START
        while True:
            self._set("connecting", "", force=True)
            try:
                await self._session()
                error = "Сервер MCP закрыл соединение"
            except BaseException as e:
                task = asyncio.current_task()
                if isinstance(e, asyncio.CancelledError) or (task is not None and task.cancelling()):
                    self.session = None
                    raise
                if isinstance(e, KeyboardInterrupt | SystemExit):
                    raise
                error = describe_error(e, self.config)
                if self.config.transport == "stdio":
                    tail = _log_tail(self.log_path)
                    if tail and tail not in error:
                        error = f"{error}: {tail}"
                log.info("MCP «%s»: %s (%r)", self.config.name, error, _leaf(e))
            if self.status == "connected":
                delay = BACKOFF_START  # был на связи — переподключаемся быстро
            self.session = None
            self.tools = []
            self._set("error", error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)

    async def _session(self) -> None:
        config = self.config
        async with contextlib.AsyncExitStack() as stack:
            if config.transport == "stdio":
                params = StdioServerParameters(
                    command=resolve_command(config.command),
                    args=list(config.args),
                    env=dict(config.env) or None,
                )
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                errlog = stack.enter_context(self.log_path.open("a", encoding="utf-8", errors="replace"))
                read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            else:
                client = httpx.AsyncClient(
                    headers=dict(config.headers), timeout=httpx.Timeout(30.0, read=300.0)
                )
                await stack.enter_async_context(client)
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(config.url, http_client=client)
                )
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=180),
                    message_handler=self._on_message,
                    client_info=types.Implementation(name="jarvis", version=__version__),
                )
            )
            await asyncio.wait_for(session.initialize(), INIT_TIMEOUT)
            self.tools = await asyncio.wait_for(list_all_tools(session), LIST_TIMEOUT)
            self._broken = self._tools_dirty = False
            self.session = session
            self._set("connected", "")
            log.info("MCP «%s» подключён: %d инструментов", config.name, len(self.tools))
            await self._keepalive(session)

    async def _keepalive(self, session: ClientSession) -> None:
        while True:
            if not (self._broken or self._tools_dirty):
                self._signal.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._signal.wait(), PING_S)
            if self._broken:
                raise ConnectionError("соединение с сервером MCP потеряно")
            if self._tools_dirty:
                self._tools_dirty = False
                self.tools = await asyncio.wait_for(list_all_tools(session), LIST_TIMEOUT)
                self._set("connected", "")
                continue
            try:
                await asyncio.wait_for(session.send_ping(), PING_TIMEOUT)
            except McpError as e:
                if e.error.code in _DEAD_CODES:
                    raise
                # сервер ответил ошибкой — значит, жив

    async def _on_message(self, message: Any) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            self._tools_dirty = True
            self._signal.set()
        elif isinstance(message, Exception):
            log.debug("MCP «%s»: %r", self.config.name, message)

    # --- вызовы -------------------------------------------------------------------------------------------
    async def call(self, tool: str, arguments: dict[str, Any], timeout: float) -> types.CallToolResult:
        session = self.session
        if session is None or self.status != "connected":
            reason = f": {self.error}" if self.error else ""
            raise UserFacingError(f"Сервер MCP «{self.config.name}» не подключён{reason}")
        try:
            return await asyncio.wait_for(
                session.call_tool(tool, arguments, read_timeout_seconds=timedelta(seconds=timeout)),
                timeout + 5,
            )
        except McpError as e:
            if e.error.code == types.CONNECTION_CLOSED:
                self._broken = True  # переподключиться, не дожидаясь проверки связи
                self._signal.set()
            raise
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream) as e:
            self._broken = True
            self._signal.set()
            raise UserFacingError(f"Связь с сервером MCP «{self.config.name}» оборвалась") from e
