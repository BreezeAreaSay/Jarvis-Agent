"""Действия с операционной системой: звук, медиа, ввод, программы, окна, система.

Platform — синхронный интерфейс (ctypes/COM/WinRT). Ядро вызывает его через AppContext.run_platform(),
в отдельном потоке с инициализированным COM. Реализации: jarvis.platform.windows (настоящая) и
jarvis.platform.fake (Linux, тесты, демо-режим интерфейса).

Ошибки — PlatformError с текстом для человека («Программа «Spotify» не найдена»).
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

MediaKey = Literal["play_pause", "next", "prev", "stop"]
WindowAction = Literal[
    "minimize", "maximize", "restore", "close", "focus", "minimize_all", "undo_minimize_all", "show_desktop"
]
PowerAction = Literal["lock", "sleep", "hibernate", "shutdown", "restart", "logoff", "cancel"]
MouseButton = Literal["left", "right", "middle"]


class PlatformError(RuntimeError):
    """Действие не удалось; текст — для человека, по-русски."""


@dataclass
class AppInfo:
    name: str  # как в меню «Пуск»: «Google Chrome»
    app_id: str  # AppUserModelID или путь к exe — то, что умеет запускать launch()
    path: str = ""  # exe, если известен (для иконки)
    source: str = "start"  # start | uwp | path | custom

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    exe: str  # имя файла процесса: chrome.exe
    pid: int = 0
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom (физические пиксели)
    minimized: bool = False
    maximized: bool = False
    foreground: bool = False

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["rect"] = list(self.rect)
        return data


@dataclass
class MediaInfo:
    title: str = ""
    artist: str = ""
    album: str = ""
    app: str = ""  # Spotify, Яндекс Музыка, Chrome…
    status: Literal["playing", "paused", "stopped", "unknown"] = "unknown"
    position_s: float = 0.0
    duration_s: float = 0.0
    thumbnail: str = ""  # data:image/png;base64,… или ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileInfo:
    name: str
    path: str
    modified: float  # unix time
    kind: str = ""  # расширение без точки

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileQuery:
    """Запрос к индексу поиска Windows (собирает плагин «Поиск по ПК»). Все слова — уже без регистра,
    «ё» → «е» и основы («презентац», «иванов»); экранирует их платформа."""

    words: list[str] = field(default_factory=list)  # части имени файла
    any_word: bool = False  # хватит любого слова (и в пути целиком, не только в имени)
    extensions: list[str] = field(default_factory=list)  # без точки: pdf, docx; пусто — любые
    folders_only: bool = False
    files_only: bool = False
    scopes: list[str] = field(default_factory=list)  # папки (вместе с вложенными); пусто — весь индекс
    path_words: list[str] = field(default_factory=list)  # части пути папки: «проект» из «в папке проекты»
    content: list[str] = field(default_factory=list)  # слова в тексте файлов
    date_from: float | None = None  # изменён не раньше (unix time)
    date_to: float | None = None  # и раньше этого
    limit: int = 50


@dataclass
class IndexedFile:
    """Найденный файл или папка."""

    path: str
    name: str
    size: int = 0
    modified: float = 0.0  # unix time
    is_dir: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SystemStats:
    cpu: float = 0.0  # загрузка, %
    ram: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    gpu: float | None = None  # % или None — неизвестно
    disk: float = 0.0  # заполненность системного диска, %
    net_down_kbps: float = 0.0
    net_up_kbps: float = 0.0
    battery: float | None = None
    charging: bool | None = None
    uptime_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class Platform(Protocol):
    name: str  # "windows" | "fake"

    # --- звук -------------------------------------------------------------------------------------
    def volume_get(self) -> int: ...
    def volume_set(self, level: int) -> int: ...
    def volume_change(self, delta: int) -> int: ...
    def mute_set(self, muted: bool | None = None) -> bool: ...  # None — переключить; вернёт новое состояние
    def is_muted(self) -> bool: ...
    def app_volume_set(self, app: str, level: int) -> bool: ...  # громкость одной программы (микшер)
    # микшер: [{app (имя процесса или "system"), title, level, muted, active}] — программы со звуком
    def app_volumes(self) -> list[dict[str, Any]]: ...

    # --- медиа ------------------------------------------------------------------------------------
    def media_key(self, key: MediaKey) -> None: ...
    def now_playing(self) -> MediaInfo | None: ...

    # --- ввод -------------------------------------------------------------------------------------
    def press_keys(self, combo: str) -> None: ...  # "ctrl+shift+s", "win+d", "volume_up", "f5"
    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def type_text(self, text: str) -> None: ...
    def paste_text(self, text: str) -> None: ...  # через буфер обмена, буфер восстанавливается
    def mouse_move(self, x: int, y: int) -> None: ...
    def mouse_click(
        self, x: int | None = None, y: int | None = None, button: MouseButton = "left", double: bool = False
    ) -> None: ...
    def mouse_scroll(self, amount: int) -> None: ...  # >0 — вверх
    def cursor_pos(self) -> tuple[int, int]: ...
    def screen_size(self) -> tuple[int, int]: ...
    # виртуальный экран (все мониторы): left, top, width, height — снимок screenshot() ровно он
    def virtual_screen(self) -> tuple[int, int, int, int]: ...
    # монитор, на котором окно (или ближайший к нему): left, top, right, bottom — в тех же точках
    def monitor_rect(self, hwnd: int) -> tuple[int, int, int, int] | None: ...
    # все мониторы: left, top, right, bottom каждого — в тех же точках
    def monitors(self) -> list[tuple[int, int, int, int]]: ...
    # пиксели экрана (BGRA) без PNG — быстрее screenshot(): всего или прямоугольника (left, top, right,
    # bottom); вернёт (байты, ширина, высота). Можно звать не из потока платформы (агент снимает экран,
    # пока поток платформы читает элементы окна)
    def screen_pixels(self, rect: tuple[int, int, int, int] | None = None) -> tuple[bytes, int, int]: ...

    # --- программы и процессы ---------------------------------------------------------------------
    def list_apps(self, refresh: bool = False) -> list[AppInfo]: ...
    def launch(self, target: str, args: str = "") -> None: ...  # exe, AppID, ярлык, файл, папка
    def open_url(self, url: str) -> None: ...
    def open_path(self, path: str) -> None: ...
    def close_app(self, name: str) -> int: ...  # закрыть окна программы мягко; число закрытых окон
    def kill_process(self, name: str) -> int: ...
    def is_running(self, name: str) -> bool: ...

    # --- окна -------------------------------------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]: ...
    def foreground_window(self) -> WindowInfo | None: ...
    def window_action(self, action: WindowAction, target: int | str | None = None) -> None: ...
    def is_fullscreen_app(self) -> bool: ...  # активное окно — игра или видео во весь экран

    # --- система ----------------------------------------------------------------------------------
    def power(self, action: PowerAction, delay_s: int = 0) -> None: ...
    def screenshot(self, window: int | None = None) -> bytes: ...  # PNG
    def clipboard_get(self) -> str: ...
    def clipboard_set(self, text: str) -> None: ...
    def clipboard_seq(self) -> int: ...  # номер версии буфера: меняется при каждом копировании
    # текст буфера и «секрет ли это» (менеджер паролей пометил копию «не запоминать») — секрет без текста
    def clipboard_get_item(self) -> tuple[str, bool]: ...
    def clipboard_snapshot(self) -> Any: ...  # всё содержимое буфера (все форматы) — для clipboard_restore
    def clipboard_restore(self, snapshot: Any) -> None: ...
    def brightness_get(self) -> int | None: ...
    def brightness_set(self, level: int) -> None: ...
    def recycle_bin_count(self) -> int: ...
    def empty_recycle_bin(self) -> None: ...
    def trash(self, path: str) -> None: ...
    def system_stats(self) -> SystemStats: ...
    def recent_files(self, limit: int = 10) -> list[FileInfo]: ...
    def idle_seconds(self) -> float: ...
    # программы, которые прямо сейчас пишут микрофон (кроме Jarvis): идёт звонок — Jarvis не заговорит сам
    def microphone_users(self) -> list[str]: ...
    def known_folder(
        self, name: str
    ) -> str | None: ...  # downloads, documents, desktop, pictures, music, videos

    # --- файлы ------------------------------------------------------------------------------------
    def search_index(self, query: FileQuery) -> list[IndexedFile]: ...  # PlatformError — индекса нет
    def reveal_path(self, path: str) -> None: ...  # показать в Проводнике с выделенным файлом
