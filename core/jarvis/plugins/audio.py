"""Звук для плагинов: захват системного звука и микрофона, нарезка на фразы, запись в WAV, ускорение речи.

- SoundcardSource — блоки float32 моно 16 кГц из WASAPI loopback (то, что звучит в колонках) или микрофона.
  Библиотека soundcard импортируется лениво, в потоке захвата (там же инициализируется COM).
- CaptureThread — поток, который читает источник и отдаёт блоки обработчику.
- EnergySegmenter — простой детектор речи по энергии: режет поток на фразы по 1–8 с («Живые субтитры»).
- Recorder — пишет смесь нескольких источников в WAV (16 бит, моно) кусками, в своём потоке («Конспект»).
- read_wav_chunks — длинная запись кусками до ~25 с, разрезанными по паузам (для распознавания).
- time_stretch — ускорение и замедление речи без изменения высоты голоса (WSOLA, «Чтение вслух»).
"""

import collections
import contextlib
import logging
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

log = logging.getLogger(__name__)

RATE = 16000
BLOCK = 1600  # 100 мс


class AudioSource(Protocol):
    """Источник звука: блоки float32 моно с частотой RATE, пока не выставлен stop."""

    name: str

    def frames(self, stop: threading.Event) -> Iterator[np.ndarray]: ...


