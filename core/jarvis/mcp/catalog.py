"""Каталог серверов MCP из официального реестра (registry.modelcontextprotocol.io).

Запись реестра → карточка для интерфейса: {name, title, description, version, repository, install}, где
install — готовая настройка подключения: npm → «npx -y <пакет>@<версия>», PyPI → «uvx <пакет>», OCI → «docker
run -i --rm <образ>», удалённый сервер → Streamable HTTP по адресу. Формат реестра менялся (в новых версиях
записи обёрнуты в {"server": {...}, "_meta": {...}}, поля в camelCase; в старых — snake_case), поэтому разбор
осторожный.
"""

import logging
import re
from typing import Any

import httpx

from jarvis import __version__
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
PAGE_SIZE = 30


def _get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _flag(data: dict[str, Any], *keys: str) -> bool:
    return bool(_get(data, *keys, default=False))


def _env_vars(raw: Any) -> list[dict[str, Any]]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        entry: dict[str, Any] = {
            "name": str(item["name"]),
            "description": str(item.get("description") or ""),
            "required": _flag(item, "isRequired", "is_required", "required"),
            "secret": _flag(item, "isSecret", "is_secret", "secret"),
        }
        default = _get(item, "default", "value")
        if default is not None:
            entry["default"] = str(default)
        out.append(entry)
    return out


def _arguments(raw: Any) -> list[str]:
    """packageArguments / runtimeArguments реестра → строки командной строки."""
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            out.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "positional")
        value = _get(item, "value", "default")
        hint = _get(item, "valueHint", "value_hint")
        required = _flag(item, "isRequired", "is_required")
        if kind == "named":
            name = str(item.get("name") or "")
            if not name:
                continue
            if value is not None:
                out += [name, str(value)]
            elif required:
                out += [name, f"<{hint}>"] if hint else [name]
        else:
            if value is not None:
                out.append(str(value))
            elif required and hint:
                out.append(f"<{hint}>")
    return out


def package_install(package: dict[str, Any]) -> dict[str, Any] | None:
    registry = str(_get(package, "registryType", "registry_type", "registry_name", default="")).lower()
    identifier = str(_get(package, "identifier", "name", default=""))
    version = str(_get(package, "version", default="") or "")
    transport = package.get("transport")
    transport_type = transport.get("type") if isinstance(transport, dict) else transport
    if not identifier or (transport_type and transport_type != "stdio"):
        return None  # пакет поднимает свой HTTP-сервер — такой запуск настраивается вручную
    env_vars = _env_vars(_get(package, "environmentVariables", "environment_variables", default=[]))
    runtime = _arguments(_get(package, "runtimeArguments", "runtime_arguments", default=[]))
    arguments = _arguments(_get(package, "packageArguments", "package_arguments", default=[]))
    if registry == "npm":
        spec = f"{identifier}@{version}" if version and version != "latest" else identifier
        command, args = "npx", ["-y", *runtime, spec, *arguments]
    elif registry == "pypi":
        command, args = "uvx", [*runtime, identifier, *arguments]
    elif registry in ("oci", "docker"):
        image = identifier
        if version and ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
            image = f"{image}:{version}"
        env_flags = [part for var in env_vars for part in ("-e", var["name"])]
        command, args = "docker", ["run", "-i", "--rm", *env_flags, *runtime, image, *arguments]
    elif registry == "nuget":
        spec = f"{identifier}@{version}" if version else identifier
        command, args = "dnx", [*runtime, spec, "--yes", *arguments]
    else:
        return None
    return {"transport": "stdio", "command": command, "args": args, "url": "", "env_vars": env_vars}


