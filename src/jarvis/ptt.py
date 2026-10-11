"""Push-to-talk: глобальная клавиша через низкоуровневый хук клавиатуры WH_KEYBOARD_LL.

RegisterHotKey не сообщает об отпускании и «съедает» клавишу — поэтому хук. Особенности (FULL-PROMPT S9):
- свой поток со своим циклом GetMessage: Windows вызывает хук внутри GetMessage этого потока;
- колбэк тривиальный: событие в очередь и CallNextHookEx. Клавиша не глушится — правый Ctrl работает
  в других программах как обычно;
- колбэк дольше LowLevelHooksTimeout (≤1 с) — Windows молча и навсегда снимает хук, а в Python к этому
  приводит и долгое удержание GIL другим потоком: хук переустанавливается каждые REHOOK_S и после пробуждения
  ПК (пауза между тиками таймера потока больше RESUME_GAP_S);
- ссылка на объект WINFUNCTYPE — на уровне модуля (`_proc`): иначе сборщик мусора → падение процесса;
- argtypes/restype объявлены явно, LRESULT = c_ssize_t;
- нажатия в окнах с правами администратора хук не видит (UIPI) — об этом говорит doctor.

События в очередь: ("down" | "up" | "other" | "esc" | "resume", time.monotonic()). "other" — нажата другая
клавиша, пока PTT удерживается (сочетание вроде правый Ctrl+C — это не голос); "esc" — Esc в любом окне
(глушит речь Jarvis, даже если его окно не в фокусе). Разбирает события jarvis.voice.
Вызовы ОС — за слоем `_api` (создаётся лениво api()); тесты подменяют `ptt._api` фейком.
"""

import logging
import queue
import re
import threading
import time
from typing import Any

log = logging.getLogger("jarvis")

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_TIMER = 0x0113
WM_QUIT = 0x0012

TICK_MS = 1000  # таймер потока хука
REHOOK_S = 10.0  # переустановка хука: тихо снятый Windows хук оживает не позже чем через столько секунд
RESUME_GAP_S = 5.0  # пауза между тиками больше TICK + этого — ПК спал
START_TIMEOUT_S = 5.0

DEFAULT_KEY = "right ctrl"

PttEvent = tuple[str, float]

# --- имя клавиши → виртуальный код ---------------------------------------------------------------