def _com_init_thread() -> None:
    """WASAPI в чужом потоке требует COM (soundcard инициализирует его только в потоке импорта)."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0)  # type: ignore[attr-defined]


def to_mono(data: Any) -> np.ndarray:
    array = np.asarray(data, dtype=np.float32)
    if array.ndim == 2:
        array = array.mean(axis=1) if array.shape[1] > 1 else array[:, 0]
    return np.ascontiguousarray(array.reshape(-1), dtype=np.float32)


class SoundcardSource:
    """Системный звук (loopback колонок) или микрофон через soundcard (WASAPI на Windows)."""

    def __init__(
        self, kind: Literal["loopback", "mic"], device: str = "", rate: int = RATE, block: int = BLOCK
    ) -> None:
        self.kind = kind
        self.device = device.strip()
        self.rate = rate
        self.block = block
        self.name = "системный звук" if kind == "loopback" else "микрофон"

    def _open(self, sc: Any) -> Any:
        if self.kind == "loopback":
            speaker = sc.get_speaker(self.device) if self.device else sc.default_speaker()
            return sc.get_microphone(id=str(speaker.id), include_loopback=True)
        return sc.get_microphone(self.device) if self.device else sc.default_microphone()

    def frames(self, stop: threading.Event) -> Iterator[np.ndarray]:
        _com_init_thread()
        import warnings

        warnings.filterwarnings("ignore", message="data discontinuity")
        try:
            import soundcard as sc
        except Exception as e:  # нет звуковой подсистемы (Linux без PulseAudio и т. п.)
            raise RuntimeError(f"Звуковая система недоступна: {e}") from e
        try:
            device = self._open(sc)
        except Exception as e:
            what = "колонки" if self.kind == "loopback" else "микрофон"
            raise RuntimeError(f"Не найден {what}{f' «{self.device}»' if self.device else ''}") from e
        with device.recorder(samplerate=self.rate) as recorder:
            while not stop.is_set():
                yield to_mono(recorder.record(numframes=self.block))


class CaptureThread:
    """Читает источник в своём потоке (daemon); блоки — в on_block, ошибка — в on_error."""

    def __init__(
        self,
        source: AudioSource,
        on_block: Callable[[np.ndarray], None],
        on_error: Callable[[str], None] | None = None,
        name: str = "audio",
    ) -> None:
        self.source = source
        self.on_block = on_block
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name=f"plugin-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            for block in self.source.frames(self.stop_event):
                if self.stop_event.is_set():
                    break
                self.on_block(block)
        except Exception as e:
            self.error = str(e) or type(e).__name__
            log.warning("захват звука (%s) остановился: %s", self.source.name, self.error)
            if self.on_error is not None:
                self.on_error(self.error)

    def stop(self, timeout: float = 2.0) -> None:
        """Остановить и дождаться потока (блокирует — звать через ctx.run_io)."""
        self.stop_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


def level_db(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20.0 * np.log10(rms + 1e-10)


class EnergySegmenter:
    """Простой детектор речи по энергии: фразы от min_s до max_s секунд.

    Порог — не ниже threshold_db и на 10 дБ выше «шумового пола» (минимум уровня за последние 3 с).
    Фраза начинается с короткого запаса до речи (preroll) и заканчивается после паузы hang_s; слишком длинная
    режется на max_s; короткая добирается тишиной до min_s; фраза, где речи меньше min_voiced_s, — шум.
    """

    def __init__(
        self,
        rate: int = RATE,
        *,
        min_s: float = 1.0,
        max_s: float = 8.0,
        hang_s: float = 0.5,
        preroll_s: float = 0.2,
        min_voiced_s: float = 0.25,
        threshold_db: float = -45.0,
        frame_ms: int = 30,
    ) -> None:
        self.rate = rate
        self.frame = max(1, int(rate * frame_ms / 1000))
        self.min_len = int(min_s * rate)
        self.max_len = int(max_s * rate)
        self.hang_len = int(hang_s * rate)
        self.min_voiced = int(min_voiced_s * rate)
        self.threshold_db = threshold_db
        self._pending = np.zeros(0, dtype=np.float32)
        self._levels: collections.deque[float] = collections.deque(maxlen=max(1, int(3000 / frame_ms)))
        self._preroll: collections.deque[np.ndarray] = collections.deque(
            maxlen=max(1, int(preroll_s * 1000 / frame_ms))
        )
        self._parts: list[np.ndarray] = []
        self._length = 0
        self._voiced = 0
        self._silence = 0
        self.active = False

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Добавить звук; вернёт законченные фразы (может быть пусто)."""
        data = np.concatenate([self._pending, np.asarray(samples, dtype=np.float32).reshape(-1)])
        count = len(data) // self.frame
        out: list[np.ndarray] = []
        for i in range(count):
            segment = self._push(data[i * self.frame : (i + 1) * self.frame])
            if segment is not None:
                out.append(segment)
        self._pending = data[count * self.frame :].copy()
        return out

    def flush(self) -> np.ndarray | None:
        """Отдать недоговорённую фразу (в конце потока)."""
        if not self.active:
            return None
        return self._emit(keep_active=False)

    def _push(self, frame: np.ndarray) -> np.ndarray | None:
        level = level_db(frame)
        self._levels.append(level)
        noise = min(self._levels)
        speech = level > max(self.threshold_db, noise + 10.0)
        if not self.active:
            self._preroll.append(frame)
            if speech:
                self._begin(list(self._preroll), len(frame))
                self._preroll.clear()
            return None
        if self._length + len(frame) > self.max_len:
            # фраза длиннее max_s — режем здесь; если речь продолжается, этот кадр начинает следующую
            segment = self._emit(keep_active=False)
            if speech:
                self._begin([frame], len(frame))
            else:
                self._preroll.append(frame)
            return segment
        self._parts.append(frame)
        self._length += len(frame)
        if speech:
            self._voiced += len(frame)
            self._silence = 0
        else:
            self._silence += len(frame)
        if self._silence >= self.hang_len and self._length >= self.min_len:
            return self._emit(keep_active=False)
        return None

    def _begin(self, parts: list[np.ndarray], voiced: int) -> None:
        self.active = True
        self._parts = parts
        self._length = sum(len(p) for p in parts)
        self._voiced = voiced
        self._silence = 0

    def _emit(self, keep_active: bool) -> np.ndarray | None:
        segment = np.concatenate(self._parts) if self._parts else np.zeros(0, dtype=np.float32)
        voiced = self._voiced
        self._parts = []
        self._length = 0
        self._voiced = 0
        self._silence = 0
        self.active = keep_active
        if voiced < self.min_voiced:
            return None
        return segment


