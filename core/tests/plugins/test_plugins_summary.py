"""«Конспект»: запись (заглушки звука) → WAV → расшифровка (заглушка голоса) → ИИ → заметка и задачи."""

import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis.context import AppContext
from jarvis.plugins import summary as summary_module
from jarvis.plugins.audio import Recorder, read_wav_chunks
from jarvis.plugins.summary import SUMMARY_SCHEMA, Summary, normalize_summary
from tests.conftest import EventLog
from tests.plugins.conftest import (
    RATE,
    FakeAi,
    FakeNotes,
    FakeSource,
    FakeVoice,
    enable,
    manager,
    speech,
    wait_until,
)

AI_SUMMARY = {
    "title": "Релиз версии 2.0",
    "about": ["Обсудили готовность релиза", "Решили, кто пишет заметки"],
    "decisions": ["Выпускаем в пятницу"],
    "tasks": ["Ане подготовить заметки к релизу", "Проверить сборку для Windows"],
}


def plugin_of(ctx: AppContext) -> Summary:
    plugin = manager(ctx).get("summary")
    assert isinstance(plugin, Summary)
    return plugin


def notes_of(ctx: AppContext) -> FakeNotes:
    notes = ctx.notes
    assert isinstance(notes, FakeNotes)
    return notes


def setup_recording(ctx: AppContext, seconds: float = 3.0) -> FakeVoice:
    plugin = plugin_of(ctx)
    plugin.source_factory = lambda config: [FakeSource(speech(seconds)), FakeSource(speech(seconds, amp=0.1))]
    voice = ctx.voice
    assert isinstance(voice, FakeVoice)
    voice.transcribe_fn = lambda samples, rate, language: "Обсудили релиз. Выпускаем в пятницу."
    return voice


async def record(ctx: AppContext, seconds: float = 3.0) -> Path:
    plugins = manager(ctx)
    started = await plugins.action("summary", "start", {})
    plugin = plugin_of(ctx)
    await wait_until(lambda: plugin.status()["seconds"] >= seconds - 0.05)
    await plugins.action("summary", "stop", {})
    return Path(started["file"])


async def test_pipeline_with_ai(pctx: AppContext, pevents: EventLog) -> None:
    pctx.ai = FakeAi(configured=True, reply=AI_SUMMARY)  # type: ignore[assignment]
    voice = setup_recording(pctx)
    await enable(pctx, "summary")
    path = await record(pctx)
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["state"] == "done")

    assert voice.transcribed and all(rate == RATE and lang == "ru" for _, rate, lang in voice.transcribed)
    notes = notes_of(pctx)
    assert len(notes.notes) == 1
    note = notes.notes[0]
    assert note["title"] == "Конспект: Релиз версии 2.0"
    for part in ("## О чём говорили", "## Решения", "- Выпускаем в пятницу", "## Задачи", "## Расшифровка"):
        assert part in note["body"]
    assert [t["title"] for t in notes.tasks] == AI_SUMMARY["tasks"]
    call = pctx.ai.calls[0]  # type: ignore[attr-defined]
    assert call["json_schema"] == SUMMARY_SCHEMA and "Обсудили релиз" in call["messages"][-1]["content"]
    assert not path.exists()  # запись не храним (keep_audio выключен)

    states = [e["state"] for e in pevents.of("plugin.summary.state")]
    for state in ("recording", "transcribing", "summarizing", "done"):
        assert state in states
    assert states.index("recording") < states.index("transcribing") < states.index("summarizing")
    done = pevents.of("plugin.summary.state")[-1]
    assert done == {"state": "done", **plugin.status()["last"]}
    assert done["note_id"] == "n1" and done["tasks"] == 2 and done["seconds"] >= 2.9
    assert any(t["title"] == "Конспект готов" for t in pevents.of("toast"))


async def test_without_ai_saves_transcript(pctx: AppContext) -> None:
    setup_recording(pctx)
    await enable(pctx, "summary", config={"keep_audio": True})
    path = await record(pctx)
    await wait_until(lambda: plugin_of(pctx).status()["state"] == "done")
    note = notes_of(pctx).notes[0]
    assert note["title"].startswith("Расшифровка от ")
    assert "Обсудили релиз. Выпускаем в пятницу." in note["body"] and "ИИ не настроен" in note["body"]
    assert notes_of(pctx).tasks == []
    assert path.exists()
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == RATE and wav.getnchannels() == 1 and wav.getsampwidth() == 2
        assert abs(wav.getnframes() / RATE - 3.0) < 0.15


async def test_too_short_recording(pctx: AppContext, pevents: EventLog) -> None:
    setup_recording(pctx, seconds=0.5)
    await enable(pctx, "summary")
    await record(pctx, seconds=0.5)
    status = plugin_of(pctx).status()
    assert status["state"] == "error" and status["failure"] == "Запись слишком короткая"
    assert notes_of(pctx).notes == []
    assert pevents.of("plugin.summary.state")[-1]["error"] == "Запись слишком короткая"


