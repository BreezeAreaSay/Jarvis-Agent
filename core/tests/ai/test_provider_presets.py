"""Провайдеры по ключу API: описание для настроек и особенности запросов каждого (заголовки, тело, id
вызовов, схемы инструментов) — через httpx.MockTransport, без сети."""

import json
from typing import Any

import httpx
import pytest

from jarvis.ai.llm import GEMINI_SKIP_SIGNATURE, NO_FOLDER, LlmError, short_tool_id
from jarvis.ai.providers import PRESETS
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, calls, completion, reply, sse

MESSAGES = [{"role": "user", "content": "Привет"}]
TOOL = {
    "type": "function",
    "function": {
        "name": "notes_add",
        "description": "Заметка",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 80, "description": "Заголовок"},
                "tags": {"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 5},
            },
            "required": ["title"],
        },
    },
}


def use(ctx: AppContext, provider: str, **ai: Any) -> None:
    ctx.settings.patch({"ai": {"provider": provider, "model": "", "base_url": "", **ai}})


def capture(llm: ScriptedLlm, answer: Any = None) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion(answer or reply("Готово"), body.get("model", "")))

    llm.add(handler)
    return seen


def test_every_preset_describes_itself_for_the_settings_page() -> None:
    keys = {
        "id",
        "title",
        "base_url",
        "needs_key",
        "default_model",
        "models",
        "local",
        "key_url",
        "login",
        "component",
        "cli",
        "group",
        "country",
        "regions",
        "folder",
    }
    for preset in PRESETS.values():
        data = preset.to_json()
        assert set(data) == keys, preset.id
        json.dumps(data)  # уходит в API как есть
        assert data["group"] in ("account", "key", "local", "custom")
        if data["group"] == "key":
            assert (
                data["needs_key"]
                and data["base_url"].startswith("https://")
                and data["key_url"].startswith("https://")
            )
            assert data["default_model"] in data["models"], preset.id
        if data["cli"]:
            assert (
                data["group"] == "account"
                and data["login"]
                and not data["needs_key"]
                and not data["base_url"]
            )
        if data["regions"]:
            # регион по умолчанию — первый, его адрес = адрес пресета; у каждого свой кабинет для ключа
            assert data["regions"][0]["base_url"] == data["base_url"]
            assert all(
                r["id"] and r["title"] and r["base_url"].startswith("https://") for r in data["regions"]
            )
            assert len({r["id"] for r in data["regions"]}) == len(data["regions"])
    assert PRESETS["qwen"].region("https://dashscope.aliyuncs.com/compatible-mode/v1/").id == "cn"  # type: ignore[union-attr]
    assert PRESETS["qwen"].region("").id == "intl"  # type: ignore[union-attr]
    assert PRESETS["qwen"].region("https://example.com/v1") is None


@pytest.mark.parametrize(
    ("provider", "url", "model"),
    [
        (
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "gemini-3.5-flash",
        ),
        ("xai", "https://api.x.ai/v1/chat/completions", "grok-4.7"),
        ("mistral", "https://api.mistral.ai/v1/chat/completions", "mistral-large-latest"),
        ("deepseek", "https://api.deepseek.com/chat/completions", "deepseek-v4-flash"),
        ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen3.7-plus"),
        ("moonshot", "https://api.moonshot.ai/v1/chat/completions", "kimi-k2.6"),
        ("zhipu", "https://api.z.ai/api/paas/v4/chat/completions", "glm-5.2"),
        ("minimax", "https://api.minimax.io/v1/chat/completions", "MiniMax-M3"),
        ("doubao", "https://ark.cn-beijing.volces.com/api/v3/chat/completions", "doubao-seed-2-1-pro-260628"),
        ("siliconflow", "https://api.siliconflow.com/v1/chat/completions", "deepseek-ai/DeepSeek-V4-Flash"),
    ],
)
async def test_key_providers_use_bearer_and_their_own_address(
    ctx: AppContext, llm: ScriptedLlm, provider: str, url: str, model: str
) -> None:
    use(ctx, provider)
    seen = capture(llm)
    result = await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert result.content == "Готово"
    assert str(seen[0].url) == url and seen[0].headers["authorization"] == "Bearer sk-test"
    assert llm.requests[0]["model"] == model and "OpenAI-Project" not in seen[0].headers
    assert "x-data-logging-enabled" not in seen[0].headers