class Recorder:
    """Запись смеси источников в WAV (моно, 16 бит) в своём потоке.

    Каждый источник читается своим CaptureThread; писатель раз в 50 мс сводит то, что накопилось. Если
    источник отстаёт больше чем на max_lag_s, его недостающий звук считается тишиной (и опоздавший кусок
    потом отбрасывается), чтобы запись не вставала. Источник с ошибкой выбывает, остальные пишутся дальше.
    """

    def __init__(
        self,
        path: Path,
        sources: Sequence[AudioSource],
        rate: int = RATE,
        max_lag_s: float = 0.5,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.rate = rate
        self.max_lag = int(max_lag_s * rate)
        self.on_error = on_error
        self.errors: list[str] = []
        self.frames_written = 0
        self.level = 0.0
        self._lock = threading.Lock()
        self._buffers: list[collections.deque[np.ndarray]] = [collections.deque() for _ in sources]
        self._sizes = [0] * len(sources)
        self._debt = [0] * len(sources)
        self._stop = threading.Event()
        self._captures = [
            CaptureThread(
                source,
                (lambda block, i=i: self._push(i, block)),
                (lambda message, s=source: self._failed(s, message)),
                name=f"rec-{i}",
            )
            for i, source in enumerate(sources)
        ]
        self._writer = threading.Thread(target=self._write_loop, name="plugin-recorder", daemon=True)
        self._wave: wave.Wave_write | None = None

    @property
    def seconds(self) -> float:
        return self.frames_written / self.rate

    @property
    def source_count(self) -> int:
        return len(self._captures)

    @property
    def alive(self) -> bool:
        return self._writer.is_alive()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        wav = wave.open(str(self.path), "wb")  # noqa: SIM115 — закрывает поток записи
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.rate)
        self._wave = wav
        for capture in self._captures:
            capture.start()
        self._writer.start()

    def stop(self) -> float:
        """Остановить запись и закрыть файл (блокирует — звать через ctx.run_io). Вернёт длительность, с."""
        for capture in self._captures:
            capture.stop_event.set()
        for capture in self._captures:
            capture.stop(2.0)
        self._stop.set()
        if self._writer.is_alive():
            self._writer.join(5.0)
        return self.seconds

    def _failed(self, source: AudioSource, message: str) -> None:
        text = f"{source.name}: {message}"
        self.errors.append(text)
        if self.on_error is not None:
            self.on_error(text)

    def _push(self, index: int, block: np.ndarray) -> None:
        with self._lock:
            debt = self._debt[index]
            if debt:
                drop = min(debt, len(block))
                block = block[drop:]
                self._debt[index] = debt - drop
            if len(block):
                self._buffers[index].append(block)
                self._sizes[index] += len(block)

    def _take(self, index: int, count: int) -> np.ndarray:
        out = np.zeros(count, dtype=np.float32)
        filled = 0
        buffer = self._buffers[index]
        while filled < count and buffer:
            block = buffer[0]
            need = count - filled
            if len(block) <= need:
                out[filled : filled + len(block)] = block
                filled += len(block)
                buffer.popleft()
            else:
                out[filled:] = block[:need]
                buffer[0] = block[need:]
                filled = count
        self._sizes[index] -= filled
        if filled < count:
            self._debt[index] += count - filled  # этот кусок записан тишиной — опоздавший звук отбросить
        return out

    def _mix(self, final: bool) -> np.ndarray | None:
        with self._lock:
            live = [i for i, c in enumerate(self._captures) if c.alive or self._sizes[i] > 0]
            if not live:
                return None
            sizes = [self._sizes[i] for i in live]
            lead, lag = max(sizes), min(sizes)
            count = lead if final else max(lag, lead - self.max_lag)
            if count <= 0:
                return None
            mixed = np.zeros(count, dtype=np.float32)
            for i in live:
                mixed += self._take(i, count)
        return np.clip(mixed, -1.0, 1.0)

    def _write(self, samples: np.ndarray) -> None:
        if self._wave is None or not len(samples):
            return
        self._wave.writeframes((samples * 32767.0).astype("<i2").tobytes())
        self.frames_written += len(samples)
        self.level = min(1.0, float(np.sqrt(np.mean(np.square(samples)))) * 4.0)

    def _write_loop(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(0.05)
                mixed = self._mix(final=False)
                if mixed is not None:
                    self._write(mixed)
                if not any(c.alive for c in self._captures) and not any(self._sizes):
                    if self._captures and all(c.error for c in self._captures):
                        break  # все источники упали — дальше писать нечего
                    if not self._captures:
                        break
            mixed = self._mix(final=True)
            if mixed is not None:
                self._write(mixed)
        except Exception as e:
            log.exception("запись звука упала")
            self.errors.append(f"запись: {e}")
        finally:
            if self._wave is not None:
                with contextlib.suppress(Exception):
                    self._wave.close()
                self._wave = None


def _pcm_to_float(raw: bytes, channels: int, width: int) -> np.ndarray:
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"неподдерживаемая разрядность WAV: {width * 8} бит")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32, copy=False)


