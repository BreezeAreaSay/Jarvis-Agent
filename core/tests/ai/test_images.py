"""Картинки Jarvis AI: запрос к OpenAI Images (httpx.MockTransport вместо сети), ключи, ошибки, расход, лимит,
образец из приложенного фото, инструмент generate_image в чате и маршруты /api/images/*."""

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.ai.images import NO_KEY, ImageError, Images, decode_data_url, estimate_cost
from jarvis.ai.tools import ToolEnv
from jarvis.ai.web import WebClient
from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, calls, reply
from tests.conftest import EventLog

PIXEL = base64.b64encode(b"\xff\xd8\xff\xe0 fake jpeg").decode()
PHOTO = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake photo").decode()
USAGE = {
    "input_tokens": 60,
    "input_tokens_details": {"text_tokens": 20, "image_tokens": 40},
    "output_tokens": 1056,
    "total_tokens": 1116,
}


class OpenAiImages:
    """Подставной OpenAI: запоминает запросы, отвечает картинкой или ошибкой."""

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json=self.body or {})
        body = self.body or {
            "created": 1,
            "data": [{"b64_json": PIXEL}],
            "output_format": "jpeg",
            "usage": USAGE,
        }
        return httpx.Response(200, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _service(ctx: AppContext, api: OpenAiImages, key: str = "sk-images") -> Images:
    if key:
        ctx.secrets.set("openai_api_key", key)
    service = ctx.images
    service.transport = api.transport
    return service


def test_helpers() -> None:
    data, mime = decode_data_url(PHOTO)
    assert data.startswith(b"\x89PNG") and mime == "image/png"
    with pytest.raises(ImageError):
        decode_data_url("https://example.com/cat.png")
    # по usage ответа: 20 текстовых и 40 картиночных токенов на входе, 1056 на выходе
    assert estimate_cost(USAGE, "medium", "1024x1024") == (
        60,
        1056,
        round((20 * 5 + 40 * 10 + 1056 * 40) / 1e6, 6),
    )
    # без usage — по таблице качества и размера
    assert estimate_cost(None, "high", "1536x1024") == (0, 6240, round(6240 * 40 / 1e6, 6))


async def test_generate_sends_request_and_records_cost(ctx: AppContext) -> None:
    api = OpenAiImages()
    service = _service(ctx, api)
    result = await service.generate("  неоновый   город  ", size="1536x1024", quality="high")

    assert result["image"] == f"data:image/jpeg;base64,{PIXEL}"
    assert result["info"] == {
        "prompt": "неоновый город",
        "size": "1536x1024",
        "model": "gpt-image-1",
        "quality": "high",
    }
    request = api.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/images/generations"
    assert request.headers["authorization"] == "Bearer sk-images"
    sent = json.loads(request.content)
    assert sent == {
        "model": "gpt-image-1",
        "prompt": "неоновый город",
        "n": 1,
        "size": "1536x1024",
        "quality": "high",
        "output_format": "jpeg",
        "output_compression": 90,
    }
    row = ctx.db.query_one("SELECT * FROM usage")
    assert row is not None and row["provider"] == "openai" and row["model"] == "gpt-image-1"
    assert row["completion_tokens"] == 1056 and row["cost"] == pytest.approx(result["cost"])


async def test_key_and_address(ctx: AppContext) -> None:
    api = OpenAiImages()
    service = _service(ctx, api, key="")
    assert service.status() == {"available": False, "model": "gpt-image-1", "problem": NO_KEY}
    with pytest.raises(ImageError, match="ключ OpenAI"):
        await service.generate("кот")
    assert api.requests == []

    # ИИ работает через OpenAI со своим адресом — его ключ и адрес подходят и для картинок
    ctx.settings.patch({"ai": {"provider": "openai", "base_url": "https://proxy.example.com/v1/"}})
    ctx.secrets.set("ai_api_key", "sk-chat")
    await service.generate("кот")
    assert str(api.requests[-1].url) == "https://proxy.example.com/v1/images/generations"
    assert api.requests[-1].headers["authorization"] == "Bearer sk-chat"

    # отдельный ключ OpenAI важнее ключа провайдера; другой провайдер — адрес OpenAI по умолчанию
    ctx.secrets.set("openai_api_key", "sk-own")
    ctx.settings.patch({"ai": {"provider": "openrouter", "base_url": ""}})
    await service.generate("кот")
    assert str(api.requests[-1].url) == "https://api.openai.com/v1/images/generations"
    assert api.requests[-1].headers["authorization"] == "Bearer sk-own"
    assert service.status()["available"] is True


async def test_reference_photo_uses_edits(ctx: AppContext) -> None:
    api = OpenAiImages()
    service = _service(ctx, api)
    await service.generate("я — космонавт", references=[PHOTO])
    request = api.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/images/edits"
    body = request.content
    assert b'name="image"; filename="reference-1.png"' in body and b"\x89PNG fake photo" in body
    assert b'name="input_fidelity"' in body and b'name="output_format"\r\n\r\njpeg' in body

    await service.generate("мы вдвоём", references=[PHOTO, PHOTO])
    assert b'name="image[]"; filename="reference-2.png"' in api.requests[1].content


@pytest.mark.parametrize(
    ("status", "body", "text"),
    [
        (
            401,
            {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}},
            "неверный ключ",
        ),
        (
            400,
            {"error": {"message": "Your request was rejected", "code": "moderation_blocked"}},
            "безопасности",
        ),
        (403, {"error": {"message": "Your organization must be verified to use gpt-image-1"}}, "подтвердите"),
        (429, {"error": {"message": "quota"}}, "нет денег"),
        (500, {}, "не отвечает"),
    ],
)
async def test_errors(ctx: AppContext, status: int, body: dict[str, Any], text: str) -> None:
    service = _service(ctx, OpenAiImages(status, body))
    with pytest.raises(ImageError, match=text):
        await service.generate("кот")
    assert ctx.db.query("SELECT * FROM usage") == []


