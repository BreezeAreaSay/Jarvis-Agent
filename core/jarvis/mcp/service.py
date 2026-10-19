"""Подключения MCP: настройки серверов (kv «mcp:servers»), фоновые сессии, инструменты для Jarvis AI, каталог.

Инструменты подключённых серверов уходят модели под именами mcp__<сервер>__<инструмент> (только [a-zA-Z0-9_-],
не длиннее 64 символов); call() находит по имени нужный сервер и возвращает текст ответа. Статус каждого
сервера — событие mcp.status {server_id, status, error?, tools_count}.
"""

import asyncio
import logging
import os
import re
from typing import Any

from mcp.shared.exceptions import McpError

from jarvis import paths
from jarvis.ai.format import one_line
from jarvis.mcp import clients
from jarvis.mcp.catalog import McpCatalog
from jarvis.mcp.client import McpConnection, result_text
from jarvis.mcp.config import McpServerConfig, load, masked, new_server_id, normalize, save, slug
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

CALL_TIMEOUT = 120.0
NAME_LIMIT = 64


class McpNotFound(UserFacingError):
    def __init__(self) -> None:
        super().__init__("Сервер MCP не найден")


def _part(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_-]", "_", text)).strip("_")


def tool_name(server: str, tool: str, used: set[str]) -> str:
    """Имя функции для модели: mcp__<сервер>__<инструмент>, [a-zA-Z0-9_-], до 64 символов, без повторов."""
    base = f"mcp__{_part(server)[:20] or 'server'}__{_part(tool) or 'tool'}"[:NAME_LIMIT]
    name, n = base, 2
    while name in used:
        suffix = f"_{n}"
        name = base[: NAME_LIMIT - len(suffix)] + suffix
        n += 1
    return name


