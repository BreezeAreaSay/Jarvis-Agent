"""Клиент OpenAI-совместимого API: поток SSE, вызовы инструментов кусками, ошибки, повторы, JSON, расходы."""

import json
import time
from typing import Any

import httpx
import pytest

from jarvis.ai import llm as llm_module
from jarvis.ai.llm import (
    LlmError,
    LlmFormatError,
    SseDecoder,
    StreamAccumulator,
    ThinkFilter,
    extract_json,
    resolve_config,
    strip_think,
)
from jarvis.ai.providers import estimate_cost, price_for
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, calls, chunked, completion, reply, sse, stream_chunks

MESSAGES = [{"role": "user", "content": "Привет"}]


def test_sse_decoder_handles_comments_multiline_and_missing_blank_lines() -> None:
    decoder = SseDecoder()
    out = []
    for line in [
        ": OPENROUTER PROCESSING",
        "event: message",
        'data: {"a": 1}',  # законченный JSON отдаётся сразу
        "",
        "data: {",
        'data: "b": 2}',
        "",
        'data: {"c": 3}',  # без пустой строки между событиями
        'data: {"d": 4}',
        "data: [DONE]",
    ]:
        value = decoder.feed(line)
        if value is not None:
            out.append(value)
    assert out == ['{"a": 1}', '{\n"b": 2}', '{"c": 3}', '{"d": 4}', "[DONE]"]


def test_accumulator_joins_tool_call_deltas_split_across_chunks() -> None:
    acc = StreamAccumulator()
    pieces = [
        {"choices": [{"index": 0, "delta": {"content": "Сейчас "}}]},
        {"choices": [{"index": 0, "delta": {"content": "сделаю."}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_command"}}]},
                }
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"te'}}]}}
            ]
        },
        # второй вызов начинается, пока первый не дописан
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 1, "id": "c2", "function": {"name": "web_search", "arguments": ""}}
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'xt": "сверни '}}]},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 1, "function": {"arguments": '{"query": "погода"}'}}]},
                }
            ]
        },
        {
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'окна"}'}}]}}
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 7}},
    ]
    texts = [acc.add(p) for p in pieces]
    assert "".join(texts) == "Сейчас сделаю."
    result = acc.result("m")
    assert [(c.id, c.name) for c in result.tool_calls] == [("c1", "run_command"), ("c2", "web_search")]
    assert result.tool_calls[0].parse_arguments() == {"text": "сверни окна"}
    assert result.tool_calls[1].parse_arguments() == {"query": "погода"}
    assert result.finish_reason == "tool_calls"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (50, 7)


def test_accumulator_without_index_and_with_dict_arguments() -> None:
    acc = StreamAccumulator()
    acc.add(
        {
            "choices": [
                {"delta": {"tool_calls": [{"id": "a", "function": {"name": "x", "arguments": {"k": 1}}}]}}
            ]
        }
    )
    acc.add(
        {"choices": [{"delta": {"tool_calls": [{"id": "b", "function": {"name": "y", "arguments": "{}"}}]}}]}
    )
    result = acc.result("m")
    assert [(c.name, c.parse_arguments()) for c in result.tool_calls] == [("x", {"k": 1}), ("y", {})]


def test_think_filter_hides_reasoning_split_between_chunks() -> None:
    think = ThinkFilter()
    out = "".join(think.feed(p) for p in ["<thi", "nk>считаю", " в уме</th", "ink>\n\nПри", "вет, сэр", "<"])
    out += think.flush()
    assert out == "Привет, сэр<"
    assert strip_think("<think>a</think>\nОтвет") == "Ответ"
    assert strip_think("рассуждаю…</think>Ответ") == "Ответ"


