"""Движки синтеза: запросы OpenAI, Fish Audio и ElevenLabs (MockTransport), Edge (подмена edge-tts),
piper и Windows."""

import json
import sys
import time
from typing import Any

import httpx
import numpy as np
import pytest

from jarvis.context import AppContext
from jarvis.voice import dsp
from jarvis.voice.engines import (
    EdgeEngine,
    ElevenLabsEngine,
    FishEngine,
    OpenAIEngine,
    PiperEngine,
    SynthOptions,
    WindowsEngine,
)
from jarvis.voice.engines import edge as edge_mod
from jarvis.voice.engines import elevenlabs as el
from jarvis.voice.engines.base import TtsEngineError
from jarvis.voice.models import ModelManager


def pcm(seconds: float, rate: int) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return dsp.float_to_pcm16((0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32))


async def test_openai_request_and_pcm(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=pcm(0.5, 24000))

    ctx.secrets.set("ai_api_key", "sk-ai")  # провайдер ИИ — OpenAI: годится и его ключ
    ctx.settings.patch({"tts": {"speed": 1.2, "pitch": 1.0}})
    engine = OpenAIEngine(ctx, transport=httpx.MockTransport(handler))
    assert engine.available() == ""
    samples, rate = await engine.synthesize("Hello, sir.", SynthOptions("onyx", 1.2, 1.0, ctx.settings.value))
    assert rate == 24000 and abs(len(samples) - 12000) <= 1
    req = seen[0]
    assert str(req.url) == "https://api.openai.com/v1/audio/speech"
    assert req.headers["authorization"] == "Bearer sk-ai"
    body = json.loads(req.content)
    assert body["model"] == "gpt-4o-mini-tts" and body["voice"] == "onyx" and body["input"] == "Hello, sir."
    assert body["response_format"] == "pcm" and body["speed"] == 1.2
    assert "J.A.R.V.I.S." in body["instructions"]


async def test_openai_prefers_own_key_tts1_no_instructions_and_pitch(ctx: AppContext) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"auth": request.headers["authorization"], **json.loads(request.content)})
        return httpx.Response(200, content=pcm(0.2, 24000))

    ctx.secrets.set("openai_api_key", "sk-tts")
    ctx.settings.patch({"ai": {"provider": "deepseek"}, "tts": {"openai_model": "tts-1"}})
    engine = OpenAIEngine(ctx, transport=httpx.MockTransport(handler))
    _, rate = await engine.synthesize("Тест", SynthOptions("echo", 1.0, 0.9, ctx.settings.value))
    assert seen[0]["auth"] == "Bearer sk-tts"
    assert "instructions" not in seen[0]  # tts-1 указаний не понимает
    assert rate == round(24000 * 0.9) and seen[0]["speed"] == round(1 / 0.9, 2)  # тон ниже, темп прежний


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (401, {"error": {"message": "Incorrect API key"}}, "неверный ключ"),
        (403, {"error": {"message": "unsupported_country"}}, "прокси"),
        (429, {"error": {"message": "quota"}}, "лимит"),
        (400, {"error": {"message": "bad voice"}}, "bad voice"),
    ],
)
async def test_openai_errors(ctx: AppContext, status: int, body: dict[str, Any], message: str) -> None:
    ctx.secrets.set("openai_api_key", "sk")
    engine = OpenAIEngine(ctx, transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))
    with pytest.raises(TtsEngineError, match=message):
        await engine.synthesize("Тест", SynthOptions("onyx"))


async def test_openai_without_key(ctx: AppContext) -> None:
    ctx.settings.patch({"ai": {"provider": "deepseek"}})
    ctx.secrets.set("ai_api_key", "sk-deepseek")  # чужой провайдер — его ключ OpenAI не подходит
    engine = OpenAIEngine(ctx)
    assert "ключа" in engine.available()
    with pytest.raises(TtsEngineError, match="ключа"):
        await engine.synthesize("Тест", SynthOptions("onyx"))


async def test_openai_network_error(ctx: AppContext) -> None:
    ctx.secrets.set("openai_api_key", "sk")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    engine = OpenAIEngine(ctx, transport=httpx.MockTransport(handler))
    with pytest.raises(TtsEngineError, match="нет связи"):
        await engine.synthesize("Тест", SynthOptions("onyx"))


