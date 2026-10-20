"""Библиотека игр: установленные игры Steam, Epic Games и GOG — пунктами списка программ, чтобы «запусти CS2»
работало голосом, командой и у агента сразу, ссылкой лаунчера, а не кликами по его окну.

Steam: папки библиотек из steamapps/libraryfolders.vdf, игры — steamapps/appmanifest_<id>.acf (установлена —
StateFlags с битом 4), запуск — steam://rungameid/<id>. Epic: манифесты ProgramData/Epic/EpicGamesLauncher/
Data/Manifests/*.item (JSON), запуск — com.epicgames.launcher://apps/<AppName>?action=launch&silent=true. GOG:
реестр SOFTWARE/GOG.com/Games — exe игры.

Разбор файлов — без Windows (тесты); реестр — только на Windows. Ошибки не роняют список программ: нет
лаунчера или файл испорчен — без его игр.
"""

import json
import logging
import ntpath
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jarvis.platform.base import AppInfo

log = logging.getLogger(__name__)

SOURCE = "game"
STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"
EPIC_MANIFESTS = r"%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests"
# не игры: среды запуска, распространяемые библиотеки, инструменты
NOT_GAMES = re.compile(
    r"redistributable|steamworks common|proton|steam linux runtime|runtime|soundtrack|dedicated server|"
    r"\bsdk\b|unreal engine|benchmark",
    re.I,
)
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict[str, Any]:
    """Текстовый KeyValues Valve (libraryfolders.vdf, appmanifest_*.acf) → словарь; испорченное — что
    успел."""
    root: dict[str, Any] = {}
    stack = [root]
    key: str | None = None
    for match in _TOKEN.finditer(text):
        word, brace = match.group(1), match.group(2)
        if brace == "{":
            child: dict[str, Any] = {}
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = word.replace("\\\\", "\\")
        else:
            stack[-1][key] = word.replace("\\\\", "\\")
            key = None
    return root


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def steam_libraries(steam_root: str) -> list[Path]:
    """Папки библиотек Steam: сам Steam и все из libraryfolders.vdf (существующие)."""
    root = Path(steam_root)
    found = [root]
    data = parse_vdf(_read(root / "steamapps" / "libraryfolders.vdf"))
    folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    for value in folders.values() if isinstance(folders, dict) else []:
        path = value.get("path") if isinstance(value, dict) else value if isinstance(value, str) else None
        if isinstance(path, str) and path.strip():
            found.append(Path(path))
    seen: set[str] = set()
    out = []
    for folder in found:
        key = os.path.normcase(str(folder))
        if key not in seen and (folder / "steamapps").is_dir():
            seen.add(key)
            out.append(folder)
    return out


def steam_games(steam_root: str) -> list[AppInfo]:
    """Установленные игры Steam: название из манифеста, запуск — steam://rungameid/<id>."""
    games: dict[str, AppInfo] = {}
    for library in steam_libraries(steam_root):
        for manifest in sorted((library / "steamapps").glob("appmanifest_*.acf")):
            state = parse_vdf(_read(manifest)).get("AppState") or {}
            if not isinstance(state, dict):
                continue
            app_id, name = str(state.get("appid") or ""), str(state.get("name") or "").strip()
            try:
                installed = int(str(state.get("StateFlags") or "0")) & 4
            except ValueError:
                installed = 0
            if not app_id.isdigit() or not name or not installed or NOT_GAMES.search(name):
                continue
            folder = library / "steamapps" / "common" / str(state.get("installdir") or "")
            games.setdefault(app_id, AppInfo(name, f"steam://rungameid/{app_id}", str(folder), SOURCE))
    return list(games.values())


def epic_games(manifests: str) -> list[AppInfo]:
    """Установленные игры Epic Games: манифесты *.item; запуск — ссылкой лаунчера (он сам проверит вход)."""
    folder = Path(os.path.expandvars(manifests))
    games = []
    for item in sorted(folder.glob("*.item")) if folder.is_dir() else []:
        try:
            data = json.loads(_read(item))
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("bIsIncompleteInstall"):
            continue
        name, app = str(data.get("DisplayName") or "").strip(), str(data.get("AppName") or "").strip()
        categories = [str(c) for c in data.get("AppCategories") or []]
        if not name or not app or (categories and "games" not in categories) or NOT_GAMES.search(name):
            continue
        exe = ntpath.join(str(data.get("InstallLocation") or ""), str(data.get("LaunchExecutable") or ""))
        url = f"com.epicgames.launcher://apps/{app}?action=launch&silent=true"
        games.append(AppInfo(name, url, exe, SOURCE))
    return games


def gog_games(entries: Iterable[dict[str, str]]) -> list[AppInfo]:
    """Игры GOG из записей реестра (gameName, exe): запуск — exe игры."""
    games = []
    for entry in entries:
        name, exe = (entry.get("gameName") or "").strip(), (entry.get("exe") or "").strip()
        if name and exe.casefold().endswith(".exe") and not NOT_GAMES.search(name):
            games.append(AppInfo(name, exe, exe, SOURCE))
    return games


# --- Windows: где лаунчеры ----------------------------------------------------------------------------


def _registry_value(root: Any, path: str, name: str) -> str:
    import winreg  # type: ignore[import-not-found]

    try:
        with winreg.OpenKey(root, path) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value or "")


def steam_root() -> str:
    """Папка Steam: из реестра (HKCU SteamPath, HKLM InstallPath), иначе — обычная."""
    import winreg  # type: ignore[import-not-found]

    for root, path, name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        value = _registry_value(root, path, name)
        if value:
            return os.path.normpath(value)
    return STEAM_DEFAULT


def gog_entries() -> list[dict[str, str]]:
    import winreg  # type: ignore[import-not-found]

    entries = []
    for path in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    game = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                entries.append(
                    {
                        name: _registry_value(winreg.HKEY_LOCAL_MACHINE, f"{path}\\{game}", name)
                        for name in ("gameName", "exe")
                    }
                )
        if entries:
            break
    return entries


def scan() -> list[AppInfo]:
    """Все установленные игры (Windows): Steam, Epic, GOG; лаунчера нет — без его игр."""
    games: list[AppInfo] = []
    for title, collect in (
        ("Steam", lambda: steam_games(steam_root())),
        ("Epic Games", lambda: epic_games(EPIC_MANIFESTS)),
        ("GOG", lambda: gog_games(gog_entries())),
    ):
        try:
            games.extend(collect())
        except Exception as e:
            log.warning("игры %s не прочитаны: %s", title, e)
    return games


def merge(apps: list[AppInfo], games: list[AppInfo]) -> list[AppInfo]:
    """Игры — в список программ; игра с тем же названием уже есть в «Пуске» (ярлык Steam) — не дублируем."""
    names = {" ".join(a.name.casefold().split()) for a in apps}
    extra = [g for g in games if " ".join(g.name.casefold().split()) not in names]
    return [*apps, *extra]
