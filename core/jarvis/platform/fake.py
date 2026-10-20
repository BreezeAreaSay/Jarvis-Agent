"""Платформа-заглушка: ничего не делает с системой, только запоминает вызовы и отдаёт правдоподобные данные.

Нужна для тестов, для разработки на Linux и для демо-режима интерфейса (--platform fake).
"""

import io
import time
from typing import Any

from jarvis.platform.base import (
    AppInfo,
    FileInfo,
    FileQuery,
    IndexedFile,
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

# «Индекс Windows» заглушки: (путь, размер, когда изменён, папка ли, текст внутри). Когда: ("ago", 0, 3) —
# три часа назад, ("d", 1, 14) — вчера в 14:00; так «фото за вчера» находится в любой день.
_HOME = "C:\\Users\\me"
DEMO_FILES: list[tuple[str, int, tuple[str, float, float], bool, str]] = [
    (_HOME + r"\Downloads\Счёт №125 от 15.10.pdf", 182_000, ("ago", 0, 0.4), False, "счет на оплату"),
    (_HOME + r"\Downloads\jarvis-setup-2.0.exe", 96_400_000, ("d", 2, 11), False, ""),
    (_HOME + r"\Downloads\Договор аренды (скан).pdf", 2_400_000, ("d", 12, 16), False, "договор аренды"),
    (_HOME + r"\Downloads\Фото с отпуска.zip", 412_000_000, ("d", 9, 20), False, ""),
    (_HOME + r"\Downloads\Инструкция к роутеру.pdf", 3_100_000, ("d", 20, 10), False, ""),
    (_HOME + r"\Downloads\Лекция по физике.mp4", 845_000_000, ("d", 5, 19), False, ""),
    (_HOME + r"\Documents\Договор с Ивановым.docx", 48_200, ("d", 1, 17), False, "договор поставки иванов"),
    (_HOME + r"\Documents\Бюджет 2026.xlsx", 64_800, ("ago", 0, 3), False, "бюджет расходы доходы"),
    (_HOME + r"\Documents\Презентация бюджет Q3.pptx", 5_600_000, ("d", 1, 12), False, "бюджет квартал"),
    (_HOME + r"\Documents\Бюджет семьи — сентябрь.xlsx", 21_500, ("d", 30, 21), False, "бюджет"),
    (_HOME + r"\Documents\Отчёт за сентябрь.docx", 132_000, ("d", 18, 15), False, "отчет за сентябрь"),
    (_HOME + r"\Documents\Итоги квартала.docx", 88_000, ("d", 6, 13), False, "отчет о продажах за квартал"),
    (_HOME + r"\Documents\Резюме Анна Петрова.pdf", 240_000, ("d", 60, 10), False, ""),
    (_HOME + r"\Documents\Проекты", 0, ("d", 2, 9), True, ""),
    (_HOME + r"\Documents\Проекты\Jarvis\План запуска.docx", 56_000, ("d", 4, 18), False, "план бюджет"),
    (_HOME + r"\Documents\Проекты\Сайт кафе", 0, ("d", 12, 12), True, ""),
    (_HOME + r"\Desktop\Заметки к созвону.txt", 2_100, ("ago", 0, 1), False, "созвон отчет"),
    (_HOME + r"\Desktop\Список покупок.txt", 640, ("d", 3, 9), False, ""),
    (_HOME + r"\Pictures\IMG_2041.jpg", 3_900_000, ("d", 1, 13), False, ""),
    (_HOME + r"\Pictures\IMG_2042.jpg", 4_200_000, ("d", 1, 13.2), False, ""),
    (_HOME + r"\Pictures\Screenshots\Скриншот 2026-10-17 214502.png", 820_000, ("d", 1, 21.7), False, ""),
    (_HOME + r"\Pictures\Отпуск\Море 01.jpg", 5_100_000, ("d", 9, 18), False, ""),
    (_HOME + r"\Music\Плейлист для бега\01 — Run.mp3", 7_300_000, ("d", 50, 8), False, ""),
    (_HOME + r"\Videos\Запись экрана 2026-10-12.mp4", 120_000_000, ("d", 6, 22), False, ""),
]


def _norm(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _demo_time(when: tuple[str, float, float], now: float) -> float:
    kind, days, value = when
    if kind == "ago":  # часов назад
        return now - value * 3600
    midnight = time.mktime((*time.localtime(now)[:3], 0, 0, 0, 0, 0, -1))
    return min(now - 60, midnight - days * 86400 + value * 3600)


class FakePlatform:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.volume = 40
        self.muted = False
        self.brightness = 70
        self.clipboard = ""
        self.clipboard_secret = False  # копия из менеджера паролей: clipboard_get_item отдаст её без текста
        self.clipboard_serial = 1  # номер версии буфера: растёт при каждой записи
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
        self.index_available = True  # тесты выключают «службу индексирования»
        # тесты подставляют: сколько секунд без ввода, игра во весь экран, кто пишет микрофон (звонок)
        self.idle = 0.0
        self.fullscreen = False
        self.mic_users: list[str] = []
        self.gpu_temp: float | None = 54.0  # температура видеокарты (как nvidia-smi); None — неизвестна

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

    def virtual_screen(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)

    def monitor_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return (0, 0, 1920, 1080)

    def screen_pixels(self, rect: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int]:
        """Экран в 6 раз мельче настоящего (как картинка screenshot): 1920×1080 → 320×180."""
        self._call("screen_pixels", rect)
        left, top, right, bottom = rect or (0, 0, 1920, 1080)
        width, height = max(1, (right - left) // 6), max(1, (bottom - top) // 6)
        return bytes([38, 28, 24, 255]) * (width * height), width, height

    # --- программы --------------------------------------------------------------------------------
    def list_apps(self, refresh: bool = False) -> list[AppInfo]:
        return list(DEMO_APPS)

    def launch(self, target: str, args: str = "") -> None:
        self._call("launch", target, args)
        if not target:
            raise PlatformError("Не указано, что запустить")
        # как в жизни: у запущенной программы появляется окно (если его ещё нет)
        app = next((a for a in DEMO_APPS if target in (a.app_id, a.name)), None)
        title = app.name if app is not None else target.rsplit("\\", 1)[-1]
        exe = app.path.rsplit("\\", 1)[-1] if app is not None and app.path else target.rsplit("\\", 1)[-1]
        if not any(w.title == title for w in self.windows):
            hwnd = max((w.hwnd for w in self.windows), default=100) + 1
            self.windows.append(WindowInfo(hwnd, title, exe, 7000 + hwnd, (240, 140, 1240, 840)))

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
        return self.fullscreen

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
        self.clipboard_secret = False
        self.clipboard_serial += 1

    def clipboard_seq(self) -> int:
        return self.clipboard_serial

    def clipboard_get_item(self) -> tuple[str, bool]:
        return ("", True) if self.clipboard_secret else (self.clipboard, False)

    def clipboard_snapshot(self) -> Any:
        return (self.clipboard, self.clipboard_secret)

    def clipboard_restore(self, snapshot: Any) -> None:
        self._call("clipboard_restore")
        self.clipboard, self.clipboard_secret = snapshot
        self.clipboard_serial += 1

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
            extra={"disk_free_gb": 186.4, "disk_total_gb": 476.9, "cpu_count": 12}
            | ({"gpu_temp_c": self.gpu_temp} if self.gpu_temp is not None else {}),
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
        return self.idle

    def microphone_users(self) -> list[str]:
        return list(self.mic_users)

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

    # --- файлы ------------------------------------------------------------------------------------
    def search_index(self, query: FileQuery) -> list[IndexedFile]:
        """Как индекс Windows, только по DEMO_FILES: слова — в имени (any_word — хоть одно, в пути),
        расширения, папки, даты, слова текста."""
        self._call("search_index", query)
        if not self.index_available:
            raise PlatformError("Индекс Windows недоступен: служба «Windows Search» выключена")
        now = time.time()
        scopes = [_norm(s).rstrip("\\") for s in query.scopes]
        found: list[IndexedFile] = []
        for path, size, when, is_dir, content in DEMO_FILES:
            folder, _, name = path.rpartition("\\")
            name_n, path_n, folder_n = _norm(name), _norm(path), _norm(folder)
            ext = name_n.rpartition(".")[2] if "." in name_n and not is_dir else ""
            modified = _demo_time(when, now)
            if query.words:
                if query.any_word and not any(w in path_n for w in query.words):
                    continue
                if not query.any_word and not all(w in name_n for w in query.words):
                    continue
            if query.extensions and ext not in query.extensions:
                continue
            if (query.folders_only and not is_dir) or (query.files_only and is_dir):
                continue
            if scopes and not any(path_n.startswith(s + "\\") for s in scopes):
                continue
            if any(w not in folder_n for w in query.path_words):
                continue
            if any(w not in _norm(content) for w in query.content):
                continue
            if (query.date_from is not None and modified < query.date_from) or (
                query.date_to is not None and modified >= query.date_to
            ):
                continue
            found.append(IndexedFile(path, name, 0 if is_dir else size, modified, is_dir))
        found.sort(key=lambda f: f.modified, reverse=True)
        return found[: max(1, query.limit)]

    def reveal_path(self, path: str) -> None:
        self._call("reveal_path", path)
        if not path.strip():
            raise PlatformError("Не указан путь")
