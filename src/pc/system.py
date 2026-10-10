"""Система: блокировка, сон/выключение/перезагрузка, буфер обмена, ввод текста в окно.

Вызовы ОС — за `_api`; shutdown.exe — через pc.subproc; ввод — через pc._input (SendInput).
"""

import logging
import os
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from pathlib import PureWindowsPath
from typing import Any, Literal

from pc import _input, policy, privacy, procs, subproc, windows
from pc.result import Caller, Result, fail, ok

log = logging.getLogger("jarvis")

PowerAction = Literal["sleep", "shutdown", "restart"]

# действие → (вопрос подтверждения, ответ)
POWER: dict[str, tuple[str, str]] = {
    "sleep": ("Перевести компьютер в сон?", "Перевожу компьютер в сон"),
    "shutdown": ("Выключить компьютер?", "Выключаю компьютер"),
    "restart": ("Перезагрузить компьютер?", "Перезагружаю компьютер"),
}
SHUTDOWN_FLAGS = {"shutdown": "/s", "restart": "/r"}

CLIPBOARD_TRIES = 5
CLIPBOARD_DELAY_S = 0.05
MAX_CLIPBOARD_GET = 65536
MAX_CLIPBOARD_SET = 100_000

MAX_TYPE = 500
CHUNK = 32
CHUNK_PAUSE_S = 0.02
MODIFIERS_WAIT_S = 1.5
CUR_TARGET_TEXT = "Укажи окно: hwnd из контекста или list_windows"

# консоли и терминалы: ввод от GPT туда — это выполнение команд
CONSOLE_EXES = frozenset(
    {
        *("cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wsl.exe", "conhost.exe"),
        *("openconsole.exe", "bash.exe", "wt.exe", "powershell_ise.exe", "wslhost.exe", "mintty.exe"),
    }
)
CONSOLE_CLASSES = frozenset({"consolewindowclass", "cascadia_hosting_window_class", "pseudoconsolewindow"})
RUN_DIALOG_CLASS = "#32770"
RUN_DIALOG_TITLES = frozenset({"выполнить", "run"})

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C


class ClipboardBusy(Exception):
    """Буфер обмена открыт другой программой."""