def test_extract_json_variants() -> None:
    assert extract_json('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert extract_json('Вот ответ: {"name": "x", "s": "}{"} — готово') == {"name": "x", "s": "}{"}
    assert extract_json("<think>{'нет'}</think>[1, 2]") == [1, 2]
    assert extract_json("без json") is None


async def test_stream_split_at_arbitrary_bytes(ctx: AppContext, llm: ScriptedLlm) -> None:
    body = sse(stream_chunks(calls(("run_command", {"text": "включи музыку"}), text="Включаю музыку, сэр.")))

    def split_response(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        # куски по 7 байт: режут строки «data:» и русские буквы пополам
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=chunked(body, [7] * 400)
        )

    llm.add(split_response)
    deltas: list[str] = []
    result = await ctx.ai.llm.chat(MESSAGES, stream=True, on_delta=deltas.append)  # type: ignore[attr-defined]
    assert "".join(deltas) == "Включаю музыку, сэр."
    assert result.content == "Включаю музыку, сэр."
    assert result.tool_calls[0].name == "run_command"
    assert result.tool_calls[0].parse_arguments() == {"text": "включи музыку"}
    request = llm.requests[0]
    assert request["stream"] is True and request["stream_options"] == {"include_usage": True}
    assert request["model"] == "gpt-5.4-mini"
    assert "temperature" not in request  # «думающая» модель OpenAI не принимает temperature
    usage = ctx.db.query("SELECT * FROM usage")
    assert usage[0]["prompt_tokens"] == 120 and usage[0]["provider"] == "openai"
    assert usage[0]["cost"] == pytest.approx(estimate_cost("gpt-5.4-mini", 120, 30))


async def test_non_stream_and_bearer_header(ctx: AppContext, llm: ScriptedLlm) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion(reply("Готово")))

    llm.add(answer)
    result = await ctx.ai.llm.chat(MESSAGES, max_tokens=50)  # type: ignore[attr-defined]
    assert result.content == "Готово"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert llm.requests[0]["max_completion_tokens"] == 50  # у OpenAI — новое имя параметра


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": {"message": "Incorrect API key provided"}}, "Ключ API не подходит"),
        (
            403,
            {"error": {"code": "unsupported_country_region_territory", "message": "Country not supported"}},
            "Провайдер недоступен из вашего региона — включите VPN или укажите прокси",
        ),
        (
            429,
            {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}},
            "Закончился баланс у провайдера ИИ",
        ),
        (404, {"error": {"message": "The model `nope` does not exist"}}, "не найдена у провайдера"),
        (
            400,
            {"error": {"message": "context_length_exceeded", "code": "context_length_exceeded"}},
            "слишком длинный",
        ),
    ],
)
async def test_error_messages(
    ctx: AppContext, llm: ScriptedLlm, status: int, body: dict, expected: str
) -> None:
    llm.add(httpx.Response(status, json=body))
    with pytest.raises(LlmError) as info:
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert expected in str(info.value)
    assert len(llm.requests) == 1  # без повторов


async def test_rate_limit_retries_then_gives_up(ctx: AppContext, llm: ScriptedLlm) -> None:
    for _ in range(3):
        llm.add(
            httpx.Response(
                429, json={"error": {"message": "Rate limit reached"}}, headers={"retry-after": "1"}
            )
        )
    with pytest.raises(LlmError) as info:
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert str(info.value).startswith("Лимит запросов исчерпан")
    assert len(llm.requests) == 3


async def test_server_error_is_retried(ctx: AppContext, llm: ScriptedLlm) -> None:
    llm.add(httpx.Response(503, text="<html>Service Unavailable</html>"), reply("Всё в порядке"))
    result = await ctx.ai.llm.chat(MESSAGES, stream=True)  # type: ignore[attr-defined]
    assert result.content == "Всё в порядке"
    assert len(llm.requests) == 2


async def test_network_errors(ctx: AppContext, llm: ScriptedLlm) -> None:
    def broken(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    llm.add(broken, broken, broken)
    with pytest.raises(LlmError) as info:
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert str(info.value) == "Нет связи с провайдером ИИ"
    # локальный сервер: повторять незачем, подсказка — запустить Ollama
    ctx.settings.patch({"ai": {"provider": "ollama", "model": ""}})
    llm.requests.clear()
    llm.add(broken)
    with pytest.raises(LlmError) as info:
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert "Ollama" in str(info.value) and len(llm.requests) == 1


async def test_no_key_means_no_request(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.secrets.set("ai_api_key", "")
    assert not ctx.ai.configured
    with pytest.raises(LlmError) as info:
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert str(info.value) == "Укажите ключ API в настройках Jarvis AI"
    assert llm.requests == []
    ctx.settings.patch({"ai": {"provider": "ollama"}})
    assert ctx.ai.configured  # локальной модели ключ не нужен


async def test_unsupported_parameter_is_dropped_and_remembered(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch(
        {"ai": {"provider": "custom", "base_url": "https://llm.example.com/v1/", "model": "m1"}}
    )
    llm.add(
        httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'temperature'"}}),
        reply("Раз"),
        reply("Два"),
    )
    assert (await ctx.ai.llm.chat(MESSAGES)).content == "Раз"  # type: ignore[attr-defined]
    assert "temperature" in llm.requests[0] and "temperature" not in llm.requests[1]
    assert (await ctx.ai.llm.chat(MESSAGES)).content == "Два"  # type: ignore[attr-defined]
    assert "temperature" not in llm.requests[2]
    assert all(r["model"] == "m1" for r in llm.requests)


async def test_structured_output_with_fallback_to_prompt(ctx: AppContext, llm: ScriptedLlm) -> None:
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    llm.add(
        httpx.Response(400, json={"error": {"message": "Invalid parameter: response_format json_schema"}}),
        reply('Конечно!\n```json\n{"name": "Режим стрима"}\n```'),
    )
    assert await ctx.ai.complete(MESSAGES, json_schema=schema) == {"name": "Режим стрима"}
    first, second = llm.requests
    assert first["response_format"]["type"] == "json_schema"
    assert first["response_format"]["json_schema"]["schema"] == schema
    assert "response_format" not in second
    assert "JSON" in second["messages"][0]["content"]  # просьба о JSON в системном сообщении

    # провайдер молча игнорирует response_format и отвечает текстом — повтор с просьбой, потом ошибка
    llm.add(reply("Не знаю"), reply("Правда не знаю"))
    with pytest.raises(LlmFormatError):
        await ctx.ai.complete(MESSAGES, json_schema=schema)


async def test_plain_complete_returns_text(ctx: AppContext, llm: ScriptedLlm) -> None:
    llm.add(reply("<think>хм</think>Перевод: hello"))
    assert await ctx.ai.complete(MESSAGES, temperature=0.2) == "Перевод: hello"


async def test_anthropic_preset(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"provider": "anthropic"}})  # модель осталась от OpenAI по умолчанию
    cfg = ctx.ai.llm.config()  # type: ignore[attr-defined]
    assert cfg.model == "claude-opus-5-5" and cfg.base_url == "https://api.anthropic.com/v1"
    llm.add(reply('{"ok": true}'))
    assert await ctx.ai.complete(MESSAGES, json_schema={"type": "object"}, temperature=0.3) == {"ok": True}
    request = llm.requests[0]
    assert "temperature" not in request and "response_format" not in request
    assert request["model"] == "claude-opus-5-5"


async def test_budget_blocks_requests(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"monthly_budget": 1.0}})
    ctx.db.insert(
        "usage",
        {
            "ts": time.time(),
            "provider": "openai",
            "model": "gpt-5.4",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "cost": 1.5,
        },
    )
    with pytest.raises(LlmError) as info:
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert "месячный лимит" in str(info.value)
    assert llm.requests == []
    usage = ctx.ai.usage()  # type: ignore[attr-defined]
    assert usage["budget"] == 1.0 and usage["month"]["cost"] == 1.5


