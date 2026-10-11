"""«Электроника» в голосе (jarvis.voicefx): 0 — без изменений, длина та же, без перегруза, детерминированно,
быстро (фраза 3 с — десятки миллисекунд)."""

import math
import time

from jarvis.voicefx import PEAK, electronic

RATE = 22050


def tone(seconds: float = 1.0, freq: float = 220.0, amp: float = 0.8) -> list[float]:
    return [amp * math.sin(2 * math.pi * freq * i / RATE) for i in range(int(RATE * seconds))]


def test_zero_amount_is_unchanged_copy() -> None:
    x = tone(0.1)
    y = electronic(x, RATE, 0.0)
    assert y == x and y is not x


def test_effect_changes_sound_but_keeps_length_and_peak() -> None:
    x = tone(0.5)
    for amount in (0.35, 0.65, 1.0):
        y = electronic(x, RATE, amount)
        assert len(y) == len(x)
        assert max(abs(v) for v in y) <= PEAK + 1e-9
        assert sum(abs(a - b) for a, b in zip(x, y, strict=True)) > 1.0


def test_stronger_amount_differs_more() -> None:
    x = tone(0.5)
    diff = [sum(abs(a - b) for a, b in zip(x, electronic(x, RATE, k), strict=True)) for k in (0.2, 1.0)]
    assert diff[1] > diff[0]


def test_deterministic_and_bounds() -> None:
    x = tone(0.2)
    assert electronic(x, RATE, 0.5) == electronic(x, RATE, 0.5)
    assert electronic([], RATE, 1.0) == []
    assert electronic(x, RATE, 5.0) == electronic(x, RATE, 1.0)  # больше 1 — как 1
    assert electronic(x, RATE, -1.0) == x


def test_fast_enough_for_a_phrase() -> None:
    x = tone(3.0)
    t = time.perf_counter()
    electronic(x, RATE, 0.65)
    assert time.perf_counter() - t < 0.5  # у владельца — десятки мс; здесь запас на медленную машину
