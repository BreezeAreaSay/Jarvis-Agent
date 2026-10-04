"""Инвентарь Linux: приложения из desktop-файлов (XDG), браузер по умолчанию, папки пользователя.

Для разработки и CI; основная платформа — Windows (`windows.py`). Читается при первом обращении.
"""

import configparser
import re
import shutil
import subprocess
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path

from jarvis.adapters.inventory.names import aliases_for, slug, unique
from jarvis.domain.inventory import AppEntry, KnownFolder

MAX_DESKTOP_FILES = 3000
XDG_TIMEOUT_S = 2.0
_XDG_DIRS = {
    KnownFolder.DOWNLOADS: ("XDG_DOWNLOAD_DIR", "Downloads"),
    KnownFolder.DOCUMENTS: ("XDG_DOCUMENTS_DIR", "Documents"),
    KnownFolder.DESKTOP: ("XDG_DESKTOP_DIR", "Desktop"),
    KnownFolder.PICTURES: ("XDG_PICTURES_DIR", "Pictures"),
    KnownFolder.MUSIC: ("XDG_MUSIC_DIR", "Music"),
    KnownFolder.VIDEOS: ("XDG_VIDEOS_DIR", "Videos"),
}


class PosixInventory:
    def __init__(self, *, home: Path | None = None, application_dirs: Sequence[Path] | None = None) -> None:
        self._home = home if home is not None else Path.home()
        self._dirs = (
            list(application_dirs)
            if application_dirs is not None
            else [
                Path("/usr/share/applications"),
                Path("/usr/local/share/applications"),
                self._home / ".local" / "share" / "applications",
            ]
        )

    def apps(self) -> Sequence[AppEntry]:
        return self._apps

    def default_browser(self) -> AppEntry | None:
        desktop_id = self._default_browser_id
        if desktop_id is None:
            return None
        return next((app for app in self._apps if Path(app.target).name == desktop_id), None)

    def known_folder(self, folder: KnownFolder) -> str | None:
        if folder is KnownFolder.HOME:
            path = self._home
        else:
            key, default = _XDG_DIRS[folder]
            path = self._user_dirs.get(key, self._home / default)
        return str(path) if path.is_dir() else None

    @cached_property
    def _apps(self) -> list[AppEntry]:
        taken: set[str] = set()
        found: list[AppEntry] = []
        seen = 0
        for folder in self._dirs:
            if not folder.is_dir():
                continue
            for file in sorted(folder.glob("*.desktop")):
                seen += 1
                if seen > MAX_DESKTOP_FILES:
                    return found
                entry = _desktop_entry(file, taken)
                if entry is not None:
                    found.append(entry)
        return found

    @cached_property
    def _default_browser_id(self) -> str | None:
        tool = shutil.which("xdg-settings")
        if tool is None:
            return None
        try:
            result = subprocess.run(
                [tool, "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=XDG_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        return value if result.returncode == 0 and value.endswith(".desktop") else None

    @cached_property
    def _user_dirs(self) -> dict[str, Path]:
        """`~/.config/user-dirs.dirs`: XDG_DOWNLOAD_DIR="$HOME/Загрузки" и т. п."""
        file = self._home / ".config" / "user-dirs.dirs"
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            return {}
        found: dict[str, Path] = {}
        for match in re.finditer(r'^(XDG_[A-Z]+_DIR)="([^"]*)"', text, re.MULTILINE):
            value = match.group(2).replace("$HOME", str(self._home))
            if value.startswith("/"):
                found[match.group(1)] = Path(value)
        return found


def _desktop_entry(file: Path, taken: set[str]) -> AppEntry | None:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment]  # ключи с регистром: Name[ru]
    try:
        parser.read(file, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    section = parser["Desktop Entry"]
    if (
        section.get("Type") != "Application"
        or section.get("NoDisplay") == "true"
        or section.get("Hidden") == "true"
    ):
        return None
    name = (section.get("Name") or "").strip()
    if not name or not section.get("Exec"):
        return None
    aliases = aliases_for(name)
    local = (section.get("Name[ru]") or "").strip()
    if local and local != name:
        aliases.append(local)
    return AppEntry(
        id=unique(slug(name), taken), name=name, aliases=aliases, target=str(file), kind="desktop_entry"
    )
