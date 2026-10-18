"""Обработка голоса Jarvis и короткие звуки-сигналы (earcons) — numpy, без внешних библиотек.

«Электроника» (electronic) — перенос проверенного jarvis-v1 voicefx на numpy; владелец выбрал голос ruslan
и слабый эффект 0,35 — «как у Джарвиса из „Железного человека“»:
- два коротких прямых эха 4,5 и 11 мс — металлический «цифровой» тембр (гребенчатый фильтр);
- кольцевая модуляция 50 Гц — едва слышное «электронное» дрожание;
- срез низа (ФВЧ ~150 Гц при amount = 1) — голос суше, «из динамика»;
- нормализация пика: эффект не даёт перегруза.
Разборчивость при 0,35–1,0 не падает (замер v1: ruslan → эффект → GigaAM v3, 6–7/10 дословно, как без него).

Сигналы: «слушаю» (две ноты вверх), «готово» (две ноты вниз, тихо), «ошибка» (низкие ноты вниз).
"""

import math
from functools import lru_cache

import numpy as np

COMB_MS = (4.5, 11.0)  # задержки прямых эх, мс
COMB_GAIN = (0.55, 0.35)  # их громкость при amount = 1
RING_HZ = 50.0
RING_DEPTH = 0.3  # глубина кольцевой модуляции при amount = 1
HIGHPASS_HZ = 150.0
PEAK = 0.95


def highpass(x: np.ndarray, rate: int, cutoff: float) -> np.ndarray:
    """ФВЧ с АЧХ RC-цепочки первого порядка, без фазовых искажений (через БПФ всей фразы)."""
    n = len(x)
    if n < 2 or cutoff <= 0:
        return x
    # степень двойки — быстрее; запас нулей ≥ 4096 — хвосты фильтра не «заворачиваются» с конца в начало
    size = 1 << (n + 4095).bit_length()
    spec = np.fft.rfft(x, size)
    f = np.fft.rfftfreq(size, 1.0 / rate)
    gain = f / np.sqrt(f * f + cutoff * cutoff)
    return np.fft.irfft(spec * gain, size)[:n].astype(np.float32)


def electronic(samples: np.ndarray, rate: int, amount: float) -> np.ndarray:
    """Голос «из машины». amount 0 — без изменений (копия), 1 — сильный эффект. Пик ≤ 0,95."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    amount = max(0.0, min(1.0, float(amount)))
    if amount <= 0.0 or not len(x) or rate <= 0:
        return x.copy()
    y = x.astype(np.float64)
    for ms, gain in zip(COMB_MS, COMB_GAIN, strict=True):
        d = round(rate * ms / 1000.0)
        if 0 < d < len(x):
            y[d:] += gain * amount * x[:-d]
    depth = RING_DEPTH * amount
    t = np.arange(len(y), dtype=np.float64)
    y *= 1.0 - depth + depth * np.sin(2.0 * math.pi * RING_HZ * t / rate)
    y = highpass(y, rate, HIGHPASS_HZ * amount + 20.0 * (1.0 - amount)).astype(np.float64)
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > PEAK:
        y *= PEAK / peak
    return y.astype(np.float32)


def apply_volume(samples: np.ndarray, volume: float) -> np.ndarray:
    """Громкость 0..1 (настройка tts.volume / 100) с защитой от перегруза."""
    v = max(0.0, min(1.0, float(volume)))
    y = np.asarray(samples, dtype=np.float32) * v
    return np.clip(y, -1.0, 1.0)


def fade(samples: np.ndarray, rate: int, ms: float = 4.0) -> np.ndarray:
    """Плавные края (без щелчка в начале и конце фрагмента)."""
    y = np.array(samples, dtype=np.float32, copy=True)
    n = min(len(y) // 2, int(rate * ms / 1000.0))
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        y[:n] *= ramp
        y[-n:] *= ramp[::-1]
    return y


# --- сигналы ----------------------------------------------------------------------------------------

EARCON_RATE = 24000

# (частота, длительность, пауза после) — мягкие синусы с обертоном и экспоненциальным затуханием
_EARCONS: dict[str, tuple[tuple[float, float, float], ...]] = {
    "wake": ((784.0, 0.075, 0.012), (1175.0, 0.11, 0.0)),  # соль → ре: «слушаю»
    "done": ((1175.0, 0.06, 0.01), (880.0, 0.09, 0.0)),  # ре → ля: «принял, отбой»
    "error": ((392.0, 0.09, 0.015), (294.0, 0.14, 0.0)),  # соль → ре малой октавы: «не вышло»
}
_EARCON_GAIN = {"wake": 0.22, "done": 0.16, "error": 0.2}


def _note(freq: float, seconds: float, rate: int) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float64) / rate
    wave_ = np.sin(2 * math.pi * freq * t) + 0.18 * np.sin(4 * math.pi * freq * t)
    attack = np.minimum(1.0, t / 0.006)
    decay = np.exp(-t / (seconds * 0.45))
    return (wave_ * attack * decay).astype(np.float32)


@lru_cache(maxsize=8)
def _earcon_cached(name: str, rate: int) -> bytes:
    parts: list[np.ndarray] = []
    for freq, seconds, gap in _EARCONS[name]:
        parts.append(_note(freq, seconds, rate))
        if gap > 0:
            parts.append(np.zeros(int(rate * gap), dtype=np.float32))
    y = np.concatenate(parts)
    y = y / max(1e-6, float(np.max(np.abs(y)))) * _EARCON_GAIN.get(name, 0.2)
    return fade(y, rate, 3.0).astype(np.float32).tobytes()


def earcon(name: str, rate: int = EARCON_RATE) -> np.ndarray:
    """Короткий сигнал: wake | done | error. Неизвестное имя — KeyError."""
    if name not in _EARCONS:
        raise KeyError(name)
    return np.frombuffer(_earcon_cached(name, rate), dtype=np.float32).copy()


EARCONS = tuple(_EARCONS)
