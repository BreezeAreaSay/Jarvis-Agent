"""AI Dev System (github.com/BreezeAreaSay/ai-dev-system) — локальный сервер MCP для ИИ-агентов: память
о проекте, подбор skills, задача от начала до подтверждённого завершения. Jarvis находит установку
и подключает её одной кнопкой; набор инструментов задают профили (переменная AI_DEV_PROFILES).

Где искать, по порядку:
1. запись «ai-dev» (или «ai-dev-system» из 2.x) в настройках клиентов MCP — её пишет `ai-dev setup`;
2. команда ai-dev в PATH (`npm link`);
3. клон в привычных папках или папка, которую указал человек: <клон>/ai-dev-mcp-server/bin/ai-dev.mjs
   (нужен Node.js 22.16+ или 24+).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.mcp.clients import LABELS, FoundServer, discover
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

GITHUB = "https://github.com/BreezeAreaSay/ai-dev-system"
SERVER_ID = "ai-dev"
SERVER_NAME = "AI Dev System"
ENTRY_NAMES = ("ai-dev", "ai-dev-system")
SCRIPT = Path("ai-dev-mcp-server") / "bin" / "ai-dev.mjs"
MIN_NODE = (22, 16)

# профиль, инструментов, что даёт (docs/ru/CAPABILITIES.md AI Dev System)
PROFILES: tuple[tuple[str, int, str], ...] = (
    ("core", 25, "Контекст проекта, подбор skills и задача до подтверждённого завершения"),
    ("memory", 17, "Решения, передачи между сессиями, инстинкты, галактика знаний"),
    ("coding", 18, "Эпики, plan gate, карточки проекта, правила репозитория"),
    ("git", 9, "Изолированные worktree, снапшоты и откат, разбор диффа, текст PR"),
    ("frontend", 13, "Бриф, визуальные направления, дизайн-система, референсы"),
    ("qa", 8, "Playwright и визуальный QA, бенчмарки поиска"),
    ("security", 9, "Сканы секретов и зависимостей, политика команд, инвентаризация MCP"),
    ("advanced", 36, "Диаграммы, индексы, дашборд, расходы, упаковка"),
)
PROFILE_IDS = tuple(p[0] for p in PROFILES)
DEFAULT_PROFILES = ("core",)  # 25 инструментов: каждый запрос к модели несёт их описания


@dataclass
class AiDevInstall:
    source: str  # client | command | folder
    label: str  # человеку: «из настроек Claude Desktop», «команда ai-dev», «папка …»
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    root: str = ""  # папка установки (клон или пакет), если известна
    version: str = ""
    docker: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "command": self.command,
            "args": list(self.args),
            "root": self.root,
            "version": self.version,
            "docker": self.docker,
        }


def normalize_profiles(profiles: Any) -> list[str]:
    """Список профилей: только известные, core всегда, порядок как в PROFILES."""
    if isinstance(profiles, str):
        profiles = profiles.split(",")
    wanted = {str(p).strip().lower() for p in profiles or []}
    wanted.add("core")
    return [p for p in PROFILE_IDS if p in wanted]


def tools_count(profiles: list[str]) -> int:
    return sum(n for pid, n, _ in PROFILES if pid in profiles)


def _version(package_json: Path) -> str:
    try:
        return str(json.loads(package_json.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _script_root(script: Path) -> tuple[str, str]:
    """…/ai-dev-mcp-server/bin/ai-dev.mjs → (папка установки, версия). Пакет npm: …/<пакет>/bin/ai-dev.mjs."""
    package = script.parent.parent
    version = _version(package / "package.json")
    root = package.parent if package.name == "ai-dev-mcp-server" else package
    return str(root), version


def _is_docker(found: FoundServer) -> bool:
    env = found.env
    return bool(
        env.get("AI_DEV_IMAGE")
        or env.get("AI_DEV_MCP_LAUNCHER")
        or any(re.search(r"run-mcp\.(sh|ps1)$", a, re.I) for a in found.args)
        or re.search(r"ClaudeMcpProxy\.exe$", found.command, re.I)
    )


_SCRIPT_ARG = re.compile(r"(^|[\\/])(ai-dev|server|mcp-stdio)\.mjs$", re.I)


def from_client_entry(
    found: FoundServer, which: Callable[[str], str | None] | None = None
) -> AiDevInstall | None:
    """Запись ai-dev из клиента → установка, если то, что она запускает, на месте."""
    which = which or shutil.which
    if found.transport != "stdio" or not found.command:
        return None
    label = f"из настроек {LABELS.get(found.client, found.client)}"
    env = {k: v for k, v in found.env.items() if k != "AI_DEV_PROFILES"}  # профили задаёт Jarvis
    if _is_docker(found):
        if not (os.path.exists(found.command) or which(found.command)):
            return None
        return AiDevInstall("client", label + " (Docker)", found.command, list(found.args), env, docker=True)
    command_ok = os.path.isfile(found.command) or bool(which(found.command))
    script = next((a for a in found.args if _SCRIPT_ARG.search(a)), "")
    if not command_ok or (script and not os.path.isfile(script)):
        return None  # Node или клон переехали — запись устарела
    root, version = _script_root(Path(script)) if script else ("", "")
    return AiDevInstall("client", label, found.command, list(found.args), env, root, version)


def node_version(node: str) -> tuple[int, int, int] | None:
    try:
        out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", out.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def node_ok(version: tuple[int, int, int]) -> bool:
    return version[0] >= 24 or (version[0] == MIN_NODE[0] and version[1] >= MIN_NODE[1])


def from_folder(
    folder: str | Path,
    which: Callable[[str], str | None] | None = None,
    version_of: Callable[[str], tuple[int, int, int] | None] | None = None,
) -> AiDevInstall:
    """Клон ai-dev-system (или его ai-dev-mcp-server) → установка; что не так — UserFacingError по-русски."""
    which = which or shutil.which
    version_of = version_of or node_version
    path = Path(str(folder).strip().strip('"')).expanduser()
    candidates = [path / SCRIPT, path / "bin" / "ai-dev.mjs"]
    script = next((c for c in candidates if c.is_file()), None)
    if script is None:
        raise UserFacingError(f"В папке «{path}» нет AI Dev System — выберите папку клона ai-dev-system")
    node = which("node")
    if not node:
        raise UserFacingError("Для AI Dev System нужен Node.js 22.16 или новее — установите его с nodejs.org")
    version = version_of(node)
    if version is None or not node_ok(version):
        found = ".".join(map(str, version)) if version else "неизвестной версии"
        raise UserFacingError(f"AI Dev System нужен Node.js 22.16 или новее, а здесь {found} — обновите его")
    root, ver = _script_root(script)
    return AiDevInstall("folder", f"папка {root}", node, [str(script), "mcp"], {}, root, ver)


def guess_folders(home: Path) -> list[Path]:
    """Где обычно лежит клон."""
    names = ("ai-dev-system", "ai-dev")
    bases = [home, home / "Projects", home / "projects", home / "Documents", home / "dev", home / "code"]
    bases += [home / "source" / "repos", home / "Desktop", home / "GitHub", home / "Documents" / "GitHub"]
    if sys.platform == "win32":
        bases += [Path("C:/"), Path("D:/")]
    return [b / n for b in bases for n in names]


def find(
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    version_of: Callable[[str], tuple[int, int, int] | None] | None = None,
) -> AiDevInstall | None:
    """Найти установку AI Dev System на этом ПК; нет — None."""
    which = which or shutil.which
    for found in discover(home, env, platform):
        if found.name in ENTRY_NAMES:
            install = from_client_entry(found, which)
            if install is not None:
                return install
    command = which("ai-dev")
    if command:
        return AiDevInstall("command", "команда ai-dev", command, ["mcp"])
    for folder in guess_folders(Path(home) if home is not None else Path.home()):
        if (folder / SCRIPT).is_file():
            try:
                return from_folder(folder, which, version_of)
            except UserFacingError as e:
                log.info("AI Dev System в %s не подходит: %s", folder, e)
    return None


def server_data(install: AiDevInstall, profiles: list[str]) -> dict[str, Any]:
    """Настройка сервера MCP для Jarvis (как из окна «Добавить сервер»)."""
    env = dict(install.env)
    env["AI_DEV_PROFILES"] = ",".join(normalize_profiles(profiles))
    return {
        "name": SERVER_NAME,
        "transport": "stdio",
        "command": install.command,
        "args": list(install.args),
        "env": env,
        "enabled": True,
    }
