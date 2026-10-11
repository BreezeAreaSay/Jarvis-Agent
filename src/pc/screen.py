"""Снимок окна или экрана для GPT — только по команде человека («посмотри на экран», «что в этом окне»).

У мозга инструмента снимка нет: policy "screenshot" — user «сразу», brain «нет». Окно снимается через
PrintWindow(PW_RENDERFULLCONTENT): оно попадает в снимок целиком, даже если его перекрывает окно Jarvis.
Экран — BitBlt монитора, на котором активное окно (иначе основного). Не снимаются никогда: окна самого Jarvis,
менеджеров паролей и программ из [vision] deny_apps, окна с именем из private_paths в заголовке; экран — если
такое окно активно или видно на этом мониторе.

Снимок живёт только в памяти: Shot(width, height, bgra) — строки сверху вниз, 32 бита на точку (B, G, R, A;
альфа не используется). Координаты и размеры — в физических точках монитора: на время снимка поток переводится
в per-monitor DPI (окно — в DPI-режим самого окна), поэтому на мониторе 150 % снимок не обрезан и не размыт.
Вызовы ОС — за `_api` (ctypes; DC, битмап и контекст DPI освобождаются в finally); тесты подменяют
`screen._api`.
"""

import ctypes
import logging
import ntpath
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pc import policy, privacy, procs, settings, windows
from pc.result import Caller, Result, fail

log = logging.getLogger("jarvis")

MAX_SIDE = 8192  # больше по стороне не снимаем (обрезка), чтобы не держать сотни МБ в памяти
SAMPLE_POINTS = 4096  # точек для быстрой проверки «снимок чёрный»

# менеджеры паролей: имя exe без «.exe», без учёта регистра
PASSWORD_MANAGERS = frozenset(
    {
        "keepass",
        "keepassxc",
        "keepassx",
        "keeweb",
        "1password",
        "bitwarden",
        "enpass",
        "dashlane",
        "nordpass",
        "roboform",
        "robotaskbaricon",
        "lastpass",
        "keeper",
        "keeperpasswordmanager",
        "pwsafe",
        "buttercup",
        "stpass",
        "proton pass",
        "protonpass",
    }
)
# заголовки окон с паролями, у которых exe ничего не говорит: менеджер паролей браузера,
# «Диспетчер учётных данных»
PASSWORD_TITLES = (
    "менеджер паролей",
    "диспетчер паролей",
    "password manager",
    "диспетчер учетных данных",
    "credential manager",
)

GONE_TEXT = "Окна, которое было активным, уже нет. Вызовите Jarvis хоткеем поверх нужного окна."
SELF_TEXT = "Это окно самого Jarvis — его не снимаю. Вызовите Jarvis хоткеем поверх нужного окна."
CLOSED_TEXT = "Это окно не отправляю в облако."
UNKNOWN_TEXT = "Не понял, чьё это окно, — снимок не отправляю в облако."
BLOCKER_TEXT = "На экране открыто окно {exe}, которое не отправляю в облако. Сверните его и спросите снова."
CHECK_FAILED_TEXT = "Не удалось проверить окна на экране — снимок не отправляю."
CONFIG_TEXT = (
    "jarvis.toml не читается — не знаю, какие окна закрыты, и снимок не отправляю. Проверьте: jarvis doctor."
)
MINIMIZED_TEXT = "Окно свёрнуто — разверните его и спросите снова."
HUNG_TEXT = "Окно не отвечает — его не снять. Можно снять весь экран: «экран: …»."
WINDOW_FAILED_TEXT = "Окно не отдало изображение. Можно снять весь экран: «экран: …»."
BLANK_WINDOW_TEXT = (
    "Снимок окна получился чёрным: окно защищено от снимков или рисует мимо Windows. "
    "Можно снять весь экран: «экран: …»."
)
SCREEN_FAILED_TEXT = "Не удалось снять экран."
BLANK_SCREEN_TEXT = "Снимок экрана получился чёрным: экран заблокирован или открыт защищённый рабочий стол."


@dataclass(frozen=True)
class Shot:
    """Снимок в памяти: строки сверху вниз, 4 байта на точку в порядке B, G, R, A (альфа не используется)."""

    width: int
    height: int
    bgra: bytes = field(repr=False)


Rect = tuple[int, int, int, int]  # left, top, right, bottom


# --- что нельзя отправлять ------------------------------------------------------------------------------


def app_key(exe: str) -> str:
    """«C:\\…\\KeePass.exe» → «keepass»: имя файла без «.exe», без учёта регистра."""
    name = ntpath.basename(str(exe).strip().replace("/", "\\")).strip().casefold()
    return name[:-4] if name.endswith(".exe") else name


