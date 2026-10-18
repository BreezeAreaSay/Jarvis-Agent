"""Серверы MCP из других программ на этом ПК: Claude Code, Claude Desktop, Cursor, VS Code, Codex, Gemini CLI.

Jarvis только читает их настройки: «Импорт» копирует выбранные серверы к себе, файлы программ не меняются.
Места и формат записей — те же, что у `ai-dev clients` (AI Dev System), чтобы находить его запись «ai-dev».
"""

import json
import logging
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# id, название, формат файла, где в файле серверы
CLIENTS: tuple[tuple[str, str, str, str], ...] = (
    ("claude-code", "Claude Code", "json", "mcpServers"),
    ("claude-desktop", "Claude Desktop", "json", "mcpServers"),
    ("cursor", "Cursor", "json", "mcpServers"),
    ("vscode", "VS Code", "json", "servers"),
    ("codex", "Codex", "toml", "mcp_servers"),
    ("gemini", "Gemini CLI", "json", "mcpServers"),
)
LABELS = {cid: label for cid, label, _, _ in CLIENTS}
MAX_BYTES = 4 * 1024 * 1024  # ~/.claude.json хранит и историю проектов, но не гигабайты


@dataclass
class ClientFile:
    client: str
    label: str
    format: str
    container: str
    path: Path


@dataclass
class FoundServer:
    client: str
    client_label: str
    file: str
    name: str
    transport: str = "stdio"  # stdio | http
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    problem: str = ""  # почему Jarvis не подключит его как есть («SSE», «${input:…}»)


def client_files(
    home: Path | None = None, env: Mapping[str, str] | None = None, platform: str | None = None
) -> list[ClientFile]:
    """Файлы настроек всех программ (есть они или нет). platform и env — параметры ради тестов."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    home = Path(home) if home is not None else Path.home()
    if platform == "darwin":
        app_data = home / "Library" / "Application Support"
    elif platform == "win32":
        app_data = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        app_data = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    claude_home = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else home
    codex_home = Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
    paths = {
        "claude-code": [claude_home / ".claude.json"],
        "claude-desktop": [app_data / "Claude" / "claude_desktop_config.json"],
        "cursor": [home / ".cursor" / "mcp.json"],
        "vscode": [app_data / "Code" / "User" / "mcp.json"],
        "codex": [codex_home / "config.toml"],
        "gemini": [home / ".gemini" / "settings.json"],
    }
    if platform == "win32":
        # Claude Desktop из Microsoft Store читает настройки из своего пакета (MSIX подменяет %APPDATA%)
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        try:
            for package in sorted((local / "Packages").glob("Claude_*")):
                paths["claude-desktop"].append(
                    package / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
                )
        except OSError:
            pass
    out = []
    for cid, label, fmt, container in CLIENTS:
        out.extend(ClientFile(cid, label, fmt, container, p) for p in paths[cid])
    return out


_JSONC_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def parse_jsonc(text: str) -> Any:
    """JSON с комментариями и висячими запятыми (mcp.json VS Code). Строки не трогаем."""
    stripped = _JSONC_TOKEN.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    # висячие запятые — вне строк: строки заменяем заглушками, правим, возвращаем
    strings: list[str] = []

    def hide(m: re.Match[str]) -> str:
        strings.append(m.group(0))
        return f'"\x00{len(strings) - 1}"'

    hidden = re.sub(r'"(?:\\.|[^"\\])*"', hide, stripped)
    hidden = _TRAILING_COMMA.sub(r"\1", hidden)
    restored = re.sub(r'"\x00(\d+)"', lambda m: strings[int(m.group(1))], hidden)
    return json.loads(restored)


def _str_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in raw.items() if str(k).strip()}


def parse_entry(name: str, entry: Any, cf: ClientFile) -> FoundServer | None:
    """Запись сервера → FoundServer; не похоже на сервер — None."""
    if not isinstance(entry, dict):
        return None
    found = FoundServer(client=cf.client, client_label=cf.label, file=str(cf.path), name=str(name))
    kind = str(entry.get("type") or entry.get("transport") or "").lower()
    url = entry.get("httpUrl") or entry.get("url") or entry.get("serverUrl") or ""
    if entry.get("command"):
        found.transport = "stdio"
        found.command = str(entry["command"])
        args = entry.get("args") or []
        found.args = [str(a) for a in args] if isinstance(args, list) else []
        found.env = _str_map(entry.get("env"))
    elif url:
        found.transport = "http"
        found.url = str(url)
        found.headers = _str_map(entry.get("headers") or entry.get("http_headers"))
        token_var = entry.get("bearer_token_env_var")  # Codex: токен в переменной окружения
        if token_var and os.environ.get(str(token_var)):
            found.headers.setdefault("Authorization", f"Bearer {os.environ[str(token_var)]}")
        if kind == "sse" or (not entry.get("httpUrl") and cf.client == "gemini" and kind != "http"):
            found.problem = "сервер работает по SSE — Jarvis подключается по Streamable HTTP"
    else:
        return None
    values = [found.command, *found.args, found.url, *found.env.values(), *found.headers.values()]
    if any("${input:" in v for v in values):
        found.problem = "в настройке есть ${input:…} из VS Code — значение надо вписать вручную"
    elif entry.get("envFile"):
        found.problem = "переменные берутся из файла envFile — перенесите их вручную"
    if entry.get("disabled") is True or entry.get("enabled") is False:
        found.problem = found.problem or "в программе этот сервер выключен"
    return found


def read_file(cf: ClientFile) -> list[FoundServer]:
    """Серверы из одного файла; файла нет или он битый — пустой список."""
    try:
        if not cf.path.is_file() or cf.path.stat().st_size > MAX_BYTES:
            return []
        text = cf.path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    try:
        data = tomllib.loads(text) if cf.format == "toml" else parse_jsonc(text)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        log.info("настройки %s не читаются: %s", cf.path, e)
        return []
    servers = data.get(cf.container) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    out = []
    for name, entry in servers.items():
        found = parse_entry(name, entry, cf)
        if found is not None:
            out.append(found)
    return out


def discover(
    home: Path | None = None, env: Mapping[str, str] | None = None, platform: str | None = None
) -> list[FoundServer]:
    """Все серверы MCP, записанные в программах на этом ПК."""
    out: list[FoundServer] = []
    for cf in client_files(home, env, platform):
        out.extend(read_file(cf))
    return out