async def test_ai_failure_keeps_recording_for_retry(pctx: AppContext) -> None:
    pctx.ai = FakeAi(configured=True, reply=RuntimeError("провайдер недоступен"))  # type: ignore[assignment]
    setup_recording(pctx)
    await enable(pctx, "summary")
    path = await record(pctx)
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["state"] == "error")
    assert "провайдер недоступен" in plugin.status()["failure"] and path.exists()
    pctx.ai = FakeAi(configured=True, reply=AI_SUMMARY)  # type: ignore[assignment]
    await manager(pctx).action("summary", "retry", {})
    await wait_until(lambda: plugin.status()["state"] == "done")
    assert notes_of(pctx).notes[0]["title"] == "Конспект: Релиз версии 2.0" and not path.exists()


async def test_voice_commands(pctx: AppContext) -> None:
    setup_recording(pctx)
    await enable(pctx, "summary")
    commands = pctx.commands
    started = await commands.get("plugin.summary:start").handler({})  # type: ignore[attr-defined]
    assert started.ok and plugin_of(pctx).status()["state"] == "recording"
    again = await commands.get("plugin.summary:start").handler({})  # type: ignore[attr-defined]
    assert again.ok is False and "уже идёт" in again.message
    await manager(pctx).action("summary", "cancel", {})
    assert plugin_of(pctx).status()["state"] == "idle"


async def test_long_transcript_is_summarized_in_parts(
    pctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
        text = messages[-1]["content"]
        if "Объедини" in text:
            return {
                "title": "Итог",
                "about": ["А", "Б"],
                "decisions": ["Решение"],
                "tasks": ["Задача 1", "Задача 2"],
            }
        if "первая" in text:
            return {"title": "Часть 1", "about": ["А"], "decisions": [], "tasks": ["Задача 1"]}
        return {"title": "Часть 2", "about": ["Б"], "decisions": ["Решение"], "tasks": ["Задача 2"]}

    ai = FakeAi(configured=True, reply=reply)
    pctx.ai = ai  # type: ignore[assignment]
    monkeypatch.setattr(summary_module, "MAX_PROMPT_CHARS", 80)
    transcript = "\n".join(
        ["первая половина разговора о деле"] * 2 + ["вторая половина разговора о планах"] * 2
    )
    result = await plugin_of(pctx).summarize(transcript)
    assert result["title"] == "Итог" and result["tasks"] == ["Задача 1", "Задача 2"]
    assert len(ai.calls) == 3  # две части и сводный конспект
    merge = ai.calls[-1]["messages"][-1]["content"]
    assert "Часть 1" in merge and "Часть 2" in merge


def test_normalize_summary_accepts_text_and_fenced_json() -> None:
    fenced = '```json\n{"title": "Т", "about": ["x", " "], "decisions": [], "tasks": ["- сделать"]}\n```'
    assert normalize_summary(fenced) == {"title": "Т", "about": ["x"], "decisions": [], "tasks": ["сделать"]}
    assert normalize_summary("просто текст")["about"] == ["просто текст"]
    assert normalize_summary(None) == {"title": "", "about": [], "decisions": [], "tasks": []}


def test_read_wav_chunks_cuts_at_pauses(tmp_path: Path) -> None:
    rate = 8000
    loud = (0.3 * np.sin(np.arange(rate * 60) * 0.3)).astype(np.float32)
    loud[int(rate * 22.0) : int(rate * 22.4)] = 0.0  # пауза около 22-й секунды
    path = tmp_path / "talk.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((loud * 32767).astype("<i2").tobytes())
    chunks = list(read_wav_chunks(path, max_s=25.0, search_s=4.0))
    assert all(r == rate for _, r in chunks)
    lengths = [len(c) / rate for c, _ in chunks]
    assert all(length <= 25.0 for length in lengths)
    assert sum(len(c) for c, _ in chunks) == len(loud)
    assert 22.0 <= lengths[0] <= 22.4  # разрезано в паузе, а не на 25-й секунде


def test_recorder_mixes_and_survives_failed_source(tmp_path: Path) -> None:
    good = FakeSource(speech(2.0))
    bad = FakeSource(speech(0.3), error="микрофон отключён")
    errors: list[str] = []
    recorder = Recorder(tmp_path / "mix.wav", [good, bad], on_error=errors.append)
    recorder.start()
    deadline = time.monotonic() + 3.0
    while recorder.seconds < 1.95 and time.monotonic() < deadline:
        time.sleep(0.01)
    seconds = recorder.stop()
    assert abs(seconds - 2.0) < 0.15
    assert errors == ["тестовый звук: микрофон отключён"]
    with wave.open(str(tmp_path / "mix.wav"), "rb") as wav:
        assert abs(wav.getnframes() / wav.getframerate() - seconds) < 0.01
