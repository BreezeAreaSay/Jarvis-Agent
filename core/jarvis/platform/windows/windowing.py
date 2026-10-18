"""Окна: список (как в Alt+Tab), активное окно, поиск цели, фокус, свернуть/развернуть/закрыть, полный экран.

Окна самого Jarvis (процессы его дерева) в список не попадают и целью «текущего окна» не бывают:
«сверни окно», сказанное поверх оверлея Jarvis, относится к окну под ним. Заголовки окон — недоверенный текст:
только показываем и сравниваем. Вызовы ОС — класс WindowApi (ctypes, только Windows); выбор окна и проверка
«во весь экран» — чистые функции (тесты на Linux).
"""

import ctypes
import logging
import ntpath
import time
from collections.abc import Callable, Sequence
from typing import Any

import psutil
from rapidfuzz import fuzz

from jarvis.platform.base import AppInfo, PlatformError, WindowInfo
from jarvis.platform.windows import inputs, keys
from jarvis.platform.windows import winapi as wa
from jarvis.platform.windows.apps import exe_candidates, exe_stem, normalize_name
from jarvis.platform.windows.procs import OwnProcesses

log = logging.getLogger(__name__)

Rect = tuple[int, int, int, int]  # left, top, right, bottom

# рабочий стол и панель задач: видимые окна без владельца, но не программы
SHELL_CLASSES = frozenset({"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"})
FRAME_HOST = "applicationframehost.exe"  # рамка UWP-программ; настоящий процесс — у дочернего окна
OWN_TITLE_EXES = frozenset({"electron", "python", "pythonw"})
TITLE_THRESHOLD = 85.0
FOREGROUND_POLLS = 5
FOREGROUND_POLL_S = 0.02
EMPTY_RECT: Rect = (0, 0, 0, 0)


# --- чистые функции --------------------------------------------------------------------------------------


def fullscreen_rect(win: Rect | None, monitor: Rect | None, maximized_with_caption: bool = False) -> bool:
    """Окно закрывает свой монитор целиком (игра, видео, презентация).

    Развёрнутое обычное окно (с заголовком) — нет, даже при автоскрытии панели задач, когда оно тоже закрывает
    весь монитор.
    """
    if win is None or monitor is None or maximized_with_caption:
        return False
    if monitor[2] <= monitor[0] or monitor[3] <= monitor[1]:
        return False
    return win[0] <= monitor[0] and win[1] <= monitor[1] and win[2] >= monitor[2] and win[3] >= monitor[3]


def is_jarvis_window(title: str, exe: str, pid: int, own_pids: frozenset[int] | set[int]) -> bool:
    """Окно самого Jarvis: процесс из его дерева или заголовок «Jarvis…» у процесса Jarvis/Electron/Python.

    Вкладка браузера «Jarvis — Google Chrome» — чужое окно.
    """
    if pid in own_pids:
        return True
    stem = exe_stem(exe)
    return title.strip().casefold().startswith("jarvis") and (
        stem.startswith("jarvis") or stem in OWN_TITLE_EXES
    )


def title_matches_app(title: str, name: str) -> bool:
    """Заголовок — окно программы name: равен имени или кончается им («Документ1 - Word», «Калькулятор»).

    «Telegram Web — Google Chrome» — не окно Telegram.
    """
    t, n = normalize_name(title), normalize_name(name)
    if len(n) < 2 or not t:
        return False
    return t == n or t.endswith(f" {n}")


def pick_window(target: str, wins: Sequence[WindowInfo], exes: set[str]) -> WindowInfo | None:
    """Окно по строке (окна — в Z-порядке, сверху вниз; из подходящих берётся верхнее):
    процесс программы → точный заголовок → заголовок программы → заголовок содержит слова → похожий заголовок.
    """
    s = target.strip()
    if not s:
        return None
    exes_cf = {e.casefold() for e in exes}
    for w in wins:
        if w.exe and w.exe.casefold() in exes_cf:
            return w
    low = s.casefold()
    for w in wins:
        if w.title.strip().casefold() == low:
            return w
    for w in wins:
        if title_matches_app(w.title, s):
            return w
    query = normalize_name(s)
    if len(query) < 3:
        return None
    words = query.split()
    for w in wins:
        title_words = normalize_name(w.title).split()
        n = len(words)
        if any(title_words[i : i + n] == words for i in range(len(title_words) - n + 1)):
            return w
    best: tuple[float, WindowInfo] | None = None
    for w in wins:
        score = float(fuzz.partial_ratio(query, normalize_name(w.title)))
        if score >= TITLE_THRESHOLD and (best is None or score > best[0]):
            best = (score, w)
    return best[1] if best else None


