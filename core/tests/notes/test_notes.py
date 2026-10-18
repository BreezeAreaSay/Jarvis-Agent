"""Память, заметки, задачи, календарь и напоминания: дубли фактов, промпт, CRUD, срабатывание, API."""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.notes.service import MEMORY_LIMIT, Notes, next_occurrence, same_fact
from jarvis.services import UserFacingError
from tests.conftest import EventLog

NOW = datetime(2026, 10, 18, 12, 0).timestamp()


@pytest.fixture
def notes(ctx: AppContext) -> Notes:
    service: Any = ctx.notes
    service.clock = lambda: NOW
    return service


@pytest.fixture
def spoken(ctx: AppContext) -> list[str]:
    said: list[str] = []

    async def say(text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        said.append(text)

    ctx.tts.say = say  # type: ignore[method-assign]
    return said


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Аня не ест мясо", "Аня ест мясо", True),
        ("Аня не ест мясо.", "аня  не ест мясо", True),
        ("Любит кофе по утрам", "По утрам любит кофе", True),
        ("Любит кофе", "Любит чай", False),
        ("Живёт в Москве", "Живёт в Казани", False),
        ("Зовут Аня", "Есть кот Барсик", False),
    ],
)
def test_same_fact(a: str, b: str, same: bool) -> None:
    assert same_fact(a, b) is same


def test_memory_dedupe_cap_and_prompt(notes: Notes, events: EventLog) -> None:
    first = notes.memory_add("аня ест мясо", "user")
    assert first["text"] == "Аня ест мясо" and first["source"] == "user"
    notes.clock = lambda: NOW + 10
    updated = notes.memory_add("Аня не ест мясо.")
    assert updated["id"] == first["id"] and updated["text"] == "Аня не ест мясо"
    assert [m["text"] for m in notes.memory_list()] == ["Аня не ест мясо"]
    notes.clock = lambda: NOW + 20
    notes.memory_add("Живёт в Казани")
    assert [m["text"] for m in notes.memory_list()] == ["Живёт в Казани", "Аня не ест мясо"]
    assert notes.memory_prompt() == "Живёт в Казани; Аня не ест мясо."
    assert [e["item"]["text"] for e in events.of("memory.added")] == [
        "Аня ест мясо",
        "Аня не ест мясо",
        "Живёт в Казани",
    ]
    with pytest.raises(UserFacingError):
        notes.memory_add("   ")
    assert notes.memory_find("мясо")[0]["text"] == "Аня не ест мясо"
    assert notes.memory_find("квантовая физика") == []

    facts = [f"Факт {uuid.uuid4().hex[:10]} {uuid.uuid4().hex[:10]}" for _ in range(MEMORY_LIMIT + 5)]
    for i, fact in enumerate(facts):
        notes.clock = lambda i=i: NOW + 100 + i
        notes.memory_add(fact)
    items = notes.memory_list()
    assert len(items) == MEMORY_LIMIT
    assert items[0]["text"] == facts[-1]  # свежие — первыми
    assert events.of("memory.deleted")
    prompt = notes.memory_prompt()
    assert len(prompt) <= 1500 and prompt.startswith(facts[-1])
    assert notes.memory_delete(items[0]["id"]) and not notes.memory_delete(items[0]["id"])


def test_notes_crud(notes: Notes, events: EventLog) -> None:
    a = notes.note_add("Покупки", "Молоко, хлеб")
    b = notes.note_add("Идеи", "Аватар в виде сферы", pinned=True, color="#3B82F6")
    assert [n["id"] for n in notes.notes_list()] == [b["id"], a["id"]]  # закреплённые выше
    assert [n["title"] for n in notes.notes_list("хлеб")] == ["Покупки"]
    notes.clock = lambda: NOW + 5
    updated = notes.note_update(a["id"], body="Молоко, хлеб, сыр", pinned=True)
    assert updated["body"] == "Молоко, хлеб, сыр" and updated["pinned"] and updated["updated"] == NOW + 5
    assert notes.note_delete(a["id"]) and not notes.note_delete(a["id"])
    ops = [(e["op"], e.get("id") or e["item"]["id"]) for e in events.of("notes.changed")]
    assert ops == [("add", a["id"]), ("add", b["id"]), ("update", a["id"]), ("delete", a["id"])]


