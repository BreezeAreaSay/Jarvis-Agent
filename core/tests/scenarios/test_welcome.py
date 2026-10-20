"""«Я вернулся», «доброе утро» и «озвучь задачи на сегодня»: приветствие в характере и что важно сейчас —
пропущенные и ближайшие напоминания, встречи, задачи на сегодня; без сети и ИИ, сразу."""

import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.scenarios import briefing
from jarvis.voice import character as ch
from jarvis.voice.character import with_address
from tests.scenarios.conftest import MONDAY, Clock

GREET_BACK = {with_address(v, "сэр") for v in ch.GREET_BACK}


def greeting_of(text: str) -> str:
    """Какое приветствие в начале текста (самое длинное подходящее: «С возвращением, сэр. С вами…»)."""
    return max((g for g in GREET_BACK if text.startswith(g)), key=len)


@pytest.fixture
async def home(ctx: AppContext) -> tuple[AppContext, Clock, list[str]]:
    """Понедельник, 17:00: часы сервисов подменены, голос — в список, ИИ выключен, погоды нет."""
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    await ctx.commands.refresh_apps()  # type: ignore[attr-defined]
    ctx.settings.patch({"ai": {"enabled": False}})
    clock = Clock(MONDAY.replace(hour=17))
    ctx.scenarios.clock = clock
    ctx.notes.clock = clock  # type: ignore[attr-defined]
    said: list[str] = []

    async def say(text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        said.append(text)

    async def no_data(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"error": "нет сети"}

    ctx.tts.say = say  # type: ignore[method-assign]
    ctx.widgets.data = no_data  # type: ignore[method-assign]
    return ctx, clock, said


def at(hour: int, minute: int = 0, days: int = 0) -> float:
    return (MONDAY + timedelta(days=days)).replace(hour=hour, minute=minute).timestamp()


async def test_back_home_with_reminders(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, clock, said = home
    notes: Any = ctx.notes
    platform: Any = ctx.platform
    # пока человека не было, сработало напоминание
    clock.set(MONDAY.replace(hour=16, minute=10))
    notes.reminder_add("Сделать розыгрыш в Telegram-канале", at(16, 5))
    await notes.tick()
    await asyncio.sleep(0)
    # скоро — ещё одно, встреча и задача на сегодня; таймеры и завтрашнее не в счёт
    notes.reminder_add("Сходить в банк", at(19))
    notes.reminder_add("Таймер на 5 минут", at(17, 3))
    notes.event_add("Созвон с командой", at(17, 30))
    notes.event_add("Завтрашнее", at(10, days=1))
    notes.task_add("Купить молоко", due=at(20))
    clock.set(MONDAY.replace(hour=17))
    platform.idle = 3600.0  # клавиатуру и мышь не трогали час — с тех пор и отходил
    said.clear()
    result = await ctx.dispatcher.handle("Джарвис, я вернулся", "voice")
    assert result["result"]["command_ids"] == ["builtin.jarvis.welcome"]
    text = said[0]
    greet = greeting_of(text)
    assert text[len(greet) :] == (
        " Пока вас не было, сработало напоминание: сделать розыгрыш в Telegram-канале."
        " Также напоминаю: в 17 часов 30 минут — созвон с командой; в 7 вечера — сходить в банк."
        " На сегодня 1 задача: купить молоко."
    )
    shown = result["result"]["message"]
    assert "Пока вас не было: Сделать розыгрыш в Telegram-канале." in shown
    assert "Также напоминаю: 17:30 — Созвон с командой; 19:00 — Сходить в банк." in shown


async def test_back_home_with_nothing_new(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, _, said = home
    t0 = time.perf_counter()
    for phrase in ("я дома", "я тут", "а вот и я"):
        said.clear()
        result = await ctx.dispatcher.handle(phrase, "voice")
        assert result["result"]["command_ids"] == ["builtin.jarvis.welcome"], phrase
        greet = greeting_of(said[0])
        assert said[0][len(greet) + 1 :] in {with_address(v, "сэр") for v in ch.ALL_CLEAR}
    assert time.perf_counter() - t0 < 1.0  # без сети и ИИ — сразу


async def test_good_morning_greets_by_time_of_day(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, clock, said = home

    async def weather(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"city": "Москва", "temp": 3, "condition": "ясно"} if kind == "weather" else {"error": "нет"}

    ctx.widgets.data = weather  # type: ignore[method-assign]
    clock.set(MONDAY.replace(hour=8))
    result = await ctx.dispatcher.handle("доброе утро", "voice")
    assert result["result"]["command_ids"] == ["builtin.jarvis.hello"]
    assert any(said[-1].startswith(with_address(v, "сэр")) for v in ch.GREET_TIME["morning"])
    assert "За окном плюс 3 градуса, ясно." in said[-1]
    clock.set(MONDAY.replace(hour=21))  # «доброе утро» вечером — Jarvis вежливо поправит
    await ctx.dispatcher.handle("доброе утро", "voice")
    assert any(said[-1].startswith(with_address(v, "сэр")) for v in ch.GREET_TIME["evening"])


async def test_own_good_morning_scenario_wins(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, _, _ = home
    assert ctx.commands.match("доброе утро").parts[0].command_id == "builtin.jarvis.hello"  # type: ignore[union-attr]
    made = ctx.scenarios.from_template("good_morning")  # своя «Доброе утро» (полная сводка) — важнее
    assert ctx.commands.match("доброе утро").parts[0].command_id == f"scenarios:{made['id']}"  # type: ignore[union-attr]


async def test_slow_weather_does_not_delay_the_greeting(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, _, said = home

    async def slow(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(3)
        return {"city": "Москва", "temp": 3, "condition": "ясно"}

    ctx.widgets.data = slow  # type: ignore[method-assign]
    t0 = time.perf_counter()
    await ctx.dispatcher.handle("привет", "voice")
    assert time.perf_counter() - t0 < 1.0 and "градус" not in said[-1]


async def test_tasks_for_today(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, clock, said = home
    notes: Any = ctx.notes
    clock.set(MONDAY.replace(hour=9))
    notes.task_add("Сходить в банк", due=at(19))
    notes.task_add("Оплатить интернет", due=at(18, days=-2))
    notes.task_add("Позвонить юристу", due=at(0))  # на сегодня, без времени
    notes.task_add("Когда-нибудь выучить японский")
    notes.event_add("Планёрка", at(10))
    notes.reminder_add("Забрать посылку", at(12, 30))
    notes.reminder_add("Таймер на 5 минут", at(9, 5))
    result = await ctx.dispatcher.handle("озвучь задачи на сегодня", "voice")
    assert result["result"]["command_ids"] == ["builtin.jarvis.today"]
    items = (
        "У вас 5 дел: оплатить интернет — просрочено, в 10 утра — планёрка, "
        "в 12 часов 30 минут — забрать посылку, в 7 вечера — сходить в банк, позвонить юристу."
    )
    lead = said[-1].removesuffix(" " + items)  # первая фраза — готовая, звучит сразу
    assert lead in {with_address(v, "сэр") for v in ch.TODAY_LEAD}
    assert result["result"]["message"].startswith(
        "На сегодня 5 дел: Оплатить интернет (просрочено); 10:00 Планёрка"
    )
    ctx.settings.patch({"ai": {"persona": "plain"}})  # «Обычный» — без готовой первой фразы
    await ctx.dispatcher.handle("озвучь задачи на сегодня", "voice")
    assert said[-1] == "Сэр, на сегодня у вас" + items.removeprefix("У вас")


async def test_tasks_for_today_only_tasks_and_nothing(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, clock, said = home
    notes: Any = ctx.notes
    clock.set(MONDAY.replace(hour=9))
    await ctx.dispatcher.handle("какие у меня задачи на сегодня", "voice")
    assert said[-1] in {with_address(v, "сэр") for v in ch.TODAY_EMPTY}
    notes.task_add("Когда-нибудь выучить японский")
    await ctx.dispatcher.handle("что у меня на сегодня", "voice")
    assert (
        said[-1]
        == "Сэр, на сегодня ничего не назначено. Без срока — 1 задача: когда-нибудь выучить японский."
    )
    notes.task_add("Сходить в банк", due=at(19))
    notes.task_add("Купить хлеб", due=at(20))
    await ctx.dispatcher.handle("мои задачи на сегодня", "voice")
    assert said[-1].endswith(" У вас 2 задачи: в 7 вечера — сходить в банк, в 8 вечера — купить хлеб.")
    ctx.settings.patch({"ai": {"persona": "plain"}})
    notes.task_update(notes.tasks_list()[1]["id"], done=True)
    notes.task_update(notes.tasks_list()[1]["id"], done=True)
    await ctx.dispatcher.handle("мои задачи на сегодня", "voice")
    assert said[-1].startswith("Сэр, на сегодня ничего не назначено.")


async def test_welcome_news_flag_and_plain_persona(home: tuple[AppContext, Clock, list[str]]) -> None:
    ctx, clock, _ = home
    found = await briefing.welcome(ctx, "back", since=clock() - 600)
    assert not found.news
    ctx.notes.reminder_add("Сходить в банк", clock() + 3600)  # type: ignore[attr-defined]
    ctx.settings.patch({"ai": {"persona": "plain"}})
    found = await briefing.welcome(ctx, "back", since=clock() - 600)
    assert found.news and found.spoken == "С возвращением, сэр. Напоминаю: в 6 вечера — сходить в банк."


def test_spoken_clock() -> None:
    assert briefing.spoken_clock(at(19)) == "в 7 вечера"
    assert briefing.spoken_clock(at(9)) == "в 9 утра"
    assert briefing.spoken_clock(at(12)) == "в полдень"
    assert briefing.spoken_clock(at(0)) == "в полночь"
    assert briefing.spoken_clock(at(14)) == "в 2 дня"
    assert briefing.spoken_clock(at(2)) == "в 2 ночи"
    assert briefing.spoken_clock(at(9, 30)) == "в 9 часов 30 минут"
