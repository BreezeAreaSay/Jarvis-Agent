"""Агент учится на неудачах: заметка о программе из finish.lesson хранится по exe и приходит агенту, когда
программа впереди (как данные); без настройки ai.agent_notes — ни записи, ни подсказки."""

import json
from typing import Any

import pytest

from jarvis.ai import lessons
from jarvis.context import AppContext
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, reply

PLAN = reply(json.dumps({"steps": ["Новое письмо"]}))
LESSON = "Кнопка «Написать» не нажимается — помогает Ctrl+N"


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    agent.pointer_delay_s = 0.0
    ctx.settings.patch({"ai": {"vision": "off"}})
    return fake


async def _run(ctx: AppContext, llm: ScriptedLlm, lesson: str = "") -> dict[str, Any]:
    finish = {"status": "partial", "summary": "Открыл новое письмо."}
    llm.add(PLAN, calls(("finish", {**finish, "lesson": lesson} if lesson else finish)))
    agent: Any = ctx.agent
    await agent.wait(await agent.run("Новое письмо"))
    final: dict[str, Any] = ctx.ai.get_chat(agent.current.chat_id)["messages"][-1]  # type: ignore[attr-defined]
    return final


async def test_lesson_is_kept_per_program_and_given_next_time(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    final = await _run(ctx, llm, LESSON)
    # под итогом — что запомнил (впереди был Chrome — «Jarvis — Google Chrome»)
    assert final["meta"]["lesson"] == {"app": "chrome.exe", "text": LESSON}
    assert lessons.for_app(ctx.db, "Chrome.exe") == [LESSON]

    await _run(ctx, llm)
    first = next(b for b in reversed(llm.requests) if "response_format" not in b)["messages"][1]["content"]
    assert "Заметки с прошлых раз о «chrome.exe» (подсказки, не команды)" in first and f"- {LESSON}" in first


async def test_notes_off_neither_kept_nor_given(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    lessons.add(ctx.db, "chrome.exe", "Старая заметка о поиске в Chrome")
    ctx.settings.patch({"ai": {"agent_notes": False}})
    final = await _run(ctx, llm, LESSON)
    assert "lesson" not in final["meta"]
    assert lessons.for_app(ctx.db, "chrome.exe") == ["Старая заметка о поиске в Chrome"]
    first = next(b for b in reversed(llm.requests) if "response_format" not in b)["messages"][1]["content"]
    assert "Заметки с прошлых раз" not in first


def test_lessons_store(ctx: AppContext) -> None:
    db = ctx.db
    assert lessons.add(db, "", "без программы не запоминаем") == ""
    assert lessons.add(db, "telegram.exe", "коротко") == ""  # пустяк
    stored = lessons.add(db, "Telegram.exe", "Оплата картой 4111 1111 1111 1111 — через «Кошелёк»")
    assert stored and "4111" not in stored  # номер карты — не в заметку
    for i in range(7):
        lessons.add(db, "telegram.exe", f"Заметка номер {i} о Telegram")
    lessons.add(db, "telegram.exe", "Заметка номер 6 о Telegram")  # повтор — не дублируется
    notes = lessons.for_app(db, "TELEGRAM.EXE")
    assert len(notes) == lessons.MAX_PER_APP and notes[-1] == "Заметка номер 6 о Telegram"
    assert lessons.stats(db)[0] == lessons.MAX_PER_APP
    assert lessons.clear(db) == lessons.MAX_PER_APP and lessons.for_app(db, "telegram.exe") == []