async def test_fish_request_and_wav(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        wav = dsp.encode_wav(dsp.pcm16_to_float(pcm(0.4, 44100)), 44100)
        return httpx.Response(200, content=wav, headers={"content-type": "audio/wav"})

    ctx.secrets.set("fish_api_key", "fish-key")
    ctx.settings.patch({"tts": {"fish_voice_id": "abc123", "fish_model": "speech-1.6"}})
    engine = FishEngine(ctx, transport=httpx.MockTransport(handler))
    samples, rate = await engine.synthesize(
        "Добрый день, сэр.", SynthOptions("", 1.0, 1.0, ctx.settings.value)
    )
    assert rate == 44100 and abs(len(samples) - int(0.4 * 44100)) <= 1
    req = seen[0]
    assert str(req.url) == "https://api.fish.audio/v1/tts"
    assert req.headers["authorization"] == "Bearer fish-key" and req.headers["model"] == "speech-1.6"
    body = json.loads(req.content)
    assert body["text"] == "Добрый день, сэр." and body["reference_id"] == "abc123"
    assert body["format"] == "wav" and body["latency"] == "normal" and "prosody" not in body


@pytest.mark.parametrize(
    ("status", "message"), [(401, "неверный ключ"), (402, "нет средств"), (404, "не найден"), (429, "много")]
)
async def test_fish_errors(ctx: AppContext, status: int, message: str) -> None:
    ctx.secrets.set("fish_api_key", "k")
    engine = FishEngine(
        ctx, transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"message": "x"}))
    )
    with pytest.raises(TtsEngineError, match=message):
        await engine.synthesize("Тест", SynthOptions("v"))