def test_tasks_order_done_and_due(notes: Notes, events: EventLog) -> None:
    milk = notes.task_add("Купить молоко", NOW + 3600)
    call = notes.task_add("Позвонить маме")
    assert [t["title"] for t in notes.tasks_list()] == ["Купить молоко", "Позвонить маме"]
    done = notes.task_update(milk["id"], done=True)
    assert done["done"] and done["completed"] == NOW
    assert [t["title"] for t in notes.tasks_list()] == [
        "Позвонить маме",
        "Купить молоко",
    ]  # выполненные — ниже
    assert [t["title"] for t in notes.tasks_list(include_done=False)] == ["Позвонить маме"]
    undone = notes.task_update(milk["id"], done=False, due=None)
    assert not undone["done"] and undone["completed"] is None and undone["due"] is None
    notes.task_update(call["id"], position=-1)
    assert notes.tasks_list()[0]["id"] == call["id"]
    assert notes.task_find("позвонить маме")["id"] == call["id"]  # type: ignore[index]
    assert notes.task_find(call["id"])["id"] == call["id"]  # type: ignore[index]
    assert notes.task_find("полететь на Марс") is None
    with pytest.raises(UserFacingError):
        notes.task_add("  ")
    assert events.of("tasks.changed")[-1]["op"] == "update"


def test_events_range(notes: Notes) -> None:
    day = datetime(2026, 10, 20).timestamp()
    meeting = notes.event_add("Созвон", day + 15 * 3600, day + 16 * 3600, remind_min=15)
    notes.event_add("Отпуск", day - 2 * 86400, day + 86400, all_day=True)
    notes.event_add("Дедлайн", day + 3 * 86400)
    titles = [e["title"] for e in notes.events_list(day, day + 86400)]
    assert titles == ["Отпуск", "Созвон"]  # пересекающие день, по началу
    assert [e["title"] for e in notes.events_list(day + 2 * 86400, None)] == ["Дедлайн"]
    assert len(notes.events_list()) == 3
    moved = notes.event_update(meeting["id"], start=day + 17 * 3600, end=day + 18 * 3600)
    assert moved["start"] == day + 17 * 3600 and moved["remind_min"] == 15 and moved["reminded"] is False
    with pytest.raises(UserFacingError):
        notes.event_update(meeting["id"], end=day)
    with pytest.raises(UserFacingError):
        notes.event_add("Плохое", day, day - 60)


async def test_reminders_fire_with_toast_and_voice(
    ctx: AppContext, notes: Notes, events: EventLog, spoken: list[str]
) -> None:
    ctx.settings.patch({"profile": {"address": "сэр"}})
    once = notes.reminder_add("Выключить духовку", NOW + 60)
    daily = notes.reminder_add("Выпить таблетку", NOW + 30, "daily")
    assert await notes.tick() == []  # ещё рано

    notes.clock = lambda: NOW + 61
    fired = await notes.tick()
    assert [f["text"] for f in fired] == ["Выпить таблетку", "Выключить духовку"]
    assert all(f["missed"] is False for f in fired)
    assert [e["item"]["id"] for e in events.of("reminder.fired")] == [daily["id"], once["id"]]
    toast = events.of("toast")[-1]
    assert (toast["title"], toast["message"], toast["status"], toast["icon"]) == (
        "Напоминание",
        "Выключить духовку",
        "info",
        "timer",
    )
    assert spoken == ["Напоминаю, сэр: Выпить таблетку", "Напоминаю, сэр: Выключить духовку"]
    assert notes.reminder_get(once["id"])["done"] is True
    again = notes.reminder_get(daily["id"])
    assert again["done"] is False and again["at"] == pytest.approx(NOW + 30 + 86400)
    assert await notes.tick() == []  # повторно не срабатывает

    ctx.settings.patch({"voice": {"silent_mode": True}})
    notes.reminder_add("Тихое", NOW + 50)
    await notes.tick()
    assert len(spoken) == 2  # тихий режим — без голоса, но с тостом
    assert events.of("toast")[-1]["message"] == "Тихое"


async def test_missed_reminders_and_event_reminders(
    ctx: AppContext, notes: Notes, events: EventLog, spoken: list[str]
) -> None:
    old = notes.reminder_add("Полить цветы", NOW - 3 * 3600, "weekly")
    notes.event_add("Созвон с командой", NOW + 10 * 60, remind_min=15)
    notes.event_add("Давняя встреча", NOW - 3 * 86400, remind_min=5)
    fired = await notes.tick()
    by_kind = {f["kind"]: f for f in fired}
    assert by_kind["reminder"]["missed"] is True and by_kind["reminder"]["id"] == old["id"]
    assert by_kind["event"]["text"] == "Созвон с командой — через 10 минут"
    assert by_kind["event"]["missed"] is True  # должно было сработать за 15 минут, а Jarvis был выключен
    assert len(fired) == 2  # о событии трёхдневной давности молчим
    assert "пропущено" in events.of("toast")[0]["message"]
    assert spoken[1] == "Напоминаю, сэр: Созвон с командой — через 10 минут"
    assert notes.reminder_get(old["id"])["at"] > NOW  # следующий раз через неделю от прежнего времени
    assert await notes.tick() == []