VK_RCONTROL = 0xA3
VK_ESCAPE = 0x1B
_KEYS: dict[str, int] = {
    **dict.fromkeys(("rctrl", "ctrlr"), VK_RCONTROL),
    **dict.fromkeys(("lctrl", "ctrll"), 0xA2),
    **dict.fromkeys(("rshift", "shiftr"), 0xA1),
    **dict.fromkeys(("lshift", "shiftl"), 0xA0),
    **dict.fromkeys(("ralt", "altr", "altgr", "rmenu"), 0xA5),
    **dict.fromkeys(("lalt", "altl", "lmenu"), 0xA4),
    **dict.fromkeys(("rwin", "winr"), 0x5C),
    **dict.fromkeys(("lwin", "winl"), 0x5B),
    **dict.fromkeys(("apps", "menu", "contextmenu", "application"), 0x5D),
    **dict.fromkeys(("capslock", "caps"), 0x14),
    **dict.fromkeys(("scrolllock", "scroll"), 0x91),
    **dict.fromkeys(("pause", "break"), 0x13),
    **dict.fromkeys(("insert", "ins"), 0x2D),
    "numlock": 0x90,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
# сокращения при разборе: «right ctrl», «RightControl», «правый ctrl», «ctrl right» → rctrl / ctrlr
_WORDS = {
    "правый": "right",
    "правая": "right",
    "левый": "left",
    "левая": "left",
    "контрол": "ctrl",
    "ктрл": "ctrl",
    "шифт": "shift",
    "альт": "alt",
}
_SHORT = (("right", "r"), ("left", "l"), ("control", "ctrl"))
_AMBIGUOUS = {"ctrl", "shift", "alt", "win"}
_LABELS = {
    0xA3: "правый Ctrl",
    0xA2: "левый Ctrl",
    0xA1: "правый Shift",
    0xA0: "левый Shift",
    0xA5: "правый Alt",
    0xA4: "левый Alt",
    0x5C: "правая Win",
    0x5B: "левая Win",
    0x5D: "Menu",
    0x14: "Caps Lock",
    0x91: "Scroll Lock",
    0x13: "Pause",
    0x2D: "Insert",
    0x90: "Num Lock",
}


def parse_key(name: str) -> int:
    """Имя клавиши из [voice] ptt_key → виртуальный код Windows; непонятное — ValueError.

    Понимает «right ctrl», «RightCtrl», «rctrl», «правый ctrl», «altgr», «f9», «caps lock», «0xA3».
    Ctrl, Shift, Alt и Win без стороны — ошибка: хук различает левую и правую клавишу.
    """
    raw = str(name).strip().casefold().replace("ё", "е")
    if re.fullmatch(r"0x[0-9a-f]{1,2}", raw):
        vk = int(raw, 16)
        if 1 <= vk <= 0xFE:
            return vk
        raise ValueError(f"код клавиши вне 0x01..0xFE: {name!r}")
    key = "".join(_WORDS.get(w, w) for w in re.split(r"[\s_+\-]+", raw) if w)
    for long, short in _SHORT:
        key = key.replace(long, short)
    if key in _KEYS:
        return _KEYS[key]
    if key in _AMBIGUOUS:
        raise ValueError(f"укажи сторону: right {key} или left {key}")
    raise ValueError(f"неизвестная клавиша: {name!r} (пример: right ctrl, f9, caps lock)")


def key_label(vk: int) -> str:
    """Название клавиши для человека: «правый Ctrl», «F9»."""
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return _LABELS.get(vk, f"клавиша 0x{vk:02X}")


# --- слой ОС ------------------------------------------------------------------------------------


class _WinApi:
    """user32/kernel32 для хука; функции — со своими argtypes в отдельном WinDLL (не общий windll)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        self._ctypes = ctypes
        self._msg_type = w.MSG
        lresult = ctypes.c_ssize_t
        self.proc_type = ctypes.WINFUNCTYPE(lresult, ctypes.c_int, w.WPARAM, w.LPARAM)  # type: ignore[attr-defined]
        u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        def fn(dll: Any, name: str, argtypes: list[Any], restype: Any) -> Any:
            f = getattr(dll, name)
            f.argtypes = argtypes
            f.restype = restype
            return f

        self._set_hook = fn(
            u, "SetWindowsHookExW", [ctypes.c_int, self.proc_type, w.HINSTANCE, w.DWORD], w.HHOOK
        )
        self._unhook = fn(u, "UnhookWindowsHookEx", [w.HHOOK], w.BOOL)
        self._call_next = fn(u, "CallNextHookEx", [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM], lresult)
        self._get_message = fn(u, "GetMessageW", [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], w.BOOL)
        self._post_thread = fn(u, "PostThreadMessageW", [w.DWORD, w.UINT, w.WPARAM, w.LPARAM], w.BOOL)
        self._set_timer = fn(
            u, "SetTimer", [w.HWND, ctypes.c_size_t, w.UINT, ctypes.c_void_p], ctypes.c_size_t
        )
        self._kill_timer = fn(u, "KillTimer", [w.HWND, ctypes.c_size_t], w.BOOL)
        self._key_state = fn(u, "GetAsyncKeyState", [ctypes.c_int], ctypes.c_short)
        self._thread_id = fn(k, "GetCurrentThreadId", [], w.DWORD)
        self._module = fn(k, "GetModuleHandleW", [w.LPCWSTR], w.HMODULE)

    def make_proc(self, fn: Any) -> Any:
        return self.proc_type(fn)

    def thread_id(self) -> int:
        return int(self._thread_id())

    def install(self, proc: Any) -> int:
        """SetWindowsHookExW(WH_KEYBOARD_LL); 0 → OSError."""
        handle = self._set_hook(WH_KEYBOARD_LL, proc, self._module(None), 0)
        if not handle:
            raise self._ctypes.WinError(self._ctypes.get_last_error())  # type: ignore[attr-defined]
        return int(handle)

    def uninstall(self, handle: int) -> None:
        if handle:
            self._unhook(handle)

    def call_next(self, code: int, wparam: int, lparam: int) -> int:
        return int(self._call_next(None, code, wparam, lparam))

    def vk_of(self, lparam: int) -> int:
        """vkCode — первое поле KBDLLHOOKSTRUCT (DWORD) по адресу lParam."""
        return int(self._ctypes.c_uint32.from_address(lparam).value)

    def get_message(self) -> int | None:
        """Следующее сообщение потока; None — WM_QUIT или ошибка."""
        msg = self._msg_type()
        r = self._get_message(self._ctypes.byref(msg), None, 0, 0)
        if r == 0 or r == -1:
            return None
        return int(msg.message)

    def post_quit(self, thread_id: int) -> None:
        self._post_thread(thread_id, WM_QUIT, 0, 0)

    def set_timer(self, ms: int) -> int:
        return int(self._set_timer(None, 0, ms, None))

    def kill_timer(self, timer: int) -> None:
        if timer:
            self._kill_timer(None, timer)

    def key_down(self, vk: int) -> bool:
        return bool(self._key_state(vk) & 0x8000)


_api: Any = None
_proc: Any = None  # объект WINFUNCTYPE живёт до конца процесса: Windows держит на него указатель
_active: "Hook | None" = None  # хук, чей поток сейчас работает (колбэк — функция модуля)


def api() -> Any:
    global _api
    if _api is None:
        _api = _WinApi()
    return _api


def _hook_proc(code: int, wparam: int, lparam: int) -> int:
    """Колбэк хука: только разбор vkCode и событие в очередь — всё остальное делает поток voice."""
    hook = _active
    if hook is not None and code == HC_ACTION:
        try:  # noqa: SIM105 — без contextlib: колбэк зовётся на каждое нажатие в системе
            hook.on_key(_api.vk_of(lparam), wparam in (WM_KEYUP, WM_SYSKEYUP))
        except Exception:  # исключение в колбэке хука нельзя выпускать наружу
            pass
    return _api.call_next(code, wparam, lparam)


class PttError(RuntimeError):
    """Хук не установлен (не Windows, отказ SetWindowsHookEx)."""


class Hook:
    """Хук клавиши PTT в своём потоке. Один на процесс: start() второго останавливает первый."""

    def __init__(self, vk: int, events: "queue.SimpleQueue[PttEvent]") -> None:
        self.vk = vk
        self.events = events
        # клавиша нажата — по событиям хука; voice сбрасывает (release()), если хук пропустил отпускание
        self.down = False
        self.rehooks = 0
        self._handle = 0
        self._tid = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # --- колбэк (поток хука) ---------------------------------------------------------------------

    def on_key(self, vk: int, up: bool) -> None:
        """Событие клавиатуры из колбэка: PTT — down/up без автоповтора; другая клавиша при PTT — other."""
        now = time.monotonic()
        if vk == self.vk:
            if up:
                if self.down:
                    self.down = False
                    self.events.put(("up", now))
            elif not self.down:
                self.down = True
                self.events.put(("down", now))
        elif self.down and not up:
            self.events.put(("other", now))
        elif vk == VK_ESCAPE and not up:
            self.events.put(("esc", now))  # Esc в любом окне: заглушить речь Jarvis (клавишу не глушим)

    # --- жизненный цикл --------------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Поток хука; ждёт установки хука. Не вышло — PttError."""
        global _active
        if _active is not None and _active is not self:
            _active.stop()
        try:
            api()
        except Exception as e:  # не Windows или нет user32
            raise PttError(f"хук клавиатуры недоступен: {e}") from e
        self._ready.clear()
        self._error = None
        _active = self
        self._thread = threading.Thread(target=self._run, name="jarvis-ptt", daemon=True)
        self._thread.start()
        if not self._ready.wait(START_TIMEOUT_S):
            raise PttError("поток хука клавиатуры не ответил")
        if self._error is not None:
            self._thread.join(1.0)
            if _active is self:
                _active = None
            raise PttError(f"хук клавиатуры не установлен: {self._error}")

    def stop(self, timeout: float = 2.0) -> None:
        global _active
        thread = self._thread
        if thread is not None and thread.is_alive() and self._tid:
            try:
                _api.post_quit(self._tid)
            except Exception as e:
                log.warning("ptt: WM_QUIT не отправлен: %s", e)
            thread.join(timeout)
        self._thread = None
        if _active is self:
            _active = None

    def release(self) -> None:
        """voice решил, что клавиша отпущена (по GetAsyncKeyState): следующее нажатие снова даст down."""
        self.down = False

    def key_down(self) -> bool:
        """Физическое состояние клавиши PTT (GetAsyncKeyState); ошибка — считаем отпущенной."""
        try:
            return bool(_api.key_down(self.vk))
        except Exception:
            return False

    # --- поток хука ------------------------------------------------------------------------------

    def _run(self) -> None:
        global _proc
        os_api = _api  # своя ссылка: поток живёт дольше, чем кто-то подменяет модуль (тесты, выход)
        timer = 0
        try:
            self._tid = os_api.thread_id()
            if _proc is None:
                _proc = os_api.make_proc(_hook_proc)
            self._handle = os_api.install(_proc)
            timer = os_api.set_timer(TICK_MS)
        except Exception as e:
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        log.info("ptt: хук клавиатуры установлен")
        try:
            self._loop(os_api)
        except Exception:
            log.exception("ptt: цикл сообщений хука упал")
        finally:
            os_api.kill_timer(timer)
            os_api.uninstall(self._handle)
            self._handle = 0
            log.info("ptt: хук клавиатуры снят")

    def _loop(self, os_api: Any) -> None:
        last = installed = time.monotonic()
        while True:
            message = os_api.get_message()
            if message is None:
                return
            if message != WM_TIMER:
                continue
            now = time.monotonic()
            gap, last = now - last, now
            if gap > TICK_MS / 1000 + RESUME_GAP_S:
                log.info("ptt: пауза %.0f с (сон ПК?) — переустанавливаю хук", gap)
                self.events.put(("resume", now))
                self._rehook(os_api)
                installed = now
            elif now - installed >= REHOOK_S:
                self._rehook(os_api)
                installed = now

    def _rehook(self, os_api: Any) -> None:
        """Новый хук — раньше снятия старого: в промежутке нажатие не теряется (дубль отсеет on_key)."""
        try:
            handle = os_api.install(_proc)
        except Exception as e:
            log.warning("ptt: хук не переустановлен: %s", e)
            return
        old, self._handle = self._handle, handle
        os_api.uninstall(old)
        self.rehooks += 1