def _quiet_cut(samples: np.ndarray, rate: int, limit: int, search: int) -> int:
    """Где резать: самое тихое окно 100 мс в последних search сэмплах до limit."""
    start = max(0, limit - search)
    window = samples[start:limit]
    step = max(1, rate // 10)
    if len(window) < step * 2:
        return limit
    frames = len(window) // step
    energy = np.square(window[: frames * step].reshape(frames, step)).mean(axis=1)
    return start + int(np.argmin(energy)) * step + step // 2


def read_wav_chunks(
    path: Path, max_s: float = 25.0, search_s: float = 4.0
) -> Iterator[tuple[np.ndarray, int]]:
    """Читать WAV кусками не длиннее max_s, разрезая по паузам; вернёт (float32 моно, частота)."""
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        limit = max(rate, int(max_s * rate))
        search = int(search_s * rate)
        carry = np.zeros(0, dtype=np.float32)
        eof = False
        while True:
            need = limit - len(carry)
            if need > 0 and not eof:
                raw = wav.readframes(need)
                block = _pcm_to_float(raw, channels, width)
                eof = len(block) < need
                buffer = np.concatenate([carry, block])
            else:
                buffer = carry
            if len(buffer) <= limit and eof:
                if len(buffer):
                    yield buffer, rate
                return
            cut = _quiet_cut(buffer, rate, limit, search)
            yield buffer[:cut], rate
            carry = buffer[cut:]


def time_stretch(samples: Any, rate: int, speed: float) -> np.ndarray:
    """Изменить темп речи в speed раз без изменения высоты (WSOLA: окна 30 мс, поиск совпадения ±7 мс)."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if abs(speed - 1.0) < 0.01 or len(x) < rate // 10:
        return x
    speed = float(min(3.0, max(0.33, speed)))
    frame = max(16, int(rate * 0.03))
    hop_out = frame // 2
    hop_in = hop_out * speed
    tolerance = max(1, int(rate * 0.007))
    window = np.hanning(frame).astype(np.float32)
    out_len = int(len(x) / speed) + frame
    out = np.zeros(out_len + frame, dtype=np.float32)
    norm = np.zeros(out_len + frame, dtype=np.float32)
    padded = np.concatenate([x, np.zeros(frame + 2 * tolerance, dtype=np.float32)])
    previous = 0
    out[:frame] += padded[:frame] * window
    norm[:frame] += window
    k = 1
    while True:
        nominal = round(k * hop_in)
        out_pos = k * hop_out
        if nominal >= len(x) or out_pos + frame > len(out):
            break
        natural = previous + hop_out
        template = padded[natural : natural + frame]
        lo = max(0, nominal - tolerance)
        hi = min(len(padded) - frame, nominal + tolerance)
        if hi > lo and len(template) == frame:
            region = padded[lo : hi + frame]
            corr = np.correlate(region, template, mode="valid")
            chosen = lo + int(np.argmax(corr))
        else:
            chosen = min(nominal, len(padded) - frame)
        out[out_pos : out_pos + frame] += padded[chosen : chosen + frame] * window
        norm[out_pos : out_pos + frame] += window
        previous = chosen
        k += 1
    result_len = min(len(out), int(len(x) / speed))
    norm[norm < 1e-3] = 1.0
    return (out / norm)[:result_len].astype(np.float32)
