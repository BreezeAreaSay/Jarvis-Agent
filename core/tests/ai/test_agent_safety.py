"""Безопасность агента: «стоп» — сразу и до конца (и когда Codex или программа ещё зовут инструменты или уже
ведут курсор к кнопке), «стоп» в его чате — тоже стоп; связь (Wi-Fi, Bluetooth, VPN, «в самолёте»), питание
(выключение, перезагрузка, сон) и быстрые настройки — только с разрешения."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from jarvis.ai.llm import ToolCall
from jarvis.ai.uia import UiElement
from jarvis.context import AppContext
from tests.ai.fakes import FakeUi, ScriptedLlm, calls, completion, reply

PLAN = reply(json.dumps({"steps": ["Открыть параметры", "Нажать переключатель"]}))


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


def _deny(ctx: AppContext) -> list[str]:
    asked: list[str] = []

    def answer(event: Any) -> None:
        asked.append(event.data["title"])
        ctx.confirmations.resolve(event.data["id"], False)

    ctx.bus.on("confirm.request", answer)
    return asked


def _hold(llm: ScriptedLlm) -> tuple[asyncio.Event, asyncio.Event]:
    """Модель «думает», пока тест не отпустит: агент в это время работает."""
    thinking, release = asyncio.Event(), asyncio.Event()

    async def hold(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        thinking.set()
        await release.wait()
        return httpx.Response(200, json=completion(calls(("ui_click", {"id": "e1"})), body.get("model", "")))

    llm.add(PLAN, hold)
    return thinking, release


async def test_after_stop_the_agent_does_nothing_more(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    agent: Any = ctx.agent
    thinking, release = _hold(llm)
    run_id = await agent.run("Напиши Ивану")
    await asyncio.wait_for(thinking.wait(), 5)
    run = agent.current
    res = await ctx.dispatcher.handle("стоп стоп", "voice")  # с перепугу — несколько раз
    assert res["kind"] == "stop"
    assert (await agent.wait(run_id)).status == "stopped" and not agent.busy
    # Codex или программа-провайдер ещё успели позвать инструмент — он ничего не делает
    content, _ = await agent._act(run, ToolCall("call_1", "ui_click", '{"id": "e1"}'))
    assert content.startswith("Ошибка: пользователь остановил работу") and ui.clicks == []
    release.set()


async def test_stop_typed_in_the_agent_chat_stops_it(ctx: AppContext, llm: ScriptedLlm, ui: FakeUi) -> None:
    agent: Any = ctx.agent
    thinking, release = _hold(llm)
    run_id = await agent.run("Напиши Ивану")
    await asyncio.wait_for(thinking.wait(), 5)
    chat_id = agent.current.chat_id
    await ctx.ai.post_message(chat_id, "Стоп!")  # type: ignore[attr-defined]
    assert (await agent.wait(run_id)).status == "stopped"
    assert agent.current.notes == []  # не поправка к задаче
    release.set()


async def test_network_toggles_and_quick_settings_need_permission(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    asked = _deny(ctx)
    # переключатель Wi-Fi в параметрах Windows — для UI Automation обычная кнопка
    ui.window = "Параметры"
    ui.elements = [UiElement("e1", "Wi-Fi", "Button", (10, 10, 100, 40))]
    llm.add(
        PLAN, calls(("ui_click", {"id": "e1"})), calls(("finish", {"status": "partial", "summary": "Нет."}))
    )
    await agent.wait(await agent.run("Открой параметры сети"))
    assert asked == ["Нажать «Wi-Fi» в «Параметры»?"] and ui.clicks == []

    # быстрые настройки (Win+A): любое нажатие, и пробел тоже, — только с разрешения
    ui.__init__()
    ui.window = "Быстрые настройки"
    ui.elements = [UiElement("e1", "Энергосбережение", "Button", (10, 10, 100, 40))]
    llm.add(
        PLAN,
        calls(("ui_click", {"id": "e1"})),
        calls(("press_keys", {"combo": "space"})),
        calls(("finish", {"status": "partial", "summary": "Нет."})),
    )
    await agent.wait(await agent.run("Включи энергосбережение"))
    assert asked[1:] == [
        "Нажать «Энергосбережение» в «Быстрые настройки»?",
        "Нажать space в «Быстрые настройки»?",
    ]
    platform: Any = ctx.platform
    assert ui.clicks == [] and not any(name == "press_keys" for name, _ in platform.calls)

    # обычная кнопка в обычном окне — без вопросов, как раньше
    ui.__init__()
    llm.add(
        PLAN, calls(("ui_click", {"id": "e1"})), calls(("finish", {"status": "partial", "summary": "Да."}))
    )
    await agent.wait(await agent.run("Открой новое письмо"))
    assert len(asked) == 3 and ui.clicks == ["Написать"]
    # и в промпте: без прямой просьбы сеть не трогать
    assert "Не отключай сеть (Wi-Fi, Ethernet, VPN, Bluetooth)" in llm.requests[-1]["messages"][0]["content"]


async def test_stop_cuts_an_action_codex_already_started(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    agent.pointer_delay_s = 30.0  # курсор агента ещё летит к кнопке
    thinking, release = _hold(llm)
    run_id = await agent.run("Напиши Ивану")
    await asyncio.wait_for(thinking.wait(), 5)
    run = agent.current
    # действие позвал Codex (или программа): оно идёт не в задаче запуска — отмена запуска его не задела бы
    act = asyncio.ensure_future(agent._act(run, ToolCall("call_1", "ui_click", '{"id": "e1"}')))
    for _ in range(100):
        if run.acting:
            break
        await asyncio.sleep(0.01)
    assert run.acting
    await ctx.dispatcher.handle("стоп", "voice")
    content, _ = await asyncio.wait_for(act, 5)
    assert content.startswith("Ошибка: пользователь остановил работу") and ui.clicks == []
    assert (await agent.wait(run_id)).status == "stopped" and not run.acting
    release.set()


async def test_power_off_restart_and_sleep_need_permission(
    ctx: AppContext, llm: ScriptedLlm, ui: FakeUi
) -> None:
    agent: Any = ctx.agent
    asked = _deny(ctx)
    # меню «Пуск» → кнопка питания
    ui.window = "Пуск"
    ui.elements = [UiElement("e1", "Перезагрузка", "MenuItem", (10, 10, 100, 40))]
    llm.add(
        PLAN, calls(("ui_click", {"id": "e1"})), calls(("finish", {"status": "partial", "summary": "Нет."}))
    )
    await agent.wait(await agent.run("Открой меню пуск"))
    assert asked == ["Нажать «Перезагрузка» в «Пуск»?"] and ui.clicks == []

    # Alt+F4 на рабочем столе — окно «Завершение работы Windows»: Enter в нём — выключение
    ui.__init__()
    ui.window = "Завершение работы Windows"
    llm.add(
        PLAN,
        calls(("press_keys", {"combo": "enter"})),
        calls(("finish", {"status": "partial", "summary": "Нет."})),
    )
    await agent.wait(await agent.run("Закрой все окна"))
    assert asked[1:] == ["Нажать enter в «Завершение работы Windows»?"]
    platform: Any = ctx.platform
    assert not any(name == "press_keys" for name, _ in platform.calls)