def remote_install(remote: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(_get(remote, "type", "transport_type", default="")).lower().replace("_", "-")
    url = str(remote.get("url") or "")
    if kind not in ("streamable-http", "streamable", "http") or not url.startswith(("http://", "https://")):
        return None  # старый SSE-транспорт не поддерживаем
    return {
        "transport": "http",
        "command": "",
        "args": [],
        "url": url,
        "env_vars": [],
        "headers": _env_vars(remote.get("headers")),
    }


def _installs(server: dict[str, Any]) -> list[dict[str, Any]]:
    packages = [p for p in server.get("packages") or [] if isinstance(p, dict)]
    remotes = [r for r in server.get("remotes") or [] if isinstance(r, dict)]
    from_packages = [i for i in (package_install(p) for p in packages) if i]
    from_remotes = [i for i in (remote_install(r) for r in remotes) if i]
    order = {"npx": 0, "uvx": 1, "dnx": 2, "docker": 3}
    from_packages.sort(key=lambda i: order.get(i["command"], 9))
    # удалённый сервер без обязательных заголовков — ничего ставить не нужно; иначе сначала пакеты
    open_remotes = [r for r in from_remotes if not any(h["required"] for h in r["headers"])]
    rest = [r for r in from_remotes if r not in open_remotes]
    return open_remotes + from_packages + rest


def _title(server: dict[str, Any], name: str) -> str:
    title = _get(server, "title")
    if title:
        return str(title)
    tail = name.rsplit("/", 1)[-1]
    tail = re.sub(
        r"^(mcp[-_]server[-_]|server[-_]|mcp[-_])|([-_]mcp[-_]server|[-_]mcp|[-_]server)$",
        "",
        tail,
        flags=re.I,
    )
    words = re.split(r"[-_.\s]+", tail) if tail else [name]
    return " ".join(w[:1].upper() + w[1:] for w in words if w) or name


def map_server(server: dict[str, Any]) -> dict[str, Any] | None:
    name = str(server.get("name") or "")
    if not name:
        return None
    repository = server.get("repository")
    repo_url = repository.get("url") if isinstance(repository, dict) else repository
    version_detail = server.get("version_detail") if isinstance(server.get("version_detail"), dict) else {}
    installs = _installs(server)
    item: dict[str, Any] = {
        "name": name,
        "title": _title(server, name),
        "description": str(server.get("description") or ""),
        "version": str(_get(server, "version", default="") or version_detail.get("version") or ""),
        "repository": str(repo_url or ""),
        "website": str(_get(server, "websiteUrl", "website_url", default="") or ""),
        "install": installs[0] if installs else None,
        "installs": installs,
    }
    return item


def _is_latest(entry: dict[str, Any], server: dict[str, Any]) -> bool | None:
    meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else server.get("_meta")
    if not isinstance(meta, dict):
        return None
    for value in meta.values():
        if isinstance(value, dict) and "isLatest" in value:
            return bool(value["isLatest"])
        if isinstance(value, dict) and "is_latest" in value:
            return bool(value["is_latest"])
    return None


def map_catalog(data: Any) -> dict[str, Any]:
    """Ответ реестра → {items, next_cursor, total}. Повторы одного сервера (разные версии) — одна карточка."""
    if not isinstance(data, dict):
        return {"items": [], "next_cursor": None, "total": 0}
    entries = data.get("servers")
    if not isinstance(entries, list):
        entries = data.get("items") if isinstance(data.get("items"), list) else []
    items: dict[str, dict[str, Any]] = {}
    latest: dict[str, bool] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        server = entry.get("server") if isinstance(entry.get("server"), dict) else entry
        try:
            item = map_server(server)
        except Exception:
            log.warning("запись каталога MCP не разобрана: %r", str(server)[:200], exc_info=True)
            continue
        if item is None:
            continue
        is_latest = _is_latest(entry, server)
        known = items.get(item["name"])
        if known is None or (is_latest and not latest.get(item["name"])):
            items[item["name"]] = item
            latest[item["name"]] = bool(is_latest)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    next_cursor = _get(metadata, "nextCursor", "next_cursor") or _get(data, "next_cursor", "nextCursor")
    total = _get(metadata, "total", "totalCount", "total_count")
    return {
        "items": list(items.values()),
        "next_cursor": str(next_cursor) if next_cursor else None,
        "total": int(total) if isinstance(total, int | float) else len(items),
    }


class McpCatalog:
    """Поиск в реестре MCP. transport — подмена сети в тестах."""

    def __init__(self) -> None:
        self.transport: httpx.AsyncBaseTransport | None = None
        self.url = REGISTRY_URL

    async def search(self, query: str = "", cursor: str = "", limit: int = PAGE_SIZE) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100)), "version": "latest"}
        if query.strip():
            params["search"] = query.strip()
        if cursor:
            params["cursor"] = cursor
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(15.0, connect=8.0),
            "headers": {"Accept": "application/json", "User-Agent": f"Jarvis/{__version__}"},
            "follow_redirects": True,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.get(self.url, params=params)
                if 400 <= response.status_code < 500:
                    # реестр не знает «version=latest» — без него (повторы версий уберёт map_catalog)
                    params.pop("version", None)
                    response = await client.get(self.url, params=params)
        except httpx.HTTPError as e:
            raise UserFacingError("Каталог MCP недоступен — нет связи с реестром") from e
        if response.status_code >= 400:
            raise UserFacingError(f"Каталог MCP недоступен (ошибка {response.status_code})")
        try:
            data = response.json()
        except ValueError as e:
            raise UserFacingError("Каталог MCP вернул непонятный ответ") from e
        return map_catalog(data)
