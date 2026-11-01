"""Журнал работы агента: экран в начале, каждый шаг с картинкой экрана после него и что модель
написала перед ним, проверка перед итогом, итог; секреты замазаны; удалили чат — журнал тоже; выключен — не
ведётся; API для «Как он это делал»."""

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai import journal
from jarvis.context import AppContext
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, reply

PLAN = reply(json.dumps({"steps": ["Новое письмо"]}))


@pytest.fixture
def ui(ctx: AppContext) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    agent.pointer_delay_s = 0.0
    ctx.settings.patch({"ai": {"vision": "on"}})
    # ввод номера карты — с разрешения: разрешаем
    ctx.bus.on("confirm.request", lambda event: ctx.confirmations.resolve(event.data["id"], True))
    return fake


async def _run(ctx: AppContext, llm: ScriptedLlm) -> tuple[str, dict[str, Any]]:
    llm.add(
        PLAN,
        calls(("ui_click", {"id": "e1"}), text="Нажму «Написать», чтобы открыть новое письмо."),
        calls(("ui_type", {"id": "e3", "text": "Карта 4111 1111 1111 1111"})),
        calls(("finish", {"summary": "Открыл новое письмо."})),
        calls(("finish", {"summary": "Открыл новое письмо."})),  # после проверки по снимку
    )
    agent: Any = ctx.agent
    run_id = await agent.run("Новое письмо")
    await agent.wait(run_id)
    final: dict[str, Any] = ctx.ai.get_chat(agent.current.chat_id)["messages"][-1]  # type: ignore[attr-defined]
    return run_id, final


async def test_every_step_is_kept_with_the_screen_after_it(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    run_id, final = await _run(ctx, llm)
    assert final["meta"]["agent"]["journal"] is True and final["meta"]["agent"]["run_id"] == run_id
    data = journal.load(run_id)
    assert data is not None and data["status"] == "done" and data["summary"] == "Открыл новое письмо."
    assert data["task"] == "Новое письмо" and data["plan"] == ["Новое письмо"] and data["finished"]
    titles = [s["title"] for s in data["steps"]]
    assert titles == ["Экран в начале", "Нажать «Написать»", "Ввести текст в «Кому»", "Проверка перед итогом"]
    start, click, typed, check = data["steps"]
    assert start["images"] == ["000-0.jpg"] and "Задача: Новое письмо" in start["result"]
    assert click["name"] == "ui_click" and click["args"] == {"id": "e1"} and click["ok"] is True
    assert (
        click["thought"] == "Нажму «Написать», чтобы открыть новое письмо."
    )  # что модель написала перед шагом
    assert click["images"] == ["001-0.jpg"] and "[Снимок окна]" in click["result"]
    assert "4111" not in typed["args"]["text"]  # номер карты — не в журнал
    assert check["images"] == ["003-0.jpg"]
    picture = journal.image(run_id, "001-0.jpg")
    assert picture is not None and picture[:3] == b"\xff\xd8\xff"  # JPEG
    assert journal.image(run_id, "../journal.json") is None and journal.load("../x") is None

    # удалили чат — журнал его работы тоже
    await ctx.agent.start()
    ctx.bus.publish("chat.deleted", {"id": data["chat_id"]})
    await asyncio.sleep(0)
    assert journal.load(run_id) is None


async def test_journal_off(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    ctx.settings.patch({"ai": {"agent_journal": False}})
    run_id, final = await _run(ctx, llm)
    assert "journal" not in final["meta"]["agent"] and journal.load(run_id) is None


def test_journal_store(ctx: AppContext) -> None:
    for i in range(journal.MAX_RUNS + 3):
        log = journal.Journal(f"run{i:02d}", "Задача", "chat")
        log.step("Экран в начале")
        log.finish("done", "done", "Готово.")
    assert journal.stats()[0] == journal.MAX_RUNS  # старые выпадают
    assert journal.load("run00") is None and journal.load(f"run{journal.MAX_RUNS + 2:02d}") is not None
    # пароль в поле пароля и аргументы-секреты инструментов MCP — целиком
    assert journal.safe_args({"id": "e3", "text": "qwerty"}, secret=True) == {"id": "e3", "text": "••••"}
    assert journal.safe_args({"api_key": "sk-1", "keyword": "погода"}) == {
        "api_key": "••••",
        "keyword": "погода",
    }
    assert journal.clear() == journal.MAX_RUNS and journal.stats() == (0, 0)


def test_journal_api(client: TestClient) -> None:
    log = journal.Journal("abc123", "Открой почту", "chat")
    log.step("Экран в начале", images=["data:image/jpeg;base64,/9j/AA=="])
    data = client.get("/api/agent/journal/abc123").json()
    assert data["task"] == "Открой почту" and data["steps"][0]["images"] == ["000-0.jpg"]
    image = client.get("/api/agent/journal/abc123/000-0.jpg").json()
    assert image == {"url": "data:image/jpeg;base64,/9j/AA=="}
    assert client.get("/api/agent/journal/nope").status_code == 404
    assert client.get("/api/agent/journal/abc123/journal.json").status_code == 404
