"""Настройки серверов MCP: список McpServerConfig в kv «mcp:servers».

Секреты в заголовках и переменных окружения (ключи, токены) интерфейсу отдаются замаскированными; если при
сохранении пришла маска — остаётся прежнее значение.
"""

import logging
import os
import re
import shlex
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.db import Database, new_id
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

KV_KEY = "mcp:servers"
MASK = "••••••"
_SECRET_KEY = re.compile(r"key|token|secret|passw|auth|credential|cookie|\bpat\b|bearer", re.I)

_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


def slug(text: str, limit: int = 20) -> str:
    """«Мои файлы» → «moi_faily»: имя для инструментов (только [a-z0-9_-])."""
    t = "".join(_TRANSLIT.get(ch, ch) for ch in text.casefold())
    t = re.sub(r"[^a-z0-9_-]+", "_", t)
    t = re.sub(r"_+", "_", t).strip("_-")
    return t[:limit].strip("_-")


def split_command(command: str) -> list[str]:
    """«npx -y @scope/server C:\\Data» → ["npx", "-y", "@scope/server", "C:\\Data"]."""
    try:
        parts = shlex.split(command, posix=sys.platform != "win32")
    except ValueError:
        parts = command.split()
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]


def is_secret(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


def masked(values: dict[str, str]) -> dict[str, str]:
    return {k: (MASK if v and is_secret(k) else v) for k, v in values.items()}


def _clean_map(raw: Any, previous: dict[str, str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        text = "" if value is None else str(value)
        if text == MASK and name in previous:
            text = previous[name]
        out[name] = text
    return out


def normalize(
    data: dict[str, Any], *, server_id: str, previous: McpServerConfig | None = None
) -> McpServerConfig:
    """Данные из интерфейса → проверенная настройка сервера (ошибки — UserFacingError по-русски)."""
    name = " ".join(str(data.get("name") or "").split())
    if not name:
        raise UserFacingError("Укажите название сервера MCP")
    transport = data.get("transport") or "stdio"
    if transport not in ("stdio", "http"):
        raise UserFacingError("Способ подключения MCP: stdio (программа на этом ПК) или http (адрес в сети)")
    command = str(data.get("command") or "").strip()
    args = [str(a) for a in data.get("args") or [] if str(a) != ""]
    url = str(data.get("url") or "").strip()
    if transport == "stdio":
        if not command:
            raise UserFacingError("Укажите команду запуска сервера MCP, например npx")
        if not args and " " in command and not os.path.exists(command):
            parts = split_command(command)  # вставили строку целиком: «npx -y @modelcontextprotocol/server-…»
            command, args = parts[0], parts[1:]
    elif not re.match(r"https?://\S+$", url, re.I):
        raise UserFacingError("Укажите адрес сервера MCP — ссылку http или https")
    try:
        return McpServerConfig(
            id=server_id,
            name=name[:80],
            transport=transport,
            command=command,
            args=args,
            env=_clean_map(data.get("env"), previous.env if previous else {}),
            url=url,
            headers=_clean_map(data.get("headers"), previous.headers if previous else {}),
            enabled=bool(data.get("enabled", True)),
        )
    except ValidationError as e:
        raise UserFacingError(f"Неверная настройка сервера MCP: {e.errors()[0].get('msg', '')}") from e


def new_server_id(name: str) -> str:
    return f"{slug(name, 24) or 'server'}-{new_id()[:6]}"


def load(db: Database) -> list[McpServerConfig]:
    out = []
    for raw in db.kv_get(KV_KEY, []) or []:
        try:
            out.append(McpServerConfig.model_validate(raw))
        except ValidationError as e:
            log.warning("настройка сервера MCP пропущена: %s", e)
    return out


def save(db: Database, configs: list[McpServerConfig]) -> None:
    db.kv_set(KV_KEY, [c.model_dump(mode="json") for c in configs])