async def test_region_switches_the_address(ctx: AppContext, llm: ScriptedLlm) -> None:
    use(ctx, "moonshot", base_url="https://api.moonshot.cn/v1")
    seen = capture(llm)
    await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert str(seen[0].url) == "https://api.moonshot.cn/v1/chat/completions"
    # регион в настройках — это просто адрес: страница настроек узнаёт его по base_url
    assert PRESETS["moonshot"].region(ctx.settings.value.ai.base_url).id == "cn"  # type: ignore[union-attr]


async def test_yandexgpt_api_key_header_folder_and_model_uri(ctx: AppContext, llm: ScriptedLlm) -> None:
    ctx.secrets.set("ai_api_key", "AQVN-secret")
    use(ctx, "yandex")
    with pytest.raises(LlmError, match="ID каталога"):
        await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert llm.requests == [] and ctx.ai.configured is False  # type: ignore[attr-defined]
    assert ctx.ai.llm.problem() == NO_FOLDER  # type: ignore[attr-defined]

    use(ctx, "yandex", folder_id="b1gfolder")
    assert ctx.ai.configured is True  # type: ignore[attr-defined]
    seen = capture(llm, reply('{"ok": true}'))
    await ctx.ai.llm.chat(MESSAGES, json_schema={"type": "object"})  # type: ignore[attr-defined]
    request = seen[0]
    assert str(request.url) == "https://llm.api.cloud.yandex.net/v1/chat/completions"
    assert request.headers["authorization"] == "Api-Key AQVN-secret"
    assert request.headers["openai-project"] == "b1gfolder"
    assert request.headers["x-data-logging-enabled"] == "false"  # Yandex не сохраняет запросы у себя
    body = llm.requests[0]
    assert body["model"] == "gpt://b1gfolder/yandexgpt/latest" and "response_format" not in body
    assert "stream_options" not in body

    # IAM-токен вместо API-ключа — Bearer; полное имя модели не переписывается
    ctx.secrets.set("ai_api_key", "t1.iam-token")
    use(ctx, "yandex", folder_id="b1gfolder", model="gpt://b1gother/yandexgpt-lite/latest")
    seen = capture(llm)
    await ctx.ai.llm.chat(MESSAGES)  # type: ignore[attr-defined]
    assert seen[0].headers["authorization"] == "Bearer t1.iam-token"
    assert llm.requests[-1]["model"] == "gpt://b1gother/yandexgpt-lite/latest"

    # список моделей: без префикса каталога
    models = {
        "data": [{"id": "gpt://b1gfolder/yandexgpt/latest"}, {"id": "gpt://b1gfolder/aliceai-llm/latest"}]
    }
    llm.routes["llm.api.cloud.yandex.net"] = lambda request: httpx.Response(200, json=models)
    assert await ctx.ai.llm.list_models() == ["aliceai-llm/latest", "yandexgpt/latest"]  # type: ignore[attr-defined]


async def test_mistral_gets_nine_character_tool_call_ids(ctx: AppContext, llm: ScriptedLlm) -> None:
    use(ctx, "mistral")
    history = [
        *MESSAGES,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_8f2a9c1b7e", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_8f2a9c1b7e", "content": "ok"},
    ]
    llm.add(reply("Готово"))
    await ctx.ai.llm.chat(history, tools=[TOOL], stream=True)  # type: ignore[attr-defined]
    sent = llm.requests[0]
    call_id = sent["messages"][1]["tool_calls"][0]["id"]
    assert len(call_id) == 9 and call_id.isalnum() and sent["messages"][2]["tool_call_id"] == call_id
    assert call_id == short_tool_id("call_8f2a9c1b7e") and short_tool_id("abcDEF123") == "abcDEF123"
    assert "stream_options" not in sent  # расход Mistral присылает сам
    assert history[1]["tool_calls"][0]["id"] == "call_8f2a9c1b7e"  # история чата не тронута


