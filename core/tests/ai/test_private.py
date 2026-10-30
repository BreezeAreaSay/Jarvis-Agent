"""«Приватно: …» — ответ только моделью на этом ПК: без облака, поиска в интернете и серверов MCP; модели на
этом ПК нет — понятная ошибка, а не тихий уход в облако."""

import json
from typing import Any

import httpx

from jarvis.ai.llm import PRIVATE_NONE
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, completion, reply, sse, stream_chunks


def answer(text: str, seen: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        seen.append(request)
        if body.get("stream"):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(stream_chunks(reply(text)))
            )
        return httpx.Response(200, json=completion(reply(text), body.get("model", "")))

    return handler


async def test_private_without_local_model_refuses(ctx: AppContext, llm: ScriptedLlm) -> None:
    result = await ctx.ai.ask("Приватно: сколько я потратил в октябре?")  # type: ignore[attr-defined]
    assert result.error == PRIVATE_NONE and llm.requests == []  # в облако — ничего


async def test_private_goes_to_local_backup_without_web_tools(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"fallback_provider": "ollama", "fallback_model": "qwen3:8b"}})
    seen: list[httpx.Request] = []
    llm.add(answer("Около 40 тысяч, сэр.", seen))
    result = await ctx.ai.ask("джарвис, приватно: сколько я потратил в октябре, пароль: hunter2")  # type: ignore[attr-defined]
    assert result.text == "Около 40 тысяч, сэр."
    assert seen[0].url.host == "127.0.0.1" and seen[0].url.port == 11434
    body = llm.requests[0]
    assert body["model"] == "qwen3:8b"
    names = {t["function"]["name"] for t in body.get("tools", [])}
    assert not names & {"web_search", "fetch_page", "get_weather", "get_news", "get_rates", "run_agent"}
    user = body["messages"][-1]["content"]
    assert "сколько я потратил в октябре" in str(user) and "приватно" not in str(user).casefold()
    assert "hunter2" in str(user)  # модель на этом ПК видит как есть: маска — только для облака
    # обычный вопрос — как раньше, основным провайдером
    seen.clear()
    llm.add(answer("Привет!", seen))
    await ctx.ai.ask("привет")  # type: ignore[attr-defined]
    assert seen[0].url.host == "api.openai.com"


async def test_private_uses_local_primary(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"provider": "lmstudio", "model": "gemma-3-12b-it"}})
    seen: list[httpx.Request] = []
    llm.add(answer("Готово.", seen))
    result = await ctx.ai.ask("локально — перескажи мою заметку о встрече")  # type: ignore[attr-defined]
    assert result.text == "Готово." and seen[0].url.host == "127.0.0.1"


async def test_private_exchange_never_reaches_the_cloud_later(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"fallback_provider": "ollama", "fallback_model": "qwen3:8b"}})
    seen: list[httpx.Request] = []
    llm.add(answer("Записал у себя, сэр.", seen))
    private = await ctx.ai.ask("приватно: мой счёт в банке 40817810099910004312", source="voice")  # type: ignore[attr-defined]
    llm.add(answer("Привет!", seen))
    public = await ctx.ai.ask("а что ты думаешь о погоде", chat_id=private.chat_id, source="voice")  # type: ignore[attr-defined]
    assert public.chat_id == private.chat_id and seen[-1].url.host == "api.openai.com"
    cloud = json.dumps(llm.requests[-1]["messages"], ensure_ascii=False)
    assert "40817810099910004312" not in cloud and "Записал у себя" not in cloud
    # а следующий приватный вопрос свою приватную историю видит
    llm.add(answer("Помню.", seen))
    await ctx.ai.ask("приватно: какой у меня счёт?", chat_id=private.chat_id)  # type: ignore[attr-defined]
    local = json.dumps(llm.requests[-1]["messages"], ensure_ascii=False)
    assert "40817810099910004312" in local and seen[-1].url.host == "127.0.0.1"
