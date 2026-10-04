"""Инвентарь Windows: ярлыки меню «Пуск», App Paths, браузер по умолчанию, известные папки (ADR 0030).

Пути известных папок — через `SHGetKnownFolderPath` (папку «Загрузки» можно перенести, и
`%USERPROFILE%\\Downloads` тогда неверен), приложения — ярлыки меню «Пуск» пользователя и общего меню
и программы, зарегистрированные в App Paths. Читается при первом обращении и держится в памяти.
Приложения Microsoft Store без ярлыка в меню «Пуск» сюда пока не попадают.
"""

import ctypes
import os
import re
import sys
import uuid
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path

from jarvis.adapters.inventory.names import aliases_for, norm, slug, unique
from jarvis.domain.inventory import AppEntry, KnownFolder

MAX_SHORTCUTS = 5000
# Ярлыки, которые не запускают приложение: удаление, справка, сайт, лицензия.
_SKIP = re.compile(
    r"uninstall|удалить|удаление|деинсталл|readme|help|справка|documentation|документация|website|"
    r"веб-сайт|release notes|license|лицензия|changelog|what's new|что нового",
    re.IGNORECASE,
)
# KNOWNFOLDERID (https://learn.microsoft.com/windows/win32/shell/knownfolderid).
_FOLDER_IDS = {
    KnownFolder.HOME: "5E6C858F-0E22-4760-9AFE-EA3317B67173",
    KnownFolder.DOWNLOADS: "374DE290-123F-4565-9164-39C4925E467B",
    KnownFolder.DOCUMENTS: "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    KnownFolder.DESKTOP: "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    KnownFolder.PICTURES: "33E28130-4E1E-4676-835A-98395C3BC3BB",
    KnownFolder.MUSIC: "4BD8D571-6D19-48D3-BE97-422220080E43",
    KnownFolder.VIDEOS: "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}
_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"  # меню «Пуск» пользователя
_COMMON_PROGRAMS = "0139D44E-6AFE-49F2-8690-3DAFCAE6FFB8"  # общее меню «Пуск»
_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
_URL_CHOICE = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"


class WindowsInventory:
    def apps(self) -> Sequence[AppEntry]:
        return self._apps

    def default_browser(self) -> AppEntry | None:
        return self._browser

    def known_folder(self, folder: KnownFolder) -> str | None:
        path = known_folder_path(_FOLDER_IDS[folder])
        return path if path is not None and os.path.isdir(path) else None

    @cached_property
    def _apps(self) -> list[AppEntry]:
        taken: set[str] = set()
        found: list[AppEntry] = []
        names: set[str] = set()
        for root in (known_folder_path(_PROGRAMS), known_folder_path(_COMMON_PROGRAMS)):
            for name, target in _shortcuts(root):
                if norm(name) in names:
                    continue  # ярлык пользователя важнее одноимённого общего
                names.add(norm(name))
                found.append(
                    AppEntry(
                        id=unique(slug(name), taken),
                        name=name,
                        aliases=aliases_for(name),
                        target=target,
                        kind="shortcut",
                    )
                )
        for name, target in _app_paths():
            if norm(name) in names or any(
                norm(name) in {norm(alias) for alias in app.aliases} for app in found
            ):
                continue
            names.add(norm(name))
            found.append(
                AppEntry(
                    id=unique(slug(name), taken),
                    name=name,
                    aliases=aliases_for(name),
                    target=target,
                    kind="executable",
                )
            )
        return found

    @cached_property
    def _browser(self) -> AppEntry | None:
        found = _default_browser()
        if found is None:
            return None
        name, executable = found
        for app in self._apps:  # тот же браузер из меню «Пуск» — с его ID и алиасами
            if (
                norm(app.name) == norm(name)
                or Path(app.target).stem.casefold() == Path(executable).stem.casefold()
            ):
                return app
        return AppEntry(
            id="default-browser", name=name, aliases=aliases_for(name), target=executable, kind="executable"
        )


def known_folder_path(folder_id: str) -> str | None:  # pragma: no cover - только Windows
    if sys.platform != "win32":
        return None

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    value = uuid.UUID(folder_id)
    guid = GUID(value.time_low, value.time_mid, value.time_hi_version, (ctypes.c_ubyte * 8)(*value.bytes[8:]))
    path = ctypes.c_wchar_p()
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path))
    try:
        return path.value if result == 0 else None
    finally:
        ole32.CoTaskMemFree(path)


def _shortcuts(root: str | None) -> list[tuple[str, str]]:  # pragma: no cover - только Windows
    if root is None or not os.path.isdir(root):
        return []
    found: list[tuple[str, str]] = []
    for folder, _, files in os.walk(root):
        for file in sorted(files):
            if len(found) >= MAX_SHORTCUTS:
                return found
            stem, extension = os.path.splitext(file)
            if extension.casefold() in (".lnk", ".appref-ms") and not _SKIP.search(stem):
                found.append((stem, os.path.join(folder, file)))
    return found


def _app_paths() -> list[tuple[str, str]]:  # pragma: no cover - только Windows
    if sys.platform != "win32":
        return []
    import winreg

    found: list[tuple[str, str]] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, _APP_PATHS)
        except OSError:
            continue
        with key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                try:
                    exe = winreg.EnumKey(key, index)
                    with winreg.OpenKey(key, exe) as entry:
                        target = str(winreg.QueryValueEx(entry, "")[0]).strip('"')
                except OSError:
                    continue
                if target.casefold().endswith(".exe") and os.path.isfile(target):
                    found.append((Path(exe).stem, target))
    return found


def _default_browser() -> tuple[str, str] | None:  # pragma: no cover - только Windows
    """Имя и исполняемый файл браузера, который открывает https-ссылки."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _URL_CHOICE) as key:
            prog_id = str(winreg.QueryValueEx(key, "ProgId")[0])
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command = str(winreg.QueryValueEx(key, "")[0])
    except OSError:
        return None
    match = re.match(r'\s*"([^"]+\.exe)"|\s*(\S+\.exe)', command, re.IGNORECASE)
    if match is None:
        return None
    executable = match.group(1) or match.group(2)
    name = Path(executable).stem
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\Application") as key:
            name = str(winreg.QueryValueEx(key, "ApplicationName")[0]) or name
    except OSError:
        pass
    if name.startswith("@"):  # ссылка на ресурс DLL — имя файла понятнее
        name = Path(executable).stem
    return (name, executable) if os.path.isfile(executable) else None