async def test_fish_voices(ctx: AppContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("self") == "true":
            return httpx.Response(200, json={"total": 1, "items": [{"_id": "mine", "title": "Мой Джарвис"}]})
        return httpx.Response(200, json={"items": [{"_id": "pop", "title": "Диктор", "languages": ["ru"]}]})

    ctx.secrets.set("fish_api_key", "k")
    ctx.settings.patch({"tts": {"fish_voice_id": "configured-id"}})
    voices = await FishEngine(ctx, transport=httpx.MockTransport(handler)).voices()
    assert [v["id"] for v in voices] == ["configured-id", "mine", "pop"]
    assert voices[2]["lang"] == "ru"


async def test_edge_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeCommunicate:
        def __init__(self, text: str, voice: str, *, rate: str, pitch: str) -> None:
            created.append({"text": text, "voice": voice, "rate": rate, "pitch": pitch})

        async def stream(self):  # type: ignore[no-untyped-def]
            yield {"type": "WordBoundary", "offset": 0}
            yield {"type": "audio", "data": b"mp3-1"}
            yield {"type": "audio", "data": b"mp3-2"}

    decoded: list[bytes] = []

    def fake_decode(data: bytes, rate: int = 24000) -> tuple[np.ndarray, int]:
        decoded.append(data)
        return np.zeros(2400, dtype=np.float32), 24000

    monkeypatch.setattr(edge_mod, "decode_mp3", fake_decode)
    engine = EdgeEngine(communicate=FakeCommunicate)
    samples, rate = await engine.synthesize("Привет", SynthOptions("ru-RU-DmitryNeural", 1.1, 0.9))
    assert rate == 24000 and len(samples) == 2400
    assert created == [{"text": "Привет", "voice": "ru-RU-DmitryNeural", "rate": "+10%", "pitch": "-10Hz"}]
    assert decoded == [b"mp3-1mp3-2"]
    assert engine.russian("ru-RU-DmitryNeural") and not engine.russian("en-US-GuyNeural")


async def test_edge_errors_and_voices_fallback() -> None:
    class Broken:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise ValueError("Invalid voice")

    with pytest.raises(TtsEngineError, match="неверный голос"):
        await EdgeEngine(communicate=Broken).synthesize("Привет", SynthOptions("bad"))

    async def offline() -> list[dict[str, Any]]:
        raise OSError("no network")

    voices = await EdgeEngine(list_voices=offline).voices()
    assert voices[0]["id"] == "ru-RU-DmitryNeural"

    def voice(short: str, friendly: str, locale: str, gender: str) -> dict[str, Any]:
        return {"ShortName": short, "FriendlyName": friendly, "Locale": locale, "Gender": gender}

    async def listing() -> list[dict[str, Any]]:
        return [
            voice("en-US-GuyNeural", "Microsoft Guy Online (Natural)", "en-US", "Male"),
            voice("ru-RU-SvetlanaNeural", "Microsoft Svetlana Online (Natural)", "ru-RU", "Female"),
        ]

    voices = await EdgeEngine(list_voices=listing).voices()
    assert voices[0] == {
        "id": "ru-RU-SvetlanaNeural",
        "name": "Svetlana",
        "lang": "ru-RU",
        "gender": "female",
    }


def test_edge_decodes_real_mp3_module_available() -> None:
    import miniaudio

    assert hasattr(miniaudio, "decode")


async def test_piper_without_model(tmp_path: Any) -> None:
    engine = PiperEngine(ModelManager(root=tmp_path))
    assert engine.available() != "" and engine.pick("ruslan") == ""
    with pytest.raises(TtsEngineError, match="не скачан"):
        await engine.synthesize("Привет", SynthOptions("ruslan"))
    voices = await engine.voices()
    assert [v["id"] for v in voices] == ["ruslan", "denis", "dmitri", "irina"]
    assert all(v["installed"] is False for v in voices)
    engine.close()


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows голоса есть")
async def test_windows_engine_off_windows() -> None:
    engine = WindowsEngine()
    assert "Windows" in engine.available()
    assert await engine.voices() == []
    with pytest.raises(TtsEngineError):
        await engine.synthesize("Привет", SynthOptions(""))
    assert engine.russian("") and engine.russian("Microsoft Irina") and not engine.russian("Microsoft David")
    engine.close()


# --- ElevenLabs ------------------------------------------------------------------------------------


def eleven(ctx: AppContext, handler: Any) -> ElevenLabsEngine:
    ctx.secrets.set("elevenlabs_api_key", "sk_el")
    return ElevenLabsEngine(ctx, transport=httpx.MockTransport(handler))


async def test_elevenlabs_request_and_pcm(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=pcm(0.5, 24000), headers={"content-type": "audio/pcm"})

    ctx.settings.patch({"tts": {"speed": 1.1}})
    engine = eleven(ctx, handler)
    assert engine.available() == "" and engine.voice(ctx.settings.value) == el.DEFAULT_VOICE
    assert not engine.russian(el.DEFAULT_VOICE)  # латиницу сервис читает сам
    opts = SynthOptions("", 1.1, 1.0, ctx.settings.value)
    samples, rate = await engine.synthesize("Добрый день, сэр.", opts)
    assert rate == 24000 and abs(len(samples) - 12000) <= 1
    req = seen[0]
    assert req.method == "POST" and req.url.path == f"/v1/text-to-speech/{el.DEFAULT_VOICE}"
    assert req.url.host == "api.elevenlabs.io" and req.url.params["output_format"] == "pcm_24000"
    assert req.headers["xi-api-key"] == "sk_el"
    body = json.loads(req.content)
    assert body == {
        "text": "Добрый день, сэр.",
        "model_id": "eleven_flash_v2_5",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": 1.1,
        },
    }


async def test_elevenlabs_pitch_speed_limits_and_v3(ctx: AppContext) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append({"path": request.url.raw_path.decode().split("?")[0], **json.loads(request.content)})
        return httpx.Response(200, content=pcm(0.2, 24000))

    engine = eleven(ctx, handler)
    ctx.settings.patch({"tts": {"elevenlabs_voice_id": "my/voice", "elevenlabs_style": 0.3}})
    _, rate = await engine.synthesize("Тест", SynthOptions("", 1.0, 0.9, ctx.settings.value))
    assert rate == round(24000 * 0.9)  # тон ниже частотой вывода…
    assert bodies[0]["voice_settings"]["speed"] == round(1 / 0.9, 2)  # …а темп выровнен сервисом
    assert bodies[0]["voice_settings"]["style"] == 0.3
    assert bodies[0]["path"] == "/v1/text-to-speech/my%2Fvoice"  # id голоса не ломает адрес
    await engine.synthesize("Тест", SynthOptions("v", 2.0, 1.0, ctx.settings.value))
    assert bodies[1]["voice_settings"]["speed"] == 1.2  # быстрее сервис не умеет
    await engine.synthesize("Тест", SynthOptions("v", 1.0, 1.0, ctx.settings.value))
    assert "speed" not in bodies[2]["voice_settings"]  # обычный темп — без параметра
    ctx.settings.patch({"tts": {"elevenlabs_model": "eleven_v3", "elevenlabs_stability": 0.7}})
    await engine.synthesize("Тест", SynthOptions("v", 1.3, 1.0, ctx.settings.value))
    assert bodies[3]["model_id"] == "eleven_v3"
    assert bodies[3]["voice_settings"] == {"stability": 0.5, "similarity_boost": 0.75}  # v3: 0 / 0,5 / 1


