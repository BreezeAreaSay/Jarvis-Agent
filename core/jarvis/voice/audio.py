"""Микрофон и звуковые устройства: sounddevice (PortAudio) — лениво, ядро грузится и без звука.

- SoundDevice — тонкий слой над sounddevice (один на процесс): список устройств, открытие потоков,
  перечитывание списка устройств (новый микрофон подключили) — только когда открытых потоков нет.
- MicInput — микрофон блоками по 512 отсчётов 16 кГц float32 моно (окно Silero VAD). Устройство не умеет
  16 кГц — пишем на его родной частоте и передискретизируем. Ошибка, отключение, тишина без колбэков дольше
  stall_s — переподключение с нарастающей паузой (1, 2, 4… 15 с); error — текст для человека.
- PreRoll — последние ~0,5 с звука до начала речи: VAD срабатывает с задержкой, первые звуки не теряются.

Колбэк PortAudio только кладёт копию блока в очередь — вся работа в потоке, который зовёт MicInput.read().
"""

import contextlib
import logging
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from jarvis.voice.dsp import FRAME, RATE, Resampler, as_mono

log = logging.getLogger(__name__)

PREROLL_S = 0.5
STALL_S = 2.5
BACKOFF_MAX_S = 15.0
MME_NAME_LIMIT = 31  # MME обрезает имена устройств до 31 символа


class AudioError(RuntimeError):
    """Звук недоступен: нет PortAudio, нет устройства, устройство занято. Текст — для человека."""


class SoundDevice:
    """sounddevice за замком: PortAudio не любит, когда устройства открывают из разных потоков разом."""

    _instance: "SoundDevice | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        try:
            import sounddevice
        except Exception as e:  # OSError: нет библиотеки PortAudio; ImportError
            raise AudioError(f"звук недоступен: {e}") from e
        self.sd: Any = sounddevice
        self.lock = threading.RLock()
        self.open_streams = 0

    @classmethod
    def get(cls) -> "SoundDevice":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def devices(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(d, index=i) for i, d in enumerate(self.sd.query_devices())]

    def hostapis(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(h) for h in self.sd.query_hostapis()]

    def default_hostapi(self) -> int:
        with self.lock:
            try:
                return int(self.sd.default.hostapi)
            except Exception:
                return 0

    def default_index(self, kind: str) -> int | None:
        with self.lock:
            try:
                value = self.sd.default.device[0 if kind == "input" else 1]
            except Exception:
                return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def refresh(self) -> bool:
        """Перечитать устройства (PortAudio заново). Только без открытых потоков; True — перечитано."""
        with self.lock:
            if self.open_streams:
                return False
            try:
                self.sd._terminate()
                self.sd._initialize()
            except Exception as e:
                log.debug("звук: перечитать устройства не вышло: %s", e)
                return False
            return True

    def open_input(
        self,
        index: int | None,
        rate: int,
        channels: int,
        blocksize: int,
        callback: Callable[..., None],
        finished: Callable[[], None],
    ) -> "Stream":
        with self.lock:
            stream = self.sd.InputStream(
                samplerate=rate,
                blocksize=blocksize,
                device=index,
                channels=channels,
                dtype="float32",
                latency="low",
                callback=callback,
                finished_callback=finished,
            )
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self.open_streams += 1
        return Stream(self, stream)

    def open_output(self, index: int | None, rate: int) -> "Stream":
        with self.lock:
            stream = self.sd.OutputStream(
                samplerate=rate, device=index, channels=1, dtype="float32", latency="low"
            )
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self.open_streams += 1
        return Stream(self, stream)


