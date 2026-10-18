"""Движки синтеза: запросы OpenAI и Fish Audio (MockTransport), Edge (подмена edge-tts), piper и Windows."""

import json
import sys
from typing import Any

import httpx
import numpy as np
import pytest

from jarvis.context import AppContext
from jarvis.voice import dsp
from jarvis.voice.engines import (
    EdgeEngine,
    FishEngine,
    OpenAIEngine,
    PiperEngine,
    SynthOptions,
    WindowsEngine,
)
from jarvis.voice.engines import edge as edge_mod
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
