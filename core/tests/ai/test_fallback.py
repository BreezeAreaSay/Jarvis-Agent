"""Запасной провайдер ИИ: основной не ответил (квота, лимит, ключ, регион, сбой, нет связи) — тот же запрос
запасному, основной на паузе и не зовётся; не тот сбой, нет запасного или ответ уже пошёл на экран — ошибка
как раньше; новые настройки или ключ снимают паузу; подписка ChatGPT на паузе — отвечает запасной по ключу."""

from typing import Any

import httpx
import pytest

from jarvis.ai.llm import LlmError, resolve_fallback
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, completion, reply, sse, stream_chunks
from tests.conftest import EventLog

MESSAGES = [{"role": "user", "content": "Привет"}]


def backup(ctx: AppContext, provider: str = "deepseek", **ai: Any) -> None:
    ctx.secrets.set("ai_fallback_api_key", "sk-backup")
    ctx.settings.patch({"ai": {"fallback_provider": provider, **ai}})


def answer(status: int, message: str, code: str = "", headers: dict[str, str] | None = None) -> Any:
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "code": code}}, headers=headers)

    return handler


def ok(text: str, seen: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        seen.append(request)
        if body.get("stream"):
            chunks = stream_chunks(reply(text))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(chunks))
        return httpx.Response(200, json=completion(reply(text), body.get("model", "")))

    return handler


def test_resolve_fallback(ctx: AppContext) -> None:
    ai = ctx.settings.value.ai
    assert resolve_fallback(ai, ctx.secrets) is None  # не выбран
    ctx.settings.patch({"ai": {"fallback_provider": "deepseek"}})
    assert resolve_fallback(ctx.settings.value.ai, ctx.secrets) is None  # нет ключа
    backup(ctx)
    cfg = resolve_fallback(ctx.settings.value.ai, ctx.secrets)
    assert cfg is not None and cfg.backup and cfg.api_key == "sk-backup" and cfg.model == "deepseek-v4-flash"
    ctx.settings.patch({"ai": {"fallback_provider": "chatgpt"}})  # подписка запасной быть не может
    assert resolve_fallback(ctx.settings.value.ai, ctx.secrets) is None
    ctx.settings.patch({"ai": {"fallback_provider": "ollama", "fallback_model": "qwen3:8b"}})
    local = resolve_fallback(ctx.settings.value.ai, ctx.secrets)
    assert local is not None and local.local and local.model == "qwen3:8b"


async def test_quota_switches_to_backup_and_pauses_primary(
    ctx: AppContext, llm: ScriptedLlm, events: EventLog
) -> None:
    backup(ctx)
    seen: list[httpx.Request] = []
    llm.add(
        answer(429, "You exceeded your current quota", "insufficient_quota"), ok("Запасной на связи", seen)
    )
    response = await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert response.content == "Запасной на связи"
    assert seen[0].url.host == "api.deepseek.com" and seen[0].headers["authorization"] == "Bearer sk-backup"
    assert ctx.ai.llm.outage("openai") == "quota"  # type: ignore[attr-defined]
    event = events.of("ai.fallback")[-1]
    assert event["active"] and event["provider"] == "openai" and event["backup"] == "deepseek"
    toast = events.of("toast")[-1]
    assert toast["title"] == "Отвечает запасной ИИ — DeepSeek" and "30 мин" in toast["message"]
    # пока пауза — основной не зовётся вовсе
    llm.add(ok("Снова запасной", seen))
    assert (await ctx.ai.llm.chat(MESSAGES)).content == "Снова запасной"  # type: ignore[attr-defined]
    assert len(llm.requests) == 3 and seen[-1].url.host == "api.deepseek.com"
    status = ctx.ai.llm.fallback_status()  # type: ignore[attr-defined]
    assert status["configured"] and status["provider"] == "deepseek"
    assert status["paused"][0]["provider"] == "openai" and status["paused"][0]["reason"] == "quota"
    # пауза кончилась — снова основной
    ctx.ai.llm.clock = lambda: 10**9  # type: ignore[attr-defined]
    llm.add(ok("Основной вернулся", seen))
    assert (await ctx.ai.llm.chat(MESSAGES)).content == "Основной вернулся"  # type: ignore[attr-defined]
    assert seen[-1].url.host == "api.openai.com"


async def test_rate_limit_pause_follows_retry_after(ctx: AppContext, llm: ScriptedLlm) -> None:
    backup(ctx)
    seen: list[httpx.Request] = []
    busy = answer(429, "Rate limit reached", "rate_limit_exceeded", {"retry-after": "30"})
    llm.add(busy, busy, busy, ok("Запасной", seen))  # повторы основного (max_retries) не помогли
    assert (await ctx.ai.llm.chat(MESSAGES)).content == "Запасной"  # type: ignore[attr-defined]
    paused = ctx.ai.llm.fallback_status()["paused"][0]  # type: ignore[attr-defined]
    assert paused["reason"] == "rate" and 20 <= paused["seconds"] <= 30


