"""«Jarvis как сервер MCP»: инструменты Jarvis AI для программ-провайдеров (Claude Code, Gemini CLI,
Qwen Code).

Маршрут /api/ai/mcp — Streamable HTTP (официальный SDK mcp: без сессий, ответы JSON). Он доступен только на
127.0.0.1, как весь API ядра, и требует два токена: токен ядра (X-Jarvis-Token — проверяет общий middleware)
и токен хода (X-Jarvis-Turn). Токен хода выдаётся на один ответ ИИ (bind) и отзывается по его окончании
(unbind); по нему вызов инструмента попадает в нужный чат и ход, и выполняется тем же кодом, что у провайдеров
по ключу: карточки «Выполнено ✓», подтверждения, источники, «Запомнил…», ограничения режима «Чат».

Имя инструмента у программы — «mcp__jarvis__<имя>» (Claude Code, Qwen Code) или «mcp_jarvis_<имя>»
(Gemini CLI); длинные имена (инструменты подключённых серверов MCP) укорачиваются до 51 символа с
хвостом-хэшем, чтобы уложиться в ограничение 64 символа на имя инструмента у моделей.
"""

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from jarvis import __version__

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

SERVER_NAME = "jarvis"  # имя сервера в настройках программ: инструменты mcp__jarvis__…
ROUTE = "/api/ai/mcp"
TOKEN_HEADER = "X-Jarvis-Token"
TURN_HEADER = "X-Jarvis-Turn"
NAME_LIMIT = 51  # 64 (предел имени инструмента) минус «mcp__jarvis__»
START_TIMEOUT_S = 10.0

NO_TURN = "Нет доступа: ход Jarvis AI не найден или уже закончился"
NOT_READY = "Сервер MCP Jarvis ещё не запущен"


@dataclass
class McpResult:
    """Ответ инструмента для программы: текст (и картинки — data URL)."""

    text: str
    is_error: bool = False
    images: list[str] = field(default_factory=list)


ToolHandler = Callable[[str, dict[str, Any]], Awaitable[McpResult]]


@dataclass
class McpBinding:
    """Инструменты одного хода ИИ: что видит программа и кто выполняет вызовы."""

    token: str
    tools: list[dict[str, Any]]  # [{name, description, inputSchema}] — имена, которые видит программа
    handler: ToolHandler  # (имя Jarvis, аргументы) → результат
    names: dict[str, str]  # имя у программы → имя инструмента Jarvis
    label: str = ""
    tasks: set["asyncio.Task[Any]"] = field(default_factory=set)
    busy: int = 0  # вызовов в работе (ход ждёт инструмент — например подтверждения человека)
    calls: int = 0
    last_activity: float = field(default_factory=time.monotonic)


def exposed_name(name: str) -> str:
    """Имя инструмента для программы: латиница, цифры, «_» и «-», не длиннее NAME_LIMIT."""
    clean = "".join(c if c.isascii() and (c.isalnum() or c in "_-") else "_" for c in name) or "tool"
    if len(clean) <= NAME_LIMIT and clean == name:
        return clean
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{clean[: NAME_LIMIT - 9]}_{digest}"


def _image_block(url: str) -> types.ImageContent | None:
    """data:image/png;base64,… → ImageContent."""
    head, sep, data = url.partition(",")
    if not sep or not head.startswith("data:") or ";base64" not in head:
        return None
    mime = head[5:].split(";", 1)[0] or "image/png"
    return types.ImageContent(type="image", data=data, mimeType=mime)