def denied_apps(extra: Iterable[str] = ()) -> frozenset[str]:
    """Менеджеры паролей и программы из [vision] deny_apps (их передаёт jarvis)."""
    return PASSWORD_MANAGERS | {key for key in (app_key(e) for e in extra if isinstance(e, str)) if key}


def _password_title(title: str) -> bool:
    low = title.casefold().replace("ё", "е")
    return any(t in low for t in PASSWORD_TITLES)


def _closed(exe: str, title: str, denied: frozenset[str]) -> bool:
    """Окно нельзя отправлять: менеджер паролей, deny_apps или имя из private_paths в заголовке."""
    if app_key(exe) in denied or _password_title(title):
        return True
    return bool(title.strip()) and privacy.redact_title(title) == privacy.HIDDEN


def blank(shot: Shot) -> bool:
    """Все точки чёрные (альфа не в счёт): окно или экран не отдали изображение."""
    data = shot.bgra
    n = min(shot.width * shot.height, len(data) // 4)
    step = max(1, n // SAMPLE_POINTS)
    for i in range(0, n, step):  # обычный снимок узнаётся по первым же точкам
        j = i * 4
        if data[j] or data[j + 1] or data[j + 2]:
            return False
    view = memoryview(data)[: n * 4]
    return not any(bytes(view[k::4]).strip(b"\x00") for k in range(3))


# --- снимки ---------------------------------------------------------------------------------------------


def capture_window(hwnd: int, caller: Caller, deny_apps: Iterable[str] = ()) -> Shot | Result:
    """Снимок окна hwnd (активного на момент хоткея). Отказ или ошибка — Result с фразой для человека."""
    denied = policy.require("screenshot", caller, "Снять окно и отправить снимок GPT")
    if denied is not None:
        return denied
    if settings.config_error():
        return fail(CONFIG_TEXT)
    a = api()
    if not hwnd or not a.is_window(hwnd):
        return fail(GONE_TEXT)
    pid = a.pid_of(hwnd)
    if pid in procs.own_pids():
        policy.require("jarvis_self", caller, "Снимок окна Jarvis")  # строка таблицы «нет»
        return fail(SELF_TEXT)
    exe = a.exe_of(pid)
    if not exe.strip():
        return fail(UNKNOWN_TEXT)
    if _closed(exe, a.title_of(hwnd), denied_apps(deny_apps)):
        return fail(CLOSED_TEXT)
    if a.is_iconic(hwnd):
        return fail(MINIMIZED_TEXT)
    if a.is_hung(hwnd):  # PrintWindow ждал бы зависшее окно
        return fail(HUNG_TEXT)
    shot = a.grab_window(hwnd)
    if shot is None:
        return fail(WINDOW_FAILED_TEXT)
    if blank(shot):
        return fail(BLANK_WINDOW_TEXT)
    return shot


def capture_screen(hwnd: int | None, caller: Caller, deny_apps: Iterable[str] = ()) -> Shot | Result:
    """Снимок монитора, на котором окно hwnd (None или окна нет — основного). Отказ или ошибка — Result."""
    denied = policy.require("screenshot", caller, "Снять экран и отправить снимок GPT")
    if denied is not None:
        return denied
    if settings.config_error():
        return fail(CONFIG_TEXT)
    a = api()
    deny = denied_apps(deny_apps)
    target = hwnd if hwnd and a.is_window(hwnd) else None
    if target is not None:
        pid = a.pid_of(target)
        if pid not in procs.own_pids() and _closed(a.exe_of(pid), a.title_of(target), deny):
            return fail(CLOSED_TEXT)
    monitor = a.monitor_rect(target)
    if monitor is None:
        return fail(SCREEN_FAILED_TEXT)
    blocker = _visible_blocker(a, monitor, deny)
    if blocker:
        return fail(blocker)
    shot = a.grab_rect(monitor)
    if shot is None:
        return fail(SCREEN_FAILED_TEXT)
    if blank(shot):
        return fail(BLANK_SCREEN_TEXT)
    return shot


def _visible_blocker(a: Any, monitor: Rect, deny: frozenset[str]) -> str:
    """Фраза отказа, если на мониторе видно окно, которое нельзя отправлять; иначе пусто."""
    try:
        wins = windows.list_windows()  # без окон Jarvis, как в Alt+Tab; свёрнутые отсеиваются ниже
        for w in wins:
            if not _closed(w.exe, w.title, deny) or a.is_iconic(w.hwnd):
                continue
            rect = a.window_rect(w.hwnd)
            if rect is not None and _overlap(rect, monitor):
                return BLOCKER_TEXT.format(exe=w.exe or "без имени")
    except Exception:
        log.exception("снимок: окна на экране не проверены")
        return CHECK_FAILED_TEXT
    return ""


def _overlap(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def clamp(rect: Rect) -> tuple[int, int]:
    """Ширина и высота снимка: не больше MAX_SIDE по стороне (лишнее справа и снизу обрезается)."""
    return min(rect[2] - rect[0], MAX_SIDE), min(rect[3] - rect[1], MAX_SIDE)


# --- слой ОС ----------------------------------------------------------------------------------------------

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0
MONITOR_DEFAULTTOPRIMARY = 0x00000001
MONITOR_DEFAULTTONEAREST = 0x00000002
HGDI_ERROR = 0xFFFFFFFFFFFFFFFF
DPI_CONTEXTS = (-4, -3)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, затем …_PER_MONITOR_AWARE


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int32),
        ("top", ctypes.c_int32),
        ("right", ctypes.c_int32),
        ("bottom", ctypes.c_int32),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


def bitmap_info(width: int, height: int) -> BITMAPINFO:
    """32 бита на точку, без сжатия; отрицательная высота — строки сверху вниз."""
    info = BITMAPINFO()
    h = info.bmiHeader
    h.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    h.biWidth, h.biHeight = width, -height
    h.biPlanes, h.biBitCount, h.biCompression = 1, 32, BI_RGB
    return info


def _load_fns() -> Any:
    """Функции user32/gdi32 с явными argtypes/restype. SetThreadDpiAwarenessContext и
    GetWindowDpiAwarenessContext (Windows 10 1607+) могут отсутствовать — тогда None."""
    from ctypes import wintypes as w
    from types import SimpleNamespace

    u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    g = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]

    def sig(dll: Any, name: str, argtypes: list[Any], restype: Any, optional: bool = False) -> Any:
        fn = getattr(dll, name, None) if optional else getattr(dll, name)
        if fn is not None:
            fn.argtypes, fn.restype = argtypes, restype
        return fn

    vp = ctypes.c_void_p
    return SimpleNamespace(
        GetDC=sig(u, "GetDC", [w.HWND], vp),
        ReleaseDC=sig(u, "ReleaseDC", [w.HWND, vp], ctypes.c_int),
        CreateCompatibleDC=sig(g, "CreateCompatibleDC", [vp], vp),
        DeleteDC=sig(g, "DeleteDC", [vp], w.BOOL),
        CreateDIBSection=sig(
            g,
            "CreateDIBSection",
            [vp, ctypes.POINTER(BITMAPINFO), w.UINT, ctypes.POINTER(vp), w.HANDLE, w.DWORD],
            vp,
        ),
        SelectObject=sig(g, "SelectObject", [vp, vp], vp),
        DeleteObject=sig(g, "DeleteObject", [vp], w.BOOL),
        BitBlt=sig(
            g,
            "BitBlt",
            [
                vp,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                vp,
                ctypes.c_int,
                ctypes.c_int,
                w.DWORD,
            ],
            w.BOOL,
        ),
        GdiFlush=sig(g, "GdiFlush", [], w.BOOL),
        PrintWindow=sig(u, "PrintWindow", [w.HWND, vp, w.UINT], w.BOOL),
        GetWindowRect=sig(u, "GetWindowRect", [w.HWND, ctypes.POINTER(RECT)], w.BOOL),
        MonitorFromWindow=sig(u, "MonitorFromWindow", [w.HWND, w.DWORD], vp),
        MonitorFromPoint=sig(u, "MonitorFromPoint", [POINT, w.DWORD], vp),
        GetMonitorInfoW=sig(u, "GetMonitorInfoW", [vp, ctypes.POINTER(MONITORINFO)], w.BOOL),
        SetThreadDpiAwarenessContext=sig(u, "SetThreadDpiAwarenessContext", [vp], vp, optional=True),
        GetWindowDpiAwarenessContext=sig(u, "GetWindowDpiAwarenessContext", [w.HWND], vp, optional=True),
    )


def _last_error() -> int:
    get = getattr(ctypes, "get_last_error", None)
    return int(get()) if get is not None else 0


class _Api:
    """Тонкий слой Win32 для снимков. Сведения об окне — через pc.windows (тот же слой, что у действий
    с окнами).

    fns — таблица функций user32/gdi32 (тесты подставляют фейк с теми же именами и смыслом аргументов).
    """

    def __init__(self, fns: Any = None) -> None:
        self._f = fns if fns is not None else _load_fns()

    # --- сведения об окне

    def is_window(self, hwnd: int) -> bool:
        return windows.api().is_window(hwnd)

    def is_iconic(self, hwnd: int) -> bool:
        return windows.api().is_iconic(hwnd)

    def is_hung(self, hwnd: int) -> bool:
        return windows.api().is_hung(hwnd)

    def pid_of(self, hwnd: int) -> int:
        return windows.api().pid_of(hwnd)

    def exe_of(self, pid: int) -> str:
        return windows.api().exe_of(pid)

    def title_of(self, hwnd: int) -> str:
        return windows.api().get_title(hwnd)

    # --- геометрия (физические точки)

    @contextmanager
    def dpi(self, hwnd: int | None = None) -> Iterator[None]:
        """Поток — в DPI-режиме окна hwnd (снимок в его собственных точках) или per-monitor (экран);
        потом — как было. Нет функции (Windows старше 10 1607) или режим не встал — работаем как есть."""
        set_ctx = getattr(self._f, "SetThreadDpiAwarenessContext", None)
        old = None
        if set_ctx is not None:
            wanted: list[Any] = []
            get_win = getattr(self._f, "GetWindowDpiAwarenessContext", None)
            if hwnd and get_win is not None:
                own = get_win(hwnd)
                if own:
                    wanted.append(own)
            wanted += [ctypes.c_void_p(c) for c in DPI_CONTEXTS]
            for ctx in wanted:
                old = set_ctx(ctx)
                if old:
                    break
        try:
            yield
        finally:
            if old:
                set_ctx(old)

    def _rect(self, hwnd: int) -> Rect | None:
        r = RECT()
        if not self._f.GetWindowRect(hwnd, ctypes.pointer(r)):
            return None
        return r.left, r.top, r.right, r.bottom

    def window_rect(self, hwnd: int) -> Rect | None:
        with self.dpi():
            return self._rect(hwnd)

    def monitor_rect(self, hwnd: int | None) -> Rect | None:
        """Монитор окна (ближайший) или основной."""
        with self.dpi():
            mon = self._f.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST) if hwnd else None
            if not mon:
                mon = self._f.MonitorFromPoint(POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
            if not mon:
                return None
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if not self._f.GetMonitorInfoW(mon, ctypes.pointer(info)):
                return None
            r = info.rcMonitor
            return r.left, r.top, r.right, r.bottom

    # --- снимки

    def grab_window(self, hwnd: int) -> Shot | None:
        """PrintWindow(PW_RENDERFULLCONTENT): окно целиком, даже перекрытое; размер — в точках самого окна."""
        with self.dpi(hwnd):
            rect = self._rect(hwnd)
            if rect is None:
                return None
            width, height = clamp(rect)
            return self._grab(
                width, height, lambda mem, _screen: bool(self._f.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT))
            )

    def grab_rect(self, rect: Rect) -> Shot | None:
        """BitBlt прямоугольника экрана (CAPTUREBLT — вместе с прозрачными окнами; окна с
        WDA_EXCLUDEFROMCAPTURE, например Jarvis, в снимок не попадают)."""
        left, top = rect[0], rect[1]
        with self.dpi():
            width, height = clamp(rect)
            return self._grab(
                width,
                height,
                lambda mem, screen: bool(
                    self._f.BitBlt(mem, 0, 0, width, height, screen, left, top, SRCCOPY | CAPTUREBLT)
                ),
            )

    def _grab(self, width: int, height: int, draw: Callable[[Any, Any], bool]) -> Shot | None:
        """DC экрана → совместимый DC → DIB-секция 32 бита (сверху вниз) → draw → копия точек.

        DC, битмап и выбор объекта освобождаются в finally — и при ошибке, и при исключении."""
        if width <= 0 or height <= 0:
            return None
        f = self._f
        screen = f.GetDC(None)
        if not screen:
            log.warning("снимок: GetDC не удался (ошибка %d)", _last_error())
            return None
        mem = bitmap = old = None
        try:
            mem = f.CreateCompatibleDC(screen)
            if not mem:
                log.warning("снимок: CreateCompatibleDC не удался (ошибка %d)", _last_error())
                return None
            bits = ctypes.c_void_p()
            info = bitmap_info(width, height)
            bitmap = f.CreateDIBSection(
                screen, ctypes.pointer(info), DIB_RGB_COLORS, ctypes.pointer(bits), None, 0
            )
            if not bitmap or not bits.value:
                log.warning(
                    "снимок: CreateDIBSection %dx%d не удался (ошибка %d)", width, height, _last_error()
                )
                return None
            old = f.SelectObject(mem, bitmap)
            if not old or old == HGDI_ERROR:
                old = None
                return None
            if not draw(mem, screen):
                log.warning("снимок: рисование не удалось (ошибка %d)", _last_error())
                return None
            f.GdiFlush()
            return Shot(width, height, ctypes.string_at(bits.value, width * height * 4))
        finally:
            if old:
                f.SelectObject(mem, old)
            if bitmap:
                f.DeleteObject(bitmap)
            if mem:
                f.DeleteDC(mem)
            f.ReleaseDC(None, screen)


_api: Any = None


def api() -> Any:
    global _api
    if _api is None:
        _api = _Api()
    return _api