def test_next_occurrence_keeps_wall_clock_time() -> None:
    at = datetime(2026, 10, 1, 9, 30).timestamp()
    nxt = datetime.fromtimestamp(next_occurrence(at, "daily", NOW))
    assert (nxt.hour, nxt.minute) == (9, 30) and nxt.date() == datetime.fromtimestamp(NOW).date() + timedelta(
        days=1
    )
    weekly = datetime.fromtimestamp(next_occurrence(at, "weekly", NOW))
    assert weekly.weekday() == datetime.fromtimestamp(at).weekday() and weekly.timestamp() > NOW


def test_reminder_validation(notes: Notes) -> None:
    with pytest.raises(UserFacingError, match="daily"):
        notes.reminder_add("Х", NOW, "monthly")
    item = notes.reminder_add("Х", NOW + 10, "")
    assert item["repeat"] is None
    moved = notes.reminder_update(item["id"], at=NOW + 99, repeat="weekly")
    assert moved["at"] == NOW + 99 and moved["repeat"] == "weekly"


async def test_scheduler_runs_in_background(running_ctx: AppContext) -> None:
    notes: Any = running_ctx.notes
    assert notes._task is not None and not notes._task.done()


# --- API ----------------------------------------------------------------------------------------------------


def test_notes_api(client: TestClient) -> None:
    fact = client.post("/api/memory", json={"text": "Любит джаз"}).json()
    assert fact["source"] == "user" and fact["text"] == "Любит джаз"
    assert client.get("/api/memory").json()["items"][0]["id"] == fact["id"]
    assert client.delete(f"/api/memory/{fact['id']}").json() == {"ok": True}
    assert client.delete(f"/api/memory/{fact['id']}").status_code == 404

    note = client.post("/api/notes", json={"title": "План", "body": "Сделать API", "pinned": True}).json()
    assert note["pinned"] is True
    patched = client.patch(f"/api/notes/{note['id']}", json={"body": "Готово"}).json()
    assert patched["body"] == "Готово" and patched["title"] == "План"
    assert client.get("/api/notes", params={"q": "готово"}).json()["items"][0]["id"] == note["id"]
    assert client.patch("/api/notes/nope", json={"body": "x"}).status_code == 404
    assert client.delete(f"/api/notes/{note['id']}").json() == {"ok": True}

    task = client.post("/api/tasks", json={"title": "Купить хлеб", "due": 1_900_000_000}).json()
    assert task["due"] == 1_900_000_000 and task["done"] is False
    done = client.patch(f"/api/tasks/{task['id']}", json={"done": True}).json()
    assert done["done"] is True and done["due"] == 1_900_000_000
    cleared = client.patch(f"/api/tasks/{task['id']}", json={"due": None}).json()
    assert cleared["due"] is None and cleared["done"] is True
    assert client.post("/api/tasks", json={"title": " "}).status_code == 400
    assert client.delete(f"/api/tasks/{task['id']}").json() == {"ok": True}

    event = client.post(
        "/api/events",
        json={"title": "Встреча", "start": 2_000_000_000, "end": 2_000_003_600, "remind_min": 10},
    ).json()
    assert event["remind_min"] == 10 and event["all_day"] is False
    items = client.get("/api/events", params={"start": 2_000_000_000 - 10, "end": 2_000_000_100}).json()[
        "items"
    ]
    assert [e["id"] for e in items] == [event["id"]]
    assert client.get("/api/events", params={"start": 2_100_000_000}).json()["items"] == []
    assert client.patch(f"/api/events/{event['id']}", json={"title": "Созвон"}).json()["title"] == "Созвон"
    assert client.delete(f"/api/events/{event['id']}").json() == {"ok": True}

    reminder = client.post(
        "/api/reminders", json={"text": "Позвонить", "at": 2_000_000_000, "repeat": "daily"}
    ).json()
    assert reminder["repeat"] == "daily"
    assert client.get("/api/reminders").json()["items"][0]["id"] == reminder["id"]
    assert client.post("/api/reminders", json={"text": "x", "at": 1, "repeat": "yearly"}).status_code == 422
    assert client.delete(f"/api/reminders/{reminder['id']}").json() == {"ok": True}
    assert client.delete(f"/api/reminders/{reminder['id']}").status_code == 404
