"""Окна Windows: список (как в Alt+Tab), активное окно, поиск цели, фокус-лестница, закрыть, свернуть.

Вызовы ОС — за `_api` (ctypes с явными argtypes/restype); тесты подменяют `windows._api` фейком.
Заголовки окон — недоверенный текст: только показываем и сравниваем, никогда не исполняем.
Окна самого Jarvis (procs.own_pids) в список не попадают, а действия над ними — policy "jarvis_self" (отказ).
"""

import logging
import ntpath
import os
import time
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from pc import _input, apps, policy, privacy, procs
from pc.result import Caller, Result, fail, ok

log = logging.getLogger("jarvis")

SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9
VK_MENU = 0x12
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000
# рабочий стол и панель задач: видимые окна без владельца, но в Alt+Tab их нет
SKIP_CLASSES = frozenset({"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"})

TITLE_THRESHOLD = 85.0
FG_POLL_TRIES = 3
FG_POLL_S = 0.015
LABEL_MAX = 60
CUR = "@cur"
SELF_TEXT = procs.SELF_TEXT
ACTIONS = ("minimize", "maximize", "restore", "minimize_all")


def clean_text(text: str) -> str:
    """Непарные суррогаты (GetWindowTextW их сохраняет) → «�»: иначе UTF-8 кодирование дальше падает."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    exe: str


# --- список и поиск -------------------------------------------------------------------------------


def _switchable(a: Any, hwnd: int) -> bool:
    """Правило Alt+Tab: видимое, не tool-окно, без владельца (или с WS_EX_APPWINDOW), не cloaked."""
    if not a.is_visible(hwnd):
        return False
    ex = a.ex_style(hwnd)
    app_window = bool(ex & WS_EX_APPWINDOW)
    if ex & WS_EX_TOOLWINDOW:
        return False
    if not app_window and (a.owner(hwnd) or ex & WS_EX_NOACTIVATE):
        return False
    if a.is_cloaked(hwnd):
        return False
    return a.class_name(hwnd).casefold() not in SKIP_CLASSES


def _enum() -> tuple[list[WindowInfo], list[WindowInfo]]:
    """(чужие окна, окна Jarvis) в Z-порядке, сверху вниз."""
    a = api()
    own = procs.own_pids()
    exe_by_pid: dict[int, str] = {}
    others: list[WindowInfo] = []
    mine: list[WindowInfo] = []
    for hwnd in a.enum_windows():
        if not _switchable(a, hwnd):
            continue
        title = a.get_title(hwnd)
        if not title.strip():
            continue
        pid = a.pid_of(hwnd)
        if pid not in exe_by_pid:
            exe_by_pid[pid] = a.exe_of(pid)
        win = WindowInfo(hwnd, title, pid, exe_by_pid[pid])
        (mine if pid in own else others).append(win)
    return others, mine


def list_windows() -> list[WindowInfo]:
    """Видимые top-level окна с заголовком, как в Alt+Tab; без окон Jarvis. Порядок — Z-порядок."""
    return _enum()[0]


def foreground() -> WindowInfo | None:
    """Активное окно (окна Jarvis не исключаются — его снимают до показа окна Jarvis)."""
    a = api()
    hwnd = a.foreground()
    if not hwnd:
        return None
    pid = a.pid_of(hwnd)
    return WindowInfo(hwnd, a.get_title(hwnd), pid, a.exe_of(pid))


def _as_hwnd(target: str | int) -> int | None:
    if isinstance(target, bool):
        return None
    if isinstance(target, int):
        return target
    s = target.strip()
    return int(s) if s.isascii() and s.isdigit() else None


def _exe_stem(exe: str) -> str:
    return exe[:-4] if exe.casefold().endswith(".exe") else exe


def _app_window(app: apps.App, wins: list[WindowInfo]) -> WindowInfo | None:
    """Окно приложения: сначала по имени процесса, потом по заголовку, который равен имени приложения или
    кончается им («Документ1 - Word», «Калькулятор»). «Telegram Web - Google Chrome» — не окно Telegram."""
    exe = apps.app_exe(app)
    if exe:
        for w in wins:
            if w.exe.casefold() == exe:
                return w
    full = apps.latin(app.name)
    keys = {k for k in (full, apps.core(full)) if len(k) >= 3}
    for w in wins:
        title = apps.latin(w.title)
        if any(title == k or title.endswith(f" {k}") for k in keys):
            return w
    return None


def _find_in(target: str | int, wins: list[WindowInfo]) -> WindowInfo | None:
    """hwnd → имя exe → точный заголовок → разговорное имя/транслит exe → приложение → похожий заголовок
    (только если цель — не известное приложение: «открой телегу» без окна Telegram должна открыть его).

    При нескольких совпадениях — самое верхнее по Z-порядку.
    """
    hwnd = _as_hwnd(target)
    if hwnd is not None:
        return next((w for w in wins if w.hwnd == hwnd), None)
    s = str(target).strip()
    if not s or s.casefold() == CUR:
        return None
    stem = _exe_stem(s).casefold()
    for w in wins:
        if w.exe and _exe_stem(w.exe).casefold() == stem:
            return w
    for w in wins:
        if w.title.strip().casefold() == s.casefold():
            return w
    variants = apps.name_variants(s)
    for w in wins:
        if w.exe and apps.latin(_exe_stem(w.exe)) in variants:
            return w
    app, _score = apps.lookup(s)
    if app is not None:
        return _app_window(app, wins)  # приложение известно, но окна нет — не подменять похожим заголовком
    return _fuzzy_title(s, wins)


def _fuzzy_title(s: str, wins: list[WindowInfo]) -> WindowInfo | None:
    query, query_lat = apps.normalize(s), apps.latin(s)
    if len(query) < 3:
        return None
    best: tuple[float, WindowInfo] | None = None
    for w in wins:
        score = max(
            fuzz.partial_ratio(query, apps.normalize(w.title)),
            fuzz.partial_ratio(query_lat, apps.latin(w.title)),
        )
        if score >= TITLE_THRESHOLD and (best is None or score > best[0]):
            best = (score, w)
    return best[1] if best else None


def find_window(target: str | int) -> WindowInfo | None:
    """Окно по hwnd (только из list_windows), exe, заголовку или имени приложения. "@cur" не разрешается."""
    return _find_in(target, list_windows())


# --- фокус ----------------------------------------------------------------------------------------


def _label(win: WindowInfo, caller: Caller = "user") -> str:
    if caller == "brain":
        return privacy.redact_title(win.title)
    title = " ".join(win.title.split())
    if not title:
        return win.exe or str(win.hwnd)
    return title if len(title) <= LABEL_MAX else title[: LABEL_MAX - 1] + "…"


def _is_foreground(a: Any, hwnd: int) -> bool:
    for i in range(FG_POLL_TRIES):
        if a.foreground() == hwnd:
            return True
        if i + 1 < FG_POLL_TRIES and FG_POLL_S:
            time.sleep(FG_POLL_S)
    return False


def _elevated(a: Any, win: WindowInfo) -> bool:
    """Окно процесса с повышенными правами, а мы — без: UIPI всё равно не даст им управлять."""
    return bool(a.is_elevated(win.pid)) and not a.is_elevated(os.getpid())


def _elevated_text(label: str) -> str:
    return f"Окно «{label}» запущено с правами администратора — Jarvis не может им управлять."


def _focus(win: WindowInfo, label: str) -> Result:
    """Лестница: (1) restore + SetForegroundWindow; (2) отпустить Alt и повторить; (3) AttachThreadInput;
    (4) мигнуть на панели задач и честно сказать, что не вышло. Ступени проверяются GetForegroundWindow."""
    a = api()
    hwnd = win.hwnd
    if not a.is_window(hwnd):
        return fail(f"Окна «{label}» уже нет.")
    if _elevated(a, win):
        return fail(_elevated_text(label))
    done = ok(f"Переключился на «{label}»")
    if a.foreground() == hwnd and not a.is_iconic(hwnd):
        return done
    # 1
    if a.is_iconic(hwnd):
        a.show(hwnd, SW_RESTORE)
    a.set_foreground(hwnd)
    if _is_foreground(a, hwnd):
        return done
    # 2: Windows разрешает смену переднего окна после «последнего ввода» — им будет отпускание Alt
    try:
        _input.send_key(VK_MENU, up=True)
    except Exception as e:
        log.warning("фокус: отпускание Alt не отправлено: %s", e)
    a.set_foreground(hwnd)
    if _is_foreground(a, hwnd):
        return done
    # 3
    fg = a.foreground()
    fg_thread = a.thread_of(fg) if fg else 0
    me = a.current_thread_id()
    attached = False
    try:
        if fg_thread and fg_thread != me:
            attached = bool(a.attach_thread_input(me, fg_thread, True))
        a.bring_to_top(hwnd)
        a.set_foreground(hwnd)
    finally:
        if attached:
            a.attach_thread_input(me, fg_thread, False)
    if _is_foreground(a, hwnd):
        return done
    # 4
    a.flash(hwnd)
    return fail(f"Не удалось переключиться на «{label}» — окно мигает на панели задач.")


def focus(hwnd: int) -> Result:
    """Вывести окно на передний план (лестница S2a)."""
    a = api()
    if not hwnd or not a.is_window(hwnd):
        return fail("Такого окна нет.")
    pid = a.pid_of(hwnd)
    win = WindowInfo(hwnd, a.get_title(hwnd), pid, a.exe_of(pid))
    return _focus(win, _label(win))


# --- высокоуровневые действия (policy, подтверждение, privacy) ------------------------------------


def _target(target: str | int | None, caller: Caller) -> WindowInfo | Result:
    if target is None or (isinstance(target, str) and not target.strip()):
        return fail("Не указано окно.")
    if isinstance(target, str) and target.strip().casefold() == CUR:
        return fail("Цель «@cur» здесь не поддерживается — укажите окно.")
    others, mine = _enum()
    win = _find_in(target, others)
    if win is not None:
        return win
    own_win = _find_in(target, mine)
    if own_win is not None:
        if policy.require("jarvis_self", caller, "Действие над окном Jarvis") is not None:
            return fail(SELF_TEXT)
        return own_win
    return fail(f"Не нашёл окно «{str(target).strip()[:LABEL_MAX]}».")


def focus_target(target: str | int, caller: Caller) -> Result:
    """Переключиться на окно (policy "focus")."""
    win = _target(target, caller)
    if isinstance(win, Result):
        return win
    label = _label(win, caller)
    if _elevated(api(), win):
        return fail(_elevated_text(label))
    denied = policy.require("focus", caller, f"Переключиться на окно «{_label(win)}» ({win.exe})?")
    if denied is not None:
        return denied
    return _focus(win, label)


def close_target(target: str | int, caller: Caller) -> Result:
    """Закрыть окно через WM_CLOSE (программа сама спросит про сохранение). Мозгу — с подтверждением."""
    win = _target(target, caller)
    if isinstance(win, Result):
        return win
    a = api()
    label = _label(win, caller)
    if _elevated(a, win):
        return fail(_elevated_text(label))
    title = " ".join(win.title.split())
    denied = policy.require(
        "close_window",
        caller,
        f"Закрыть окно «{_label(win)}» ({win.exe})?",
        f"Заголовок: {title}\nПрограмма: {win.exe}",
    )
    if denied is not None:
        return denied
    if not a.post_close(win.hwnd):
        return fail(f"Не удалось закрыть «{label}».")
    return ok(f"Закрываю «{label}»")


def window_action(action: str, target: str | int | None, caller: Caller) -> Result:
    """Свернуть/развернуть/восстановить окно или свернуть все (policy "window")."""
    if action not in ACTIONS:
        return fail(f"Неизвестное действие с окном: {action}")
    a = api()
    if action == "minimize_all":
        denied = policy.require("window", caller, "Свернуть все окна?")
        if denied is not None:
            return denied
        try:
            a.minimize_all()
        except Exception as e:
            log.warning("свернуть все окна: %s", e)
            return fail("Не удалось свернуть все окна.")
        return ok("Свернул все окна")
    win = _target(target, caller)
    if isinstance(win, Result):
        return win
    label = _label(win, caller)
    if _elevated(a, win):
        return fail(_elevated_text(label))
    verbs = {"minimize": "Свернуть", "maximize": "Развернуть", "restore": "Восстановить"}
    denied = policy.require("window", caller, f"{verbs[action]} окно «{_label(win)}» ({win.exe})?")
    if denied is not None:
        return denied
    if action == "minimize":
        a.show(win.hwnd, SW_MINIMIZE)
        return ok(f"Свернул «{label}»")
    a.show(win.hwnd, SW_MAXIMIZE if action == "maximize" else SW_RESTORE)
    if a.foreground() != win.hwnd:
        _focus(win, label)  # развёрнутое окно человек хочет видеть; не вышло — не ошибка самого действия
    return ok(f"{'Развернул' if action == 'maximize' else 'Восстановил'} «{label}»")


def list_windows_result(caller: Caller) -> Result:
    """Список окон: data — [{hwnd, title, exe}]; мозгу заголовки — через privacy.redact_title."""
    denied = policy.require("list_windows", caller, "Показать список окон")
    if denied is not None:
        return denied
    wins = list_windows()
    data = [
        {
            "hwnd": w.hwnd,
            "title": privacy.redact_title(w.title) if caller == "brain" else w.title,
            "exe": w.exe,
        }
        for w in wins
    ]
    return ok(f"Окон: {len(wins)}", data)


# --- слой ОС ----------------------------------------------------------------------------------------

GWL_EXSTYLE = -20
GW_OWNER = 4
WM_CLOSE = 0x0010
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation
FLASHW_ALL = 0x3
FLASHW_TIMERNOFG = 0xC


def _minimize_all_com() -> None:
    """Shell.Application.MinimizeAll — только в STA-потоке (apps.run_sta)."""
    import win32com.client

    win32com.client.Dispatch("Shell.Application").MinimizeAll()


class _Api:
    """Тонкий слой Win32 на ctypes: user32, kernel32, advapi32, dwmapi."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        self._c = ctypes
        self._w = w
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        try:
            dwm: Any = ctypes.WinDLL("dwmapi", use_last_error=True)
        except OSError:
            dwm = None

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", w.UINT),
                ("hwnd", w.HWND),
                ("dwFlags", w.DWORD),
                ("uCount", w.UINT),
                ("dwTimeout", w.DWORD),
            ]

        self._FLASHWINFO = FLASHWINFO
        self._enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def sig(fn: Any, argtypes: list[Any], restype: Any) -> Any:
            fn.argtypes = argtypes
            fn.restype = restype
            return fn

        self._EnumWindows = sig(u.EnumWindows, [self._enum_proc, w.LPARAM], w.BOOL)
        self._GetWindowTextLengthW = sig(u.GetWindowTextLengthW, [w.HWND], ctypes.c_int)
        self._GetWindowTextW = sig(u.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._GetClassNameW = sig(u.GetClassNameW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._IsWindowVisible = sig(u.IsWindowVisible, [w.HWND], w.BOOL)
        self._IsWindow = sig(u.IsWindow, [w.HWND], w.BOOL)
        self._IsIconic = sig(u.IsIconic, [w.HWND], w.BOOL)
        get_long = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        self._GetWindowLongPtrW = sig(get_long, [w.HWND, ctypes.c_int], ctypes.c_ssize_t)
        self._GetWindow = sig(u.GetWindow, [w.HWND, w.UINT], w.HWND)
        self._GetWindowThreadProcessId = sig(
            u.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD
        )
        self._GetForegroundWindow = sig(u.GetForegroundWindow, [], w.HWND)
        self._SetForegroundWindow = sig(u.SetForegroundWindow, [w.HWND], w.BOOL)
        self._BringWindowToTop = sig(u.BringWindowToTop, [w.HWND], w.BOOL)
        self._ShowWindow = sig(u.ShowWindow, [w.HWND, ctypes.c_int], w.BOOL)
        self._PostMessageW = sig(u.PostMessageW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL)
        self._AttachThreadInput = sig(u.AttachThreadInput, [w.DWORD, w.DWORD, w.BOOL], w.BOOL)
        self._FlashWindowEx = sig(u.FlashWindowEx, [ctypes.POINTER(FLASHWINFO)], w.BOOL)
        self._GetCurrentThreadId = sig(k.GetCurrentThreadId, [], w.DWORD)
        self._OpenProcess = sig(k.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
        self._CloseHandle = sig(k.CloseHandle, [w.HANDLE], w.BOOL)
        self._QueryFullProcessImageNameW = sig(
            k.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL
        )
        self._OpenProcessToken = sig(
            adv.OpenProcessToken, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL
        )
        self._GetTokenInformation = sig(
            adv.GetTokenInformation,
            [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)],
            w.BOOL,
        )
        self._DwmGetWindowAttribute = (
            sig(dwm.DwmGetWindowAttribute, [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD], ctypes.c_long)
            if dwm is not None
            else None
        )

    def enum_windows(self) -> list[int]:
        out: list[int] = []

        def callback(hwnd: Any, _lparam: Any) -> bool:
            if hwnd:
                out.append(int(hwnd))
            return True

        self._EnumWindows(self._enum_proc(callback), 0)
        return out

    def get_title(self, hwnd: int) -> str:
        n = self._GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = self._c.create_unicode_buffer(n + 1)
        got = self._GetWindowTextW(hwnd, buf, n + 1)
        return clean_text(buf.value[:got]) if got > 0 else ""

    def class_name(self, hwnd: int) -> str:
        buf = self._c.create_unicode_buffer(256)
        got = self._GetClassNameW(hwnd, buf, 256)
        return buf.value[:got] if got > 0 else ""

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._IsWindowVisible(hwnd))

    def is_window(self, hwnd: int) -> bool:
        return bool(self._IsWindow(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._IsIconic(hwnd))

    def is_cloaked(self, hwnd: int) -> bool:
        if self._DwmGetWindowAttribute is None:
            return False
        value = self._w.DWORD(0)
        hr = self._DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, self._c.byref(value), self._c.sizeof(value))
        return hr == 0 and value.value != 0

    def ex_style(self, hwnd: int) -> int:
        return int(self._GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF

    def owner(self, hwnd: int) -> int:
        return int(self._GetWindow(hwnd, GW_OWNER) or 0)

    def pid_of(self, hwnd: int) -> int:
        pid = self._w.DWORD(0)
        self._GetWindowThreadProcessId(hwnd, self._c.byref(pid))
        return int(pid.value)

    def thread_of(self, hwnd: int) -> int:
        return int(self._GetWindowThreadProcessId(hwnd, None))

    def current_thread_id(self) -> int:
        return int(self._GetCurrentThreadId())

    def exe_of(self, pid: int) -> str:
        """Имя файла процесса (chrome.exe); нет доступа — через psutil; не вышло — пустая строка."""
        handle = self._OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                size = self._w.DWORD(1024)
                buf = self._c.create_unicode_buffer(size.value)
                if self._QueryFullProcessImageNameW(handle, 0, buf, self._c.byref(size)):
                    return ntpath.basename(buf.value[: size.value])
            finally:
                self._CloseHandle(handle)
        try:
            return str(procs.psutil.Process(pid).name())
        except Exception:
            return ""

    def is_elevated(self, pid: int) -> bool:
        """Процесс с повышенными правами. Нет доступа к процессу или токену — считаем, что да."""
        handle = self._OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True
        try:
            token = self._w.HANDLE()
            if not self._OpenProcessToken(handle, TOKEN_QUERY, self._c.byref(token)):
                return True
            try:
                value = self._w.DWORD(0)
                size = self._w.DWORD(0)
                if not self._GetTokenInformation(
                    token,
                    TOKEN_ELEVATION_CLASS,
                    self._c.byref(value),
                    self._c.sizeof(value),
                    self._c.byref(size),
                ):
                    return True
                return value.value != 0
            finally:
                self._CloseHandle(token)
        finally:
            self._CloseHandle(handle)

    def foreground(self) -> int:
        return int(self._GetForegroundWindow() or 0)

    def set_foreground(self, hwnd: int) -> bool:
        return bool(self._SetForegroundWindow(hwnd))

    def bring_to_top(self, hwnd: int) -> bool:
        return bool(self._BringWindowToTop(hwnd))

    def show(self, hwnd: int, cmd: int) -> None:
        self._ShowWindow(hwnd, cmd)

    def post_close(self, hwnd: int) -> bool:
        return bool(self._PostMessageW(hwnd, WM_CLOSE, 0, 0))

    def attach_thread_input(self, thread: int, to_thread: int, attach: bool) -> bool:
        return bool(self._AttachThreadInput(thread, to_thread, attach))

    def flash(self, hwnd: int) -> None:
        info = self._FLASHWINFO(self._c.sizeof(self._FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 3, 0)
        self._FlashWindowEx(self._c.byref(info))

    def minimize_all(self) -> None:
        apps.run_sta(_minimize_all_com)


_api: Any = None


def api() -> Any:
    global _api
    if _api is None:
        _api = _Api()
    return _api
