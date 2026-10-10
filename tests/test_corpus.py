"""Корпус фраз bench/phrases.ru.jsonl: грамматика ловит свои фразы и только их, роутер выбирает уровень.

Ложное срабатывание грамматики хуже промаха, поэтому ни одна hands/brain-фраза не должна ловиться. Инвентарь —
общая фикстура tests/fixtures/apps.json через apps.set_inventory, а не приложения с ПК. Точность выбора уровня
печатается: `uv run pytest -s tests/test_corpus.py`.
"""

import json
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from jarvis import grammar
from jarvis.context import Context
from jarvis.router import route
from pc import apps
from pc.windows import WindowInfo

CORPUS = Path(__file__).resolve().parents[1] / "bench" / "phrases.ru.jsonl"
HANDS_TOOLS = {"open", "close", "focus", "win", "find", "vol", "media", "kill", "reply", "clarify", "ask_gpt"}
GRAMMAR_TOOLS = {"open", "close", "focus", "win", "find", "vol", "media", "open_found", "lock", "clock"}


def load() -> list[dict[str, Any]]:
    with CORPUS.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


PHRASES = load()


def make_ctx(raw: dict[str, Any] | None) -> Context:
    """ctx из корпуса: {"window": {"title", "exe"}, "last": имя, "found": [пути]}."""
    ctx = Context()
    raw = raw or {}
    if window := raw.get("window"):
        ctx.active_window = WindowInfo(hwnd=1001, title=window["title"], pid=4242, exe=window["exe"])
    if last := raw.get("last"):
        ctx.remember(last, "file" if "." in last else "app")
    if found := raw.get("found"):
        ctx.set_found(found)
    return ctx


@pytest.fixture(autouse=True)
def inventory(apps_fixture: list[dict[str, str]]) -> Any:
    """Настоящий apps.resolve на общем инвентаре из фикстуры."""
    apps.set_inventory([apps.App(i["name"], i["app_id"]) for i in apps_fixture])
    yield
    apps.set_inventory(None)


def test_corpus_shape() -> None:
    levels = Counter(p["level"] for p in PHRASES)
    assert levels == {"grammar": 60, "hands": 60, "brain": 30}
    texts = [p["text"] for p in PHRASES]
    assert (
        len(set(zip(texts, [json.dumps(p.get("ctx"), sort_keys=True) for p in PHRASES], strict=True))) == 150
    )
    for p in PHRASES:
        assert set(p) <= {"text", "level", "tool", "args", "ctx"}, p
        assert p["text"] == p["text"].strip() and p["text"], p
        if p["level"] == "grammar":
            assert p["tool"] in GRAMMAR_TOOLS and isinstance(p["args"], dict), p
        elif p["level"] == "hands":
            assert p["tool"] in HANDS_TOOLS, p
        for path in (p.get("ctx") or {}).get("found", []):
            assert path.startswith("C:\\Users\\me\\"), path


def test_grammar_phrases_are_caught() -> None:
    """Каждая grammar-фраза ловится грамматикой с ожидаемым инструментом и ключевыми args."""
    bad = []
    for p in PHRASES:
        if p["level"] != "grammar":
            continue
        hit = grammar.match(p["text"], make_ctx(p.get("ctx")))
        got = (hit.action, {k: hit.args.get(k) for k in p["args"]}) if hit else None
        if got != (p["tool"], p["args"]):
            bad.append(f"{p['text']!r}: ждали {p['tool']} {p['args']}, получили {hit}")
    assert not bad, "\n".join(bad)


def test_no_false_positives() -> None:
    """Ни одна hands/brain-фраза не ловится грамматикой (текст — после снятия префикса, как в роутере)."""
    bad = []
    for p in PHRASES:
        if p["level"] == "grammar":
            continue
        ctx = make_ctx(p.get("ctx"))
        hit = grammar.match(route(p["text"], ctx, "normal").text, ctx)
        if hit is not None:
            bad.append(f"{p['text']!r} ({p['level']}): {hit}")
    assert not bad, "ложные срабатывания грамматики:\n" + "\n".join(bad)


def test_router_levels() -> None:
    """Роутер: grammar → grammar, hands → hands (эвристики мозга не срабатывают), brain → brain."""
    per_level: Counter[str] = Counter()
    right: Counter[str] = Counter()
    bad = []
    for p in PHRASES:
        r = route(p["text"], make_ctx(p.get("ctx")), "normal")
        per_level[p["level"]] += 1
        if r.level == p["level"]:
            right[p["level"]] += 1
        else:
            bad.append(f"{p['text']!r}: ждали {p['level']}, получили {r.level} ({r.reason})")
    total = sum(right.values())
    print(f"\nточность выбора уровня: {total}/{len(PHRASES)} ({100 * total / len(PHRASES):.1f} %)")
    for level in ("grammar", "hands", "brain"):
        print(f"  {level}: {right[level]}/{per_level[level]}")
    for line in bad:
        print("  ✗", line)
    assert not bad, "\n".join(bad)


def test_local_mode_never_reaches_brain() -> None:
    for p in PHRASES:
        r = route(p["text"], make_ctx(p.get("ctx")), "local")
        assert r.level != "brain", p["text"]
        if p["level"] == "brain":
            assert (r.level, r.reason) == ("local", "local:needs_gpt"), p["text"]


def test_match_speed_on_corpus() -> None:
    """match ≤2 мс на фразу: медиана по корпусу после прогрева (при переменной окружения CI порог ×3)."""
    cases = [(p["text"], make_ctx(p.get("ctx"))) for p in PHRASES]
    for text, ctx in cases:
        grammar.match(text, ctx)
    times = []
    for text, ctx in cases:
        t0 = time.perf_counter()
        grammar.match(text, ctx)
        times.append(time.perf_counter() - t0)
    median, worst = statistics.median(times), max(times)
    print(f"\nmatch: медиана {median * 1000:.3f} мс, максимум {worst * 1000:.3f} мс на {len(times)} фразах")
    limit = 0.002 * (3 if os.environ.get("CI") else 1)
    assert median <= limit, f"медиана match {median * 1000:.3f} мс > {limit * 1000:.0f} мс"
