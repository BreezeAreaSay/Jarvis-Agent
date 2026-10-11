"""«Электроника» в голосе Jarvis: эффект поверх синтеза piper, без numpy и без Qt.

`electronic(samples, rate, amount)` — amount 0..1 ([voice] tts_effect; 0 — выключено):
- два коротких прямых эха (4,5 и 11 мс) — металлический «цифровой» тембр (гребенчатый фильтр);
- лёгкая кольцевая модуляция 50 Гц — едва слышное «электронное» дрожание;
- срез низа (ФВЧ ~150 Гц) — голос суше и «из динамика»;
- нормализация пика, чтобы эффект не давал перегруза.
Всё — операциями над списками за один-два прохода: фраза 3 с при 22 кГц — десятки миллисекунд.
"""

import math
from collections.abc import Sequence

COMB_MS = (4.5, 11.0)  # задержки прямых эх, мс
COMB_GAIN = (0.55, 0.35)  # их громкость при amount = 1
RING_HZ = 50.0  # частота кольцевой модуляции
RING_DEPTH = 0.3  # глубина при amount = 1
HIGHPASS_HZ = 150.0
PEAK = 0.95


def _shifted(x: list[float], delay: int) -> list[float]:
    """x, задержанный на delay отсчётов (начало — тишина)."""
    if delay <= 0:
        return x
    n = len(x)
    return [0.0] * min(delay, n) + x[: max(0, n - delay)]


def _highpass(x: list[float], rate: int, cutoff: float) -> list[float]:
    """ФВЧ первого порядка (RC): убирает «бубнящий» низ."""
    rc = 1.0 / (2 * math.pi * cutoff)
    dt = 1.0 / rate
    alpha = rc / (rc + dt)
    out: list[float] = []
    prev_x = prev_y = 0.0
    append = out.append
    for v in x:
        prev_y = alpha * (prev_y + v - prev_x)
        prev_x = v
        append(prev_y)
    return out


def electronic(samples: Sequence[float], rate: int, amount: float) -> list[float]:
    """Голос «из машины». amount 0 — без изменений (копия), 1 — сильный эффект."""
    x = [float(v) for v in samples]
    amount = max(0.0, min(1.0, float(amount)))
    if amount <= 0.0 or not x or rate <= 0:
        return x
    g1, g2 = (g * amount for g in COMB_GAIN)
    d1, d2 = (round(rate * ms / 1000) for ms in COMB_MS)
    y = [a + g1 * b + g2 * c for a, b, c in zip(x, _shifted(x, d1), _shifted(x, d2), strict=True)]
    period = max(1, round(rate / RING_HZ))
    depth = RING_DEPTH * amount
    wave = [1.0 - depth + depth * math.sin(2 * math.pi * i / period) for i in range(period)]
    reps = len(y) // period + 1
    y = [v * m for v, m in zip(y, wave * reps, strict=False)]
    y = _highpass(y, rate, HIGHPASS_HZ * amount + 20.0 * (1.0 - amount))
    peak = max(abs(v) for v in y)
    if peak > PEAK:
        k = PEAK / peak
        y = [v * k for v in y]
    return y
