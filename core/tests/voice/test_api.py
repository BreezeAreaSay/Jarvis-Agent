"""Маршруты голоса и синтеза речи (docs/CONTRACTS.md, раздел 3) через приложение целиком."""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.voice.helpers import FakeEngine, FakeSink


def test_voice_status_and_enabled(client: TestClient) -> None:
    status = client.get("/api/voice").json()
    assert status["enabled"] is True and status["wake_words"] == ["джарвис"]
    assert status["models_ready"] is False
    assert (
        status["state"] == "error" and "не скачаны" in status["error"]
    )  # заглушка: моделей нет, сами не качаем
    assert set(status) >= {"state", "enabled", "error", "wake_words", "models_ready", "input_device"}
    off = client.post("/api/voice/enabled", json={"enabled": False}).json()
    assert off["enabled"] is False and off["state"] == "off"
    assert client.get("/api/settings").json()["settings"]["voice"]["enabled"] is False
    assert client.get("/api/status").json()["voice"]["state"] == "off"


def test_listen_and_capture_need_models(client: TestClient) -> None:
    r = client.post("/api/voice/listen")
    assert r.status_code == 400 and "не скачаны" in r.json()["detail"]
    r = client.post("/api/voice/capture", json={"timeout": 2})
    assert r.status_code == 400 and "не скачаны" in r.json()["detail"]
    assert client.post("/api/voice/stop").json() == {"ok": True}


def test_stop_and_listen_toggle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def stop() -> None:
        calls.append("stop")

    monkeypatch.setattr(client.ctx.dispatcher, "stop", stop)  # type: ignore[attr-defined]
    assert client.post("/api/stop").json() == {"ok": True}
    # хоткей «Сказать команду», пока Jarvis слушает, — «стоп»
    monkeypatch.setattr(client.ctx.voice, "state", "listening")  # type: ignore[attr-defined]
    assert client.post("/api/voice/listen", json={"toggle": True}).json() == {"ok": True, "stopped": True}
    assert calls == ["stop", "stop"]
    # без toggle — как раньше: слушать (моделей нет — понятная ошибка)
    r = client.post("/api/voice/listen")
    assert r.status_code == 400 and "не скачаны" in r.json()["detail"]


def test_capture_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, bool]] = []

    async def capture_text(timeout: float = 15.0, *, punct: bool = True) -> str:
        calls.append((timeout, punct))
        return "Купить хлеба."

    monkeypatch.setattr(client.ctx.voice, "capture_text", capture_text)  # type: ignore[attr-defined]
    assert client.post("/api/voice/capture", json={"timeout": 5, "punct": False}).json() == {
        "text": "Купить хлеба."
    }
    assert client.post("/api/voice/capture").json() == {"text": "Купить хлеба."}
    assert calls == [(5.0, False), (15.0, True)]
    assert client.post("/api/voice/capture", json={"timeout": 0}).status_code == 422


def test_devices_without_sound_system(client: TestClient) -> None:
    body = client.get("/api/voice/devices").json()
    assert isinstance(body["inputs"], list) and isinstance(body["outputs"], list)


def test_models_routes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    items = {i["id"]: i for i in client.get("/api/voice/models").json()["items"]}
    assert {
        "silero-vad",
        "vosk-small",
        "gigaam-v3",
        "gigaam-v3-punct",
        "piper-ruslan",
        "whisper-base",
    } <= set(items)
    giga = items["gigaam-v3"]
    assert giga["kind"] == "asr" and giga["size_mb"] == 167.4 and giga["required"] is True
    assert giga["installed"] is False and giga["downloading"] is False and giga["progress"] == 0.0
    assert items["piper-ruslan"]["required"] and not items["piper-denis"]["required"]
    assert items["vosk-small"]["kind"] == "stream" and items["whisper-tiny"]["kind"] == "whisper"
    started: list[str] = []
    manager: Any = client.ctx.voice.models  # type: ignore[attr-defined]
    monkeypatch.setattr(manager, "start", lambda model_id: started.append(model_id))
    assert client.post("/api/voice/models/silero-vad/download").json() == {"ok": True}
    assert started == ["silero-vad"]
    assert client.post("/api/voice/models/nope/download").status_code == 404
    assert client.delete("/api/voice/models/nope").status_code == 404
    assert client.delete("/api/voice/models/whisper-tiny").json() == {"ok": True}


def test_tts_voices(client: TestClient) -> None:
    piper = client.get("/api/tts/voices", params={"engine": "piper"}).json()["items"]
    assert [v["id"] for v in piper] == ["ruslan", "denis", "dmitri", "irina"]
    assert piper[0]["lang"] == "ru-RU" and piper[0]["gender"] == "male"
    assert client.get("/api/tts/voices").json()["items"][0]["id"] == "ruslan"  # движок из настроек
    assert client.get("/api/tts/voices", params={"engine": "openai"}).json()["items"][0]["id"] == "onyx"
    assert client.get("/api/tts/voices", params={"engine": "nope"}).status_code == 400


def test_tts_say_test_stop(client: TestClient) -> None:
    tts: Any = client.ctx.tts  # type: ignore[attr-defined]
    r = client.post("/api/tts/test", json={"engine": "piper", "voice": "denis"})
    assert r.status_code == 400 and "не скачан" in r.json()["detail"]
    r = client.post("/api/tts/test", json={"engine": "openai"})
    assert r.status_code == 400 and "ключа OpenAI" in r.json()["detail"]
    sink = FakeSink()
    engine = FakeEngine("piper")
    tts.engines["piper"] = engine
    tts.player.sink_factory = lambda: sink
    assert client.post("/api/tts/test", json={}).json() == {"ok": True}
    assert "Добрый день, сэр" in engine.calls[0][0]
    end = time.monotonic() + 3
    while sink.samples == 0 and time.monotonic() < end:
        time.sleep(0.01)
    assert sink.samples > 0
    assert client.post("/api/tts/say", json={"text": "Привет, сэр."}).json() == {"ok": True}
    assert client.post("/api/tts/stop").json() == {"ok": True}
    assert client.post("/api/tts/say", json={}).status_code == 422
