"""Какой браузер открыть для Jarvis и с чем: Google Chrome или Microsoft Edge, свой профиль и порт отладки.

С Chrome 136 порт отладки (--remote-debugging-port) не открывается для профиля по умолчанию
(https://developer.chrome.com/blog/remote-debugging-port), поэтому у Jarvis свой профиль — папка
<data>/browser (--user-data-dir): человек один раз входит в нужные сайты в этом окне, его обычный браузер
и профиль Jarvis не трогает. Порт — случайный (--remote-debugging-port=0): браузер слушает только 127.0.0.1
и пишет порт и путь в <профиль>/DevToolsActivePort. Chrome — первым (если выбрано «Авто»), Edge есть в любой
Windows 10/11. Реестр Windows (App Paths) читается лениво и только на Windows.
"""

import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis import paths

PORT_FILE = "DevToolsActivePort"
APPS: dict[str, tuple[str, str]] = {  # id → (название, exe на Windows)
    "chrome": ("Google Chrome", "chrome.exe"),
    "edge": ("Microsoft Edge", "msedge.exe"),
}
# куда их ставят на Windows: (переменная окружения, путь внутри)
_WINDOWS_DIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "chrome": (
        ("ProgramFiles", r"Google\Chrome\Application"),
        ("ProgramFiles(x86)", r"Google\Chrome\Application"),
        ("LOCALAPPDATA", r"Google\Chrome\Application"),
    ),
    "edge": (
        ("ProgramFiles(x86)", r"Microsoft\Edge\Application"),
        ("ProgramFiles", r"Microsoft\Edge\Application"),
        ("LOCALAPPDATA", r"Microsoft\Edge\Application"),
    ),
}
# Linux — для разработки и проверок (Jarvis для людей — только Windows)
_LINUX_NAMES: dict[str, tuple[str, ...]] = {
    "chrome": ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
    "edge": ("microsoft-edge", "microsoft-edge-stable"),
}


@dataclass(frozen=True)
class BrowserApp:
    id: str  # chrome | edge
    title: str  # «Google Chrome»
    path: str  # полный путь к exe


def profile_dir() -> Path:
    """Профиль браузера Jarvis: входы на сайты, cookies, история — только браузера, Jarvis их не читает."""
    return paths.data_dir() / "browser"


def find(choice: str = "auto") -> BrowserApp | None:
    """Браузер для Jarvis: выбранный в настройках (chrome / edge) или первый найденный: Chrome, потом Edge."""
    order = [choice] if choice in APPS else list(APPS)
    for app_id in order:
        path = _find_one(app_id)
        if path:
            return BrowserApp(app_id, APPS[app_id][0], path)
    return None


def installed() -> list[BrowserApp]:
    """Какие из браузеров есть на компьютере — для выбора в настройках."""
    found = (find(app_id) for app_id in APPS)
    return [app for app in found if app is not None]


def _find_one(app_id: str) -> str:
    if sys.platform == "win32":
        exe = APPS[app_id][1]
        for path in [*_app_paths(exe), *_standard_paths(app_id, exe)]:
            if path and os.path.isfile(path):
                return path
        return ""
    for name in _LINUX_NAMES.get(app_id, ()):
        path = shutil.which(name)
        if path:
            return path
    return ""


def _standard_paths(app_id: str, exe: str) -> list[str]:
    out = []
    for variable, folder in _WINDOWS_DIRS.get(app_id, ()):
        base = os.environ.get(variable)
        if base:
            out.append(os.path.join(base, folder, exe))
    return out


def _app_paths(exe: str) -> list[str]:
    """HKCU и HKLM …\\App Paths\\<exe> — куда установщик записал браузер."""
    import winreg  # type: ignore[import-not-found,unused-ignore]

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    out = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        with contextlib.suppress(OSError), winreg.OpenKey(hive, key) as handle:
            value, _kind = winreg.QueryValueEx(handle, "")
            if value:
                out.append(os.path.expandvars(str(value).strip().strip('"')))
    return out


def arguments(profile: Path, url: str = "") -> list[str]:
    """Аргументы запуска: свой профиль, случайный порт отладки, без приветствия и вопроса «по умолчанию»."""
    args = [
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if url:
        args.append(url)
    return args


def command_line(args: list[str]) -> str:
    """Аргументы одной строкой для ShellExecute — с кавычками по правилам Windows (путь с пробелами)."""
    return subprocess.list2cmdline(args)


def read_port(profile: Path) -> tuple[int, str] | None:
    """(порт, путь WebSocket браузера) из DevToolsActivePort; файла нет или он недописан — None."""
    try:
        lines = (profile / PORT_FILE).read_text("utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].strip().isdigit() or not lines[1].startswith("/devtools/browser/"):
        return None
    port = int(lines[0].strip())
    return (port, lines[1].strip()) if 0 < port < 65536 else None