async def test_doubao_tools_lose_length_limits_only(ctx: AppContext, llm: ScriptedLlm) -> None:
    use(ctx, "doubao")
    llm.add(reply("Готово"))
    await ctx.ai.llm.chat(MESSAGES, tools=[TOOL])  # type: ignore[attr-defined]
    params = llm.requests[0]["tools"][0]["function"]["parameters"]
    text = json.dumps(params)
    assert "minLength" not in text and "maxLength" not in text and "maxItems" not in text
    assert params["required"] == ["title"] and params["properties"]["title"]["description"] == "Заголовок"
    assert TOOL["function"]["parameters"]["properties"]["title"]["maxLength"] == 80  # type: ignore[index]
    use(ctx, "openai")
    llm.add(reply("Готово"))
    await ctx.ai.llm.chat(MESSAGES, tools=[TOOL])  # type: ignore[attr-defined]
    assert "maxLength" in json.dumps(llm.requests[1]["tools"])


async def test_gemini_thought_signatures_go_back_with_tool_calls(ctx: AppContext, llm: ScriptedLlm) -> None:
    use(ctx, "gemini")
    signature = {"google": {"thought_signature": "c2lnbmF0dXJl"}}
    answer = completion(calls(("get_time", {})))
    answer["choices"][0]["message"]["tool_calls"][0]["extra_content"] = signature
    llm.add(httpx.Response(200, json=answer))
    result = await ctx.ai.llm.chat(MESSAGES, tools=[TOOL])  # type: ignore[attr-defined]
    call = result.tool_calls[0]
    assert call.extra == signature and call.to_message()["extra_content"] == signature

    # то же в потоке: подпись приходит в куске вызова
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_time", "arguments": "{}"},
                            "extra_content": signature,
                        }
                    ]
                },
            }
        ]
    }
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
    llm.add(httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([chunk, done])))
    streamed = await ctx.ai.llm.chat(MESSAGES, tools=[TOOL], stream=True)  # type: ignore[attr-defined]
    assert streamed.tool_calls[0].extra == signature

    # вызовы без подписи (сделаны другой моделью) — заглушка у первого, иначе Gemini 3 отвечает 400
    history = [
        *MESSAGES,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "g", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]
    llm.add(reply("Готово"))
    await ctx.ai.llm.chat(history, tools=[TOOL])  # type: ignore[attr-defined]
    sent = llm.requests[-1]["messages"][1]["tool_calls"]
    assert sent[0]["extra_content"] == {"google": {"thought_signature": GEMINI_SKIP_SIGNATURE}}
    assert "extra_content" not in sent[1]

    # другим провайдерам служебное поле Gemini не уходит
    use(ctx, "openai")
    signed = [
        *MESSAGES,
        {**history[1], "tool_calls": [call.to_message()]},
        {"role": "tool", "tool_call_id": call.id, "content": "1"},
    ]
    llm.add(reply("Готово"))
    await ctx.ai.llm.chat(signed, tools=[TOOL])  # type: ignore[attr-defined]
    assert "extra_content" not in llm.requests[-1]["messages"][1]["tool_calls"][0]


def test_retired_deepseek_models_move_to_v4(ctx: AppContext) -> None:
    use(ctx, "deepseek", model="deepseek-chat")
    assert ctx.ai.llm.config().model == "deepseek-v4-flash"  # type: ignore[attr-defined]
    use(ctx, "deepseek", model="deepseek-v4-pro")
    assert ctx.ai.llm.config().model == "deepseek-v4-pro"  # type: ignore[attr-defined]
    # модель от прежнего провайдера по умолчанию — заменяется моделью нового
    use(ctx, "zhipu", model="gpt-5.4-mini")
    assert ctx.ai.llm.config().model == "glm-5.2"  # type: ignore[attr-defined]
