"""Вывод звука Jarvis: очередь фрагментов, блоки по ~20 мс, мгновенная остановка, уровень для аватара.

Поток «jarvis-tts-play» берёт фрагменты по очереди, приводит их к частоте устройства и пишет блоками; между
блоками проверяет поколение: stop() увеличивает его, текущий фрагмент обрывается (поток вывода — abort, без
доигрывания буфера), очередь речи выбрасывается. Метки (marker) — «фраза доиграла»: зовутся по очереди или
при сбросе, так что say(wait=True) всегда дождётся. Сигналы (earcon) идут вне очереди речи и не считаются
речью — пока звучит «слушаю», микрофон не глушится.

Устройство вывода открывается при первом звуке и закрывается после idle_close_s тишины.
"""

import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from jarvis.voice.audio import AudioError, SoundDevice, device_rate, find_device
from jarvis.voice.dsp import level, resample

log = logging.getLogger(__name__)

BLOCK_S = 0.02
IDLE_CLOSE_S = 2.0
LEVEL_HZ = 20.0


class Sink(Protocol):
    rate: int

    def write(self, block: np.ndarray) -> None: ...
    def close(self, abort: bool = False) -> None: ...


SinkFactory = Callable[[], Sink]


class SoundDeviceSink:
    """Динамики через sounddevice: устройство по части имени, частота — родная для устройства."""

    def __init__(self, device: str = "") -> None:
        api = SoundDevice.get()
        index = find_device(api, device, "output")
        native = device_rate(api, index, "output") or 48000
        last: Exception | None = None
        for rate in dict.fromkeys((native, 48000, 44100)):
            try:
                self.stream = api.open_output(index, rate)
                self.rate = rate
                break
            except Exception as e:
                last = e
        else:
            raise AudioError(f"устройство вывода звука не открылось: {last}")

    def write(self, block: np.ndarray) -> None:
        self.stream.write(block)

    def close(self, abort: bool = False) -> None:
        self.stream.close(abort=abort)


@dataclass
class Chunk:
    gen: int
    samples: np.ndarray | None = None
    rate: int = 0
    text: str = ""
    earcon: bool = False
    marker: Callable[[], None] | None = None