async def test_timeout_and_empty_answer(ctx: AppContext) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("долго", request=request)

    service = _service(ctx, OpenAiImages())
    service.transport = httpx.MockTransport(slow)
    with pytest.raises(ImageError, match="слишком долго"):
        await service.generate("кот")
    service.transport = OpenAiImages(body={"created": 1, "data": []}).transport
    with pytest.raises(ImageError, match="непонятный ответ"):
        await service.generate("кот")


async def test_budget_checked_before_request(ctx: AppContext) -> None:
    api = OpenAiImages()
    service = _service(ctx, api)
    ctx.settings.patch({"ai": {"monthly_budget": 1.0}})
    ctx.db.insert("usage", {"ts": 9e9, "provider": "openai", "model": "gpt-5.4-mini", "cost": 1.5})
    with pytest.raises(ImageError, match="месячный лимит"):
        await service.generate("кот")
    assert api.requests == []


async def test_tool_attaches_image_to_reply(ctx: AppContext, events: EventLog) -> None:
    """Без модели: chat.tool → инструмент → chat.image → итог ответа получает meta.images."""
    service = _service(ctx, OpenAiImages())
    chats = ctx.ai.chats  # type: ignore[attr-defined]
    chat = chats.create(title="Картинки")
    chats.add_message(chat["id"], "user", "нарисуй меня в космосе", meta={"images": [PHOTO]})
    ctx.bus.publish(
        "chat.tool",
        {"chat_id": chat["id"], "message_id": "reply-1", "name": "generate_image", "status": "running"},
    )
    env = ToolEnv(ctx, WebClient(ctx), chat_id=chat["id"], user_text="нарисуй меня в космосе")
    out = await service.tool(env, {"prompt": "я в космосе", "format": "portrait", "use_reference": True})
    assert out.ok and out.title == "Нарисовать: я в космосе" and "Скачать" in out.content
    shown = events.of("chat.image")[-1]
    assert shown["chat_id"] == chat["id"] and shown["message_id"] == "reply-1"
    assert shown["image"].startswith("data:image/jpeg;base64,") and shown["info"]["size"] == "1024x1536"

    # ядро сохраняет итог ответа — картинка дописывается в его meta, интерфейс получает chat.message ещё раз
    from jarvis.ai.chats import ui_message

    row = chats.add_message(
        chat["id"], "assistant", "Готово, сэр.", meta={"turn": "u1"}, message_id="reply-1"
    )
    ctx.bus.publish("chat.message", {"chat_id": chat["id"], "message": ui_message(row)})
    saved = chats.message(chat["id"], "reply-1")
    assert saved["meta"]["images"] == [shown["image"]]
    assert saved["meta"]["generated"][0]["prompt"] == "я в космосе" and saved["meta"]["turn"] == "u1"
    await asyncio.sleep(0)
    again = [e["message"] for e in events.of("chat.message") if e["message"]["meta"].get("images")]
    assert again and again[-1]["id"] == "reply-1" and again[-1]["meta"]["images"] == [shown["image"]]
    assert "turn" not in again[-1]["meta"]


