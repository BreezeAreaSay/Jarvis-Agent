"""Голосовые команды Telegram без ИИ («напиши Ане в телеграме: …», «что написал папа в телеграме») на
имитаторе: те же действия агента, что у модели, — чат по имени с падежом, вопрос «какой?», подтверждение
отправки, отказ."""

import asyncio
from typing import Any

import pytest

from evals.sim.desktop import Desktop
from evals.sim.platform import SimPlatform
from evals.sim.ui import SimOcr, SimToasts, SimUi
from jarvis.context import AppContext


@pytest.fixture
async def desktop(ctx: AppContext) -> Desktop:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    sim = Desktop.build()
    sim.telegram.open_main()
    world = sim.world
    ctx.platform = SimPlatform(world)
    agent: Any = ctx.agent
    agent.ui, agent.ocr, agent.toast_source = SimUi(world), SimOcr(world), SimToasts(world)
    agent.settle_s = agent.launch_settle_s = agent.pointer_delay_s = 0.0
    ctx.settings.patch({"ai": {"vision": "off"}, "voice": {"silent_mode": True}})
    await ctx.commands.refresh_apps(True)  # type: ignore[attr-defined]
    return sim


def _decide(ctx: AppContext, allow: bool) -> list[str]:
    asked: list[str] = []

    def answer(event: Any) -> None:
        asked.append(f"{event.data['title']} | {event.data.get('detail', '')}")
        ctx.confirmations.resolve(event.data["id"], allow)

    ctx.bus.on("confirm.request", answer)
    return asked


async def test_send_without_ai(ctx: AppContext, desktop: Desktop) -> None:
    """Модель не настроена — команда всё равно пишет: чат «Аня Смирнова» по «Ане Смирновой», с
    подтверждением, где видно, кому и что."""
    agent: Any = ctx.agent
    assert agent._llm().problem()  # ключа нет — модель недоступна
    asked = _decide(ctx, True)
    result = await ctx.commands.run_text("напиши Ане Смирновой в телеграме что буду в семь", "voice")
    assert result is not None and result.ok, result
    assert desktop.telegram.sent() == {"Аня Смирнова": ["Буду в семь"]}
    assert asked == ["Отправить в Telegram «Аня Смирнова»? | Будет отправлено: «Буду в семь»"]
    assert "Аня Смирнова" in result.message and "Буду в семь" in result.message


async def test_two_anyas_asks_which(ctx: AppContext, desktop: Desktop) -> None:
    _decide(ctx, True)
    questions: list[str] = []
    agent: Any = ctx.agent

    def state(event: Any) -> None:
        text = str(event.data.get("text") or "")
        if event.data.get("status") == "waiting" and text.startswith("Жду ответа"):
            questions.append(text)
            asyncio.get_running_loop().call_soon(agent.interject, "второй")

    ctx.bus.on("agent.state", state)
    result = await ctx.commands.run_text("напиши Ане в телеге перезвоню позже", "voice")
    assert result is not None and result.ok, result
    assert questions and "Аня (работа)" in questions[0] and "Аня Смирнова" in questions[0]
    assert desktop.telegram.sent() == {"Аня Смирнова": ["Перезвоню позже"]}


async def test_denied_sends_nothing_and_clears_the_text(ctx: AppContext, desktop: Desktop) -> None:
    _decide(ctx, False)
    result = await ctx.commands.run_text("напиши маме в телеграме я опоздаю", "voice")
    assert result is not None and not result.ok
    assert "не разрешили" in result.message
    assert desktop.telegram.sent() == {}
    window = desktop.telegram.main()
    assert window is not None and window.values.get("draft:Мама", "") == ""


async def test_unknown_chat_is_an_error(ctx: AppContext, desktop: Desktop) -> None:
    _decide(ctx, True)
    result = await ctx.commands.run_text("напиши Василию в телеграме привет", "voice")
    assert result is not None and not result.ok
    assert "не нашёл чат" in result.message
    assert desktop.telegram.sent() == {}


async def test_read_without_ai(ctx: AppContext, desktop: Desktop) -> None:
    result = await ctx.commands.run_text("что написал папа в телеграме", "voice")
    assert result is not None and result.ok, result
    assert "Машину забрал из сервиса" in result.message
    assert desktop.telegram.sent() == {}
