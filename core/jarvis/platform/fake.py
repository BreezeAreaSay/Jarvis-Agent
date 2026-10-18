"""Платформа-заглушка: ничего не делает с системой, только запоминает вызовы и отдаёт правдоподобные данные.

Нужна для тестов, для разработки на Linux и для демо-режима интерфейса (--platform fake).
"""

import io
import time
from typing import Any

from jarvis.platform.base import (
    AppInfo,
    FileInfo,
    MediaInfo,
    MediaKey,
    MouseButton,
    PlatformError,
    PowerAction,
    SystemStats,
    WindowAction,
    WindowInfo,
)

DEMO_APPS = [
    AppInfo("Google Chrome", "Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    AppInfo("Spotify", "Spotify.exe", r"C:\Users\me\AppData\Roaming\Spotify\Spotify.exe"),
    AppInfo("Telegram", "Telegram.exe", r"C:\Users\me\AppData\Roaming\Telegram Desktop\Telegram.exe"),
    AppInfo("OBS Studio", "obs64.exe", r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"),
    AppInfo("Discord", "Discord.exe", r"C:\Users\me\AppData\Local\Discord\Update.exe"),
    AppInfo("Steam", "steam.exe", r"C:\Program Files (x86)\Steam\steam.exe"),
    AppInfo(
        "Visual Studio Code", "Code.exe", r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\Code.exe"
    ),
    AppInfo("Блокнот", "notepad.exe", r"C:\Windows\System32\notepad.exe"),
    AppInfo("Калькулятор", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "", "uwp"),
    AppInfo("Проводник", "explorer.exe", r"C:\Windows\explorer.exe"),
    AppInfo("Adobe Photoshop 2026", "Photoshop.exe", r"C:\Program Files\Adobe\Photoshop\Photoshop.exe"),
    AppInfo("Minecraft Launcher", "MinecraftLauncher.exe", ""),
    AppInfo("Яндекс Браузер", "browser.exe", ""),
    AppInfo("Word", "WINWORD.EXE", ""),
    AppInfo("Excel", "EXCEL.EXE", ""),
]


class FakePlatform:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.volume = 40
        self.muted = False
        self.brightness = 70
        self.clipboard = ""
        self.cursor = (960, 540)
        self.recycle = 3
        self.media = MediaInfo(
            title="Night Drive",
            artist="Neon Coast",
            app="Spotify",
            status="playing",
            duration_s=214,
            position_s=63,
        )
        self.windows = [
            WindowInfo(101, "Документы — Проводник", "explorer.exe", 4100, (200, 160, 1100, 760)),
            WindowInfo(
                102, "Jarvis — Google Chrome", "chrome.exe", 5200, (300, 80, 1600, 980), foreground=True
            ),
            WindowInfo(103, "Spotify Premium", "Spotify.exe", 6100, (120, 120, 1300, 900)),
        ]
        self._started = time.time()

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    # --- звук -------------------------------------------------------------------------------------
    def volume_get(self) -> int:
        return self.volume

    def volume_set(self, level: int) -> int:
        self._call("volume_set", level)
        self.volume = max(0, min(100, int(level)))
        return self.volume

    def volume_change(self, delta: int) -> int:
        self._call("volume_change", delta)
        return self.volume_set(self.volume + delta)

    def mute_set(self, muted: bool | None = None) -> bool:
        self._call("mute_set", muted)
        self.muted = (not self.muted) if muted is None else muted
        return self.muted

    def is_muted(self) -> bool:
        return self.muted

    def app_volume_set(self, app: str, level: int) -> bool:
        self._call("app_volume_set", app, level)
        return True

    # --- медиа ------------------------------------------------------------------------------------
    def media_key(self, key: MediaKey) -> None:
        self._call("media_key", key)
        if key == "play_pause":
            self.media.status = "paused" if self.media.status == "playing" else "playing"

    def now_playing(self) -> MediaInfo | None:
        return self.media

    # --- ввод -------------------------------------------------------------------------------------
    def press_keys(self, combo: str) -> None:
        self._call("press_keys", combo)

    def key_down(self, key: str) -> None:
        self._call("key_down", key)

    def key_up(self, key: str) -> None:
        self._call("key_up", key)

    def type_text(self, text: str) -> None:
        self._call("type_text", text)

    def paste_text(self, text: str) -> None:
        self._call("paste_text", text)

    def mouse_move(self, x: int, y: int) -> None:
        self._call("mouse_move", x, y)
        self.cursor = (x, y)

    def mouse_click(
        self, x: int | None = None, y: int | None = None, button: MouseButton = "left", double: bool = False
    ) -> None:
        self._call("mouse_click", x, y, button, double)
        if x is not None and y is not None:
            self.cursor = (x, y)

    def mouse_scroll(self, amount: int) -> None:
        self._call("mouse_scroll", amount)

    def cursor_pos(self) -> tuple[int, int]:
        return self.cursor

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    # --- программы --------------------------------------------------------------------------------
    def list_apps(self, refresh: bool = False) -> list[AppInfo]:
        return list(DEMO_APPS)

    def launch(self, target: str, args: str = "") -> None:
        self._call("launch", target, args)
        if not target:
            raise PlatformError("Не указано, что запустить")

    def open_url(self, url: str) -> None:
        self._call("open_url", url)

    def open_path(self, path: str) -> None:
        self._call("open_path", path)

    def close_app(self, name: str) -> int:
        self._call("close_app", name)
        return 1

    def kill_process(self, name: str) -> int:
        self._call("kill_process", name)
        return 1

    def is_running(self, name: str) -> bool:
        return any(name.lower() in w.exe.lower() for w in self.windows)

    # --- окна -------------------------------------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def foreground_window(self) -> WindowInfo | None:
        return next((w for w in self.windows if w.foreground), None)

    def window_action(self, action: WindowAction, target: int | str | None = None) -> None:
        self._call("window_action", action, target)

    def is_fullscreen_app(self) -> bool:
        return False

    # --- система ----------------------------------------------------------------------------------
    def power(self, action: PowerAction, delay_s: int = 0) -> None:
        self._call("power", action, delay_s)

    def screenshot(self, window: int | None = None) -> bytes:
        self._call("screenshot", window)
        try:
            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (320, 180), (24, 28, 38)).save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            return b""

    def clipboard_get(self) -> str:
        return self.clipboard

    def clipboard_set(self, text: str) -> None:
        self._call("clipboard_set", text)
        self.clipboard = text

    def brightness_get(self) -> int | None:
        return self.brightness

    def brightness_set(self, level: int) -> None:
        self._call("brightness_set", level)
        self.brightness = max(0, min(100, int(level)))

    def recycle_bin_count(self) -> int:
        return self.recycle

    def empty_recycle_bin(self) -> None:
        self._call("empty_recycle_bin")
        self.recycle = 0

    def trash(self, path: str) -> None:
        self._call("trash", path)
        self.recycle += 1

    def system_stats(self) -> SystemStats:
        t = time.time()
        wave = (t % 20) / 20
        return SystemStats(
            cpu=round(18 + 20 * wave, 1),
            ram=46.0,
            ram_used_gb=14.7,
            ram_total_gb=32.0,
            gpu=round(40 + 30 * wave, 1),
            disk=61.0,
            net_down_kbps=1250.0,
            net_up_kbps=180.0,
            battery=None,
            charging=None,
            uptime_s=t - self._started + 3600 * 5,
        )

    def recent_files(self, limit: int = 10) -> list[FileInfo]:
        now = time.time()
        files = [
            FileInfo("Отчёт.docx", r"C:\Users\me\Documents\Отчёт.docx", now - 1800, "docx"),
            FileInfo("Бюджет.xlsx", r"C:\Users\me\Documents\Бюджет.xlsx", now - 86400, "xlsx"),
            FileInfo("Презентация.pptx", r"C:\Users\me\Documents\Презентация.pptx", now - 3 * 86400, "pptx"),
            FileInfo("Договор.pdf", r"C:\Users\me\Downloads\Договор.pdf", now - 6 * 86400, "pdf"),
        ]
        return files[:limit]

    def idle_seconds(self) -> float:
        return 0.0

    def known_folder(self, name: str) -> str | None:
        folders = {
            "downloads": r"C:\Users\me\Downloads",
            "documents": r"C:\Users\me\Documents",
            "desktop": r"C:\Users\me\Desktop",
            "pictures": r"C:\Users\me\Pictures",
            "music": r"C:\Users\me\Music",
            "videos": r"C:\Users\me\Videos",
        }
        return folders.get(name)