async def test_tool_reference_missing(ctx: AppContext) -> None:
    service = _service(ctx, OpenAiImages())
    chat = ctx.ai.chats.create()  # type: ignore[attr-defined]
    env = ToolEnv(ctx, WebClient(ctx), chat_id=chat["id"])
    out = await service.tool(env, {"prompt": "я в космосе", "use_reference": True})
    assert not out.ok and "приложить фото" in out.content


async def test_chat_draws_picture(ctx: AppContext, llm: ScriptedLlm, events: EventLog) -> None:
    """Целиком: модель зовёт generate_image, ответ в чате — с картинкой и без карточки «Выполнено»."""
    api = OpenAiImages()
    ctx.images.transport = api.transport
    llm.add(calls(("generate_image", {"prompt": "кот в шляпе"})), reply("Готово, сэр — кот в шляпе."))
    # «нарисуй …» в режиме «Всё вместе» — чату, а не агенту (apphints.app_task)
    result = await ctx.ai.ask("Нарисуй кота в шляпе")
    assert result.error == ""
    assert [(e["status"], e["name"]) for e in events.of("chat.tool")] == [
        ("running", "generate_image"),
        ("ok", "generate_image"),
    ]
    await asyncio.sleep(0)  # повтор chat.message с картинкой уходит следом за итогом
    message = events.of("chat.message")[-1]["message"]
    assert message["role"] == "assistant" and message["content"] == "Готово, сэр — кот в шляпе."
    assert message["meta"]["images"][0].startswith("data:image/jpeg;base64,")
    assert events.types().index("chat.done") < len(events.types()) - 1 - events.types()[::-1].index(
        "chat.message"
    )
    saved = ctx.ai.chats.message(result.chat_id, message["id"])  # type: ignore[attr-defined]
    assert saved["meta"]["images"] == message["meta"]["images"]
    assert message["meta"]["actions"] == [
        {"title": "Нарисовать: кот в шляпе", "ok": True, "detail": "Готово"}
    ]
    assert str(api.requests[0].url).endswith("/images/generations")


async def test_privacy_clears_generated_images(ctx: AppContext) -> None:
    chats = ctx.ai.chats  # type: ignore[attr-defined]
    chat = chats.create()
    chats.add_message(chat["id"], "user", "нарисуй кота")
    row = chats.add_message(
        chat["id"],
        "assistant",
        "Готово",
        meta={"images": [f"data:image/jpeg;base64,{PIXEL}"], "generated": [{"prompt": "кот"}]},
    )
    inventory = {i["id"]: i for i in ctx.retention.inventory()["items"]}
    assert inventory["images"]["count"] == 1 and inventory["images"]["bytes"] > 0
    cleared = await ctx.retention.clear(["images"])
    assert cleared == {"images": 1}
    meta = chats.message(chat["id"], row["id"])["meta"]
    assert "images" not in meta and "generated" not in meta and meta["images_removed"] == 1
    assert chats.message(chat["id"], row["id"])["content"] == "Готово"


def test_api(client: TestClient) -> None:
    ctx: AppContext = client.ctx  # type: ignore[attr-defined]
    r = client.get("/api/images/status")
    assert r.status_code == 200 and r.json()["available"] is False
    r = client.post("/api/images/generate", json={"prompt": "кот"})
    assert r.status_code == 400 and "ключ OpenAI" in r.json()["detail"]

    api = OpenAiImages()
    _service(ctx, api)
    r = client.post("/api/images/generate", json={"prompt": "кот", "format": "landscape", "quality": "low"})
    assert r.status_code == 200
    assert r.json()["image"].startswith("data:image/jpeg;base64,") and r.json()["info"]["size"] == "1536x1024"
    assert client.post("/api/images/generate", json={"prompt": ""}).status_code == 422
    assert client.post("/api/images/generate", json={"prompt": "кот", "format": "huge"}).status_code == 422