class Player:
    def __init__(
        self,
        sink_factory: SinkFactory,
        *,
        on_level: Callable[[float], None] | None = None,
        on_chunk: Callable[[Chunk], None] | None = None,
        on_idle: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        block_s: float = BLOCK_S,
        idle_close_s: float = IDLE_CLOSE_S,
    ) -> None:
        self.sink_factory = sink_factory
        self.on_level = on_level
        self.on_chunk = on_chunk
        self.on_idle = on_idle
        self.on_error = on_error
        self.block_s = block_s
        self.idle_close_s = idle_close_s
        self.volume = 1.0
        self.gen = 0
        self._items: deque[Chunk] = deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._playing: Chunk | None = None
        self._speech_queued = 0
        self._reset_sink = False
        self._last_level = 0.0

    # --- из любого потока
    @property
    def busy(self) -> bool:
        """Звучит или ждёт очереди речь (сигналы не в счёт)."""
        with self._cond:
            playing = self._playing is not None and not self._playing.earcon
            return playing or self._speech_queued > 0

    def put(self, chunk: Chunk, *, front: bool = False) -> None:
        with self._cond:
            if self._closed:
                closed = True
            else:
                closed = False
                if front:
                    self._items.appendleft(chunk)
                else:
                    self._items.append(chunk)
                if chunk.samples is not None and not chunk.earcon:
                    self._speech_queued += 1
                self._ensure_thread()
                self._cond.notify_all()
        if closed and chunk.marker is not None:
            self._call(chunk.marker)

    def stop(self) -> None:
        """Замолчать: текущая речь обрывается, очередь речи сброшена (метки — вызваны)."""
        markers: list[Callable[[], None]] = []
        with self._cond:
            self.gen += 1
            keep: deque[Chunk] = deque()
            for item in self._items:
                if item.earcon:
                    keep.append(item)
                elif item.marker is not None:
                    markers.append(item.marker)
            self._items = keep
            self._speech_queued = 0
            self._cond.notify_all()
        for marker in markers:
            self._call(marker)

    def reset_sink(self) -> None:
        """Сменилось устройство вывода: следующий звук — на новое."""
        with self._cond:
            self._reset_sink = True
            self._cond.notify_all()

    def close(self) -> None:
        self.stop()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    # --- поток вывода
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="jarvis-tts-play", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        sink: Sink | None = None
        try:
            while True:
                with self._cond:
                    if not self._items and not self._closed:
                        self._cond.wait(self.idle_close_s if sink is not None else None)
                    if self._closed:
                        return
                    if self._reset_sink and sink is not None:
                        self._reset_sink = False
                        sink = _close_sink(sink)
                    if not self._items:
                        sink = _close_sink(sink)  # тишина — отпускаем устройство
                        continue
                    chunk = self._items.popleft()
                    if chunk.samples is not None and not chunk.earcon:
                        self._speech_queued = max(0, self._speech_queued - 1)
                    stale = chunk.samples is not None and not chunk.earcon and chunk.gen != self.gen
                    self._playing = None if (chunk.marker or stale) else chunk
                if chunk.marker is not None:
                    self._call(chunk.marker)
                    continue
                if stale or chunk.samples is None:
                    continue
                if sink is None:
                    with self._cond:
                        self._reset_sink = False
                    try:
                        sink = self.sink_factory()
                    except Exception as e:
                        self._failed(chunk, e)
                        continue
                try:
                    interrupted = self._play(sink, chunk)
                except Exception as e:
                    log.warning("голос: вывод звука оборвался: %s", e)
                    sink = _close_sink(sink, abort=True)
                    self._failed(chunk, e)
                    continue
                if interrupted:
                    sink = _close_sink(sink, abort=True)
                with self._cond:
                    self._playing = None
                    idle = self._speech_queued == 0
                if idle and not chunk.earcon:
                    self._level(0.0, force=True)
                    if self.on_idle is not None:
                        self._call(self.on_idle)
        finally:
            _close_sink(sink, abort=True)

    def _failed(self, chunk: Chunk, error: Exception) -> None:
        with self._cond:
            self._playing = None
        log.warning("голос: звук не воспроизводится: %s", error)
        if self.on_error is not None and not chunk.earcon:
            self._call(lambda: self.on_error(f"нет вывода звука: {error}") if self.on_error else None)
        if self.on_idle is not None:
            self._call(self.on_idle)

    def _play(self, sink: Sink, chunk: Chunk) -> bool:
        """Фрагмент блоками; True — оборван stop()."""
        assert chunk.samples is not None
        gen = chunk.gen
        x = chunk.samples
        if chunk.rate and chunk.rate != sink.rate:
            x = resample(x, chunk.rate, sink.rate)
        if not chunk.earcon and self.on_chunk is not None:
            self._call(lambda: self.on_chunk(chunk) if self.on_chunk else None)
        step = max(1, int(sink.rate * self.block_s))
        for off in range(0, len(x), step):
            if not chunk.earcon and gen != self.gen:
                return True
            block = np.clip(x[off : off + step] * float(self.volume), -1.0, 1.0).astype(np.float32)
            if not chunk.earcon:
                self._level(level(block, -50.0, -10.0))
            sink.write(block)
        return not chunk.earcon and gen != self.gen

    def _level(self, value: float, force: bool = False) -> None:
        if self.on_level is None:
            return
        now = time.monotonic()
        if not force and now - self._last_level < 1.0 / LEVEL_HZ:
            return
        self._last_level = now
        self._call(lambda: self.on_level(round(value, 3)) if self.on_level else None)

    @staticmethod
    def _call(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            log.exception("голос: обработчик вывода звука упал")


def _close_sink(sink: Sink | None, abort: bool = False) -> None:
    if sink is not None:
        with contextlib.suppress(Exception):
            sink.close(abort=abort)
    return None