class _Api:
    """Тонкий слой над Win32 (только Windows; импорт win32* — лениво)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("действие доступно только в Windows")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        u = self._user32
        u.LockWorkStation.argtypes = ()
        u.LockWorkStation.restype = wintypes.BOOL
        u.GetForegroundWindow.argtypes = ()
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.GetClassNameW.restype = ctypes.c_int
        u.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        u.GetAsyncKeyState.restype = ctypes.c_short
        self._powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
        self._powrprof.SetSuspendState.argtypes = (wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN)
        self._powrprof.SetSuspendState.restype = wintypes.BOOLEAN

    def lock_workstation(self) -> bool:
        return bool(self._user32.LockWorkStation())

    def suspend(self) -> bool:
        """Сон (не гибернация). SetSuspendState возвращает управление после пробуждения."""
        try:
            import win32api
            import win32con
            import win32security

            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY
            )
            luid = win32security.LookupPrivilegeValue(None, "SeShutdownPrivilege")
            win32security.AdjustTokenPrivileges(token, False, [(luid, win32con.SE_PRIVILEGE_ENABLED)])
        except Exception as e:
            log.warning("SeShutdownPrivilege не включена: %r", e)
        return bool(self._powrprof.SetSuspendState(False, False, False))

    def clipboard_get(self) -> str | None:
        import pywintypes
        import win32clipboard

        try:
            win32clipboard.OpenClipboard()
        except pywintypes.error as e:
            raise ClipboardBusy() from e
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return None
            data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return data if isinstance(data, str) else None
        finally:
            win32clipboard.CloseClipboard()

    def clipboard_set(self, text: str) -> None:
        import pywintypes
        import win32clipboard

        try:
            win32clipboard.OpenClipboard()
        except pywintypes.error as e:
            raise ClipboardBusy() from e
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

    def foreground(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def class_name(self, hwnd: int) -> str:
        buf = self._ctypes.create_unicode_buffer(256)
        n = self._user32.GetClassNameW(hwnd, buf, len(buf))
        return buf.value[:n] if n > 0 else ""

    def is_elevated(self, pid: int) -> bool:
        """Процесс окна запущен с повышенными правами. Не удалось проверить — считаем, что да."""
        import win32api
        import win32con
        import win32security

        try:
            proc = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                token = win32security.OpenProcessToken(proc, win32con.TOKEN_QUERY)
                try:
                    return bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
                finally:
                    token.Close()
            finally:
                proc.Close()
        except Exception as e:
            log.info("права процесса %d не проверены: %r", pid, e)
            return True

    def modifiers_down(self) -> bool:
        keys = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
        return any(self._user32.GetAsyncKeyState(vk) & 0x8000 for vk in keys)


_api: _Api | None = None


def api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def _preview(text: str, n: int) -> str:
    """Первые n символов одной строкой: переводы строк — видимым «⏎»."""
    flat = text.replace("\r\n", "\n").replace("\n", "⏎")
    return flat[:n] + ("…" if len(flat) > n else "")


# --- блокировка и питание ------------------------------------------------------------------------


def lock(caller: Caller) -> Result:
    """Заблокировать компьютер (LockWorkStation)."""
    denied = policy.require("lock", caller, "Заблокировать компьютер?")
    if denied:
        return denied
    try:
        done = api().lock_workstation()
    except OSError as e:
        log.warning("LockWorkStation: %r", e)
        done = False
    return ok("Заблокировал компьютер") if done else fail("Не удалось заблокировать компьютер")


def shutdown_exe() -> str:
    """shutdown.exe из %SystemRoot%\\System32 (не из PATH)."""
    root = os.environ.get("SYSTEMROOT", "").strip()
    if not (len(root) >= 3 and root[1] == ":" and root[2] in "\\/"):
        root = r"C:\Windows"
    return str(PureWindowsPath(root, "System32", "shutdown.exe"))


def power(action: PowerAction, caller: Caller) -> Result:
    """Сон, выключение или перезагрузка — всегда с подтверждением."""
    if action not in POWER:
        return fail("Не понял: сон, выключение или перезагрузка?")
    question, answer = POWER[action]
    denied = policy.require("power", caller, question)
    if denied:
        return denied
    if action == "sleep":
        try:
            done = api().suspend()
        except OSError as e:
            log.warning("SetSuspendState: %r", e)
            done = False
        return ok(answer) if done else fail("Не удалось перевести компьютер в сон")
    argv = [shutdown_exe(), SHUTDOWN_FLAGS[action], "/t", "0"]
    try:
        r = subproc.run(argv, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("shutdown.exe: %r", e)
        return fail("Не удалось запустить shutdown.exe")
    if r.returncode != 0:
        log.warning("shutdown.exe вернул код %d", r.returncode)
        return fail(f"Windows не дала это сделать (shutdown.exe, код {r.returncode})")
    return ok(answer)


# --- буфер обмена --------------------------------------------------------------------------------


def _with_clipboard[T](fn: Callable[[], T]) -> T:
    """Повторить, пока буфер занят другой программой (до CLIPBOARD_TRIES раз через CLIPBOARD_DELAY_S)."""
    for attempt in range(CLIPBOARD_TRIES):
        try:
            return fn()
        except ClipboardBusy:
            if attempt == CLIPBOARD_TRIES - 1:
                raise
            time.sleep(CLIPBOARD_DELAY_S)
    raise ClipboardBusy()


def clipboard_get(caller: Caller) -> Result:
    """Текст из буфера обмена (data — текст; для GPT — через privacy)."""
    denied = policy.require("clipboard_get", caller, "GPT просит прочитать буфер обмена (уйдёт в облако)")
    if denied:
        return denied
    try:
        text = _with_clipboard(lambda: api().clipboard_get())
    except ClipboardBusy:
        return fail("Буфер обмена занят другой программой — попробуй ещё раз")
    except OSError as e:
        log.warning("буфер обмена: %r", e)
        return fail("Не удалось прочитать буфер обмена")
    if not text:
        return ok("В буфере обмена нет текста", "")
    text = text.replace("\r\n", "\n")
    if len(text) > MAX_CLIPBOARD_GET:
        text = text[:MAX_CLIPBOARD_GET] + "\n(обрезано)"
    if caller == "brain":
        text = privacy.redact_text(text)
    return ok(f"В буфере: {_preview(text, 60)}", text)


def clipboard_set(text: str, caller: Caller) -> Result:
    """Записать текст в буфер обмена. Для GPT ответ — «⚙ буфер: …» (текст виден в окне)."""
    if not isinstance(text, str) or not text.replace("\0", ""):
        return fail("Нечего копировать")
    text = text.replace("\0", "")
    if len(text) > MAX_CLIPBOARD_SET:
        return fail("Слишком длинный текст для буфера обмена")
    denied = policy.require("clipboard_set", caller, f"Записать в буфер: {_preview(text, 60)}?", text)
    if denied:
        return denied
    windows_text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        _with_clipboard(lambda: api().clipboard_set(windows_text))
    except ClipboardBusy:
        return fail("Буфер обмена занят другой программой — попробуй ещё раз")
    except OSError as e:
        log.warning("буфер обмена: %r", e)
        return fail("Не удалось записать в буфер обмена")
    if caller == "brain":
        return ok(f"⚙ буфер: {_preview(text, 60)}")
    return ok("Скопировал в буфер обмена")


# --- ввод текста ---------------------------------------------------------------------------------


def _find_target(target: str | int) -> windows.WindowInfo | Result:
    if isinstance(target, bool):
        return fail(CUR_TARGET_TEXT)
    if isinstance(target, str):
        s = target.strip()
        if not s or s.casefold() == "@cur":
            return fail(CUR_TARGET_TEXT)
        key: str | int = int(s) if s.isascii() and s.isdigit() else s
    elif isinstance(target, int):
        key = target
    else:
        return fail(CUR_TARGET_TEXT)
    win = windows.find_window(key)
    if win is None:
        return fail(f"Не нашёл окно «{target}»")
    return win


def _deny_reason(win: windows.WindowInfo) -> str | None:
    """Окна, куда текст не вводится: сам Jarvis, консоли и терминалы, «Выполнить», окна администратора."""
    if win.pid in procs.own_pids():
        return "Это окно самого Jarvis — вводить в него нельзя"
    a = api()
    try:
        cls = a.class_name(win.hwnd).casefold()
    except OSError:
        cls = ""
    if (win.exe or "").casefold() in CONSOLE_EXES or cls in CONSOLE_CLASSES:
        return "В консоли и терминалы текст не ввожу"
    if cls == RUN_DIALOG_CLASS and (win.title or "").strip().casefold() in RUN_DIALOG_TITLES:
        return "В окно «Выполнить» текст не ввожу"
    if a.is_elevated(win.pid):
        return "Окно запущено с правами администратора — ввести текст не получится"
    return None


def _wait_modifiers() -> bool:
    """Подождать, пока человек отпустит Ctrl/Alt/Shift/Win (Ctrl+Enter в подтверждении — ещё нажат)."""
    deadline = time.monotonic() + MODIFIERS_WAIT_S
    while api().modifiers_down():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _brain_text(caller: Caller, result: Result, title: str) -> Result:
    if caller != "brain":
        return result
    shown = privacy.redact_title(title)
    text = privacy.redact_text(result.text.replace(title, shown) if title else result.text)
    return Result(result.ok, text, result.data)


def type_text(text: str, target: str | int, caller: Caller) -> Result:
    """Ввести текст в окно target (hwnd из list_windows или название окна) — после подтверждения.

    Перед каждой порцией проверяется, что окно всё ещё на переднем плане; иначе ввод останавливается.
    """
    if policy.check("type_text", caller) == "deny":
        return fail(policy.DENY_TEXT)
    if not isinstance(text, str) or not text:
        return fail("Нечего вводить")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_TYPE:
        return fail(f"Слишком длинный текст: больше {MAX_TYPE} символов")
    if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in text):
        return fail("В тексте управляющие символы — такой текст не ввожу")

    win = _find_target(target)
    if isinstance(win, Result):
        return _brain_text(caller, win, "")
    reason = _deny_reason(win)
    if reason:
        return fail(reason)

    title = (win.title or "")[:80]
    summary = f"Ввести текст в «{title}» ({win.exe})?"
    details = text.replace("\n", "⏎\n")
    denied = policy.require("type_text", caller, summary, details)
    if denied:
        return denied

    focused = windows.focus(win.hwnd)
    if not focused.ok:
        return _brain_text(caller, focused, win.title or "")
    if not _wait_modifiers():
        return fail("Отпусти Ctrl, Alt, Shift и Win — ввод не начат")

    typed = 0
    for start in range(0, len(text), CHUNK):
        chunk = text[start : start + CHUNK]
        if api().foreground() != win.hwnd:
            return fail("Окно сменилось — ввод остановлен", {"typed": typed})
        expected = len(_input.text_inputs(chunk))
        try:
            sent = _input.send_text(chunk)
        except OSError as e:
            log.warning("SendInput: %r", e)
            sent = -1
        if sent != expected:
            return fail("Ввод прерван: Windows не приняла нажатия", {"typed": typed})
        typed += len(chunk)
        time.sleep(CHUNK_PAUSE_S)
    data: dict[str, Any] = {"typed": typed, "hwnd": win.hwnd}
    return _brain_text(caller, ok(f"Ввёл {typed} символов в «{title}»", data), title)
