"""Повторяющиеся задачи и «Входящие» целиком: запуск сразу и по расписанию, замеры таблицей, история запусков,
лимиты на запуск, пауза после неудач, остановка, смена расписания; задачи из «Входящие.md»."""

import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.background import runner
from jarvis.background import service as service_module
from jarvis.background.model import Task
from jarvis.context import AppContext
from jarvis.services import UserFacingError
from tests.ai.fakes import ScriptedLlm, calls
from tests.background.conftest import service, until
from tests.conftest import EventLog

Bg = tuple[AppContext, ScriptedLlm, EventLog]
DAILY = {"kind": "daily", "at": "21:00", "weekdays": []}


def measure_run(script: ScriptedLlm, value: str, summary: str) -> None:
    script.add(
        calls(
            (
                "measure_add",
                {"table": "Курс доллара", "values": [{"column": "Курс ЦБ, ₽", "value": value}]},
            )
        ),
        calls(("finish", {"status": "done", "summary": summary})),
    )


async def finished_cycle(task: Task, cycle: int) -> None:
    await until(lambda: task.status == "scheduled" and len(task.history) >= cycle)


async def test_recurring_task_runs_now_then_by_schedule(bg: Bg, vault_dir: Path) -> None:
    ctx, script, events = bg
    bg_service = service(ctx)
    measure_run(script, "81,10", "Курс ЦБ 81,10 ₽")
    task = bg_service.create("Записывай курс доллара ЦБ в замеры", repeat=DAILY, source="ui")
    assert task.recurring and task.cycle == 1
    await finished_cycle(task, 1)

    assert task.history[-1]["status"] == "done" and task.history[-1]["summary"] == "Курс ЦБ 81,10 ₽"
    assert task.tables == ["Jarvis/Замеры/Курс доллара.md"] and task.result == task.tables[0]
    assert task.next_run > time.time() and task.fails == 0
    table = (vault_dir / task.tables[0]).read_text("utf-8")
    assert "| Курс ЦБ, ₽ |" in table and "81,10" in table
    done = events.of("background.done")[-1]
    assert done["status"] == "done" and done["next_run"] == task.next_run and not done["paused"]
    card = task.card()
    assert card["status"] == "scheduled" and card["repeat_text"] == "каждый день в 21:00"
    assert card["last_run"]["summary"] == "Курс ЦБ 81,10 ₽" and card["tables"] == task.tables
    await until(  # заметку задачи ядро пишет в фоне, следом за состоянием
        lambda: (
            bool(task.note)
            and (vault_dir / task.note).exists()
            and "## Запуски" in (vault_dir / task.note).read_text("utf-8")
        )
    )
    note = (vault_dir / task.note).read_text("utf-8")
    assert "**Повторяется:** каждый день в 21:00" in note and "## Замеры" in note

    # время не пришло — ничего; пришло — новый запуск со своим планом и лимитами
    await bg_service.tick(now=task.next_run - 60)
    assert task.status == "scheduled"
    measure_run(script, "81,42", "Курс ЦБ 81,42 ₽, +0,32")
    steps_before = task.steps
    await bg_service.tick(now=task.next_run + 1)
    assert task.cycle == 2 and task.cycle_steps == steps_before
    await finished_cycle(task, 2)
    assert [h["summary"] for h in task.history] == ["Курс ЦБ 81,10 ₽", "Курс ЦБ 81,42 ₽, +0,32"]
    second = script.requests[-2]["messages"][1]["content"]
    assert "Это повторяющаяся задача: каждый день в 21:00. Сейчас — запуск №2" in second
    assert "Курс ЦБ 81,10 ₽" in second and "Jarvis/Замеры/Курс доллара.md" in second
    assert "Это продолжение задачи" not in second  # прошлый запуск — не «продолжай с этого места»
    assert {"measure_add"} <= {t["function"]["name"] for t in script.requests[-2]["tools"]}


async def test_repeat_from_text_and_later_start(bg: Bg) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)
    task = bg_service.create("каждый вечер записывай курс доллара ЦБ", run_now=False)
    assert task.repeat == {"kind": "daily", "at": "20:00", "weekdays": []}
    assert task.title == "Записывай курс доллара ЦБ" and task.status == "scheduled" and task.cycle == 0
    assert task.next_run > time.time() and not script.requests
    plain = bg_service.create("Узнай, что меняется каждый день на рынке", run_now=False)
    assert not plain.recurring and plain.status == "queued"


