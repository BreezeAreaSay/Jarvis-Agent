"""Роутер: префиксы, грамматика, эвристики мозга, local-режим и local_only, reason."""

import os
import statistics
import time

import pytest

from jarvis import router
from jarvis.context import Context
from jarvis.router import Route, route
from pc import apps

_KNOWN = {"телега": "Telegram", "телегу": "Telegram", "хром": "Google Chrome"}


@pytest.fixture(autouse=True)
def _fake_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Инвентарь из двух приложений: роутеру важен только факт «разрешилось / нет»."""

    def resolve(name: str) -> tuple[apps.App | None, float]:
        key = name.casefold()
        return (apps.App(_KNOWN[key], "id"), 100.0) if key in _KNOWN else (None, 10.0)

    monkeypatch.setattr(apps, "resolve", resolve)
    monkeypatch.setattr(apps, "THRESHOLD", 85.0)


def ctx() -> Context:
    return Context()


# --- префиксы ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["gpt: открой телегу", "GPT:открой телегу", "gpt : открой телегу",
                                  "гпт: открой телегу", "  Гпт :  открой телегу"])  # fmt: skip
def test_prefix_gpt(text: str) -> None:
    r = route(text, ctx(), "normal")
    assert (r.level, r.reason, r.text, r.deep, r.local_only) == (
        "brain",
        "prefix:gpt",
        "открой телегу",
        False,
        False,
    )


@pytest.mark.parametrize("text", ["думай: стоит ли обновляться", "Думай :стоит ли обновляться"])
def test_prefix_think(text: str) -> None:
    r = route(text, ctx(), "normal")
    assert (r.level, r.reason, r.text, r.deep) == ("brain", "prefix:think", "стоит ли обновляться", True)


def test_prefix_local_keeps_grammar_and_hands() -> None:
    r = route("локально: открой телегу", ctx(), "normal")
    assert (r.level, r.reason, r.text, r.local_only) == ("grammar", "grammar:open", "открой телегу", True)
    assert r.hit is not None and r.hit.args == {"target": "Telegram", "kind": "app"}
    r = route("Локально : заверши процесс дискорда", ctx(), "normal")
    assert (r.level, r.reason, r.local_only) == ("hands", "hands", True)


def test_prefix_local_blocks_brain() -> None:
    r = route("локально: почему тормозит комп", ctx(), "normal")
    assert (r.level, r.reason, r.text, r.local_only) == (
        "local",
        "local:needs_gpt",
        "почему тормозит комп",
        True,
    )
    r = route("локально: думай: почему тормозит комп", ctx(), "normal")
    assert (r.level, r.reason, r.deep) == ("local", "local:needs_gpt", True)


def test_prefix_only_at_start() -> None:
    r = route("напомни что такое gpt: модель", ctx(), "normal")
    assert r.text == "напомни что такое gpt: модель"


# --- грамматика ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason"),
    [("громкость 30", "grammar:vol"), ("пауза", "grammar:media"), ("открой телегу", "grammar:open"),
     ("который час", "grammar:clock"), ("заблокируй компьютер", "grammar:lock")],
)  # fmt: skip
def test_grammar(text: str, reason: str) -> None:
    r = route(text, ctx(), "normal")
    assert (r.level, r.reason, r.text) == ("grammar", reason, text)
    assert r.hit is not None and r.reason == f"grammar:{r.hit.action}"


# --- эвристики мозга -----------------------------------------------------------------------------------

QUESTION = [
    "почему тормозит комп",
    "зачем нужен файл подкачки",
    "как сделать скриншот",
    "как мне ускорить винду",
    "что такое dns",
    "кто такой тьюринг",
    "объясни про ssd",
    "сравни два ноутбука",
    "посоветуй сериал",
    "расскажи анекдот",
    "Чем отличается ssd от hdd",
    "в чём разница между ними",
]
GENERATE = [
    "напиши письмо начальнику",
    "переведи привет на английский",
    "сочини стих",
    "посчитай 15 процентов от 2400",
    "придумай название канала",
    "перескажи книгу",
    "составь план на день",
    "помоги написать резюме",
]


@pytest.mark.parametrize("text", QUESTION)
def test_heuristic_question_words(text: str) -> None:
    r = route(text, ctx(), "normal")
    assert (r.level, r.reason, r.deep, r.local_only) == ("brain", "heur:question", False, False)


@pytest.mark.parametrize("text", GENERATE)
def test_heuristic_generate(text: str) -> None:
    assert route(text, ctx(), "normal").reason == "heur:generate"


def test_heuristic_question_mark_needs_more_than_four_words() -> None:
    assert route("можно ли мыть видеокарту водой?", ctx(), "normal").reason == "heur:question"
    r = route("а это работает?", ctx(), "normal")  # коротко — пусть разбираются руки
    assert (r.level, r.reason) == ("hands", "hands")
    assert route("открой фотошоп?", ctx(), "normal").level == "hands"


def test_heuristic_long() -> None:
    words14 = " ".join(["слово"] * 14)
    assert route(words14, ctx(), "normal").level == "hands"
    r = route(words14 + " ещё", ctx(), "normal")
    assert (r.level, r.reason) == ("brain", "heur:long")


def test_words_are_whole() -> None:
    """«почемучка» и «написание» — не повод звать мозг."""
    assert route("открой почемучку", ctx(), "normal").level == "hands"
    assert route("найди написание", ctx(), "normal").level == "hands"


@pytest.mark.parametrize(
    "text",
    ["заверши процесс дискорда", "открой фотошоп", "привет джарвис", "как у тебя дела", "включи музыку",
     "какая погода будет завтра", "закрой его", "открой второй"],
)  # fmt: skip
def test_hands(text: str) -> None:
    r = route(text, ctx(), "normal")
    assert (r.level, r.reason, r.hit, r.text) == ("hands", "hands", None, text)


def test_empty() -> None:
    assert route("   ", ctx(), "normal") == Route("hands", "empty", "")


def test_heuristics_are_fast() -> None:
    texts = QUESTION + GENERATE + ["заверши процесс дискорда", " ".join(["слово"] * 20)]
    for t in texts:
        router._heuristic(t)
    times = []
    for t in texts:
        t0 = time.perf_counter()
        router._heuristic(t)
        times.append(time.perf_counter() - t0)
    limit = 0.001 * (3 if os.environ.get("CI") else 1)
    assert statistics.median(times) <= limit and max(times) <= limit * 5


# --- локальный режим -----------------------------------------------------------------------------------


def test_local_mode() -> None:
    r = route("почему тормозит комп", ctx(), "local")
    assert (r.level, r.reason, r.local_only) == ("local", "local:needs_gpt", True)
    r = route("gpt: почему тормозит комп", ctx(), "local")
    assert (r.level, r.reason) == ("local", "local:needs_gpt")
    r = route("думай: почему тормозит комп", ctx(), "local")
    assert (r.level, r.reason, r.deep) == ("local", "local:needs_gpt", True)
    r = route("громкость 30", ctx(), "local")
    assert (r.level, r.reason, r.local_only) == ("grammar", "grammar:vol", True)
    r = route("заверши процесс дискорда", ctx(), "local")
    assert (r.level, r.reason, r.local_only) == ("hands", "hands", True)
