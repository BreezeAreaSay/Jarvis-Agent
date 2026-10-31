"""Исправления по независимому ревью: голосовое «да» и необратимое у агента, «\\» в адресе, заголовки
скрытых окон, проверка связи без запасного, подписка после паузы, запасной того же провайдера."""

import asyncio
from typing import Any

import httpx
import pytest

from jarvis.ai import tools as chat_tools
from jarvis.ai.policy import NO_HOST, USERINFO, web_url
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from tests.ai.fakes import ScriptedLlm, completion, reply


async def test_voice_yes_does_not_approve_high_risk_agent_action(ctx: AppContext) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    agent: Any = ctx.agent
    risky = asyncio.ensure_future(
        ctx.confirmations.request("Нажать «Отправить» в «Почта»?", "", kind="agent", risk="high")
    )
    plain = asyncio.ensure_future(ctx.confirmations.request("Открыть сайт?", "", kind="agent"))
    await asyncio.sleep(0)
    assert agent._decide("да", "voice") is True  # услышано, но необратимое — только кнопкой
    assert await asyncio.wait_for(plain, 1) is True
    await asyncio.sleep(0.05)
    assert not risky.done()
    assert agent._decide("да", "text") is True  # напечатанное «да» — как кнопка
    assert await asyncio.wait_for(risky, 1) is True


async def test_voice_no_still_refuses_high_risk(ctx: AppContext) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    risky = asyncio.ensure_future(ctx.confirmations.request("Оплатить заказ?", "", kind="agent", risk="high"))
    await asyncio.sleep(0)
    assert ctx.agent._decide("нет", "voice") is True  # type: ignore[attr-defined]
    assert await asyncio.wait_for(risky, 1) is False


def test_backslash_and_login_in_links() -> None:
    url, problem = web_url("https://gооgle.com\\@google.com/")
    assert (
        url == "https://gооgle.com/@google.com/" and not problem
    )  # браузер увидит gооgle.com — так и проверяем
    assert web_url("https://user:pw@bank.example/")[1] == USERINFO


async def test_hidden_window_titles_and_typing(ctx: AppContext) -> None:
    platform: Any = ctx.platform
    for w in platform.windows:
        w.foreground = False
    platform.windows.append(
        WindowInfo(150, "Сбербанк — пароль — KeePass", "KeePass.exe", 9150, (0, 0, 800, 600), foreground=True)
    )
    text = await ctx.agent._windows_text()  # type: ignore[attr-defined]
    assert "Сбербанк" not in text and "(окно скрыто от ИИ)" in text
    from jarvis.ai.tools import ToolEnv, ToolRegistry

    env = ToolEnv(ctx, ctx.ai.web, "text", "", user_text="")  # type: ignore[attr-defined,arg-type]
    tools = ToolRegistry(ctx).available("all")
    out = await tools["type_text"].handler(env, {"text": "секрет"})
    assert not out.ok and "скрыто от ИИ" in out.content
    assert not any(name in ("type_text", "paste_text") for name, _ in platform.calls)


async def test_connection_test_checks_only_the_primary(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.secrets.set("ai_fallback_api_key", "sk-backup")
    ctx.settings.patch({"ai": {"fallback_provider": "deepseek"}})
    llm.add(lambda r, b: httpx.Response(401, json={"error": {"message": "bad key"}}), reply("Готов"))
    result = await ctx.ai.llm.test()  # type: ignore[attr-defined]
    assert result["ok"] is False and len(llm.requests) == 1  # запасной не звали
    assert ctx.ai.llm.outage("openai") == ""  # type: ignore[attr-defined]  # и основной не на паузе


async def test_paused_subscription_keeps_backup_after_pause_ends(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.secrets.set("ai_fallback_api_key", "sk-backup")
    ctx.settings.patch({"ai": {"provider": "chatgpt", "fallback_provider": "deepseek"}})
    assert ctx.ai.llm.note_outage("chatgpt", "limit")  # type: ignore[attr-defined]
    ctx.ai.llm.clock = lambda: 10**9  # type: ignore[attr-defined]  # пауза кончилась посреди долгой работы
    assert ctx.ai.llm.config().provider == "deepseek"  # type: ignore[attr-defined]  # клиент API — не подписка


async def test_backup_on_the_same_provider_is_used(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch(
        {"ai": {"model": "gpt-5.4", "fallback_provider": "openai", "fallback_model": "gpt-5.4-nano"}}
    )
    seen: list[str] = []

    def quota(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        seen.append(body["model"])
        return httpx.Response(429, json={"error": {"message": "quota", "code": "insufficient_quota"}})

    def ok(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        seen.append(body["model"])
        return httpx.Response(200, json=completion(reply("Запасной"), body["model"]))

    llm.add(quota, ok)
    result = await ctx.ai.llm.chat([{"role": "user", "content": "Привет"}])  # type: ignore[attr-defined]
    assert result.content == "Запасной" and seen == ["gpt-5.4", "gpt-5.4-nano"]


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        ("https://\\gооgle.com\\login", "https://gооgle.com/login"),
        ("https:///evil.example/?d=SECRET", "https://evil.example/?d=SECRET"),
        ("https:\\\\evil.example", "https://evil.example"),
        ("HTTPS:evil.example/x", "https://evil.example/x"),
    ],
)
def test_extra_slashes_after_the_scheme_still_name_the_site(raw: str, url: str) -> None:
    assert web_url(raw) == (url, "")  # браузер пропускает лишние «/» и «\\» — проверка видит тот же сайт


def test_links_with_login_or_without_site_are_refused() -> None:
    assert web_url("https:///google.com@evil.example/")[1] == USERINFO
    assert web_url("https://")[1] == NO_HOST


async def test_lookalike_behind_extra_slashes_is_asked(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    async def deny(env: Any, title: str, detail: str = "", **kw: Any) -> bool:
        asked.append(f"{title} {detail}")
        return False

    monkeypatch.setattr(chat_tools, "confirm", deny)
    env = chat_tools.ToolEnv(ctx, ctx.ai.web, "text", "", user_text="")  # type: ignore[attr-defined,arg-type]
    tools = chat_tools.ToolRegistry(ctx).available("all")
    out = await tools["open_url"].handler(env, {"url": "https:///gооgle.com/login"})
    assert not out.ok and asked and "похоже на подделку" in asked[0]
    assert not any(name == "open_url" for name, _ in ctx.platform.calls)  # type: ignore[attr-defined]


async def test_window_change_to_hidden_window_names_no_title(ctx: AppContext) -> None:
    from jarvis.ai.agent import AgentRun, WindowChanged
    from jarvis.services import UserFacingError

    platform: Any = ctx.platform
    run = AgentRun(
        id="r", task="Ответь в почте", source="text", chat_id="", turn_id="", reply_id="", speak=False
    )
    run.target = platform.windows[1]  # работал в Chrome
    for w in platform.windows:
        w.foreground = False
    platform.windows.append(
        WindowInfo(160, "Сбербанк — пароль — KeePass", "KeePass.exe", 9160, (0, 0, 800, 600), foreground=True)
    )
    try:
        await ctx.agent._target(run)  # type: ignore[attr-defined]
    except WindowChanged as e:
        raise AssertionError(f"заголовок скрытого окна ушёл модели: {e}") from e
    except UserFacingError as e:
        assert "Сбербанк" not in str(e) and "скрыто от ИИ" in str(e)
    else:
        raise AssertionError("в скрытое окно не вводят")