async def test_failures_pause_repeats_and_resume(bg: Bg) -> None:
    ctx, script, events = bg
    bg_service = service(ctx)
    for _ in range(3):
        script.add(calls(("finish", {"status": "failed", "summary": "Сайт не открылся"})))
    task = bg_service.create("Проверяй цену на RTX 5070", repeat={"kind": "hours", "hours": 2})
    await finished_cycle(task, 1)
    assert task.fails == 1 and task.next_run > time.time()
    for cycle in (2, 3):
        await bg_service.tick(now=task.next_run + 1)
        await until(lambda c=cycle: len(task.history) >= c and not task.active)
    assert task.status == "paused" and task.fails == 3 and task.next_run == 0
    assert events.of("background.done")[-1]["paused"]
    await bg_service.tick(now=time.time() + 10 * 3600)
    assert task.status == "paused"  # на паузе — не запускается

    bg_service.resume(task.id)
    assert task.status == "scheduled" and task.fails == 0 and task.next_run > time.time()
    bg_service.pause(task.id)
    assert task.status == "paused"
    with pytest.raises(UserFacingError):
        bg_service.pause(bg_service.create("Обычная задача", run_now=False, repeat={}).id)


async def test_cancel_retry_and_change_schedule(bg: Bg) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)
    task = bg_service.create("Сводка по проекту", repeat=DAILY, run_now=False)
    await bg_service.cancel(task.id)
    assert task.status == "cancelled" and task.next_run == 0 and not task.history
    await bg_service.tick(now=time.time() + 3 * 24 * 3600)
    assert task.status == "cancelled"
    script.add(calls(("finish", {"status": "done", "summary": "Сводка готова"})))
    bg_service.retry(task.id)  # «Ещё раз»: запуск сейчас, дальше снова по расписанию
    assert task.cycle == 1
    await finished_cycle(task, 1)

    bg_service.set_repeat(task.id, {"kind": "daily", "at": "08:30", "weekdays": [0, 1, 2, 3, 4]})
    assert task.repeat["at"] == "08:30" and task.status == "scheduled"
    bg_service.set_repeat(task.id, {})
    assert not task.recurring and task.status == "done" and task.next_run == 0

    script.add(calls(("finish", {"status": "done", "summary": "Обзор готов"})))
    once = bg_service.create("Обзор рынка ноутбуков", repeat={})
    await until(lambda: once.status == "done")
    bg_service.set_repeat(once.id, {"kind": "daily", "at": "10:00", "weekdays": [0]})
    assert once.recurring and once.status == "scheduled" and once.card()["repeat_text"].startswith("по поне")


async def test_step_limit_is_per_run(bg: Bg, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)
    ctx.settings.patch({"background": {"max_steps": 10}})
    task = bg_service.create("Записывай курс", repeat=DAILY, run_now=False)
    task.steps = 500  # прошлые запуски сделали много действий — лимит этого запуска не тронут
    measure_run(script, "81,5", "Курс ЦБ 81,5 ₽")
    await bg_service.tick(now=task.next_run + 1)
    await finished_cycle(task, 1)
    assert task.history[-1]["status"] == "done" and task.history[-1]["steps"] == 1
    assert runner.LIMIT not in str(script.requests[-1]["messages"])


# --- «Входящие.md» -----------------------------------------------------------------------------------------


def inbox_file(vault_dir: Path) -> Path:
    return vault_dir / "Jarvis" / "Входящие.md"


async def test_inbox_lines_become_tasks(bg: Bg, vault_dir: Path) -> None:
    ctx, script, events = bg
    bg_service = service(ctx)
    bg_service.vault.ensure()
    path = inbox_file(vault_dir)
    assert path.exists() and "Задачи для Jarvis" in path.read_text("utf-8")
    script.add(
        calls(("finish", {"status": "done", "summary": "Нашёл"})),
        calls(("finish", {"status": "done", "summary": "Записал"})),
    )
    path.write_text(
        path.read_text("utf-8")
        + "- Найди 3 лучших робота-пылесоса до 30 000 ₽\n  - для квартиры с котом\n"
        + "каждый вечер записывай курс доллара\n",
        "utf-8",
    )
    settle(path)
    await bg_service.tick()
    tasks = [t for t in bg_service._tasks.values() if t.source == "vault"]
    titles = [t.title for t in tasks]
    assert titles == ["Найди 3 лучших робота-пылесоса до 30 000 ₽", "Записывай курс доллара"]
    assert tasks[0].brief.endswith("для квартиры с котом") and tasks[1].recurring
    text = path.read_text("utf-8")
    assert "- [x] Найди 3 лучших робота-пылесоса до 30 000 ₽ — [[Jarvis/Задачи/" in text
    assert "- [x] каждый вечер записывай курс доллара — [[" in text and "  - для квартиры с котом" in text
    assert any(t.get("title") == "Задача из «Входящих»" for t in events.of("toast"))
    await bg_service.tick()  # второй раз — не берёт
    assert len([t for t in bg_service._tasks.values() if t.source == "vault"]) == 2