class JarvisMcpServer:
    """Сервер MCP ядра: привязки ходов к токенам и обработка запросов /api/ai/mcp."""

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._bindings: dict[str, McpBinding] = {}
        self._server: Server[Any, Any] = Server(SERVER_NAME, version=__version__)
        self._manager: StreamableHTTPSessionManager | None = None
        self._task: asyncio.Task[Any] | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Event | None = None
        self._register()

    # --- адрес и токены ---------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        base = (getattr(self.ctx, "api_url", "") or "").rstrip("/")
        return base + ROUTE if base else ""

    @property
    def available(self) -> bool:
        """Адрес и токен ядра известны (ядро запущено как сервер, а не в тестовом клиенте без порта)."""
        return bool(self.url and getattr(self.ctx, "api_token", ""))

    def headers(self, binding: McpBinding) -> dict[str, str]:
        return {TOKEN_HEADER: str(getattr(self.ctx, "api_token", "") or ""), TURN_HEADER: binding.token}

    # --- ходы ---------------------------------------------------------------------------------------------
    def bind(self, tools: list[dict[str, Any]], handler: ToolHandler, label: str = "") -> McpBinding:
        """Выдать токен хода: tools — [{name, description, inputSchema}] с именами Jarvis."""
        names: dict[str, str] = {}
        exposed: list[dict[str, Any]] = []
        for tool in tools:
            name = exposed_name(str(tool["name"]))
            if name in names:
                continue
            names[name] = str(tool["name"])
            exposed.append({**tool, "name": name})
        binding = McpBinding(secrets.token_urlsafe(32), exposed, handler, names, label)
        self._bindings[binding.token] = binding
        return binding

    def unbind(self, binding: McpBinding | None) -> None:
        """Отозвать токен хода; вызовы, которые ещё идут, отменяются."""
        if binding is None:
            return
        self._bindings.pop(binding.token, None)
        for task in list(binding.tasks):
            task.cancel()

    def binding(self, token: str) -> McpBinding | None:
        return self._bindings.get(token) if token else None

    # --- жизненный цикл ------------------------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._task = self.ctx.spawn(self._serve(), "mcp-server")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), START_TIMEOUT_S)

    async def _serve(self) -> None:
        """Группа задач SDK живёт в своей задаче: вход и выход из неё — в одной задаче, как требует anyio."""
        manager = StreamableHTTPSessionManager(app=self._server, json_response=True, stateless=True)
        assert self._stop is not None and self._ready is not None
        async with manager.run():
            self._manager = manager
            self._ready.set()
            try:
                await self._stop.wait()
            finally:
                self._manager = None

    async def stop(self) -> None:
        for binding in list(self._bindings.values()):
            self.unbind(binding)
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(task, 5.0)

    # --- HTTP ---------------------------------------------------------------------------------------------
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI-обработчик /api/ai/mcp (токен ядра уже проверил middleware): токен хода → SDK."""
        request = Request(scope, receive)
        binding = self.binding(request.headers.get(TURN_HEADER, ""))
        if binding is None:
            await JSONResponse({"detail": NO_TURN}, status_code=403)(scope, receive, send)
            return
        manager = self._manager
        if manager is None:
            await JSONResponse({"detail": NOT_READY}, status_code=503)(scope, receive, send)
            return
        binding.last_activity = time.monotonic()
        await manager.handle_request(scope, receive, send)

    # --- обработчики SDK ----------------------------------------------------------------------------------
    def _current(self) -> McpBinding | None:
        """Привязка хода текущего запроса (по заголовку X-Jarvis-Turn)."""
        with contextlib.suppress(LookupError, AttributeError):
            request = self._server.request_context.request
            if request is not None:
                return self.binding(request.headers.get(TURN_HEADER, ""))
        return None

    def _register(self) -> None:
        server = self._server

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            binding = self._current()
            if binding is None:
                return []
            return [
                types.Tool(
                    name=t["name"],
                    description=str(t.get("description") or ""),
                    inputSchema=t.get("inputSchema") or {"type": "object", "properties": {}},
                )
                for t in binding.tools
            ]

        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            binding = self._current()
            if binding is None:
                return _error_result("Ошибка: ход уже закончился")
            real = binding.names.get(name)
            if real is None:
                return _error_result(f"Ошибка: инструмента «{name}» нет")
            task = asyncio.current_task()
            if task is not None:
                binding.tasks.add(task)
            binding.busy += 1
            binding.calls += 1
            try:
                result = await binding.handler(real, dict(arguments or {}))
            except asyncio.CancelledError:
                if binding.token in self._bindings or task is None:
                    raise  # отмена от SDK (программа закрыла соединение) — пусть идёт дальше
                task.uncancel()  # ход отозван (unbind): ответить программе, что вызов прерван
                return _error_result("Остановлено пользователем")
            except Exception as e:
                log.exception("инструмент %s для программы упал", real)
                return _error_result(f"Ошибка: внутренняя ошибка Jarvis: {e}")
            finally:
                binding.busy -= 1
                binding.last_activity = time.monotonic()
                if task is not None:
                    binding.tasks.discard(task)
            content: list[types.TextContent | types.ImageContent] = []
            if result.text or not result.images:
                content.append(types.TextContent(type="text", text=result.text))
            for url in result.images:
                block = _image_block(url)
                if block is not None:
                    content.append(block)
            return types.CallToolResult(content=list(content), isError=result.is_error)


def _error_result(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)
