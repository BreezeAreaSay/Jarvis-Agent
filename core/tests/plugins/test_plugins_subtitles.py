"""«Живые субтитры»: нарезка звука на фразы, распознавание и перевод (заглушки звука, голоса и ИИ)."""

import numpy as np
import pytest

from jarvis.context import AppContext
from jarvis.plugins.audio import EnergySegmenter
from jarvis.plugins.subtitles import Subtitles, guess_lang, is_noise_text
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.plugins.conftest import (
    RATE,
    FakeAi,
    FakeSource,
    FakeVoice,
    enable,
    manager,
    silence,
    speech,
    wait_until,
)


def feed_all(segmenter: EnergySegmenter, signal: np.ndarray, block: int = 1600) -> list[np.ndarray]:
    out: list[np.ndarray] = []
    for start in range(0, len(signal), block):
        out.extend(segmenter.feed(signal[start : start + block]))
    return out


def test_segmenter_cuts_phrases_between_one_and_eight_seconds() -> None:
    signal = np.concatenate([silence(0.5), speech(1.5), silence(1.0), speech(10.0), silence(1.0)])
    segments = feed_all(EnergySegmenter(RATE), signal)
    lengths = [len(s) / RATE for s in segments]
    assert len(lengths) == 3
    assert all(1.0 <= length <= 8.0 for length in lengths), lengths
    assert 1.5 <= lengths[0] <= 2.4 and lengths[1] == pytest.approx(8.0, abs=0.05)


def test_segmenter_ignores_silence_and_clicks() -> None:
    signal = np.concatenate([silence(2.0), speech(0.1), silence(2.0)])
    segmenter = EnergySegmenter(RATE)
    assert feed_all(segmenter, signal) == [] and segmenter.flush() is None


def test_short_phrase_is_padded_to_a_second() -> None:
    segments = feed_all(EnergySegmenter(RATE), np.concatenate([silence(0.3), speech(0.4), silence(2.0)]))
    assert len(segments) == 1 and len(segments[0]) / RATE >= 1.0


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Hello, how are you?", "en"),
        ("Привет, как дела?", "ru"),
        ("Привіт, як справи?", "uk"),
        ("Schöne Grüße aus Berlin", "de"),
        ("¿Dónde está la biblioteca?", "es"),
        ("こんにちは", "ja"),
        ("123", ""),
    ],
)
def test_guess_lang(text: str, lang: str) -> None:
    assert guess_lang(text) == lang


def test_noise_text() -> None:
    assert is_noise_text("Продолжение следует...")
    assert is_noise_text("Thanks for watching!")
    assert is_noise_text("…")
    assert not is_noise_text("Продолжаем разговор")


def plugin_of(ctx: AppContext) -> Subtitles:
    plugin = manager(ctx).get("subtitles")
    assert isinstance(plugin, Subtitles)
    return plugin


def voice_of(ctx: AppContext) -> FakeVoice:
    voice = ctx.voice
    assert isinstance(voice, FakeVoice)
    return voice


async def test_live_subtitles_with_translation(pctx: AppContext, pevents: EventLog) -> None:
    pctx.ai = FakeAi(configured=True, reply="Привет, мир")  # type: ignore[assignment]
    voice_of(pctx).transcribe_fn = lambda samples, rate, language: "Hello world"
    plugin = plugin_of(pctx)
    plugin.source_factory = lambda config: FakeSource(
        np.concatenate([silence(0.3), speech(1.5), silence(1.0)])
    )
    await enable(pctx, "subtitles")
    assert await manager(pctx).action("subtitles", "start", {}) == {"ok": True, "active": True}
    await wait_until(lambda: len(pevents.of("plugin.subtitles.text")) == 2)
    first, second = pevents.of("plugin.subtitles.text")
    assert first["text"] == "Hello world" and first["lang"] == "en"
    assert first["pending"] is True and first["translation"] == ""
    assert (
        second["id"] == first["id"] and second["translation"] == "Привет, мир" and second["pending"] is False
    )
    assert voice_of(pctx).transcribed[0][1:] == (RATE, "auto")
    prompt = pctx.ai.calls[0]["messages"]  # type: ignore[attr-defined]
    assert "русский" in prompt[0]["content"] and prompt[-1]["content"].endswith("Hello world")
    assert plugin.status()["last"]["translation"] == "Привет, мир"
    assert await manager(pctx).action("subtitles", "stop", {}) == {"ok": True, "active": False}
    assert plugin.status()["active"] is False


async def test_same_language_and_noise_are_not_translated(pctx: AppContext, pevents: EventLog) -> None:
    ai = FakeAi(configured=True, reply="перевод")
    pctx.ai = ai  # type: ignore[assignment]
    await enable(pctx, "subtitles", config={"source_lang": "ru"})
    plugin = plugin_of(pctx)
    voice_of(pctx).transcribe_fn = lambda samples, rate, language: "Привет всем"
    item = await plugin.process(speech(1.5))
    assert item is not None and item["pending"] is False and item["lang"] == "ru"
    voice_of(pctx).transcribe_fn = lambda samples, rate, language: "Продолжение следует..."
    assert await plugin.process(speech(1.5)) is None
    await manager(pctx).update("subtitles", config={"source_lang": "en", "target_lang": "none"})
    voice_of(pctx).transcribe_fn = lambda samples, rate, language: "Good morning"
    item = await plugin.process(speech(1.5))
    assert item is not None and item["pending"] is False
    assert ai.calls == [] and [e["text"] for e in pevents.of("plugin.subtitles.text")] == [
        "Привет всем",
        "Good morning",
    ]


async def test_missing_model_stops_subtitles(pctx: AppContext, pevents: EventLog) -> None:
    def no_model(samples: np.ndarray, rate: int, language: str) -> str:
        raise UserFacingError("Для языка «auto» скачайте модель Whisper")

    voice_of(pctx).transcribe_fn = no_model
    plugin = plugin_of(pctx)
    plugin.source_factory = lambda config: FakeSource(np.concatenate([speech(1.5), silence(1.0)]))
    await enable(pctx, "subtitles")
    await manager(pctx).action("subtitles", "start", {})
    await wait_until(lambda: not plugin.active)
    assert "Whisper" in plugin.error
    assert any("Whisper" in t["message"] for t in pevents.of("toast"))


async def test_capture_failure_is_reported(pctx: AppContext) -> None:
    plugin = plugin_of(pctx)
    plugin.source_factory = lambda config: FakeSource(silence(0.2), error="Звуковая система недоступна")
    await enable(pctx, "subtitles")
    await manager(pctx).action("subtitles", "start", {})
    await wait_until(lambda: not plugin.active)
    assert plugin.error == "Звуковая система недоступна"