async def test_inbox_off_and_waits_for_ai(bg: Bg, vault_dir: Path) -> None:
    ctx, _, _ = bg
    bg_service = service(ctx)
    bg_service.vault.ensure()
    path = inbox_file(vault_dir)
    path.write_text("- Сравни тарифы мобильной связи\n", "utf-8")
    settle(path)
    ctx.settings.patch({"background": {"inbox": False}})
    await bg_service.tick()
    assert not bg_service._tasks
    ctx.settings.patch({"background": {"inbox": True}, "ai": {"enabled": False}})
    await bg_service.tick()
    assert not bg_service._tasks and "- Сравни тарифы" in path.read_text("utf-8")  # строка ждёт модель
    ctx.settings.patch({"ai": {"enabled": True}})
    await bg_service.tick()
    assert [t.title for t in bg_service._tasks.values()] == ["Сравни тарифы мобильной связи"]
    assert path.read_text("utf-8").startswith("- [x] Сравни тарифы мобильной связи — [[")


async def test_question_goes_to_telegram_with_buttons(bg: Bg, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, script, _ = bg
    asked: list[tuple[str, str, list[str]]] = []

    async def ask_owner(task_id: str, question: Any, text: str) -> bool:
        asked.append((task_id, text, list(question.options)))
        return True

    telegram = ctx.remote.telegram  # type: ignore[attr-defined]
    monkeypatch.setattr(telegram, "ask_owner", ask_owner)
    ctx.settings.patch({"background": {"notify_question": ["telegram"]}})
    script.add(
        calls(("ask_user", {"question": "Какой бюджет?", "options": ["до 50 000", "до 100 000"]})),
        calls(("finish", {"status": "done", "summary": "Ок"})),
    )
    task = service(ctx).create("Подбери ноутбук")
    await until(lambda: task.status == "waiting" and task.open_question() is not None)
    assert asked == [(task.id, "Подбери ноутбук: Какой бюджет?", ["до 50 000", "до 100 000"])]


# --- по ревью ---------------------------------------------------------------------------------------------


async def test_approval_does_not_carry_over_to_next_run(bg: Bg) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)
    task = bg_service.create("Проверяй цену и купи, если дешевле 1000", repeat=DAILY, run_now=False)
    task.approved.append("click:shop.test:Купить")  # разрешили в прошлом запуске
    script.add(calls(("finish", {"status": "done", "summary": "Цена 1200 — не купил"})))
    bg_service.retry(task.id)
    assert task.approved == []  # новый запуск — снова спросит
    await finished_cycle(task, 1)


async def test_answer_while_question_is_being_sent_is_not_lost(
    bg: Bg, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)

    async def ask_owner(task_id: str, question: Any, text: str) -> bool:
        bg_service.answer(task_id, question.id, "до 50 000")  # ответили, пока вопрос уходил в Telegram
        return True

    monkeypatch.setattr(ctx.remote.telegram, "ask_owner", ask_owner)  # type: ignore[attr-defined]
    ctx.settings.patch({"background": {"notify_question": ["telegram"]}})
    script.add(
        calls(("ask_user", {"question": "Какой бюджет?", "options": ["до 50 000", "до 100 000"]})),
        calls(("finish", {"status": "done", "summary": "Подобрал"})),
    )
    task = bg_service.create("Подбери ноутбук")
    await until(lambda: task.status == "done", timeout=10)
    assert "Ответ человека: «до 50 000»" in str(script.requests[1]["messages"])


async def test_answer_right_after_parking_continues_the_task(bg: Bg, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)
    monkeypatch.setattr(runner, "LIVE_WAIT_S", 0.2)
    original = service_module.run_session

    async def run_and_answer(svc: Any, task: Task) -> Any:
        session = await original(svc, task)
        question = task.open_question()
        if session.outcome == "waiting" and question is not None:
            # заход уже ушёл на паузу, но ещё не закончился — и тут человек ответил
            svc.answer(task.id, question.id, "Яндекс Маркет")
        return session

    monkeypatch.setattr(service_module, "run_session", run_and_answer)
    script.add(
        calls(("ask_user", {"question": "В каком магазине смотреть?"})),
        calls(("finish", {"status": "done", "summary": "Нашёл на Маркете"})),
    )
    task = bg_service.create("Найди робот-пылесос")
    await until(lambda: task.status == "done", timeout=10)
    assert task.summary == "Нашёл на Маркете" and task.runs == 2
    assert any("Ответ пришёл — продолжаю" in e.text for e in task.log)
    assert "Яндекс Маркет" in str(script.requests[-1]["messages"])