def clean_text(text: str) -> str:
    """Непарные суррогаты (GetWindowTextW их сохраняет) → «�»: иначе дальше падает кодирование в UTF-8."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


# --- Windows -------------------------------------------------------------------------------------------


class WindowApi:
    """Окна через user32/dwmapi. Создаётся лениво в потоке платформы."""

    def __init__(
        self,
        own: OwnProcesses,
        apps: Callable[[], list[AppInfo]],
        sender: Callable[[], inputs.Sender],
    ) -> None:
        try:
            self.w = wa.api()
        except OSError as e:
            raise PlatformError("Управление окнами доступно только в Windows") from e
        self._own = own
        self._apps = apps
        self._sender = sender

    # --- сведения об окне

    def is_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self.w.IsWindow(hwnd))

    def title(self, hwnd: int) -> str:
        n = int(self.w.GetWindowTextLengthW(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        got = int(self.w.GetWindowTextW(hwnd, buf, n + 1))
        return clean_text(buf.value[:got]) if got > 0 else ""

    def class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        got = int(self.w.GetClassNameW(hwnd, buf, 256))
        return buf.value[:got] if got > 0 else ""

    def pid_of(self, hwnd: int) -> int:
        pid = wa.DWORD(0)
        self.w.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def thread_of(self, hwnd: int) -> int:
        return int(self.w.GetWindowThreadProcessId(hwnd, None))

    def exe_of(self, pid: int) -> str:
        """Имя файла процесса (chrome.exe); нет доступа — через psutil; не вышло — ""."""
        handle = self.w.OpenProcess(wa.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                size = wa.DWORD(1024)
                buf = ctypes.create_unicode_buffer(1024)
                if self.w.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return ntpath.basename(buf.value[: size.value])
            finally:
                self.w.CloseHandle(handle)
        try:
            return str(psutil.Process(pid).name())
        except psutil.Error:
            return ""

    def ex_style(self, hwnd: int) -> int:
        return int(self.w.GetWindowLongPtrW(hwnd, wa.GWL_EXSTYLE)) & 0xFFFFFFFF

    def style(self, hwnd: int) -> int:
        return int(self.w.GetWindowLongPtrW(hwnd, wa.GWL_STYLE)) & 0xFFFFFFFF

    def is_cloaked(self, hwnd: int) -> bool:
        """Окно спрятано DWM: другой виртуальный стол, приостановленная UWP-программа."""
        if self.w.DwmGetWindowAttribute is None:
            return False
        value = wa.DWORD(0)
        hr = self.w.DwmGetWindowAttribute(hwnd, wa.DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return wa.hresult_ok(hr) and value.value != 0

    def window_rect(self, hwnd: int) -> Rect | None:
        rect = wa.RECT()
        if not self.w.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.as_tuple()

    def frame_rect(self, hwnd: int) -> Rect | None:
        """Видимые границы окна (без невидимых рамок Windows 10/11); нет DWM — GetWindowRect."""
        if self.w.DwmGetWindowAttribute is not None:
            rect = wa.RECT()
            hr = self.w.DwmGetWindowAttribute(
                hwnd, wa.DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
            )
            if wa.hresult_ok(hr) and rect.right > rect.left and rect.bottom > rect.top:
                return rect.as_tuple()
        return self.window_rect(hwnd)

    def monitor_rect(self, hwnd: int) -> Rect | None:
        monitor = self.w.MonitorFromWindow(hwnd, wa.MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = wa.MONITORINFO()
        info.cbSize = ctypes.sizeof(wa.MONITORINFO)
        if not self.w.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        return info.rcMonitor.as_tuple()

    def switchable(self, hwnd: int) -> bool:
        """Правило Alt+Tab: видимое, не tool-окно, без владельца (или с WS_EX_APPWINDOW), не спрятано DWM."""
        if not self.w.IsWindowVisible(hwnd):
            return False
        ex = self.ex_style(hwnd)
        if ex & wa.WS_EX_TOOLWINDOW:
            return False
        app_window = bool(ex & wa.WS_EX_APPWINDOW)
        if not app_window and (self.w.GetWindow(hwnd, wa.GW_OWNER) or ex & wa.WS_EX_NOACTIVATE):
            return False
        if self.is_cloaked(hwnd):
            return False
        return self.class_name(hwnd).casefold() not in SHELL_CLASSES

    def child_pid(self, hwnd: int, pid: int) -> int:
        """UWP-программа: pid дочернего окна из другого процесса (настоящая программа); нет — 0."""
        found: list[int] = []

        def callback(child: Any, _lparam: Any) -> bool:
            if child:
                child_pid = self.pid_of(int(child))
                if child_pid and child_pid != pid:
                    found.append(child_pid)
                    return False
            return True

        self.w.EnumChildWindows(hwnd, self.w.WNDENUMPROC(callback), 0)
        return found[0] if found else 0

    def info(self, hwnd: int, foreground: int = 0, exe_cache: dict[int, str] | None = None) -> WindowInfo:
        cache = exe_cache if exe_cache is not None else {}
        pid = self.pid_of(hwnd)
        if pid not in cache:
            cache[pid] = self.exe_of(pid)
        exe = cache[pid]
        if exe.casefold() == FRAME_HOST:
            real = self.child_pid(hwnd, pid)
            if real:
                pid = real
                if pid not in cache:
                    cache[pid] = self.exe_of(pid)
                exe = cache[pid]
        minimized = bool(self.w.IsIconic(hwnd))
        rect = EMPTY_RECT if minimized else (self.frame_rect(hwnd) or EMPTY_RECT)
        return WindowInfo(
            hwnd=hwnd,
            title=self.title(hwnd),
            exe=exe,
            pid=pid,
            rect=rect,
            minimized=minimized,
            maximized=bool(self.w.IsZoomed(hwnd)),
            foreground=bool(foreground) and hwnd == foreground,
        )

    def enum_handles(self) -> list[int]:
        out: list[int] = []

        def callback(hwnd: Any, _lparam: Any) -> bool:
            if hwnd:
                out.append(int(hwnd))
            return True

        self.w.EnumWindows(self.w.WNDENUMPROC(callback), 0)
        return out

    def enum(self) -> tuple[list[WindowInfo], list[WindowInfo]]:
        """(чужие окна, окна Jarvis) в Z-порядке сверху вниз; только окна из Alt+Tab с заголовком."""
        own = self._own.pids()
        fg = int(self.w.GetForegroundWindow() or 0)
        cache: dict[int, str] = {}
        others: list[WindowInfo] = []
        mine: list[WindowInfo] = []
        for hwnd in self.enum_handles():
            try:
                if not self.switchable(hwnd) or not self.title(hwnd).strip():
                    continue
                info = self.info(hwnd, fg, cache)
            except Exception as e:  # окно закрылось посреди перечисления
                log.debug("окно %s пропущено: %s", hwnd, e)
                continue
            (mine if is_jarvis_window(info.title, info.exe, info.pid, own) else others).append(info)
        return others, mine

    def list_windows(self) -> list[WindowInfo]:
        return self.enum()[0]

    def foreground(self) -> WindowInfo | None:
        """Активное окно; если впереди Jarvis — верхнее чужое окно (foreground=False); рабочий стол — None."""
        fg = int(self.w.GetForegroundWindow() or 0)
        if fg:
            if self.class_name(fg).casefold() in SHELL_CLASSES:
                return None
            info = self.info(fg, fg)
            if not is_jarvis_window(info.title, info.exe, info.pid, self._own.pids()):
                return info
        others, _mine = self.enum()
        return next((w for w in others if not w.minimized), others[0] if others else None)

    def find(self, target: int | str | None) -> WindowInfo:
        """Цель: None — активное окно (не Jarvis), int или «12345» — hwnd, иначе программа или заголовок."""
        if target is None or (isinstance(target, str) and not target.strip()):
            win = self.foreground()
            if win is None:
                raise PlatformError("Нет активного окна")
            return win
        hwnd: int | None = None
        if isinstance(target, int) and not isinstance(target, bool):
            hwnd = target
        elif isinstance(target, str) and target.strip().isdigit():
            hwnd = int(target.strip())
        if hwnd is not None:
            if not self.is_window(hwnd):
                raise PlatformError("Такого окна нет")
            return self.info(hwnd, int(self.w.GetForegroundWindow() or 0))
        query = str(target).strip()
        others, _mine = self.enum()
        win = pick_window(query, others, exe_candidates(query, self._apps()))
        if win is None:
            raise PlatformError(f"Окно «{query[:60]}» не найдено")
        return win

    # --- действия

    def is_hung(self, hwnd: int) -> bool:
        return bool(self.w.IsHungAppWindow(hwnd))

    def show(self, hwnd: int, cmd: int) -> None:
        """ShowWindowAsync: не ждём окно (зависшая программа не подвесит Jarvis)."""
        if not self.w.ShowWindowAsync(hwnd, cmd):
            log.debug("ShowWindowAsync(%s, %s) вернул 0", hwnd, cmd)

    def close(self, hwnd: int) -> bool:
        """WM_CLOSE: программа закроется сама (и сама спросит про несохранённое)."""
        return bool(self.w.PostMessageW(hwnd, wa.WM_CLOSE, 0, 0))

    def _is_foreground(self, hwnd: int) -> bool:
        for i in range(FOREGROUND_POLLS):
            if int(self.w.GetForegroundWindow() or 0) == hwnd:
                return True
            if i + 1 < FOREGROUND_POLLS:
                time.sleep(FOREGROUND_POLL_S)
        return False

    def focus(self, hwnd: int) -> None:
        """Вывести окно вперёд. Лестница: SetForegroundWindow → ввод «от нас» (пустое движение мыши и Alt с
        маской) → AttachThreadInput → SwitchToThisWindow; не вышло — окно мигает на панели задач и
        PlatformError.
        """
        w = self.w
        if not self.is_window(hwnd):
            raise PlatformError("Окна уже нет")
        if self.is_hung(hwnd):  # ShowWindow/BringWindowToTop ждали бы зависшее окно
            raise PlatformError("Окно не отвечает")
        if w.IsIconic(hwnd):
            w.ShowWindow(hwnd, wa.SW_RESTORE)
        if int(w.GetForegroundWindow() or 0) == hwnd:
            return
        w.AllowSetForegroundWindow(self.pid_of(hwnd))
        # 1. просто попросить
        w.SetForegroundWindow(hwnd)
        if self._is_foreground(hwnd):
            return
        # 2. Windows разрешает смену переднего окна процессу с последним вводом: пустое движение мыши и Alt
        #    (с «маской» перед отпусканием, чтобы не открылось меню окна)
        try:
            sender = self._sender()
            sender.send([inputs.nudge_input(), sender.key(keys.VK_MENU)])
            try:
                w.SetForegroundWindow(hwnd)
            finally:
                sender.send(
                    [
                        inputs.key_input(keys.VK_MASK),
                        inputs.key_input(keys.VK_MASK, up=True),
                        sender.key(keys.VK_MENU, up=True),
                    ]
                )
        except PlatformError as e:
            log.debug("фокус: ввод не отправлен: %s", e)
        if self._is_foreground(hwnd):
            return
        # 3. присоединиться к очереди ввода переднего окна
        fg = int(w.GetForegroundWindow() or 0)
        fg_thread = self.thread_of(fg) if fg else 0
        me = int(w.GetCurrentThreadId())
        attached = False
        try:
            if fg_thread and fg_thread != me:
                attached = bool(w.AttachThreadInput(me, fg_thread, True))
            w.BringWindowToTop(hwnd)
            w.SetForegroundWindow(hwnd)
        finally:
            if attached:
                w.AttachThreadInput(me, fg_thread, False)
        if self._is_foreground(hwnd):
            return
        # 4. последнее средство
        if w.SwitchToThisWindow is not None:
            w.SwitchToThisWindow(hwnd, True)
            if self._is_foreground(hwnd):
                return
        info = wa.FLASHWINFO(ctypes.sizeof(wa.FLASHWINFO), hwnd, wa.FLASHW_ALL | wa.FLASHW_TIMERNOFG, 3, 0)
        w.FlashWindowEx(ctypes.byref(info))
        raise PlatformError("Windows не дала переключиться на окно — оно мигает на панели задач")

    def is_fullscreen_foreground(self) -> bool:
        """Активное окно (не рабочий стол и не Jarvis) закрывает свой монитор; или идёт полноэкранная
        Direct3D-игра/презентация (SHQueryUserNotificationState)."""
        w = self.w
        fg = int(w.GetForegroundWindow() or 0)
        if not fg:
            return False
        if self.class_name(fg).casefold() in SHELL_CLASSES:
            return False
        if fg in (int(w.GetShellWindow() or 0), int(w.GetDesktopWindow() or 0)):
            return False
        if self.pid_of(fg) in self._own.pids():
            return False
        maximized_with_caption = bool(w.IsZoomed(fg)) and self.style(fg) & wa.WS_CAPTION == wa.WS_CAPTION
        if fullscreen_rect(self.frame_rect(fg), self.monitor_rect(fg), maximized_with_caption):
            return True
        if w.SHQueryUserNotificationState is not None:
            state = wa.INT(0)
            if wa.hresult_ok(w.SHQueryUserNotificationState(ctypes.byref(state))):
                return state.value in (wa.QUNS_RUNNING_D3D_FULL_SCREEN, wa.QUNS_PRESENTATION_MODE)
        return False
