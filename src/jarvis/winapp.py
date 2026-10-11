"""Windows-обвязка приложения без Qt: Job Object, один экземпляр, автозапуск, хоткей, передний план,
хук смены переднего окна (полноэкранные окна прячут сферу на рабочем столе).

Все вызовы ОС — за тонким слоем `_api` (создаётся лениво функцией api()); тесты подменяют `winapp._api`.
На Linux модуль импортируется, но без фейка `available()` ложно, и приложение эти функции не вызывает.
PySide6 здесь не импортируется. DWM (скругления, acrylic) — в jarvis/ui/dwm.py.
"""

import functools
import logging
import ntpath
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pc import settings

log = logging.getLogger("jarvis")

# --- константы Win32 ---------------------------------------------------------------------------------

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
ERROR_ALREADY_EXISTS = 183
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ASFW_ANY = 0xFFFFFFFF  # (DWORD)-1

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Jarvis"
SHOW_MESSAGE_NAME = "Jarvis.Show"
RECEIVER_TITLE = "Jarvis.Receiver"  # заголовок скрытого окна-приёмника (хоткей и «покажись»)

FALLBACK_HOTKEYS = ["ctrl+alt+shift+space", "ctrl+alt+j"]

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
MONITOR_DEFAULTTONULL = 0x00000000
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
"""Рабочий стол и панель задач закрывают монитор целиком, но полноэкранными не считаются."""


# --- слой ОС -------------------------------------------------------------------------------------------


def _fn(dll: Any, name: str, argtypes: tuple[Any, ...], restype: Any) -> Any:
    f = getattr(dll, name)
    f.argtypes = argtypes
    f.restype = restype
    return f