class Mcp(BaseService):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.catalog = McpCatalog()
        self.call_timeout = CALL_TIMEOUT
        self._conns: dict[str, McpConnection] = {}
        self._names: dict[str, tuple[str, str]] = {}  # имя функции → (id сервера, имя инструмента)
        self._titles: dict[str, str] = {}
        self._running = False

    # --- жизненный цикл --------------------------------------------------------------------------------
    async def start(self) -> None:
        self._running = True
        for config in load(self.ctx.db):
            self._open(config)

    async def stop(self) -> None:
        self._running = False
        conns, self._conns = list(self._conns.values()), {}
        if conns:
            await asyncio.gather(*(c.stop() for c in conns), return_exceptions=True)

    def _open(self, config: McpServerConfig) -> McpConnection:
        conn = McpConnection(
            config, on_change=self._changed, log_path=paths.logs_dir() / f"mcp-{config.id}.log"
        )
        self._conns[config.id] = conn
        if self._running:
            conn.start()
        return conn

    async def _close(self, server_id: str) -> None:
        conn = self._conns.pop(server_id, None)
        if conn is not None:
            await conn.stop()

    def _changed(self, conn: McpConnection) -> None:
        data: dict[str, Any] = {
            "server_id": conn.config.id,
            "status": conn.status,
            "tools_count": len(conn.tools) if conn.status == "connected" else 0,
        }
        if conn.error:
            data["error"] = conn.error
        self.ctx.bus.publish("mcp.status", data)

    # --- настройки серверов -------------------------------------------------------------------------------
    def _configs(self) -> list[McpServerConfig]:
        return load(self.ctx.db)

    def _config(self, server_id: str) -> McpServerConfig:
        config = next((c for c in self._configs() if c.id == server_id), None)
        if config is None:
            raise McpNotFound()
        return config

    def server_json(self, config: McpServerConfig) -> dict[str, Any]:
        conn = self._conns.get(config.id)
        status = conn.status if conn is not None else ("connecting" if config.enabled else "disabled")
        connected = conn is not None and conn.status == "connected"
        return {
            "id": config.id,
            "name": config.name,
            "transport": config.transport,
            "command": config.command,
            "args": list(config.args),
            "env": masked(config.env),
            "url": config.url,
            "headers": masked(config.headers),
            "enabled": config.enabled,
            "status": status,
            "error": conn.error if conn is not None else "",
            "tools": [
                {"name": t.name, "description": one_line(t.description or t.title or "", 300)}
                for t in (conn.tools if connected and conn is not None else [])
            ],
        }

    def servers(self) -> list[dict[str, Any]]:
        return [self.server_json(c) for c in self._configs()]

    def get(self, server_id: str) -> dict[str, Any]:
        return self.server_json(self._config(server_id))

    async def add(self, data: dict[str, Any]) -> dict[str, Any]:
        configs = self._configs()
        config = normalize(data, server_id=new_server_id(str(data.get("name") or "")))
        configs.append(config)
        save(self.ctx.db, configs)
        self._open(config)
        return self.server_json(config)

    async def update(self, server_id: str, data: dict[str, Any]) -> dict[str, Any]:
        configs = self._configs()
        index = next((i for i, c in enumerate(configs) if c.id == server_id), None)
        if index is None:
            raise McpNotFound()
        config = normalize(data, server_id=server_id, previous=configs[index])
        configs[index] = config
        save(self.ctx.db, configs)
        await self._close(server_id)
        self._open(config)
        return self.server_json(config)

    async def remove(self, server_id: str) -> bool:
        configs = self._configs()
        rest = [c for c in configs if c.id != server_id]
        if len(rest) == len(configs):
            return False
        save(self.ctx.db, rest)
        await self._close(server_id)
        self.ctx.bus.publish("mcp.status", {"server_id": server_id, "status": "disabled", "tools_count": 0})
        return True

    async def reconnect(self, server_id: str) -> dict[str, Any]:
        config = self._config(server_id)
        await self._close(server_id)
        self._open(config)
        return self.server_json(config)

    async def wait_connected(self, server_id: str, timeout: float = 30.0) -> bool:
        """Дождаться подключения (тесты, кнопка «Проверить»)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            conn = self._conns.get(server_id)
            if conn is not None and conn.status == "connected":
                return True
            await asyncio.sleep(0.1)
        return False

    # --- инструменты для ИИ ----------------------------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        names: dict[str, tuple[str, str]] = {}
        titles: dict[str, str] = {}
        for conn in list(self._conns.values()):
            if conn.status != "connected":
                continue
            server = slug(conn.config.name) or conn.config.id
            for tool in conn.tools:
                name = tool_name(server, tool.name, set(names))
                names[name] = (conn.config.id, tool.name)
                titles[name] = f"{conn.config.name}: {tool.title or tool.name}"
                schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
                specs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": one_line(tool.description or tool.title or tool.name, 1000),
                            "parameters": schema or {"type": "object", "properties": {}},
                        },
                    }
                )
        self._names, self._titles = names, titles
        return specs

    def server_notes(self) -> list[dict[str, str]]:
        """Подключённые серверы для подсказок ИИ: {name, prefix (mcp__<сервер>__), instructions}."""
        out = []
        for conn in list(self._conns.values()):
            if conn.status != "connected":
                continue
            server = slug(conn.config.name) or conn.config.id
            prefix = f"mcp__{_part(server)[:20] or 'server'}__"
            out.append({"name": conn.config.name, "prefix": prefix, "instructions": conn.instructions})
        return out

    def tool_title(self, name: str) -> str:
        """Заголовок карточки: «Файлы: read_file»."""
        if name not in self._titles:
            self.tools()
        return self._titles.get(name, name)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        target = self._names.get(name)
        if target is None:
            self.tools()
            target = self._names.get(name)
        if target is None:
            raise UserFacingError(f"Инструмента {name} нет — сервер MCP отключён или изменился")
        server_id, tool = target
        conn = self._conns.get(server_id)
        if conn is None:
            raise UserFacingError("Сервер MCP не подключён")
        label = conn.config.name
        try:
            result = await conn.call(tool, arguments or {}, self.call_timeout)
        except TimeoutError:
            raise UserFacingError(f"Сервер MCP «{label}» не ответил вовремя") from None
        except McpError as e:
            raise UserFacingError(f"Сервер MCP «{label}»: {e.error.message}") from None
        except UserFacingError:
            raise
        except Exception as e:
            log.warning("MCP «%s», инструмент %s: %r", label, tool, e)
            raise UserFacingError(f"Сервер MCP «{label}»: {e}") from None
        text = result_text(result)
        if result.isError:
            raise UserFacingError(f"Инструмент «{tool}» вернул ошибку: {one_line(text, 300)}")
        return text

    # --- импорт из других программ -----------------------------------------------------------------------
    @staticmethod
    def _import_key(found: clients.FoundServer) -> str:
        return f"{found.client}|{found.file}|{found.name}"

    @staticmethod
    def _same(found: clients.FoundServer, config: McpServerConfig) -> bool:
        if found.transport != config.transport:
            return False
        if found.transport == "http":
            return found.url.rstrip("/") == config.url.rstrip("/")

        def program(command: str) -> str:
            return os.path.splitext(os.path.basename(command))[0].lower()

        return program(found.command) == program(config.command) and found.args == config.args

    async def import_candidates(self) -> list[dict[str, Any]]:
        """Серверы MCP из Claude, Cursor, VS Code, Codex и Gemini — для окна «Импорт»."""
        configs = self._configs()
        out = []
        for found in await self.ctx.run_io(clients.discover):
            out.append(
                {
                    "key": self._import_key(found),
                    "client": found.client,
                    "client_label": found.client_label,
                    "file": found.file,
                    "name": found.name,
                    "transport": found.transport,
                    "command": found.command,
                    "args": list(found.args),
                    "env": masked(found.env),
                    "url": found.url,
                    "headers": masked(found.headers),
                    "problem": found.problem,
                    "added": any(self._same(found, c) for c in configs),
                }
            )
        return out

    async def import_servers(self, keys: list[str]) -> dict[str, Any]:
        """Скопировать выбранные серверы к себе. Уже подключённые и неподходящие — в skipped с причиной."""
        wanted = set(keys)
        added: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for found in await self.ctx.run_io(clients.discover):
            key = self._import_key(found)
            if key not in wanted:
                continue
            configs = self._configs()
            if any(self._same(found, c) for c in configs):
                skipped.append({"key": key, "reason": "уже подключён"})
                continue
            if found.problem:
                skipped.append({"key": key, "reason": found.problem})
                continue
            name = found.name
            if any(c.name.casefold() == name.casefold() for c in configs):
                name = f"{name} ({found.client_label})"
            data = {
                "name": name,
                "transport": found.transport,
                "command": found.command,
                "args": found.args,
                "env": found.env,
                "url": found.url,
                "headers": found.headers,
                "enabled": True,
            }
            try:
                added.append(await self.add(data))
            except UserFacingError as e:
                skipped.append({"key": key, "reason": str(e)})
        return {"items": added, "skipped": skipped}

    # --- каталог ---------------------------------------------------------------------------------------
    async def catalog_search(self, query: str = "", cursor: str = "") -> dict[str, Any]:
        return await self.catalog.search(query, cursor)
