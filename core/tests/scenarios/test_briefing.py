"""«Сводка дня» (Jarvis.Briefing) на подставных данных: приветствие, погода, встречи, задачи, курсы, новости,
режимы short / tasks / missed, сбой сети, действие в своей команде."""

from datetime import datetime, timedelta
from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.scenarios import briefing
from jarvis.scenarios.service import Scenarios
from tests.scenarios.conftest import MONDAY

NOW = MONDAY.replace(hour=8, minute=0).timestamp()

WIDGETS: dict[str, dict[str, Any]] = {
    "weather": {
        "city": "Москва",
        "temp": 4.6,
        "feels": 1,
        "condition": "облачно",
        "daily": [{"date": "2026-10-19", "min": 2, "max": 9, "condition": "облачно"}],
    },
    "rates": {
        "items": [
            {"code": "USD", "symbol": "$", "value": 90.5},
            {"code": "EUR", "symbol": "€", "value": 98.01},
            {"code": "CNY", "symbol": "¥", "value": 12.4},
        ]
    },
    "news": {"items": [{"title": f"Новость {i}."} for i in range(1, 6)]},
}


@pytest.fixture
def widgets(ctx: AppContext) -> dict[str, Any]:
    """Данные виджетов без сети; requested — что и с какими параметрами спросили."""
    data: dict[str, Any] = {"by_kind": dict(WIDGETS), "requested": []}

    async def fake(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        data["requested"].append((kind, dict(params)))
        return data["by_kind"].get(kind, {"error": "нет данных"})

    ctx.widgets.data = fake  # type: ignore[method-assign]
    return data


@pytest.fixture
def day(ctx: AppContext) -> AppContext:
    """Календарь и задачи на понедельник."""
    notes: Any = ctx.notes
    notes.event_add("Планёрка", MONDAY.replace(hour=10).timestamp())
    notes.event_add("Звонок с клиентом", MONDAY.replace(hour=15, minute=30).timestamp())
    notes.event_add("Вчерашнее", (MONDAY - timedelta(days=1)).replace(hour=10).timestamp())
    notes.event_add("Завтрашнее", (MONDAY + timedelta(days=1)).replace(hour=10).timestamp())
    notes.task_add("Отправить отчёт", due=(MONDAY - timedelta(days=2)).replace(hour=18).timestamp())
    notes.task_add("Купить молоко", due=MONDAY.replace(hour=19).timestamp())
    notes.task_add("Когда-нибудь выучить японский")
    notes.task_add("Дело на завтра", due=(MONDAY + timedelta(days=1)).replace(hour=12).timestamp())
    done = notes.task_add("Уже сделано", due=MONDAY.replace(hour=9).timestamp())
    notes.task_update(done["id"], done=True)
    return ctx


async def test_full_morning_briefing(ctx: AppContext, day: AppContext, widgets: dict[str, Any]) -> None:
    shown, spoken = await briefing.build(ctx, "", {}, now=NOW)
    assert shown == (
        "Доброе утро, сэр! Сегодня понедельник, 19 октября. Москва: +5°, облачно, днём до +9°. "
        "В календаре: 10:00 Планёрка, 15:30 Звонок с клиентом. "
        "Задачи на сегодня: Отправить отчёт (просрочено), Купить молоко. "
        "Курсы: $ 90,50 ₽, € 98,01 ₽. Новости: Новость 1; Новость 2; Новость 3."
    )
    assert spoken == (
        "Доброе утро, сэр! Сегодня понедельник, девятнадцатое октября. "
        "За окном плюс 5 градусов, облачно, днём до плюс 9 градусов. "
        "В календаре 2 встречи: в 10 часов — Планёрка, в 15 часов 30 минут — Звонок с клиентом. "
        "Задачи: Отправить отчёт — просрочено, Купить молоко. "
        "Курс: доллар — 90 рублей 50 копеек, евро — 98 рублей 1 копейка. "
        "Главные новости. Новость 1. Новость 2. Новость 3."
    )
    assert ("weather", {"city": "Москва"}) in widgets["requested"]


async def test_short_briefing_and_greetings(ctx: AppContext, widgets: dict[str, Any]) -> None:
    ctx.settings.patch({"profile": {"address": "", "city": "Казань"}})
    evening = MONDAY.replace(hour=19, minute=30).timestamp()
    shown, _ = await briefing.build(ctx, "short", {}, now=evening)
    assert shown.startswith("Добрый вечер! Сегодня понедельник, 19 октября. Москва: +5°, облачно.")
    assert "днём до" not in shown  # вечером дневной максимум уже не нужен
    assert "Встреч на сегодня нет." in shown and "Срочных задач нет." in shown
    assert "Курсы" not in shown and "Новости" not in shown
    assert [k for k, _ in widgets["requested"]] == ["weather"]
    assert widgets["requested"][0][1] == {"city": "Казань"}
    assert briefing.greeting(6) == "Доброе утро" and briefing.greeting(13) == "Добрый день"
    assert briefing.greeting(2) == "Доброй ночи"


async def test_briefing_without_network(ctx: AppContext, day: AppContext, widgets: dict[str, Any]) -> None:
    widgets["by_kind"] = {}  # все виджеты отвечают {"error": …}
    shown, _ = await briefing.build(ctx, "", {}, now=NOW)
    assert shown == (
        "Доброе утро, сэр! Сегодня понедельник, 19 октября. "
        "В календаре: 10:00 Планёрка, 15:30 Звонок с клиентом. "
        "Задачи на сегодня: Отправить отчёт (просрочено), Купить молоко."
    )

    async def broken(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("сеть упала")

    ctx.widgets.data = broken  # type: ignore[method-assign]
    shown_again, _ = await briefing.build(ctx, "", {}, now=NOW)
    assert shown_again == shown


async def test_tasks_mode(ctx: AppContext, day: AppContext) -> None:
    shown, spoken = await briefing.build(ctx, "tasks", {}, now=MONDAY.replace(hour=18, minute=30).timestamp())
    assert shown == (
        "4 незакрытые задачи: Отправить отчёт (просрочено), Купить молоко, Дело на завтра, "
        "Когда-нибудь выучить японский."
    )
    assert spoken.startswith("У вас 4 незакрытые задачи: Отправить отчёт — просрочено, Купить молоко")
    for task in ctx.notes.tasks_list(include_done=False):  # type: ignore[attr-defined]
        ctx.notes.task_update(task["id"], done=True)  # type: ignore[attr-defined]
    assert await briefing.build(ctx, "tasks", {}, now=NOW) == (
        "Все задачи закрыты — можно отдыхать.",
        "Все задачи закрыты — можно отдыхать.",
    )


async def test_missed_mode(ctx: AppContext, svc: Scenarios) -> None:
    svc.clock = lambda: MONDAY.replace(hour=13).timestamp()
    ctx.bus.publish("reminder.fired", {"item": {"text": "Старое", "kind": "reminder"}})
    svc.clock = lambda: MONDAY.replace(hour=14, minute=5).timestamp()
    ctx.bus.publish("reminder.fired", {"item": {"text": "Позвонить маме", "kind": "reminder"}})
    svc.clock = lambda: MONDAY.replace(hour=14, minute=30).timestamp()
    ctx.bus.publish("reminder.fired", {"item": {"text": "Созвон — через 5 минут", "kind": "event"}})
    now = MONDAY.replace(hour=14, minute=40).timestamp()
    away = MONDAY.replace(hour=14).timestamp()
    shown, spoken = await briefing.build(ctx, "missed", {"_away_since": away, "number": 40}, now=now)
    assert shown == (
        "С возвращением, сэр! Вас не было 40 минут. "
        "Пока вас не было: 14:05 — Позвонить маме; 14:30 — Созвон — через 5 минут."
    )
    assert "Пока вас не было, 2 напоминания: в 14 часов 5 минут — Позвонить маме;" in spoken
    # без известного ухода — за последний час
    svc.clock = lambda: now
    shown, _ = await briefing.build(ctx, "missed", {}, now=now)
    assert "14:05 — Позвонить маме" in shown and "Старое" not in shown
    shown, _ = await briefing.build(ctx, "missed", {"_away_since": now}, now=now)
    assert shown == "С возвращением, сэр! Вы ничего не пропустили."


async def test_briefing_action_in_own_command(ctx: AppContext, widgets: dict[str, Any]) -> None:
    from jarvis.commands.model import CommandIn

    commands: Any = ctx.commands
    created = commands.create(
        CommandIn(
            name="Сводка",
            phrases=["что сегодня"],
            actions=[{"type": "Jarvis.Briefing", "value": "short"}],  # type: ignore[list-item]
        )
    )
    result = await commands.run_command(created["id"])
    assert result.ok
    hello = briefing.greeting(datetime.now().hour)
    assert result.message.startswith(hello) and "Москва: +5°" in result.message
    assert "За окном плюс 5 градусов" in result.say
