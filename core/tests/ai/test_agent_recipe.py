"""Удачный прогон агента → сценарий: шаги записываются (Ui.Click по названию, Ui.Type, клавиши, запуск…),
сохраняются кнопкой (API) или фразой «запомни это»; сценарий повторяет их без ИИ, а не нашёл кнопку —
дело передаётся агенту."""

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.ai.agent import AgentRun
from jarvis.ai.uia import UiElement, find_by_name
from jarvis.commands import executor as executor_module
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from jarvis.services import UserFacingError
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, reply

PLAN = reply(json.dumps({"steps": ["Открыть письмо", "Заполнить", "Сохранить черновик"]}))
DRAFT = [
    {"type": "Ui.Click", "value": "Написать"},
    {"type": "Ui.Type", "value": "Кому | ivan@example.com"},
    {"type": "Keys.Press", "value": "ctrl+s"},
    {"type": "Wait", "value": "0.5"},
]


@pytest.fixture
def ui(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> FakeUi:
    fake = FakeUi()
    agent: Any = ctx.agent
    agent.ui = fake
    agent.settle_s = 0.0
    agent.launch_settle_s = 0.0
    agent.pointer_delay_s = 0.0
    ctx.settings.patch({"ai": {"vision": "off"}})
    monkeypatch.setattr(executor_module, "UI_SETTLE_S", 0.0)
    return fake


async def _draft_run(ctx: AppContext, llm: ScriptedLlm) -> tuple[str, dict[str, Any]]:
    llm.add(
        PLAN,
        calls(("ui_click", {"id": "e1"})),
        calls(("ui_type", {"id": "e3", "text": "ivan@example.com"}), ("press_keys", {"combo": "Ctrl+S"})),
        calls(("wait", {"seconds": 0.5})),
        calls(("finish", {"summary": "Черновик сохранён."})),
        calls(("finish", {"summary": "Черновик сохранён."})),  # после проверки по снимку
    )
    agent: Any = ctx.agent
    result = await agent.wait(await agent.run("Напиши Ивану черновик"))
    assert result.outcome == "done"
    chat_id = agent.current.chat_id
    final = ctx.ai.get_chat(chat_id)["messages"][-1]  # type: ignore[attr-defined]
    return chat_id, final


async def test_successful_run_is_saved_as_scenario_and_replayed_without_ai(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    chat_id, final = await _draft_run(ctx, llm)
    assert final["meta"]["recipe"] == {"request": "Напиши Ивану черновик", "actions": DRAFT}

    scenario = agent.save_scenario(chat_id, final["id"])
    assert scenario["name"] == "Напиши Ивану черновик" and scenario["actions"] == DRAFT
    assert scenario["triggers"] == [{"type": "phrase", "phrases": ["напиши ивану черновик"]}]
    again = agent.save_scenario(chat_id, final["id"])  # второй раз — тот же сценарий
    assert again["id"] == scenario["id"] and len(ctx.scenarios.list_all()) == 1
    saved = ctx.ai.get_chat(chat_id)["messages"][-1]  # type: ignore[attr-defined]
    assert saved["meta"]["recipe"]["scenario_id"] == scenario["id"]

    # та же фраза — сценарий: те же шаги, без ИИ
    ui.__init__()  # снова «Входящие»
    asked = len(llm.requests)
    res = await ctx.dispatcher.handle("напиши Ивану черновик", "text")
    assert res["kind"] == "command" and res["result"]["ok"], res
    assert ui.clicks == ["Написать"] and ui.typed == [("Кому", "ivan@example.com")]
    assert ("press_keys", ("ctrl+s",)) in ctx.platform.calls  # type: ignore[attr-defined]
    assert len(llm.requests) == asked


async def test_remember_this_by_voice(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    agent: Any = ctx.agent
    await _draft_run(ctx, llm)
    assert agent.recent_recipe()
    res = await ctx.dispatcher.handle("Запомни это как сценарий", "text")
    assert res["kind"] == "command" and res["result"]["ok"]
    assert res["result"]["title"] == "Сценарий «Напиши Ивану черновик»"
    assert [s["name"] for s in ctx.scenarios.list_all()] == ["Напиши Ивану черновик"]
    # давно — «запомни это» уже не про дело агента
    chat_id, message_id, _ = agent._last_recipe
    agent._last_recipe = (chat_id, message_id, time.monotonic() - 3600)
    assert not agent.recent_recipe()
    # последнее дело сценарием не повторить — прежнее «запомни это» уже не запомнит
    agent._last_recipe = (chat_id, message_id, time.monotonic())
    llm.add(PLAN, calls(("press_keys", {"combo": "ctrl+n"})), calls(("finish", {"status": "partial"})))
    await agent.wait(await agent.run("Создай письмо"))
    assert not agent.recent_recipe()


async def test_scenario_hands_over_to_agent_when_the_button_is_missing(
    ctx: AppContext, ui: FakeUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_module, "UI_WAIT_S", 0.05)
    item = ctx.scenarios.create(
        {
            "name": "Заархивируй письмо",
            "triggers": [{"type": "phrase", "phrases": ["заархивируй письмо"]}],
            "actions": [{"type": "Ui.Click", "value": "Архивировать"}],
        }
    )
    result = await ctx.scenarios.run(item["id"])
    assert not result.ok and result.message == "В окне «Входящие — Почта» нет «Архивировать»"
    assert result.data["agent_task"] == (
        "Заархивируй письмо. (Готовый сценарий не справился: в окне «Входящие — Почта» нет «Архивировать».)"
    )


async def test_runs_that_a_scenario_cannot_repeat_are_not_offered(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    run = AgentRun("r1", "Задача", "text", "c1", "t1", "m1", False)
    run.target = WindowInfo(7, "Telegram", "Telegram.exe")
    steps = agent._recipe_steps
    password = UiElement("e1", "Пароль", "Edit", password=True)
    assert steps(run, "ui_type", {"id": "e1", "text": "secret"}, password) is None
    assert steps(run, "ui_click", {"id": "e9"}, UiElement("e9", "", "Button")) is None  # без имени
    assert steps(run, "type_text", {"text": "карта 4111 1111 1111 1111"}, None) is None  # секрет
    assert steps(run, "mcp__telegram__send_message", {"text": "Привет"}, None) is None
    assert steps(run, "screenshot", {}, None) == [] and steps(run, "ask_user", {"question": "?"}, None) == []
    assert steps(run, "focus_window", {"title": "телеграм"}, None) == [
        {"type": "App.Focus", "value": "Telegram.exe"}
    ]
    assert steps(run, "launch_app", {"name": "Почта"}, None) == [{"type": "Launch.App", "value": "Почта"}]
    assert steps(run, "open_url", {"url": "tg://resolve?domain=durov"}, None) == [
        {"type": "Launch.Url", "value": "tg://resolve?domain=durov"}
    ]
    assert steps(run, "run_command", {"text": "пауза", "id": "spotify.pause"}, None) == [
        {"type": "Jarvis.Command", "value": "spotify.pause"}
    ]
    assert steps(run, "type_text", {"text": "строка\nвторая"}, None) == [
        {"type": "Keys.Paste", "value": "строка\nвторая"}
    ]
    # точка — от ближнего края окна (минус — от правого или нижнего): окно сдвинут — сценарий не промахнётся
    run.pointer = (640, 360)
    assert steps(run, "click_point", {"x": 1, "y": 2}, None) is None  # окно точки неизвестно
    run.pointer_window = WindowInfo(7, "Telegram", "Telegram.exe", rect=(100, 50, 1100, 850))
    assert steps(run, "click_point", {"x": 1, "y": 2, "button": "right"}, None) == [
        {"type": "Mouse.MoveInWindow", "value": "Telegram.exe | -460 310"},
        {"type": "Mouse.ClickRight", "value": ""},
    ]
    assert steps(run, "scroll", {"direction": "up", "amount": 3}, None) == [
        {"type": "Mouse.MoveInWindow", "value": "Telegram.exe | -460 310"},
        {"type": "Mouse.Scroll", "value": "3"},
    ]
    # что запомнить: только удачный прогон, где сценарий повторит всё
    run.recipe, run.outcome = [{"type": "Keys.Press", "value": "ctrl+s"}], "done"
    assert agent._recipe(run, "done") == {"request": "Задача", "actions": run.recipe}
    run.outcome = "partial"
    assert agent._recipe(run, "done") is None
    run.outcome, run.recipe = "done", [{"type": "Wait", "value": "1"}]
    assert agent._recipe(run, "done") is None  # одно ожидание — не дело
    run.recipe, run.replayable = [{"type": "Keys.Press", "value": "ctrl+s"}], False
    assert agent._recipe(run, "done") is None


async def test_point_in_window_follows_the_window(ctx: AppContext, ui: FakeUi) -> None:
    platform: Any = ctx.platform
    spotify = next(w for w in platform.windows if w.exe == "Spotify.exe")  # не впереди: (120, 120, 1300, 900)
    item = ctx.scenarios.create(
        {
            "name": "Нажми плей в спотифае",
            "triggers": [{"type": "phrase", "phrases": ["нажми плей в спотифае"]}],
            "actions": [
                {"type": "Mouse.MoveInWindow", "value": "Spotify | 150 -40"},
                {"type": "Mouse.ClickLeft", "value": ""},
            ],
        }
    )
    assert (await ctx.scenarios.run(item["id"])).ok
    assert ("window_action", ("focus", spotify.hwnd)) in platform.calls  # окно — вперёд
    assert ("mouse_move", (270, 860)) in platform.calls and (
        "mouse_click",
        (None, None, "left", False),
    ) in platform.calls
    # окно растянули — точка у нижнего края осталась на месте относительно окна
    spotify.rect = (0, 0, 1920, 1040)
    platform.calls.clear()
    assert (await ctx.scenarios.run(item["id"])).ok
    assert ("mouse_move", (150, 1000)) in platform.calls
    # окна нет — дело агенту
    platform.windows.remove(spotify)
    result = await ctx.scenarios.run(item["id"])
    assert not result.ok and result.message == "Окна «Spotify» нет"
    assert (
        result.data["agent_task"]
        == "Нажми плей в спотифае. (Готовый сценарий не справился: окна «Spotify» нет.)"
    )
    with pytest.raises(UserFacingError, match="программа \\| x y"):
        ctx.scenarios.create(
            {
                "name": "Плохая точка",
                "triggers": [{"type": "phrase", "phrases": ["плохая точка"]}],
                "actions": [{"type": "Mouse.MoveInWindow", "value": "150 60"}],
            }
        )


def test_long_request_still_gets_a_phrase(ctx: AppContext) -> None:
    agent: Any = ctx.agent
    request = (
        "открой телеграм найди чат с аней и напиши ей что я задержусь "
        "на полчаса из-за пробок на кольцевой дороге"
    )
    assert len(request) > 100
    chat = ctx.ai.chats.create(title="Агент", mode="agent")  # type: ignore[attr-defined]
    recipe = {"request": request, "actions": [{"type": "Keys.Press", "value": "ctrl+k"}]}
    row = ctx.ai.chats.add_message(chat["id"], "assistant", "Готово.", meta={"recipe": recipe})  # type: ignore[attr-defined]
    item = agent.save_scenario(chat["id"], row["id"])
    phrase = item["triggers"][0]["phrases"][0]
    assert len(phrase) <= 100 and request.startswith(phrase) and phrase.endswith("на кольцевой")
    assert item["name"].endswith("…") and len(item["name"]) <= 60


def test_find_element_by_name() -> None:
    elements = [
        UiElement("e1", "Отправить позже", "Button"),
        UiElement("e2", "Отправить", "Button", enabled=False),
        UiElement("e3", "Отправить", "Button"),
        UiElement("e4", "Поиск в почте", "Edit"),
        UiElement("e5", "Входящие (3)", "ListItem"),
    ]
    assert find_by_name(elements, "отправить").id == "e3"  # type: ignore[union-attr]  # точное и доступное
    assert find_by_name(elements, "«Поиск»").id == "e4"  # type: ignore[union-attr]  # начало названия
    assert find_by_name(elements, "входящие").id == "e5"  # type: ignore[union-attr]
    assert find_by_name(elements, "Поиск в почтe").id == "e4"  # type: ignore[union-attr]  # латинская «e»
    assert find_by_name(elements, "Архивировать") is None and find_by_name(elements, " ") is None


def test_saving_without_a_run_is_a_clear_error(client: TestClient) -> None:
    res = client.post("/api/agent/scenario", json={})
    assert res.status_code == 400 and "Запоминать нечего" in res.json()["detail"]