class _WinApi:
    """Тонкий слой над Win32: ctypes (user32/kernel32), pywin32 (win32job) и winreg — всё лениво."""

    @functools.cached_property
    def _u32(self) -> dict[str, Any]:
        import ctypes
        from ctypes import wintypes

        dll = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        return {
            "RegisterHotKey": _fn(
                dll,
                "RegisterHotKey",
                (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT),
                wintypes.BOOL,
            ),
            "UnregisterHotKey": _fn(dll, "UnregisterHotKey", (wintypes.HWND, ctypes.c_int), wintypes.BOOL),
            "AllowSetForegroundWindow": _fn(
                dll, "AllowSetForegroundWindow", (wintypes.DWORD,), wintypes.BOOL
            ),
            "SetForegroundWindow": _fn(dll, "SetForegroundWindow", (wintypes.HWND,), wintypes.BOOL),
            "FindWindowW": _fn(dll, "FindWindowW", (wintypes.LPCWSTR, wintypes.LPCWSTR), wintypes.HWND),
            "PostMessageW": _fn(
                dll,
                "PostMessageW",
                (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
                wintypes.BOOL,
            ),
            "RegisterWindowMessageW": _fn(dll, "RegisterWindowMessageW", (wintypes.LPCWSTR,), wintypes.UINT),
            "GetWindowThreadProcessId": _fn(
                dll,
                "GetWindowThreadProcessId",
                (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)),
                wintypes.DWORD,
            ),
        }

    @functools.cached_property
    def _k32(self) -> dict[str, Any]:
        import ctypes
        from ctypes import wintypes

        dll = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        return {
            "CreateMutexW": _fn(
                dll, "CreateMutexW", (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR), wintypes.HANDLE
            ),
            "CloseHandle": _fn(dll, "CloseHandle", (wintypes.HANDLE,), wintypes.BOOL),
            "GetTickCount": _fn(dll, "GetTickCount", (), wintypes.DWORD),
        }

    @staticmethod
    def _last_error() -> int:
        import ctypes

        return ctypes.get_last_error()

    # --- хоткей и окна

    def register_hotkey(self, hwnd: int, hotkey_id: int, mods: int, vk: int) -> tuple[bool, int]:
        ok = bool(self._u32["RegisterHotKey"](hwnd, hotkey_id, mods, vk))
        return ok, 0 if ok else self._last_error()

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> bool:
        return bool(self._u32["UnregisterHotKey"](hwnd, hotkey_id))

    def allow_set_foreground(self, pid: int) -> bool:
        return bool(self._u32["AllowSetForegroundWindow"](pid))

    def set_foreground(self, hwnd: int) -> bool:
        return bool(self._u32["SetForegroundWindow"](hwnd))

    def find_window(self, title: str) -> int:
        return int(self._u32["FindWindowW"](None, title) or 0)

    def window_pid(self, hwnd: int) -> int:
        import ctypes
        from ctypes import wintypes

        pid = wintypes.DWORD(0)
        self._u32["GetWindowThreadProcessId"](hwnd, ctypes.byref(pid))
        return int(pid.value)

    def post_message(self, hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        return bool(self._u32["PostMessageW"](hwnd, msg, wparam, lparam))

    def register_window_message(self, name: str) -> int:
        return int(self._u32["RegisterWindowMessageW"](name))

    def tick_count(self) -> int:
        return int(self._k32["GetTickCount"]())

    # --- один экземпляр

    def create_mutex(self, name: str) -> tuple[int, bool]:
        """(handle, уже существовал). Не создан — OSError."""
        handle = self._k32["CreateMutexW"](None, False, name)
        err = self._last_error()
        if not handle:
            raise OSError(err, f"CreateMutexW: ошибка {err}")
        return int(handle), err == ERROR_ALREADY_EXISTS

    def close_handle(self, handle: int) -> None:
        self._k32["CloseHandle"](handle)

    # --- Job Object (pywin32)

    def create_job(self) -> Any:
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        # SILENT_BREAKAWAY_OK: в job только то, что положили явно (cmd.exe, llama-server.exe, codex.exe);
        # программы, которые pc.mcp откроет через ShellExecute по просьбе GPT, job не наследуют
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        )
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        return job

    def assign_to_job(self, job: Any, pid: int) -> bool:
        """Добавить процесс в job; False — он уже там (например, унаследовал членство от родителя)."""
        import win32api
        import win32con
        import win32job

        access = (
            win32con.PROCESS_SET_QUOTA
            | win32con.PROCESS_TERMINATE
            | win32con.PROCESS_QUERY_LIMITED_INFORMATION
        )
        handle = win32api.OpenProcess(access, False, pid)
        try:
            if win32job.IsProcessInJob(handle, job):
                return False
            win32job.AssignProcessToJobObject(job, handle)
            return True
        finally:
            win32api.CloseHandle(handle)

    def close_job(self, job: Any) -> None:
        import win32api

        win32api.CloseHandle(job)

    # --- реестр HKCU\...\Run

    def reg_get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def reg_set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def reg_delete(self, name: str) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return False
        return True

    # --- снимки экрана

    def set_display_affinity(self, hwnd: int, affinity: int) -> tuple[bool, int]:
        """SetWindowDisplayAffinity; (успех, GetLastError)."""
        import ctypes
        from ctypes import wintypes

        dll = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        fn = _fn(dll, "SetWindowDisplayAffinity", (wintypes.HWND, wintypes.DWORD), wintypes.BOOL)
        ok = bool(fn(hwnd, affinity))
        return ok, 0 if ok else self._last_error()

    # --- передний план: хук смены окна и полноэкранные окна

    @functools.cached_property
    def _fg(self) -> dict[str, Any]:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            )

        dll = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        proc = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
            None,
            wintypes.HANDLE,  # HWINEVENTHOOK
            wintypes.DWORD,  # event
            wintypes.HWND,
            wintypes.LONG,  # idObject
            wintypes.LONG,  # idChild
            wintypes.DWORD,  # idEventThread
            wintypes.DWORD,  # dwmsEventTime
        )
        return {
            "MONITORINFO": MONITORINFO,
            "WINEVENTPROC": proc,
            "SetWinEventHook": _fn(
                dll,
                "SetWinEventHook",
                (
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HMODULE,
                    proc,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                ),
                wintypes.HANDLE,
            ),
            "UnhookWinEvent": _fn(dll, "UnhookWinEvent", (wintypes.HANDLE,), wintypes.BOOL),
            "GetForegroundWindow": _fn(dll, "GetForegroundWindow", (), wintypes.HWND),
            "GetWindowRect": _fn(
                dll, "GetWindowRect", (wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL
            ),
            "MonitorFromWindow": _fn(
                dll, "MonitorFromWindow", (wintypes.HWND, wintypes.DWORD), wintypes.HMONITOR
            ),
            "GetMonitorInfoW": _fn(
                dll, "GetMonitorInfoW", (wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)), wintypes.BOOL
            ),
            "GetClassNameW": _fn(
                dll, "GetClassNameW", (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int
            ),
        }

    def set_foreground_hook(self, callback: Callable[[int], None]) -> tuple[int, Any]:
        """SetWinEventHook(EVENT_SYSTEM_FOREGROUND), вне контекста, без своих окон: (handle, колбэк)."""
        fg = self._fg

        def on_event(_hook: Any, _event: int, hwnd: Any, *_rest: Any) -> None:
            try:
                callback(int(hwnd or 0))
            except Exception:  # исключение не должно уйти в Windows через ctypes
                log.exception("обработчик смены переднего окна")

        proc = fg["WINEVENTPROC"](on_event)
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        handle = fg["SetWinEventHook"](
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, proc, 0, 0, flags
        )
        if not handle:
            err = self._last_error()
            raise OSError(err, f"SetWinEventHook: ошибка {err}")
        return int(handle), proc

    def unhook_win_event(self, handle: int) -> bool:
        return bool(self._fg["UnhookWinEvent"](handle))

    def foreground_window(self) -> int:
        return int(self._fg["GetForegroundWindow"]() or 0)

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not self._fg["GetWindowRect"](hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def monitor_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        """Прямоугольник монитора, на котором окно (без учёта панели задач); окна нет — None."""
        import ctypes

        fg = self._fg
        monitor = fg["MonitorFromWindow"](hwnd, MONITOR_DEFAULTTONULL)
        if not monitor:
            return None
        info = fg["MONITORINFO"]()
        info.cbSize = ctypes.sizeof(info)
        if not fg["GetMonitorInfoW"](monitor, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return r.left, r.top, r.right, r.bottom

    def class_name(self, hwnd: int) -> str:
        import ctypes

        buf = ctypes.create_unicode_buffer(256)
        n = self._fg["GetClassNameW"](hwnd, buf, len(buf))
        return buf.value if n > 0 else ""


_api: Any = None


def api() -> Any:
    """Слой ОС; на не-Windows без подменённого фейка — OSError."""
    global _api
    if _api is None:
        if sys.platform != "win32":
            raise OSError("jarvis.winapp: только Windows")
        _api = _WinApi()
    return _api


def available() -> bool:
    """Есть ли с чем работать: настоящий Windows или фейк в тестах."""
    return _api is not None or sys.platform == "win32"


# --- Job Object --------------------------------------------------------------------------------------


def create_job() -> Any | None:
    """Job Object с KILL_ON_JOB_CLOSE: закрылся последний дескриптор (Jarvis вышел или упал) — дети завершены.

    Не вышло — None и запись в лог (Jarvis работает и без job).
    """
    try:
        return api().create_job()
    except Exception as e:
        log.warning("Job Object не создан: %s", e)
        return None


def _child_pids(pid: int) -> list[int]:
    """Потомки процесса (рекурсивно); процесса уже нет — пусто."""
    try:
        import psutil

        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except Exception:
        return []


def own_descendants(exe_name: str) -> list[int]:
    """PID потомков этого процесса с таким именем exe (без учёта регистра); ошибка — пусто."""
    try:
        import psutil

        result = []
        for p in psutil.Process().children(recursive=True):
            try:
                if p.name().casefold() == exe_name.casefold():
                    result.append(p.pid)
            except psutil.Error:
                continue
        return result
    except Exception as e:
        log.debug("потомки процесса: %s", e)
        return []


def add_to_job(job: Any, proc: Any) -> bool:
    """Положить процесс (Popen или PID) в job. Ошибки — в лог, без исключения.

    Потомки, которых процесс успел запустить до назначения (cmd.exe → llama-server.exe), добавляются следом;
    запущенные после назначения попадают в job сами — членство в job наследуется.
    """
    if job is None or proc is None:
        return False
    pid = int(getattr(proc, "pid", proc))
    try:
        api().assign_to_job(job, pid)
    except Exception as e:
        log.warning("процесс %d не добавлен в Job Object: %s", pid, e)
        return False
    for child in _child_pids(pid):
        try:
            api().assign_to_job(job, child)
        except Exception as e:  # успел завершиться
            log.debug("потомок %d не добавлен в Job Object: %s", child, e)
    log.info("процесс %d в Job Object", pid)
    return True


def job_hook(job: Any) -> Callable[[Any], None] | None:
    """Хук для Hands(job_hook=…) и Brain(process_hook=…): каждый запущенный процесс — в job."""
    if job is None:
        return None

    def hook(proc: Any) -> None:
        add_to_job(job, proc)

    return hook


def close_job(job: Any) -> None:
    """Закрыть job: всё, что ещё в нём, Windows завершит (KILL_ON_JOB_CLOSE)."""
    if job is None:
        return
    try:
        api().close_job(job)
    except Exception as e:
        log.warning("Job Object не закрыт: %s", e)


# --- один экземпляр ------------------------------------------------------------------------------------


def _user() -> str:
    raw = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_.")[:40] or "user"


def instance_mutex_name() -> str:
    return rf"Local\Jarvis-{_user()}-run"


def acquire_instance() -> int | None:
    """Именованный mutex одного экземпляра. None — Jarvis уже запущен; иначе дескриптор (держать до выхода).

    Mutex не создался (редкая ошибка ОС) — 0: лучше второй экземпляр, чем ни одного.
    """
    try:
        handle, exists = api().create_mutex(instance_mutex_name())
    except OSError as e:
        log.warning("mutex одного экземпляра не создан: %s", e)
        return 0
    if exists:
        api().close_handle(handle)
        return None
    return handle


def release_instance(handle: int | None) -> None:
    if handle:
        try:
            api().close_handle(handle)
        except OSError as e:
            log.warning("mutex не закрыт: %s", e)


@functools.cache
def show_message_id() -> int:
    """Зарегистрированное сообщение «покажи окно» (одно и то же число во всех процессах сеанса)."""
    try:
        return api().register_window_message(SHOW_MESSAGE_NAME)
    except Exception as e:
        log.warning("RegisterWindowMessage: %s", e)
        return 0


def signal_first_instance(timeout_s: float = 3.0) -> bool:
    """Второй запуск: найти окно-приёмник первого экземпляра, дать ему передний план, попросить показаться.

    Первый экземпляр мог ещё не создать окно — ждём до timeout_s.
    """
    msg = show_message_id()
    if not msg:
        return False
    deadline = time.monotonic() + timeout_s
    while True:
        hwnd = api().find_window(RECEIVER_TITLE)
        if hwnd:
            pid = api().window_pid(hwnd)
            if pid:
                api().allow_set_foreground(pid)
            ok = api().post_message(hwnd, msg, 0, 0)
            log.info("второй запуск: окно первого экземпляра %s", "показано" if ok else "не ответило")
            return ok
        if time.monotonic() >= deadline:
            log.warning("второй запуск: окно первого экземпляра не найдено")
            return False
        time.sleep(0.1)


# --- автозапуск ----------------------------------------------------------------------------------------


def autostart_command() -> str:
    """Команда для HKCU\\…\\Run.

    exe — `"<install>\\Jarvis.exe"`; разработка — `"<venv>\\Scripts\\pythonw.exe" -m jarvis run`.
    """
    if settings.is_frozen():
        return f'"{ntpath.join(str(settings.install_dir()), "Jarvis.exe")}"'
    pythonw = ntpath.join(ntpath.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" -m jarvis run'


def autostart_enabled() -> bool:
    if not available():
        return False
    try:
        return bool(api().reg_get(RUN_VALUE))
    except OSError as e:
        log.warning("автозапуск не прочитан: %s", e)
        return False


def set_autostart(on: bool) -> str:
    """Включить/выключить автозапуск при входе в Windows. Возвращает фразу для человека."""
    if not available():
        return "Автозапуск есть только в Windows"
    try:
        if on:
            command = autostart_command()
            api().reg_set(RUN_VALUE, command)
            log.info("автозапуск включён: %s", command)
            return f"Автозапуск включён: {command}"
        removed = api().reg_delete(RUN_VALUE)
        log.info("автозапуск выключен")
        return "Автозапуск выключен" if removed else "Автозапуск и так выключен"
    except OSError as e:
        log.warning("автозапуск не изменён: %s", e)
        return f"Не удалось изменить автозапуск: {e}"


# --- хоткей ------------------------------------------------------------------------------------------

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_MOD_NAMES = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))

_KEYS: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "pause": 0x13,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "`": 0xC0,
    "backquote": 0xC0,
    "-": 0xBD,
    "minus": 0xBD,
    "=": 0xBB,
    "equals": 0xBB,
    "plus": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    "backslash": 0xDC,
    ";": 0xBA,
    "semicolon": 0xBA,
    "'": 0xDE,
    "quote": 0xDE,
    ",": 0xBC,
    "comma": 0xBC,
    ".": 0xBE,
    "period": 0xBE,
    "/": 0xBF,
    "slash": 0xBF,
}
_KEYS.update({chr(c): ord(chr(c).upper()) for c in range(ord("a"), ord("z") + 1)})
_KEYS.update({str(d): 0x30 + d for d in range(10)})
_KEYS.update({f"f{n}": 0x70 + n - 1 for n in range(1, 25)})
_KEYS.update({f"numpad{d}": 0x60 + d for d in range(10)})
_KEY_NAMES = {vk: name for name, vk in reversed(list(_KEYS.items()))}


def parse_hotkey(text: str) -> tuple[int, int]:
    """«ctrl+alt+space» → (модификаторы | MOD_NOREPEAT, код клавиши VK). Ошибка — ValueError по-русски."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("пустое сочетание клавиш")
    parts = [p.strip().casefold() for p in text.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"лишний «+» в сочетании «{text}»")
    mods = 0
    vk: int | None = None
    for part in parts:
        if part in _MODS:
            if mods & _MODS[part]:
                raise ValueError(f"модификатор «{part}» повторяется в «{text}»")
            mods |= _MODS[part]
        elif part in _KEYS:
            if vk is not None:
                raise ValueError(f"в «{text}» две обычные клавиши — нужна одна")
            vk = _KEYS[part]
        else:
            raise ValueError(
                f"неизвестная клавиша «{part}» в «{text}»: можно ctrl, alt, shift, win и одну клавишу "
                "(латинская буква, цифра, space, F1–F24, enter, tab…)"
            )
    if vk is None:
        raise ValueError(f"в «{text}» нет обычной клавиши — только модификаторы")
    if not mods and not 0x7C <= vk <= 0x87:  # без модификаторов — только F13–F24
        raise ValueError(f"«{text}»: нужен хотя бы один модификатор (ctrl, alt, shift, win)")
    return mods | MOD_NOREPEAT, vk


def format_hotkey(text: str) -> str:
    """«ctrl+alt+space» → «Ctrl+Alt+Space» (для подсказки и уведомлений); неразборное — как есть."""
    try:
        mods, vk = parse_hotkey(text)
    except ValueError:
        return text
    names = [name for flag, name in _MOD_NAMES if mods & flag]
    key = _KEY_NAMES.get(vk, hex(vk))
    names.append(key.upper() if len(key) == 1 or (key.startswith("f") and key[1:].isdigit()) else key.title())
    return "+".join(names)


def register_hotkey(hwnd: int, hotkey_id: int, mods: int, vk: int) -> tuple[bool, int]:
    """RegisterHotKey на окно; (успех, GetLastError)."""
    try:
        return api().register_hotkey(hwnd, hotkey_id, mods, vk)
    except OSError as e:
        return False, int(e.errno or 0)


def unregister_hotkey(hwnd: int, hotkey_id: int) -> bool:
    try:
        return api().unregister_hotkey(hwnd, hotkey_id)
    except OSError:
        return False


@dataclass(frozen=True)
class HotkeyResult:
    combo: str | None  # зарегистрированное сочетание; None — ни одно
    message: str  # что сказать человеку; пусто — всё как в конфиге


def register_with_fallback(hwnd: int, hotkey_id: int, preferred: str) -> HotkeyResult:
    """Сочетание из конфига; занято (1409) или не разобрано — запасные FALLBACK_HOTKEYS по порядку."""
    candidates = [preferred, *FALLBACK_HOTKEYS]
    tried: set[tuple[int, int]] = set()
    problem = ""
    for combo in candidates:
        try:
            mods, vk = parse_hotkey(combo)
        except ValueError as e:
            problem = f"Сочетание «{combo}» из конфига не разобрано: {e}."
            continue
        if (mods, vk) in tried:
            continue
        tried.add((mods, vk))
        ok, err = register_hotkey(hwnd, hotkey_id, mods, vk)
        if ok:
            log.info("хоткей %s зарегистрирован", format_hotkey(combo))
            if combo == preferred:
                return HotkeyResult(combo, "")
            return HotkeyResult(combo, f"{problem} Jarvis открывается по {format_hotkey(combo)}.".strip())
        if err != ERROR_HOTKEY_ALREADY_REGISTERED:
            log.warning("RegisterHotKey %s: ошибка %d", combo, err)
            return HotkeyResult(
                None,
                f"Хоткей {format_hotkey(combo)} не зарегистрирован (ошибка {err}). Открой Jarvis из трея.",
            )
        log.warning("хоткей %s занят другой программой", format_hotkey(combo))
        if not problem:
            problem = f"{format_hotkey(combo)} занято другой программой."
    return HotkeyResult(None, f"{problem} Запасные сочетания тоже заняты — открой Jarvis из трея.".strip())


# --- передний план -------------------------------------------------------------------------------------


def allow_set_foreground_any() -> bool:
    """AllowSetForegroundWindow(ASFW_ANY): запущенная по команде программа сможет открыться поверх."""
    try:
        return api().allow_set_foreground(ASFW_ANY)
    except OSError:
        return False


def set_foreground(hwnd: int) -> bool:
    """SetForegroundWindow; сразу после WM_HOTKEY право на передний план есть, позже Windows только мигнёт."""
    try:
        return api().set_foreground(hwnd)
    except OSError:
        return False


def tick_count() -> int:
    """GetTickCount (мс, 32 бита) — для задержки сообщения в очереди: MSG.time в тех же единицах."""
    return api().tick_count()


# --- снимки экрана -------------------------------------------------------------------------------------

WDA_EXCLUDEFROMCAPTURE = 0x00000011


def exclude_from_capture(hwnd: int) -> bool:
    """Окно Jarvis не попадает в снимки экрана («экран: …» для GPT, PrintScreen, запись экрана):
    SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE), Windows 10 2004+; на более старой Windows то же значение
    работает как WDA_MONITOR (в снимке — чёрный прямоугольник). Не Windows, нет окна или ошибка — False и лог.
    Действует на HWND: пересозданное окно нужно исключить снова."""
    if not hwnd or not available():
        return False
    try:
        ok, err = api().set_display_affinity(int(hwnd), WDA_EXCLUDEFROMCAPTURE)
    except Exception as e:
        log.warning("окно Jarvis не исключено из снимков экрана: %s", e)
        return False
    if ok:
        log.info("окно %#x исключено из снимков экрана", int(hwnd))
    else:
        log.warning("окно Jarvis не исключено из снимков экрана: ошибка %d", err)
    return ok


# --- полноэкранные окна (сфера на рабочем столе прячется) -------------------------------------------------


@dataclass
class ForegroundWatch:
    """Поставленный хук смены переднего окна. proc — колбэк ctypes: пока хук жив, ссылку на него держим здесь,
    иначе сборщик мусора освободит его и Windows позовёт освобождённую память."""

    handle: int
    proc: Any


def watch_foreground(callback: Callable[[int], None]) -> ForegroundWatch | None:
    """callback(hwnd) при смене переднего окна — без опроса по таймеру.

    Хук вне контекста (WINEVENT_OUTOFCONTEXT): колбэк приходит в цикле сообщений потока, который его поставил
    (UI-поток Qt). Свои окна не сообщаются (WINEVENT_SKIPOWNPROCESS). Не Windows или ошибка — None и лог.
    Ограничение: окно, ставшее полноэкранным без смены переднего окна (F11 в браузере), хук не заметит.
    """
    if not available():
        return None
    try:
        handle, proc = api().set_foreground_hook(callback)
    except Exception as e:
        log.warning("хук смены переднего окна не поставлен: %s", e)
        return None
    return ForegroundWatch(handle, proc)


def unwatch_foreground(watch: ForegroundWatch | None) -> None:
    """UnhookWinEvent; повторно — ничего."""
    if watch is None or not watch.handle:
        return
    try:
        api().unhook_win_event(watch.handle)
    except Exception as e:
        log.warning("хук смены переднего окна не снят: %s", e)
    watch.handle = 0


def is_fullscreen(hwnd: int) -> bool:
    """Окно закрывает весь свой монитор (игра, видео во весь экран).

    Рабочий стол и панель задач — нет; развёрнутое обычное окно (панель задач видна) — нет. Ошибка — нет.
    """
    if not hwnd or not available():
        return False
    try:
        if api().class_name(hwnd) in SHELL_CLASSES:
            return False
        win, mon = api().window_rect(hwnd), api().monitor_rect(hwnd)
    except Exception as e:
        log.debug("размер окна %s: %s", hwnd, e)
        return False
    if win is None or mon is None:
        return False
    return win[0] <= mon[0] and win[1] <= mon[1] and win[2] >= mon[2] and win[3] >= mon[3]


def foreground_fullscreen() -> bool:
    """Сейчас впереди полноэкранное окно (при старте: хук сообщает только о сменах)."""
    if not available():
        return False
    try:
        hwnd = api().foreground_window()
    except Exception as e:
        log.debug("переднее окно: %s", e)
        return False
    return is_fullscreen(hwnd)