@pytest.mark.parametrize(
    ("status", "message", "tries", "fallback"),
    [
        (401, "Incorrect API key provided", 1, True),
        (403, "unsupported_country_region_territory", 1, True),
        (503, "The server is overloaded", 3, True),  # 5xx основному повторяют (max_retries), потом — запасной
        (400, "Invalid value for messages", 1, False),  # запрос плохой — запасной не поможет
        (404, "The model `gpt-x` does not exist", 1, False),  # модель выбрана неверно — чинить настройки
    ],
)
async def test_which_errors_go_to_backup(
    ctx: AppContext, llm: ScriptedLlm, status: int, message: str, tries: int, fallback: bool
) -> None:
    backup(ctx)
    seen: list[httpx.Request] = []
    llm.add(*[answer(status, message)] * tries, ok("Запасной", seen))
    if fallback:
        assert (await ctx.ai.llm.chat(MESSAGES)).content == "Запасной"  # type: ignore[attr-defined]
    else:
        with pytest.raises(LlmError):
            await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
        assert seen == [] and ctx.ai.llm.outage("openai") == ""  # type: ignore[attr-defined]


async def test_no_backup_no_pause_and_backup_failure_is_reported(ctx: AppContext, llm: ScriptedLlm) -> None:
    llm.add(answer(429, "quota", "insufficient_quota"))
    with pytest.raises(LlmError, match=r"кредит|баланс|квот"):
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert ctx.ai.llm.outage("openai") == ""  # type: ignore[attr-defined]  # без запасного паузы нет
    backup(ctx)
    llm.add(answer(429, "quota", "insufficient_quota"), answer(401, "bad key"))
    with pytest.raises(LlmError) as failure:
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert failure.value.kind == "auth"  # ошибка запасного — человеку, дальше по цепочке не идём


async def test_settings_or_key_change_lifts_the_pause(
    ctx: AppContext, llm: ScriptedLlm, events: EventLog
) -> None:
    backup(ctx)
    await ctx.ai.start()  # type: ignore[attr-defined]
    try:
        assert ctx.ai.llm.note_outage("openai", "quota")  # type: ignore[attr-defined]
        assert ctx.ai.llm.config().provider == "deepseek"  # type: ignore[attr-defined]
        ctx.secrets.set("ai_api_key", "sk-new")
        assert ctx.ai.llm.outage("openai") == ""  # type: ignore[attr-defined]
        assert events.of("ai.fallback")[-1] == {"active": False}
        assert ctx.ai.llm.note_outage("openai", "server")  # type: ignore[attr-defined]
        ctx.settings.patch({"ai": {"model": "gpt-5.4"}})
        assert ctx.ai.llm.config().provider == "openai"  # type: ignore[attr-defined]
        # настройки проверяют основной, даже на паузе
        assert ctx.ai.llm.note_outage("openai", "network")  # type: ignore[attr-defined]
        assert ctx.ai.llm.primary_config().provider == "openai"  # type: ignore[attr-defined]
    finally:
        await ctx.ai.stop()  # type: ignore[attr-defined]


async def test_subscription_outage_routes_chat_to_backup(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"provider": "chatgpt"}})
    assert ctx.ai.account_backend() is ctx.ai.chatgpt  # type: ignore[attr-defined]
    assert not ctx.ai.llm.note_outage("chatgpt", "quota")  # type: ignore[attr-defined]  # запасного нет
    backup(ctx)
    assert ctx.ai.llm.note_outage("chatgpt", "limit")  # type: ignore[attr-defined]
    assert ctx.ai.account_backend() is None  # type: ignore[attr-defined]  # отвечает запасной по ключу
    assert ctx.ai.account_backend(routed=False) is ctx.ai.chatgpt  # type: ignore[attr-defined]
    seen: list[httpx.Request] = []
    llm.add(ok("Отвечаю через DeepSeek", seen))
    result = await ctx.ai.ask("Привет")  # type: ignore[attr-defined]
    assert result.text == "Отвечаю через DeepSeek" and seen[0].url.host == "api.deepseek.com"


async def test_no_backup_after_part_of_the_answer_is_shown(ctx: AppContext, llm: ScriptedLlm) -> None:
    backup(ctx)
    seen: list[httpx.Request] = []

    def broken(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "Начало ответа "}}]},
            {"error": {"message": "boom"}},
        ]
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(chunks, done=False)
        )

    llm.add(broken, ok("Запасной", seen))
    shown: list[str] = []
    with pytest.raises(LlmError):
        await ctx.ai.llm.chat(MESSAGES, stream=True, on_delta=shown.append)  # type: ignore[attr-defined]
    assert "".join(shown) == "Начало ответа " and seen == []  # ответ не задвоился чужим текстом