def test_every_n_hours_counts_from_run_start(bg: Bg) -> None:
    ctx, _, _ = bg
    now = time.time()
    task = Task("h", "x", repeat={"kind": "hours", "hours": 2}, cycle_started=now - 3000)
    assert service(ctx)._next_after(task, now) == pytest.approx(task.cycle_started + 7200)
    task.cycle_started = now - 8000  # запуск шёл дольше интервала — следующий не в прошлом
    assert service(ctx)._next_after(task, now) == pytest.approx(task.cycle_started + 14400)


def settle(path: Path) -> None:
    """Файл правили давно — «Входящие» можно разбирать (свежие правки ждут INBOX_SETTLE_S)."""
    old = time.time() - 120
    os.utime(path, (old, old))


async def test_inbox_waits_until_typing_stops(bg: Bg, vault_dir: Path) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)
    bg_service.vault.ensure()
    path = inbox_file(vault_dir)
    path.write_text("- Найди 3 лучших робота-пыле\n", "utf-8")  # Obsidian сохранил на полуслове
    await bg_service.tick()
    assert not bg_service._tasks
    script.add(calls(("finish", {"status": "done", "summary": "Нашёл"})))
    path.write_text("- Найди 3 лучших робота-пылесоса\n", "utf-8")
    settle(path)
    await bg_service.tick()
    assert [t.title for t in bg_service._tasks.values()] == ["Найди 3 лучших робота-пылесоса"]


async def test_inbox_marking_failure_does_not_duplicate(
    bg: Bg, vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, script, _ = bg
    bg_service = service(ctx)
    bg_service.vault.ensure()
    path = inbox_file(vault_dir)
    path.write_text("- Сводка новостей\n", "utf-8")
    settle(path)
    script.add(
        calls(("finish", {"status": "done", "summary": "Сводка"})),
        calls(("finish", {"status": "done", "summary": "Сводка 2"})),
    )

    def locked(marks: Any) -> Any:
        raise PermissionError("файл занят синхронизацией")

    monkeypatch.setattr(bg_service, "_mark_inbox", locked)
    await bg_service.tick()
    assert len(bg_service._tasks) == 1 and "Сводка новостей" in ctx.db.kv_get("background:inbox_pending")
    monkeypatch.undo()
    bg_service._inbox_pending = {}  # как после перезапуска Jarvis: помнится в базе
    bg_service._started = False
    await bg_service.start()
    await bg_service.tick()
    assert len(bg_service._tasks) == 1  # второй задачи нет — строка только отмечена
    assert path.read_text("utf-8").startswith("- [x] Сводка новостей — [[")
    assert not ctx.db.kv_get("background:inbox_pending")
    # та же строка завтра — новая задача
    path.write_text(path.read_text("utf-8") + "- Сводка новостей\n", "utf-8")
    settle(path)
    await bg_service.tick()
    assert len(bg_service._tasks) == 2


async def test_inbox_in_other_encoding_is_left_alone(bg: Bg, vault_dir: Path) -> None:
    ctx, _, _ = bg
    bg_service = service(ctx)
    bg_service.vault.ensure()
    path = inbox_file(vault_dir)
    raw = "- Сравни тарифы\n".encode("cp1251")
    path.write_bytes(raw)
    settle(path)
    await bg_service.tick()
    assert not bg_service._tasks and path.read_bytes() == raw


def test_parse_endpoint_shows_repeat_before_create(client: TestClient) -> None:
    r = client.post("/api/background/parse", json={"text": "каждый вечер записывай курс доллара"}).json()
    assert r == {
        "repeat": {"kind": "daily", "at": "20:00", "weekdays": []},
        "repeat_text": "каждый день в 20:00",
        "title": "Записывай курс доллара",
    }
    story = client.post("/api/background/parse", json={"text": "Каждый день я трачу 2 часа на дорогу"}).json()
    assert story["repeat"] == {} and story["repeat_text"] == ""
    assert client.post("/api/background/parse", json={"text": ""}).json()["title"] == ""