def quota_handler(subscription: httpx.Response) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user/subscription":
            return subscription
        detail = {
            "status": "quota_exceeded",
            "message": "This request exceeds your quota of 10000. You have 25 credits remaining, "
            "while 120 credits are required for this request.",
        }
        return httpx.Response(401, json={"detail": detail})

    return handler


async def test_elevenlabs_quota_waits_until_reset(ctx: AppContext) -> None:
    reset = int(time.time()) + 7200
    engine = eleven(ctx, quota_handler(httpx.Response(200, json={"next_character_count_reset_unix": reset})))
    with pytest.raises(TtsEngineError, match="кредиты") as info:
        await engine.synthesize("Тест", SynthOptions("v"))
    assert info.value.cooldown_s is not None and 7000 < info.value.cooldown_s <= 7200
    no_right = httpx.Response(401, json={"detail": {"status": "missing_permissions"}})
    with pytest.raises(TtsEngineError, match="кредиты") as info:
        await eleven(ctx, quota_handler(no_right)).synthesize("Тест", SynthOptions("v"))
    assert info.value.cooldown_s == el.QUOTA_COOLDOWN_S  # когда обновятся — неизвестно: час


@pytest.mark.parametrize(
    ("status", "body", "message", "cooldown"),
    [
        (401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}, "неверный ключ", 600),
        (
            401,
            {"detail": {"status": "detected_unusual_activity", "message": "Unusual activity detected."}},
            "VPN",
            600,
        ),
        (
            401,
            {
                "detail": {
                    "status": "missing_permissions",
                    "message": "The API key you used is missing the permission text_to_speech",
                }
            },
            "нет права «Text to Speech»",
            600,
        ),
        (
            402,
            {"detail": {"status": "payment_required", "message": "Free users cannot use library voices"}},
            "платном тарифе",
            None,
        ),
        (404, {"detail": {"status": "voice_not_found", "message": "Not found"}}, "голос не найден", None),
        (
            422,
            {"detail": [{"loc": ["body", "model_id"], "msg": "unknown model", "type": "value_error"}]},
            "модель",
            None,
        ),
        (429, {"detail": {"status": "too_many_concurrent_requests"}}, "перегружен", None),
        (403, "Forbidden", "региона", None),
        (500, {"detail": {"status": "internal_error", "message": "boom"}}, "500: boom", None),
    ],
)
async def test_elevenlabs_errors(
    ctx: AppContext, status: int, body: Any, message: str, cooldown: float | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    with pytest.raises(TtsEngineError, match=message) as info:
        await eleven(ctx, handler).synthesize("Тест", SynthOptions("v"))
    assert info.value.cooldown_s == cooldown


async def test_elevenlabs_no_key_json_reply_and_network(ctx: AppContext) -> None:
    engine = ElevenLabsEngine(ctx)
    assert "ключа" in engine.available()
    with pytest.raises(TtsEngineError, match="ключа"):
        await engine.synthesize("Тест", SynthOptions("v"))

    json_reply = eleven(ctx, lambda r: httpx.Response(200, json={"detail": "ok?"}))
    with pytest.raises(TtsEngineError, match="без голоса"):
        await json_reply.synthesize("Тест", SynthOptions("v"))
    empty = eleven(ctx, lambda r: httpx.Response(200, content=b""))
    with pytest.raises(TtsEngineError, match="не вернул звук"):
        await empty.synthesize("Тест", SynthOptions("v"))

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TtsEngineError, match="нет связи"):
        await eleven(ctx, offline).synthesize("Тест", SynthOptions("v"))