def test_proxy_and_local_config(ctx: AppContext) -> None:
    ctx.settings.patch({"ai": {"proxy": "socks5://127.0.0.1:1080"}})
    cfg = resolve_config(ctx.settings.value.ai, ctx.secrets)
    assert cfg.proxy == "socks5://127.0.0.1:1080"
    client = ctx.ai.llm._client(cfg)  # type: ignore[attr-defined]
    assert isinstance(client, httpx.AsyncClient)
    ctx.settings.patch({"ai": {"provider": "ollama"}})
    local = resolve_config(ctx.settings.value.ai, ctx.secrets)
    assert local.local and local.proxy == "" and local.model == "qwen3:8b"
    ctx.settings.patch({"ai": {"provider": "openai", "proxy": "bad://proxy"}})
    with pytest.raises(LlmError):
        ctx.ai.llm._client(resolve_config(ctx.settings.value.ai, ctx.secrets))  # type: ignore[attr-defined]


def test_prices() -> None:
    assert price_for("openai/gpt-5.4-mini") == (0.75, 4.50)
    assert price_for("gpt-5.4-mini-2026-03-17") == (0.75, 4.50)
    assert price_for("anthropic/claude-sonnet-5.5") == (2.00, 10.00)
    assert price_for("qwen3:8b") is None
    assert estimate_cost("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert estimate_cost("gpt-5.4", 1000, 1000, local=True) == 0.0


async def test_list_models_and_test(ctx: AppContext, llm: ScriptedLlm) -> None:
    models = {"data": [{"id": "gpt-5.4"}, {"id": "text-embedding-3-small"}, {"id": "gpt-5.4-mini"}]}
    llm.routes["api.openai.com"] = lambda request: httpx.Response(200, json=models)
    assert await ctx.ai.llm.list_models() == ["gpt-5.4", "gpt-5.4-mini"]  # type: ignore[attr-defined]
    llm.routes["api.openai.com"] = lambda request: httpx.Response(500)
    assert "gpt-5.4-nano" in await ctx.ai.llm.list_models()  # type: ignore[attr-defined]
    llm.add(reply("Готов"))
    result = await ctx.ai.llm.test()  # type: ignore[attr-defined]
    assert result["ok"] is True and result["latency_ms"] >= 0
    llm.add(httpx.Response(401, json={"error": {"message": "bad key"}}))
    assert (await ctx.ai.llm.test())["error"] == "Ключ API не подходит"  # type: ignore[attr-defined]


async def test_stream_error_event_inside_stream(ctx: AppContext, llm: ScriptedLlm) -> None:
    body = sse([{"error": {"message": "Upstream overloaded", "code": 529}}], done=False)
    llm.add(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body),
        httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(stream_chunks(reply("Ок")))
        ),
    )
    assert (await ctx.ai.llm.chat(MESSAGES, stream=True)).content == "Ок"  # type: ignore[attr-defined]


def test_user_content_images() -> None:
    url = "data:image/png;base64,AAAA"
    parts = llm_module.user_content("Что это?", [url], vision=True)
    assert isinstance(parts, list) and parts[1] == {"type": "image_url", "image_url": {"url": url}}
    text = llm_module.user_content("Что это?", [url], vision=False)
    assert isinstance(text, str) and "Зрение" in text
    assert json.dumps(parts)