class Stream:
    """Поток PortAudio; close() — один раз, счётчик открытых потоков сходится."""

    def __init__(self, owner: SoundDevice, stream: Any) -> None:
        self.owner = owner
        self.raw = stream
        self._closed = False

    @property
    def samplerate(self) -> int:
        return int(self.raw.samplerate)

    def write(self, data: np.ndarray) -> None:
        self.raw.write(np.ascontiguousarray(data, dtype=np.float32).reshape(-1, 1))

    def close(self, abort: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if abort:
                self.raw.abort()
            else:
                self.raw.stop()
        except Exception as e:
            log.debug("звук: остановка потока: %s", e)
        try:
            self.raw.close()
        except Exception as e:
            log.debug("звук: закрытие потока: %s", e)
        with self.owner.lock:
            self.owner.open_streams = max(0, self.owner.open_streams - 1)


# --- устройства -----------------------------------------------------------------------------------


def _channels_key(kind: str) -> str:
    return "max_input_channels" if kind == "input" else "max_output_channels"


def _name_matches(query: str, name: str) -> bool:
    q, n = query.casefold().strip(), name.casefold().strip()
    if not q or not n:
        return False
    # MME обрезает имя до 31 символа: «Микрофон (USB Audio Device With» — начало полного имени
    return q in n or (len(n) >= MME_NAME_LIMIT - 1 and q.startswith(n))


def find_device(api: SoundDevice, name: str, kind: str = "input") -> int | None:
    """Устройство по части имени (без учёта регистра); "" или не найдено — None (по умолчанию в системе).

    Среди совпадений — сначала API по умолчанию (на Windows MME: любая частота и моно без капризов).
    """
    if not name.strip():
        return None
    key = _channels_key(kind)
    pool = [d for d in api.devices() if d.get(key, 0) > 0]
    found = [d for d in pool if _name_matches(name, str(d.get("name", "")))]
    if not found:
        return None
    # полное имя (WASAPI) нашлось — его обрезанный двойник в MME тоже подходит
    full = [str(d.get("name", "")) for d in found]
    for d in pool:
        short = str(d.get("name", ""))
        if d not in found and len(short) >= MME_NAME_LIMIT - 1 and any(f.startswith(short) for f in full):
            found.append(d)
    default_api = api.default_hostapi()
    found.sort(key=lambda d: (d.get("hostapi") != default_api, len(str(d.get("name", "")))))
    return int(found[0]["index"])


def _preferred_hostapi(api: SoundDevice) -> int:
    """Для списка в интерфейсе: WASAPI на Windows (полные имена), иначе API по умолчанию."""
    hostapis = api.hostapis()
    if sys.platform == "win32":
        for i, h in enumerate(hostapis):
            if "wasapi" in str(h.get("name", "")).casefold() and h.get("devices"):
                return i
    return api.default_hostapi()


def list_devices(api: SoundDevice | None = None) -> dict[str, list[dict[str, Any]]]:
    """{inputs: [{id, name, default}], outputs: [...]}; id — имя (его и пишем в настройки)."""
    api = api or SoundDevice.get()
    devices = api.devices()
    hostapis = api.hostapis()
    preferred = _preferred_hostapi(api)
    out: dict[str, list[dict[str, Any]]] = {"inputs": [], "outputs": []}
    for kind, field in (("input", "inputs"), ("output", "outputs")):
        key = _channels_key(kind)
        host = hostapis[preferred] if 0 <= preferred < len(hostapis) else {}
        default_index = host.get(f"default_{kind}_device", -1)
        if not isinstance(default_index, int) or default_index < 0:
            default_index = api.default_index(kind)
        pool = [d for d in devices if d.get("hostapi") == preferred and d.get(key, 0) > 0]
        if not pool:  # у выбранного API нет устройств — берём все
            pool = [d for d in devices if d.get(key, 0) > 0]
        seen: set[str] = set()
        for d in pool:
            name = str(d.get("name", "")).strip()
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            out[field].append({"id": name, "name": name, "default": d.get("index") == default_index})
    return out


def device_rate(api: SoundDevice, index: int | None, kind: str = "input") -> int:
    try:
        if index is None:
            index = api.default_index(kind)
        if index is None:
            return 0
        return int(api.devices()[index].get("default_samplerate") or 0)
    except Exception:
        return 0


def device_name(api: SoundDevice, index: int | None, kind: str = "input") -> str:
    try:
        if index is None:
            index = api.default_index(kind)
        if index is None:
            return ""
        return str(api.devices()[index].get("name", ""))
    except Exception:
        return ""


# --- микрофон -------------------------------------------------------------------------------------


class MicInput:
    """Микрофон блоками FRAME отсчётов 16 кГц. read() — из одного рабочего потока; close() — из него же."""

    def __init__(
        self,
        device: str = "",
        *,
        api_factory: Callable[[], SoundDevice] = SoundDevice.get,
        stall_s: float = STALL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self.api_factory = api_factory
        self.stall_s = stall_s
        self.clock = clock
        self.error = ""  # текст для человека; "" — микрофон работает
        self.device_name = ""
        self.rate = RATE
        self.restarts = 0
        self.overflows = 0
        self._q: queue.SimpleQueue[np.ndarray | None] = queue.SimpleQueue()
        self._stream: Stream | None = None
        self._resampler: Resampler | None = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._retry_at = 0.0
        self._backoff = 1.0
        self._last_data = 0.0
        self._refresh = False
        self._token = 0  # поколение потока: блоки закрытого потока выбрасываются

    @property
    def active(self) -> bool:
        return self._stream is not None

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        """Следующий блок FRAME отсчётов или None (нет звука за timeout / микрофон переподключается)."""
        deadline = self.clock() + timeout
        while True:
            if len(self._pending) >= FRAME:
                frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
                return frame
            now = self.clock()
            if self._stream is None:
                if now < self._retry_at:
                    time.sleep(max(0.0, min(deadline, self._retry_at) - now))
                    if self.clock() < self._retry_at:
                        return None
                self._try_open()
                if self._stream is None:
                    return None
            left = deadline - self.clock()
            try:
                item = self._q.get(timeout=left) if left > 0 else self._q.get_nowait()
            except queue.Empty:
                if self.clock() - self._last_data > self.stall_s:
                    self._restart("микрофон перестал отвечать — переподключаюсь")
                return None
            if item is None:
                self._restart("микрофон отключился — переподключаюсь")
                return None
            token, chunk = item
            if token != self._token:
                continue
            self._last_data = self.clock()
            x = as_mono(chunk)
            if self._resampler is not None:
                x = self._resampler.process(x)
            self._pending = np.concatenate([self._pending, x]) if len(self._pending) else x

    def close(self) -> None:
        stream, self._stream = self._stream, None
        self._token += 1
        if stream is not None:
            stream.close(abort=True)
        self._pending = np.zeros(0, dtype=np.float32)

    # --- внутреннее
    def _try_open(self) -> None:
        try:
            self._open()
        except AudioError as e:
            self.error = str(e)
            self._retry_at = self.clock() + self._backoff
            log.warning("звук: %s (повтор через %.0f с)", e, self._backoff)
            self._backoff = min(BACKOFF_MAX_S, self._backoff * 2)
            self._refresh = True

    def _open(self) -> None:
        api = self.api_factory()
        if self._refresh:
            api.refresh()
            self._refresh = False
        index = find_device(api, self.device, "input")
        if self.device and index is None:
            log.warning("звук: микрофон «%s» не найден — беру микрофон по умолчанию", self.device)
        no_default = index is None and api.default_index("input") is None
        if no_default and not any(d.get("max_input_channels", 0) > 0 for d in api.devices()):
            raise AudioError("микрофон не найден — подключите микрофон")
        native = device_rate(api, index, "input") or 48000
        attempts = [(RATE, 1), (native, 1), (native, 2)]
        last: Exception | None = None
        self._token += 1
        token = self._token
        for rate, channels in dict.fromkeys(attempts):
            block = max(64, round(rate * FRAME / RATE))
            try:
                self._stream = api.open_input(
                    index,
                    rate,
                    channels,
                    block,
                    self._make_callback(token),
                    self._make_finished(token),
                )
            except Exception as e:
                last = e
                continue
            self.rate = rate
            self._resampler = Resampler(rate, RATE) if rate != RATE else None
            break
        else:
            raise AudioError(f"микрофон не открылся: {last}")
        self.device_name = device_name(api, index, "input")
        self._pending = np.zeros(0, dtype=np.float32)
        self._last_data = self.clock()
        self._backoff = 1.0
        if self.error:
            log.info("звук: микрофон снова работает (%s)", self.device_name)
        self.error = ""
        log.info("звук: микрофон «%s», %d Гц", self.device_name or "по умолчанию", self.rate)

    def _make_callback(self, token: int) -> Callable[..., None]:
        q = self._q

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                self.overflows += 1
            q.put((token, indata.copy()))

        return callback

    def _make_finished(self, token: int) -> Callable[[], None]:
        def finished() -> None:
            if token == self._token:
                self._q.put(None)

        return finished

    def _restart(self, reason: str) -> None:
        log.warning("звук: %s", reason)
        self.close()
        self.restarts += 1
        self.error = reason[:1].upper() + reason[1:]
        self._refresh = True
        self._retry_at = self.clock() + min(self._backoff, 2.0)


class PreRoll:
    """Кольцо последних блоков: звук чуть раньше срабатывания VAD."""

    def __init__(self, seconds: float = PREROLL_S, frame: int = FRAME, rate: int = RATE) -> None:
        self.frames: deque[np.ndarray] = deque(maxlen=max(1, int(np.ceil(seconds * rate / frame))))

    def push(self, frame: np.ndarray) -> None:
        self.frames.append(frame)

    def take(self) -> list[np.ndarray]:
        out = list(self.frames)
        self.frames.clear()
        return out

    def clear(self) -> None:
        self.frames.clear()


def close_quietly(obj: Any) -> None:
    with contextlib.suppress(Exception):
        obj.close()