async def test_elevenlabs_voices(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        voices = [
            {"voice_id": "own1", "name": "Мой голос", "category": "cloned", "labels": {}},
            {
                "voice_id": el.DEFAULT_VOICE,
                "name": "George - Warm, Captivating Storyteller",
                "category": "premade",
                "labels": {"accent": "british", "gender": "male", "age": "middle aged"},
            },
            {
                "voice_id": "aria",
                "name": "Aria",
                "category": "premade",
                "labels": {"accent": "american", "gender": "female"},
                "verified_languages": [{"language": "ru", "model_id": "eleven_flash_v2_5"}],
            },
        ]
        return httpx.Response(200, json={"voices": voices, "has_more": False, "total_count": 3})

    ctx.settings.patch({"tts": {"elevenlabs_voice_id": "pasted-id"}})
    voices = await eleven(ctx, handler).voices()
    assert seen[0].url.path == "/v2/voices" and seen[0].url.params["page_size"] == "100"
    assert seen[0].headers["xi-api-key"] == "sk_el"
    # вставленный id, потом стандартные голоса, потом свои
    assert [v["id"] for v in voices] == ["pasted-id", "aria", el.DEFAULT_VOICE, "own1"]
    assert voices[1] == {
        "id": "aria",
        "name": "Aria — американский",
        "lang": "ru",
        "gender": "female",
        "category": "premade",
    }
    assert voices[2]["name"] == "George — британский" and voices[2]["gender"] == "male"
    assert voices[3]["name"] == "Мой голос (ваш)" and voices[3]["lang"] == "multi"

    broken = eleven(ctx, lambda r: httpx.Response(401, json={"detail": {"status": "invalid_api_key"}}))
    assert [v["id"] for v in await broken.voices()] == ["pasted-id"]  # ошибка — только свой id
    ctx.secrets.set("elevenlabs_api_key", "")
    ctx.settings.patch({"tts": {"elevenlabs_voice_id": ""}})
    assert await ElevenLabsEngine(ctx).voices() == []  # без ключа — список у интерфейса


async def test_elevenlabs_account(ctx: AppContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user/subscription":
            return httpx.Response(
                200,
                json={
                    "tier": "free",
                    "status": "free",
                    "character_count": 2500,
                    "character_limit": 10000,
                    "next_character_count_reset_unix": 1767225600,
                },
            )
        assert request.url.path == "/v1/models"
        ru = [{"language_id": "ru", "name": "Russian"}, {"language_id": "en", "name": "English"}]
        return httpx.Response(
            200,
            json=[
                {
                    "model_id": "eleven_flash_v2_5",
                    "name": "Eleven Flash v2.5",
                    "can_do_text_to_speech": True,
                    "languages": ru,
                    "model_rates": {"character_cost_multiplier": 0.5},
                },
                {"model_id": "eleven_multilingual_sts_v2", "can_do_text_to_speech": False, "languages": ru},
                {
                    "model_id": "eleven_monolingual_v1",
                    "can_do_text_to_speech": True,
                    "languages": [{"language_id": "en", "name": "English"}],
                },
                {"model_id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2", "languages": ru},
            ],
        )

    account = await eleven(ctx, handler).account()
    assert account == {
        "supported": True,
        "tier": "free",
        "status": "free",
        "used": 2500,
        "limit": 10000,
        "reset_at": "2026-01-01T00:00:00+00:00",
        "models": [
            {"id": "eleven_flash_v2_5", "name": "Eleven Flash v2.5", "cost": 0.5},
            {"id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2"},
        ],
        "error": "",
    }


async def test_elevenlabs_account_without_user_right_and_key(ctx: AppContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user/subscription":
            detail = {"status": "missing_permissions", "message": "missing the permission user_read"}
            return httpx.Response(401, json={"detail": detail})
        return httpx.Response(200, json=[{"model_id": "eleven_flash_v2_5", "can_do_text_to_speech": True}])

    account = await eleven(ctx, handler).account()
    assert "User (Read)" in account["error"] and account["used"] is None
    assert account["models"] == [{"id": "eleven_flash_v2_5", "name": ""}]  # модели видны и без права на счёт
    ctx.secrets.set("elevenlabs_api_key", "")
    bare = await ElevenLabsEngine(ctx).account()
    assert bare["supported"] is True and "ключа" in bare["error"]
