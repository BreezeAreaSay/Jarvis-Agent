"""Руки на живом сервере (llama-server 127.0.0.1:8081): точность выбора инструмента, задержка, кэш префикса.

Запускает только владелец на своём ПК, сервер рук уже запущен:
    uv run pytest -q -s -m live tests/test_hands_live.py
Итог печатается и пишется в bench/results/hands-live-<дата>.txt (каталог в .gitignore).
Фразы здесь не повторяют примеры SYSTEM и scripts/hands_probe.ps1 — иначе замер завышен.
"""

import math
import statistics
from datetime import date
from pathlib import Path

import pytest

from jarvis.config import HandsConfig
from jarvis.context import Context
from jarvis.hands import Hands, HandsDecision
from pc.windows import WindowInfo

pytestmark = pytest.mark.live

RESULTS = Path(__file__).resolve().parents[1] / "bench" / "results"
CALC = WindowInfo(1, "Калькулятор", 2, "CalculatorApp.exe")
PLAYER = WindowInfo(3, "Плейлист — Музыка", 4, "Music.UI.exe")

# (фраза, ожидаемый инструмент, окно на момент хоткея)
CASES: list[tuple[str, str, WindowInfo | None]] = [
    ("открой калькулятор", "open", None),
    ("запусти вскод", "open", None),
    ("открой рабочий стол", "open", None),
    ("покажи папку документы", "open", None),
    ("переключись на телеграм", "focus", None),
    ("закрой хром", "close", None),
    ("закрой его", "close", CALC),
    ("сверни это окно", "win", CALC),
    ("разверни на весь экран", "win", CALC),
    ("сверни всё", "win", None),
    ("громкость 70", "vol", None),
    ("сделай звук погромче", "vol", PLAYER),
    ("отключи звук совсем", "vol", None),
    ("включи следующую песню", "media", PLAYER),
    ("верни прошлый трек", "media", PLAYER),
    ("продолжи воспроизведение", "media", PLAYER),
    ("найди фото с моря", "find", None),
    ("найди папку с проектами", "find", None),
    ("убей процесс дискорда", "kill", None),
    ("заверши процесс телеграма", "kill", None),
    ("как работает блокчейн", "ask_gpt", None),
    ("напиши стих про осень", "ask_gpt", None),
    ("переведи на французский доброе утро", "ask_gpt", None),
    ("привет джарвис", "reply", None),
    ("ну давай короче это самое", "clarify", None),
]
SAME = {"open": {"open", "focus"}, "focus": {"open", "focus"}}  # open↔focus взаимозаменяемы для app/folder


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)] if s else 0.0


def _got(d: HandsDecision) -> str:
    return d.tool if d.kind == "tool" else f"error({d.reason})"


def test_hands_live() -> None:
    assert len(CASES) == 25
    hands = Hands(HandsConfig())
    try:
        hands.ensure_server()
        hands.warmup()
        rows: list[tuple[str, str, HandsDecision, bool]] = []
        for text, expected, window in CASES:
            d = hands.decide(text, Context(active_window=window))
            ok = d.kind == "tool" and d.tool in SAME.get(expected, {expected})
            rows.append((text, expected, d, ok))
    finally:
        hands.close()

    total = [float(d.timings.get("total_ms", 0.0)) for _, _, d, _ in rows]
    prompt_n = [int(d.timings["prompt_n"]) for _, _, d, _ in rows if "prompt_n" in d.timings]
    tps = [
        float(d.timings["predicted_per_second"]) for _, _, d, _ in rows if "predicted_per_second" in d.timings
    ]
    good = sum(ok for *_, ok in rows)
    accuracy = good / len(rows)
    p50, p95 = _pct(total, 0.5), _pct(total, 0.95)
    lines = [
        f"руки live {date.today().isoformat()}: модель {hands.status['model']}, "
        f"префикс {hands.status['prefix_tokens']} ток., кэш {hands.status['prefix_cache']}, "
        f"VRAM {hands.status['vram']}, прогрев {hands.status['tps']} т/с",
        f"точность {good}/{len(rows)} ({accuracy:.0%}) | p50 {p50:.0f} мс | p95 {p95:.0f} мс | "
        f"max {max(total):.0f} мс | prompt_n ср. {statistics.fmean(prompt_n) if prompt_n else 0:.1f} "
        f"(max {max(prompt_n, default=0)}) | {statistics.fmean(tps) if tps else 0:.1f} т/с",
        "",
        "фраза → ожидалось → получено (аргументы) | total_ms prompt_n",
    ]
    for text, expected, d, ok in rows:
        mark = "✓" if ok else "✗"
        lines.append(
            f"{mark} {text} → {expected} → {_got(d)} {d.args or ''} | "
            f"{d.timings.get('total_ms', 0):.0f} {d.timings.get('prompt_n', '-')}"
        )
    report = "\n".join(lines)
    print("\n" + report)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"hands-live-{date.today().isoformat()}.txt").write_text(report + "\n", encoding="utf-8")

    assert accuracy >= 0.9, f"точность {accuracy:.0%} < 90 %"
    assert p95 <= 1200, f"p95 {p95:.0f} мс > 1200 мс"
    assert prompt_n and max(prompt_n) < 100, f"prompt_n тёплых запросов ≥100: {max(prompt_n, default=0)}"
