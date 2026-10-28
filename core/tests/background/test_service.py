"""Фоновые задачи целиком: очередь, работа модели с инструментами, вопросы (сразу и после паузы), отмена,
продолжение после перезапуска, итог в хранилище."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.background import runner
from jarvis.background.model import Task, TaskStore
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, calls, completion
from tests.background.conftest import service, until
from tests.conftest import EventLog

Bg = tuple[AppContext, ScriptedLlm, EventLog]
RESULTS = [{"title": "Ноутбуки в DNS", "url": "https://dns-shop.ru/laptops", "snippet": "от 40 000 ₽"}]


def _search(ctx: AppContext, results: list[dict[str, Any]] | None = None) -> list[str]:
    asked: list[str] = []

    async def search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
        asked.append(query)
        return results if results is not None else RESULTS

    ctx.ai.web.search = search  # type: ignore[method-assign]
    return asked


def _tool_texts(script: ScriptedLlm, index: int) -> list[str]:
    return [str(m.get("content")) for m in script.requests[index]["messages"] if m.get("role") == "tool"]


async def test_task_runs_to_result_note(bg: Bg, vault_dir: Path) -> None:
    ctx, script, events = bg
    asked = _search(ctx)
    script.add(
        calls(("update_plan", {"steps": [{"text": "Найти цены"}, {"text": "Записать отчёт"}]})),
        calls(("web_search", {"query": "цены ноутбуки"})),
        calls(
            (
                "vault_write",
                {
                    "path": "Исследования/Ноутбуки",
                    "content": "# Ноутбуки\nот 40 000 ₽ [DNS](https://dns-shop.ru)",
                },
            )
        ),
        calls(("finish", {"status": "done", "summary": "Отчёт готов.", "result": "Исследования/Ноутбуки"})),
    )
    task = service(ctx).create("Собери цены на ноутбуки и запиши отчёт", source="text")
    await until(lambda: task.status == "done")

    assert asked == ["цены ноутбуки"]
    assert task.summary == "Отчёт готов." and task.result == "Jarvis/Исследования/Ноутбуки.md"
    assert task.steps == 3 and task.runs == 1
    assert (vault_dir / task.result).read_text("utf-8").startswith("# Ноутбуки")
    await until(
        lambda: (vault_dir / task.note).exists() and "готово" in (vault_dir / task.note).read_text("utf-8")
    )
    note = (vault_dir / task.note).read_text("utf-8")
    assert "- [ ] Найти цены" in note and "[[Jarvis/Исследования/Ноутбуки]]" in note
    journal = list((vault_dir / "Jarvis" / "Журнал").glob("*.md"))
    assert journal and "Отчёт готов." in journal[0].read_text("utf-8")
    done = events.of("background.done")
    assert done and done[-1]["status"] == "done" and done[-1]["result"] == task.result
    assert any(t.get("task_id") == task.id for t in events.of("toast"))
    # промпт: фоновый агент, без экрана; инструменты задачи и finish; низкая температура
    first = script.requests[0]
    assert "фоновый агент Jarvis" in first["messages"][0]["content"]
    names = {t["function"]["name"] for t in first["tools"]}
    assert {"web_search", "web_read", "vault_write", "ask_user", "finish"} <= names
    assert not names & {"click_point", "ui_click", "type_text", "run_command"}
    assert first.get("temperature", runner.TEMPERATURE) == runner.TEMPERATURE  # «думающим» — не передаётся
    stored = TaskStore().load_all()
    assert [t.status for t in stored] == ["done"]


async def test_question_answered_while_waiting(bg: Bg) -> None:
    ctx, script, events = bg
    script.add(
        calls(("ask_user", {"question": "Какой бюджет?", "options": ["до 50 000", "до 100 000"]})),
        calls(("finish", {"status": "done", "summary": "Ок"})),
    )
    task = service(ctx).create("Подбери ноутбук")
    await until(lambda: task.status == "waiting" and task.open_question() is not None)
    question = task.open_question()
    assert question is not None and question.options == ["до 50 000", "до 100 000"]
    assert events.of("background.question")[-1]["question"]["text"] == "Какой бюджет?"
    service(ctx).answer(task.id, question.id, "до 50 000")
    await until(lambda: task.status == "done")
    assert "Ответ человека: «до 50 000»" in _tool_texts(script, 1)[0]
    assert task.runs == 1


async def test_unanswered_question_parks_and_resumes(bg: Bg, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, script, _ = bg
    monkeypatch.setattr(runner, "LIVE_WAIT_S", 0.05)
    script.add(calls(("ask_user", {"question": "В какой город?"})))
    task = service(ctx).create("Найди концерты на выходных")
    # ответа нет — заход заканчивается сразу (цикл API не тратит запрос на finish), задача ждёт
    await until(lambda: task.status == "waiting" and task.id not in service(ctx)._runners)
    assert len(script.requests) == 1

    script.add(calls(("finish", {"status": "done", "summary": "Нашёл 3 концерта"})))
    question = task.open_question()
    assert question is not None
    service(ctx).answer(task.id, question.id, "Казань")
    await until(lambda: task.status == "done")
    assert task.runs == 2
    resumed = script.requests[1]["messages"][1]["content"]
    assert "продолжение задачи (заход 2)" in resumed and "В какой город? — ответ: Казань" in resumed


async def test_parked_session_tells_subscription_models_to_finish(bg: Bg) -> None:
    """Бэкенды подписки зовут действия по одному: после паузы — просьба вызвать finish, потом — стоп."""
    ctx, _, _ = bg
    task = Task("p1", "x")
    session = runner.Session(service(ctx), task, parked=True)
    host = runner.Host(service(ctx))
    from jarvis.ai.llm import ToolCall

    for _ in range(runner.LIMIT_GRACE):
        assert (await host._act(session, ToolCall("c", "report", "{}")))[0] == runner.PARKED
    await host._act(session, ToolCall("c", "report", "{}"))
    assert session.count >= 10**9  # бэкенд по своему лимиту останавливает программу
    assert host.finish_summary(session, {"status": "done", "summary": "Жду"}) == "Жду"
    assert session.outcome == "waiting"


async def test_failed_action_stops_rest_of_batch(bg: Bg, vault_dir: Path) -> None:
    ctx, script, _ = bg
    script.add(
        calls(
            ("vault_read", {"path": "Нет такой.md"}),
            ("vault_write", {"path": "Исследования/Итог", "content": "по прочитанному"}),
        ),
        calls(("finish", {"status": "partial", "summary": "Заметки нет"})),
    )
    task = service(ctx).create("Перескажи мою заметку")
    await until(lambda: task.status == "partial")
    read, write = _tool_texts(script, 1)
    assert read.startswith("Ошибка") and write == runner.SKIPPED
    assert not (vault_dir / "Jarvis" / "Исследования" / "Итог.md").exists()


async def test_repeating_without_effect_gets_a_hint(bg: Bg) -> None:
    ctx, script, _ = bg
    _search(ctx)
    search = ("web_search", {"query": "одно и то же"})
    script.add(calls(search), calls(search), calls(("finish", {"status": "partial", "summary": "Не нашёл"})))
    task = service(ctx).create("Найди что-нибудь")
    await until(lambda: task.status == "partial")
    first, second = _tool_texts(script, 2)
    assert "уже 2 раза подряд" not in first and "уже 2 раза подряд" in second


async def test_finish_with_missing_result_note_is_returned(bg: Bg) -> None:
    ctx, script, _ = bg
    script.add(
        calls(("finish", {"status": "done", "summary": "Готово", "result": "Исследования/Нет"})),
        calls(("finish", {"status": "done", "summary": "Готово"})),
    )
    task = service(ctx).create("Сделай отчёт")
    await until(lambda: task.status == "done")
    assert "в хранилище нет" in _tool_texts(script, 1)[0]


async def test_cancel_running_task(bg: Bg) -> None:
    ctx, script, events = bg
    started = asyncio.Event()

    async def slow(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json=completion(calls(("finish", {"summary": "x"}))))

    script.add(slow)
    task = service(ctx).create("Долгая задача")
    await asyncio.wait_for(started.wait(), 5)
    await service(ctx).cancel(task.id)
    assert task.status == "cancelled"
    assert events.of("background.done")[-1]["status"] == "cancelled"


async def test_parallel_limit_queues_the_rest(bg: Bg) -> None:
    ctx, script, _ = bg
    ctx.settings.patch({"background": {"parallel": 1}})
    gate = asyncio.Event()

    async def held(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200, json=completion(calls(("finish", {"summary": "ok"}))))

    script.add(held, held)
    first = service(ctx).create("Первая")
    second = service(ctx).create("Вторая")
    await until(lambda: first.status == "running")
    assert second.status == "queued"
    gate.set()
    await until(lambda: first.status == "done" and second.status == "done")


async def test_running_task_resumes_after_restart(data_dir: Path, vault_dir: Path) -> None:
    from jarvis.app import build_context, start_services, stop_services
    from tests.ai.fakes import no_sleep

    store = TaskStore()
    old = Task("resume1", "Собери новости про ИИ", title="Новости про ИИ", status="running", runs=1)
    old.plan = [{"text": "Найти новости", "done": True}, {"text": "Записать", "done": False}]
    store.save(old)

    ctx = build_context("fake")
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    ctx.settings.patch({"background": {"vault": str(vault_dir)}})
    ctx.secrets.set("ai_api_key", "sk-test")
    script = ScriptedLlm(calls(("finish", {"status": "done", "summary": "Записал"})))
    ctx.ai.set_transport(script.transport)  # type: ignore[attr-defined]
    ctx.ai.llm.sleep = no_sleep  # type: ignore[attr-defined]
    await start_services(ctx)
    try:
        task = service(ctx).get("resume1")
        await until(lambda: task.status == "done")
        first = script.requests[0]["messages"][1]["content"]
        assert "заход 2" in first and "[x] Найти новости" in first
        assert any("перезапущен" in e.text for e in task.log)
    finally:
        await stop_services(ctx)


async def test_mcp_risky_tool_asks_and_remembers_permission(bg: Bg) -> None:
    ctx, script, _ = bg
    sent: list[dict[str, Any]] = []

    async def call(name: str, args: dict[str, Any]) -> str:
        sent.append(args)
        return "отправлено"

    ctx.mcp.tools = lambda: [  # type: ignore[method-assign]
        {
            "type": "function",
            "function": {
                "name": "mcp__tg__send_message",
                "description": "Send",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        }
    ]
    ctx.mcp.call = call  # type: ignore[method-assign]
    script.add(
        calls(("mcp__tg__send_message", {"text": "Привет"})),
        calls(("finish", {"status": "done", "summary": "Отправил"})),
    )
    task = service(ctx).create("Напиши в канал «Привет»")
    await until(lambda: task.open_question() is not None)
    question = task.open_question()
    assert question is not None and question.kind == "approval" and not sent
    service(ctx).answer(task.id, question.id, "Разрешить")
    await until(lambda: task.status == "done")
    assert sent == [{"text": "Привет"}]
    assert task.approved and json.loads(task.approved[0].split(":", 2)[2]) == {"text": "Привет"}


async def test_create_needs_configured_ai(ctx: AppContext) -> None:
    from jarvis.services import UserFacingError

    with pytest.raises(UserFacingError):
        service(ctx).create("Что-нибудь")
