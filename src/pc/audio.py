"""Громкость устройства вывода по умолчанию (pycaw).

Вся работа с COM — в одном выделенном daemon-потоке с очередью: поток вызывает comtypes.CoInitialize()
при старте и CoUninitialize() в конце; COM-объекты из него не выходят. Устройство (GetSpeakers) берётся заново
на каждую операцию: устройство вывода могло смениться. pycaw ≥ 20251023: громкость — `dev.EndpointVolume`
(старый `.Activate(IAudioEndpointVolume._iid_, …)` падает); `pycaw.magic` не импортируется.
"""

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from pc import policy
from pc.result import Caller, Result, fail, ok

log = logging.getLogger("jarvis")

TIMEOUT_S = 5.0
E_NOTFOUND = 0x80070490  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND): нет устройства вывода


class NoAudioDevice(Exception):
    """Нет активного устройства вывода звука."""


class _Api:
    """COM-слой. Все методы вызываются только из потока звука."""

    def thread_init(self) -> None:
        import comtypes

        comtypes.CoInitialize()

    def thread_done(self) -> None:
        import comtypes

        comtypes.CoUninitialize()

    def endpoint(self) -> Any:
        """IAudioEndpointVolume устройства вывода по умолчанию — заново на каждую операцию."""
        from _ctypes import COMError

        from pycaw.utils import AudioUtilities

        try:
            dev = AudioUtilities.GetSpeakers()
        except COMError as e:
            if (getattr(e, "hresult", 0) or 0) & 0xFFFFFFFF == E_NOTFOUND:
                raise NoAudioDevice() from e
            raise
        if dev is None:
            raise NoAudioDevice()
        return dev.EndpointVolume


_api: _Api | None = None


def api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


# --- поток COM -----------------------------------------------------------------------------------

_Job = tuple[Callable[[], Any], Future]
_lock = threading.Lock()
_queue: "queue.Queue[_Job | None]" = queue.Queue()
_thread: threading.Thread | None = None


def _loop(q: "queue.Queue[_Job | None]", started: Future) -> None:
    try:
        api().thread_init()
    except BaseException as e:
        started.set_exception(e)
        return
    started.set_result(True)
    try:
        while True:
            job = q.get()
            if job is None:
                return
            fn, fut = job
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as e:
                fut.set_exception(e)
    finally:
        try:
            api().thread_done()
        except Exception:
            log.exception("звук: CoUninitialize")


def _ensure_thread() -> "queue.Queue[_Job | None]":
    global _thread, _queue
    with _lock:
        if _thread is None or not _thread.is_alive():
            _queue = queue.Queue()
            started: Future = Future()
            _thread = threading.Thread(target=_loop, args=(_queue, started), name="jarvis-audio", daemon=True)
            _thread.start()
            started.result(TIMEOUT_S)
        return _queue


def _call[T](fn: Callable[[Any], T]) -> T:
    """Выполнить fn(endpoint) в потоке звука и дождаться результата."""
    q = _ensure_thread()
    fut: Future = Future()
    q.put((lambda: fn(api().endpoint()), fut))
    return fut.result(TIMEOUT_S)


def _stop_thread() -> None:
    """Остановить поток звука (тесты); следующий вызов запустит новый."""
    global _thread
    with _lock:
        t, _thread = _thread, None
        if t is not None and t.is_alive():
            _queue.put(None)
            t.join(TIMEOUT_S)


def warmup() -> None:
    """Запустить поток звука и импортировать pycaw заранее (в фоне при старте Jarvis), не дожидаясь."""

    def run() -> None:
        try:
            _ensure_thread().put((lambda: __import__("pycaw.utils"), Future()))
        except Exception:
            log.warning("звук: прогрев не удался", exc_info=True)

    threading.Thread(target=run, name="jarvis-audio-warmup", daemon=True).start()


# --- действия ------------------------------------------------------------------------------------


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def _state(ev: Any) -> dict[str, Any]:
    level = _clamp(round(float(ev.GetMasterVolumeLevelScalar()) * 100))
    return {"level": level, "muted": bool(ev.GetMute())}


def _as_int(value: Any) -> int | None:
    """Число из аргумента инструмента: int, float или строка из цифр; bool — не число."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) < 1e6:
        return round(value)
    if isinstance(value, str):
        s = value.strip().removesuffix("%").strip()
        if s.lstrip("+-").isdigit() and len(s) <= 7:
            return int(s)
    return None


def _do(fn: Callable[[Any], dict[str, Any]]) -> dict[str, Any] | Result:
    try:
        return _call(fn)
    except NoAudioDevice:
        return fail("Нет устройства вывода звука")
    except FutureTimeout:
        return fail("Звук не отвечает — попробуй ещё раз")
    except Exception as e:
        log.warning("звук: ошибка COM: %r", e)
        return fail("Не удалось изменить звук: ошибка Windows Audio")


def volume_get(caller: Caller) -> Result:
    """Текущая громкость: data {"level": 0–100, "muted": bool}."""
    denied = policy.require("volume_get", caller, "Узнать громкость?")
    if denied:
        return denied
    state = _do(_state)
    if isinstance(state, Result):
        return state
    text = f"Громкость {state['level']} %" + (", звук выключен" if state["muted"] else "")
    return ok(text, state)


def volume(
    set: int | None = None, delta: int | None = None, mute: bool | None = None, caller: Caller = "user"
) -> Result:
    """Ровно одно из: set — уровень 0–100; delta — изменение от текущего; mute — выключить/включить звук.

    set/delta > 0 снимают mute. Уровень ограничивается 0..100.
    """
    given = [name for name, v in (("set", set), ("delta", delta), ("mute", mute)) if v is not None]
    if len(given) != 1:
        return fail("Скажи одно: уровень громкости, громче/тише или выключить/включить звук")
    if mute is not None and not isinstance(mute, bool):
        return fail("mute — да или нет")
    level = _as_int(set) if set is not None else None
    step = _as_int(delta) if delta is not None else None
    if (set is not None and level is None) or (delta is not None and step is None):
        return fail("Громкость — число от 0 до 100")

    denied = policy.require("volume", caller, "Изменить громкость?")
    if denied:
        return denied

    def apply(ev: Any) -> dict[str, Any]:
        if mute is not None:
            ev.SetMute(int(mute), None)
            return _state(ev)
        if level is not None:
            new, unmute = _clamp(level), level > 0
        else:
            assert step is not None
            current = round(float(ev.GetMasterVolumeLevelScalar()) * 100)
            new, unmute = _clamp(current + step), step > 0
        ev.SetMasterVolumeLevelScalar(new / 100, None)
        if unmute:
            ev.SetMute(0, None)
        return _state(ev)

    state = _do(apply)
    if isinstance(state, Result):
        return state
    if mute is True:
        return ok("Звук выключен", state)
    if mute is False:
        return ok("Звук включён", state)
    return ok(f"Громкость {state['level']} %", state)
