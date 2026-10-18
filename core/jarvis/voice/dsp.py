"""Звук в числах (numpy): приведение к float32 моно, передискретизация, громкость, PCM и WAV.

Всё здесь — чистые функции и маленькие классы без ввода-вывода: их зовут поток микрофона, поток распознавания
и поток вывода звука. Частота распознавания — 16 кГц (RATE), блок микрофона — 512 отсчётов (32 мс): такое окно
ждёт Silero VAD.
"""

import io
import math
import wave
from typing import Any

import numpy as np

RATE = 16000  # частота, на которой работают VAD и модели распознавания
FRAME = 512  # блок микрофона, отсчётов (32 мс при 16 кГц) — окно Silero VAD

LEVEL_FLOOR_DB = -60.0  # тише — уровень 0 (шум комнаты)
LEVEL_CEIL_DB = -14.0  # громче — уровень 1 (громкая речь у микрофона)


def as_mono(samples: Any) -> np.ndarray:
    """Любые отсчёты (список, array, ndarray, int16, стерео) → float32 моно в [-1, 1]."""
    x = np.asarray(samples)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2147483648.0
    elif x.dtype == np.uint8:
        x = (x.astype(np.float32) - 128.0) / 128.0
    else:
        x = x.astype(np.float32, copy=False)
    if x.ndim == 2:
        # (кадры, каналы) — как у sounddevice; узкое измерение — каналы
        x = x.mean(axis=1) if x.shape[1] <= x.shape[0] else x.mean(axis=0)
    return np.ascontiguousarray(x.reshape(-1), dtype=np.float32)


def lowpass_fir(cutoff: float, taps: int = 31) -> np.ndarray:
    """ФНЧ окном Хэмминга; cutoff — в долях частоты дискретизации (0..0,5). Сумма коэффициентов — 1."""
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float64)


class Resampler:
    """Потоковая передискретизация: блоки любой длины, непрерывно между блоками.

    При понижении частоты сначала ФНЧ (иначе шум выше 8 кГц «заворачивается» в полосу речи), потом линейная
    интерполяция. Задержка — половина фильтра (15 отсчётов), для речи незаметно.
    """

    def __init__(self, src: int, dst: int, taps: int = 31) -> None:
        self.src = int(src)
        self.dst = int(dst)
        self.step = self.src / self.dst
        self._fir = lowpass_fir(0.45 * self.dst / self.src, taps) if self.src > self.dst else None
        self._hist = np.zeros(taps - 1, dtype=np.float64)
        self._buf = np.zeros(0, dtype=np.float64)
        self._pos = 0.0  # где в _buf следующий выходной отсчёт

    def process(self, block: np.ndarray) -> np.ndarray:
        x = np.asarray(block, dtype=np.float64).reshape(-1)
        if self.src == self.dst:
            return x.astype(np.float32)
        if self._fir is not None:
            joined = np.concatenate([self._hist, x])
            self._hist = joined[len(joined) - len(self._hist) :]
            x = np.convolve(joined, self._fir, mode="valid")
        buf = np.concatenate([self._buf, x]) if len(self._buf) else x
        last = len(buf) - 1
        if last < 1 or self._pos > last - 1:
            self._buf = buf
            return np.zeros(0, dtype=np.float32)
        n = math.floor((last - 1 - self._pos) / self.step) + 1
        idx = self._pos + self.step * np.arange(n)
        out = np.interp(idx, np.arange(len(buf)), buf)
        next_pos = self._pos + self.step * n
        drop = math.floor(next_pos)
        self._buf = buf[drop:]
        self._pos = next_pos - drop
        return out.astype(np.float32)


def resample(samples: Any, src: int, dst: int = RATE) -> np.ndarray:
    """Передискретизация записи целиком (голосовое, файл): float32 моно на частоте dst."""
    x = as_mono(samples)
    if src == dst or not len(x):
        return x
    r = Resampler(src, dst)
    # хвост: запас нулей, чтобы фильтр и интерполяция выдали последние отсчёты
    pad = np.zeros(64 + int(src / dst) * 2, dtype=np.float32)
    out = np.concatenate([r.process(x), r.process(pad)])
    want = round(len(x) * dst / src)
    shift = (len(r._hist)) // 2 if r._fir is not None else 0  # задержка ФНЧ во входных отсчётах
    skip = round(shift * dst / src)
    out = out[skip : skip + want]
    if len(out) < want:
        out = np.concatenate([out, np.zeros(want - len(out), dtype=np.float32)])
    return out


def rms(samples: np.ndarray) -> float:
    if not len(samples):
        return 0.0
    x = np.asarray(samples, dtype=np.float32)
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def level(samples: np.ndarray, floor_db: float = LEVEL_FLOOR_DB, ceil_db: float = LEVEL_CEIL_DB) -> float:
    """Громкость блока 0..1: RMS → дБ полной шкалы → линейно между floor_db и ceil_db."""
    value = rms(samples)
    if value <= 1e-7:
        return 0.0
    db = 20.0 * math.log10(value)
    return float(min(1.0, max(0.0, (db - floor_db) / (ceil_db - floor_db))))


class LevelMeter:
    """Сглаженный уровень для аватара: быстрый подъём, плавный спад; отдаёт значение не чаще rate_hz."""

    def __init__(self, rate_hz: float = 15.0, attack: float = 0.6, release: float = 0.25) -> None:
        self.period = 1.0 / rate_hz
        self.attack = attack
        self.release = release
        self.value = 0.0
        self._peak = 0.0
        self._last_emit = -1e9
        self._last_value = -1.0

    def feed(self, raw: float, now: float) -> float | None:
        """Новый уровень блока; вернёт значение для публикации или None (рано / ничего не изменилось)."""
        self._peak = max(self._peak, raw)
        if now - self._last_emit < self.period * 0.9:  # блоки по 32 мс: каждый второй — ≈15 Гц
            return None
        target, self._peak = self._peak, 0.0
        k = self.attack if target > self.value else self.release
        self.value += (target - self.value) * k
        if self.value < 0.005:
            self.value = 0.0
        self._last_emit = now
        value = round(self.value, 3)
        if value == 0.0 and self._last_value == 0.0:
            return None  # тишина: ноль уже отправлен
        self._last_value = value
        return value

    def reset(self) -> None:
        self.value = 0.0
        self._peak = 0.0
        self._last_value = -1.0


# --- PCM и WAV ------------------------------------------------------------------------------------


def pcm16_to_float(data: bytes) -> np.ndarray:
    """PCM 16 бит, little-endian, моно → float32."""
    n = len(data) // 2
    return np.frombuffer(data[: n * 2], dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(samples: np.ndarray) -> bytes:
    x = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (x * 32767.0).astype("<i2").tobytes()


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """WAV (PCM 8/16/24/32 бит, любое число каналов) → (float32 моно, частота). Не WAV — ValueError."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            width = w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"не WAV: {e}") from e
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw[: len(raw) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        x = v.astype(np.float32) / 8388608.0
    elif width == 4:
        x = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"WAV: неизвестная разрядность {width * 8} бит")
    if channels > 1:
        x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), rate


def encode_wav(samples: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(float_to_pcm16(samples))
    return buf.getvalue()


def silence(seconds: float, rate: int) -> np.ndarray:
    return np.zeros(max(0, round(seconds * rate)), dtype=np.float32)
