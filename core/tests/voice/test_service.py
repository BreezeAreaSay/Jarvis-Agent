"""Голосовой сервис целиком: поток распознавания, просьбы (кнопка, диктовка, да/нет), состояние, модели."""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from jarvis.context import AppContext
from jarvis.platform.base import MediaInfo
from jarvis.services import UserFacingError
from jarvis.settings import Settings
from jarvis.voice import service as service_mod
from jarvis.voice.asr import vad_params
from jarvis.voice.models import ModelManager
from jarvis.voice.service import Voice
from tests.conftest import EventLog
from tests.voice.helpers import (
    FakeCloud,
    FakeMic,
    FakeRecognizers,
    FakeVad,
    FakeWhisper,
    InstalledModels,
    Script,
)


class FakeTts:
    def __init__(self) -> None:
        self.speaking = False
        self.silenced = 0
        self.earcons: list[str] = []

    def silence(self) -> None:
        self.silenced += 1
        self.speaking = False

    def play_earcon(self, name: str) -> None:
        self.earcons.append(name)


class Rig:
    def __init__(self, ctx: AppContext, models: Any = None) -> None:
        self.ctx = ctx
        self.script = Script()
        self.mics: list[FakeMic] = []
        self.dispatched: list[tuple[str, str]] = []
        self.result: dict[str, Any] = {"kind": "command", "result": {"ok": True, "say": "", "data": {}}}
        self.tts = FakeTts()
        ctx.tts = self.tts  # type: ignore[assignment]
        self.recognizers = FakeRecognizers(self.script)
        self.vads: list[tuple[float, float]] = []  # (чувствительность, пауза в конце фразы) каждого VAD
        self.voice = Voice(
            ctx,
            models=models or InstalledModels(),  # type: ignore[arg-type]
            recognizers=self.recognizers,
            mic_factory=self._mic,
            vad_factory=self._vad,
            auto_download=False,
        )
        ctx.voice = self.voice

        async def handle(text: str, source: str, *, speak: bool | None = None) -> dict[str, Any]:
            self.dispatched.append((text, source))
            return self.result

        ctx.dispatcher.handle = handle  # type: ignore[method-assign]

    def _mic(self, device: str) -> FakeMic:
        mic = FakeMic(device)
        self.mics.append(mic)
        return mic

    def _vad(self, sensitivity: float, end_silence: float) -> FakeVad:
        self.vads.append((sensitivity, end_silence))
        return FakeVad()

    @property
    def mic(self) -> FakeMic:
        return self.mics[-1]

    def say(self, text: str, seconds: float = 1.0) -> None:
        self.mic.push(self.script.silence(0.3) + self.script.utter(text, seconds) + self.script.silence(0.6))


async def until(cond: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.01)


@pytest.fixture
async def rig(ctx: AppContext, events: EventLog) -> AsyncIterator[Rig]:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    r = Rig(ctx)
    yield r
    await r.voice.stop()


def states(events: EventLog) -> list[str]:
    return [e["state"] for e in events.of("voice.state")]


async def test_wake_command_dispatched(rig: Rig, events: EventLog) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    rig.say("джарвис включи музыку")
    await until(lambda: rig.dispatched)
    assert rig.dispatched == [("включи музыку", "voice")]
    await asyncio.sleep(0.05)
    seq = states(events)
    assert seq[:2] == ["loading", "idle"] and "processing" in seq and seq[-1] == "idle"
    assert events.of("voice.final")[-1]["command"] == "включи музыку"
    assert events.of("voice.wake") and events.of("voice.level")
    status = rig.voice.status()
    assert status["state"] == "idle" and status["models_ready"]
    assert status["input_device"] == "Тестовый микрофон"


async def test_quiet_mode_feedback_earcon(rig: Rig, ctx: AppContext) -> None:
    ctx.settings.patch({"voice": {"speak_confirmations": "off"}})
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    rig.say("джарвис сверни окна")
    await until(lambda: "done" in rig.tts.earcons)
    rig.result = {"kind": "command", "result": {"ok": False, "say": "", "data": {}}}
    ctx.settings.patch({"voice": {"silent_mode": True}})
    rig.say("джарвис открой почту")
    await until(lambda: "error" in rig.tts.earcons)


async def test_capture_text_with_voice_disabled(
    rig: Rig, ctx: AppContext, events: EventLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_mod, "LINGER_S", 0.05)
    ctx.settings.patch({"voice": {"enabled": False}})
    await rig.voice.start()
    assert rig.voice.state == "off" and rig.mics == []
    task = asyncio.ensure_future(rig.voice.capture_text(5.0, punct=True))
    await until(lambda: rig.mics and rig.voice.state == "listening")  # микрофон открыт на время просьбы
    rig.say("Купить хлеба и молока.")
    assert await asyncio.wait_for(task, 3.0) == "Купить хлеба и молока."
    assert rig.dispatched == []  # диктовка не выполняется
    assert rig.script.finals[-1] == ("Купить хлеба и молока.", True)
    assert events.of("voice.final")[-1] == {"text": "Купить хлеба и молока.", "wake": False, "command": ""}
    await until(lambda: rig.mic.closed and rig.voice.state == "off")  # и закрыт после


async def test_capture_times_out_empty(rig: Rig) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics)
    assert await asyncio.wait_for(rig.voice.capture_text(0.3), 3.0) == ""


async def test_listen_now_then_capture_cancels_listen(rig: Rig) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    await rig.voice.listen_now()
    assert rig.tts.silenced == 1
    capture = asyncio.ensure_future(rig.voice.capture_text(5.0))
    await asyncio.sleep(0.1)
    rig.say("привет из чата")
    assert await asyncio.wait_for(capture, 3.0) == "привет из чата"
    assert rig.dispatched == []  # кнопка микрофона отменена: фраза ушла в диктовку, а не в команды
    await rig.voice.listen_now()
    await asyncio.sleep(0.1)
    rig.say("открой калькулятор")
    await until(lambda: rig.dispatched)
    assert rig.dispatched == [("открой калькулятор", "voice")]


async def test_stop_listening_cancels_capture(rig: Rig) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics)
    capture = asyncio.ensure_future(rig.voice.capture_text(10.0))
    await asyncio.sleep(0.1)
    await rig.voice.stop_listening()
    assert await asyncio.wait_for(capture, 2.0) == ""


async def test_ask_yes_no(rig: Rig) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics)
    answer = asyncio.ensure_future(rig.voice.ask_yes_no(5.0))
    await asyncio.sleep(0.1)
    rig.say("да конечно")
    assert await asyncio.wait_for(answer, 3.0) is True
    answer = asyncio.ensure_future(rig.voice.ask_yes_no(5.0))
    await asyncio.sleep(0.1)
    rig.say("джарвис нет")
    assert await asyncio.wait_for(answer, 3.0) is False
    assert await asyncio.wait_for(rig.voice.ask_yes_no(0.3), 3.0) is None
    assert rig.dispatched == []


async def test_speaking_state_and_follow_up(rig: Rig, events: EventLog) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    rig.tts.speaking = True
    rig.ctx.bus.publish("tts.state", {"speaking": True, "text": "Ответ"})
    await until(lambda: rig.voice.state == "speaking")
    rig.voice.follow_up(3.0)
    rig.say("включи музыку")  # пока говорит — без активатора не слушаем
    await asyncio.sleep(0.4)
    assert rig.dispatched == []
    rig.tts.speaking = False
    rig.ctx.bus.publish("tts.state", {"speaking": False})
    await until(lambda: rig.voice.state == "listening")  # окно продолжения
    await asyncio.sleep(0.35)  # человек отвечает не мгновенно: хвост речи Jarvis в комнате уже стих
    rig.say("а погромче")
    await until(lambda: rig.dispatched)
    assert rig.dispatched == [("а погромче", "voice")]


async def test_set_enabled_and_device_change(rig: Rig, ctx: AppContext) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics)
    ctx.settings.patch({"voice": {"input_device": "USB"}})
    await until(lambda: len(rig.mics) == 2)
    assert rig.mics[0].closed and rig.mics[1].device == "USB"
    await rig.voice.set_enabled(False)
    assert ctx.settings.value.voice.enabled is False
    await until(lambda: rig.voice.state == "off" and rig.mic.closed)
    await rig.voice.set_enabled(True)
    await until(lambda: len(rig.mics) == 3 and rig.voice.state == "idle")


async def test_no_models(ctx: AppContext, events: EventLog) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    r = Rig(ctx, models=InstalledModels(False))
    try:
        await r.voice.start()
        assert r.voice.state == "error" and "не скачаны" in r.voice.status()["error"]
        assert r.voice.status()["models_ready"] is False
        with pytest.raises(UserFacingError, match="не скачаны"):
            await r.voice.listen_now()
        with pytest.raises(UserFacingError):
            await r.voice.capture_text(1.0)
        assert await r.voice.ask_yes_no(1.0) is None
        assert r.mics == []
    finally:
        await r.voice.stop()


class DownloadingModels(InstalledModels):
    """Ничего нет; ensure() «качает» по сигналу."""

    def __init__(self) -> None:
        super().__init__(False)
        self.gate = asyncio.Event()
        self.asked: list[str] = []

    async def ensure(self, ids: list[str]) -> dict[str, str]:
        self.asked = list(ids)
        await self.gate.wait()
        self.is_installed = True
        return {}


async def test_auto_download_on_first_start(ctx: AppContext, events: EventLog) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    models = DownloadingModels()
    r = Rig(ctx, models=models)
    r.voice._auto_download = True
    try:
        await r.voice.start()
        await until(lambda: models.asked)
        assert models.asked == ["silero-vad", "vosk-small", "gigaam-v3", "piper-ruslan"]
        assert r.voice.state == "loading"
        assert events.of("voice.state")[-1] == {"state": "loading", "reason": "Скачиваю голосовые модели…"}
        models.gate.set()
        await until(lambda: r.mics and r.voice.state == "idle")
    finally:
        await r.voice.stop()


async def test_transcribe(rig: Rig) -> None:
    script = rig.script
    speech = np.concatenate(script.utter("Привет, это голосовое.", 1.0))
    samples = np.repeat(speech, 3)  # «48 кГц»
    assert await rig.voice.transcribe(samples, 48000) == "Привет, это голосовое."
    with pytest.raises(UserFacingError, match="Whisper"):
        await rig.voice.transcribe(samples, 48000, language="en")


async def test_no_follow_up_while_media_plays(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Музыка или видео в колонках — их речь Jarvis принял бы за продолжение разговора: окна нет."""
    calls: list[float] = []
    monkeypatch.setattr(rig.voice.listener, "follow_up", lambda seconds, **_: calls.append(seconds))
    await rig.voice.start()
    try:
        watcher = SimpleNamespace(media=MediaInfo(title="Трек", status="playing"))
        monkeypatch.setattr(rig.ctx, "widgets", SimpleNamespace(watcher=watcher), raising=False)
        rig.voice.follow_up(5)
        assert calls == []
        watcher.media = MediaInfo(title="Трек", status="paused")
        rig.voice.follow_up(5)
        assert calls == [5.0]
    finally:
        await rig.voice.stop()


async def test_can_follow_only_while_listening(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """«Что-нибудь ещё?» — только если голос слушает, продолжение не выключено и не играет музыка."""
    assert not rig.voice.can_follow(question=True)  # голос ещё не запущен
    await rig.voice.start()
    try:
        assert rig.voice.can_follow(question=True)
        rig.ctx.settings.patch({"voice": {"follow_up_s": 0}})
        assert not rig.voice.can_follow(question=True)
        rig.ctx.settings.patch({"voice": {"follow_up_s": 6}})
        watcher = SimpleNamespace(media=MediaInfo(title="Трек", status="playing"))
        monkeypatch.setattr(rig.ctx, "widgets", SimpleNamespace(watcher=watcher), raising=False)
        assert not rig.voice.can_follow(question=True)
    finally:
        await rig.voice.stop()


def test_has_multilingual_follows_whisper_models(ctx: AppContext) -> None:
    models = InstalledModels(installed=False)
    rig = Rig(ctx, models)
    assert not rig.voice.has_multilingual()
    models.is_installed = True
    assert rig.voice.has_multilingual()


async def test_mic_error_state(rig: Rig) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    rig.mic.error = "Микрофон отключился — переподключаюсь"
    await until(lambda: rig.voice.state == "error")
    assert rig.voice.status()["error"].startswith("Микрофон отключился")
    rig.mic.error = ""
    await until(lambda: rig.voice.state == "idle")


async def test_command_language_uses_whisper(rig: Rig, ctx: AppContext, events: EventLog) -> None:
    """Команды не по-русски: итог фразы — Whisper с кодом языка, потоковая (русская) модель не участвует;
    «Jarvis» латиницей — тот же активатор «джарвис»."""
    whisper = FakeWhisper(rig.script)
    rig.recognizers.whisper_model = whisper
    ctx.settings.patch({"voice": {"language": "en"}})
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    rig.say("Jarvis, open YouTube.")
    await until(lambda: rig.dispatched)
    assert rig.dispatched == [("open YouTube", "voice")]
    assert whisper.languages and set(whisper.languages) == {"en"}
    assert rig.recognizers.sessions == 0 and rig.script.finals == []  # ни потоковой, ни GigaAM
    assert events.of("voice.partial") == []
    ctx.settings.patch({"voice": {"language": "auto"}})
    rig.say("Jarvis, what time is it")
    await until(lambda: len(rig.dispatched) == 2)
    assert whisper.languages[-1] == ""  # язык Whisper определит сам


async def test_command_language_without_whisper_falls_back_to_russian(rig: Rig, ctx: AppContext) -> None:
    ctx.settings.patch({"voice": {"language": "en"}})
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    rig.say("джарвис включи музыку")
    await until(lambda: rig.dispatched)
    assert rig.dispatched == [("включи музыку", "voice")]
    assert rig.recognizers.sessions >= 1 and rig.script.finals


async def test_end_of_phrase_pause_reconfigures_vad(rig: Rig, ctx: AppContext) -> None:
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    assert rig.vads[-1] == (0.5, 0.45)
    ctx.settings.patch({"voice": {"end_silence_ms": 900}})
    rig.say("джарвис включи свет")  # перенастройка — в потоке распознавания, перед следующим блоком
    await until(lambda: rig.vads[-1] == (0.5, 0.9))
    with pytest.raises(ValidationError):
        ctx.settings.patch({"voice": {"end_silence_ms": 100}})


def test_vad_end_of_phrase_pause() -> None:
    assert vad_params(0.5)["min_silence"] == 0.45
    assert vad_params(0.5, 0.9)["min_silence"] == 0.9
    assert vad_params(0.5, 5.0)["min_silence"] == 2.0 and vad_params(0.5, 0.1)["min_silence"] == 0.3


def test_required_models_follow_command_language() -> None:
    settings = Settings.model_validate({"voice": {"language": "en"}})
    assert ModelManager.required_ids(settings)[-2] == "whisper-base"
    assert ModelManager.required_ids(settings, lambda i: i == "whisper-tiny")[-2] == "whisper-tiny"
    assert "whisper-base" not in ModelManager.required_ids(Settings())


async def test_follow_up_after_question(rig: Rig, ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[float, bool]] = []
    monkeypatch.setattr(
        rig.voice.listener, "follow_up", lambda seconds, question=False: calls.append((seconds, question))
    )
    await rig.voice.start()
    rig.voice.follow_up(question=True)
    assert calls[-1] == (service_mod.QUESTION_FOLLOW_S, True)  # 6 с из настроек — мало, чтобы подумать
    rig.voice.follow_up()
    assert calls[-1] == (6.0, False)
    ctx.settings.patch({"voice": {"follow_up_s": 0}})
    rig.voice.follow_up(question=True)
    assert calls[-1] == (0.0, True)  # продолжение выключено — и для вопросов


class FakeScribe:
    """Облачное распознавание для сервиса: поток на каждую обращённую фразу — FakeCloud по сценарию."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.clouds: list[FakeCloud] = []
        self.resets = 0

    def stream(self) -> FakeCloud:
        self.clouds.append(FakeCloud(self.script))
        return self.clouds[-1]

    def available(self) -> str:
        return ""

    def reset(self) -> None:
        self.resets += 1


async def test_elevenlabs_mode_uses_cloud_and_not_gigaam(rig: Rig, ctx: AppContext) -> None:
    ctx.settings.patch({"voice": {"asr_model": "elevenlabs"}})
    scribe = FakeScribe(rig.script)
    rig.voice.scribe = scribe
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    rig.say("джарвис включи музыку")
    await until(lambda: rig.dispatched)
    assert rig.dispatched == [("включи музыку", "voice")]
    assert len(scribe.clouds) == 1 and scribe.clouds[0].frames and scribe.clouds[0].closed
    assert rig.script.finals == []  # GigaAM в этом режиме не зовётся
    rig.say("какая сегодня погода")  # чужой разговор в облако не уходит
    await asyncio.sleep(0.3)
    assert len(scribe.clouds) == 1


async def test_elevenlabs_without_key_hears_with_vosk(rig: Rig, ctx: AppContext) -> None:
    ctx.settings.patch({"voice": {"asr_model": "elevenlabs"}})
    await rig.voice.start()
    await until(lambda: rig.mics and rig.voice.state == "idle")
    assert "ключ" in rig.voice.status()["cloud_error"]
    rig.say("джарвис сверни все окна")
    await until(lambda: rig.dispatched)
    assert rig.dispatched == [("сверни все окна", "voice")]
    assert rig.script.finals == []  # текст потоковой Vosk, без GigaAM


async def test_new_elevenlabs_key_lifts_cloud_pause(rig: Rig, ctx: AppContext) -> None:
    scribe = FakeScribe(rig.script)
    rig.voice.scribe = scribe
    await rig.voice.start()
    ctx.secrets.set("elevenlabs_api_key", "sk_" + "1" * 40)
    await until(lambda: scribe.resets == 1)
    ctx.secrets.set("openai_api_key", "sk-x")
    await asyncio.sleep(0.05)
    assert scribe.resets == 1


def test_elevenlabs_mode_needs_no_gigaam_download() -> None:
    settings = Settings.model_validate({"voice": {"asr_model": "elevenlabs"}})
    ids = ModelManager.required_ids(settings)
    assert "gigaam-v3" not in ids and "vosk-small" in ids and "silero-vad" in ids


class FakeHook:
    """Хук «держи и говори» без pynput: жив ли поток и сколько не слышал нажатий — как скажет тест."""

    made: list["FakeHook"] = []

    def __init__(self, key: str, *callbacks: Any) -> None:
        self.down, self.running, self.quiet = False, True, 0.0
        FakeHook.made.append(self)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.running = False

    def alive(self) -> bool:
        return self.running

    def silent_for(self) -> float:
        return self.quiet


async def test_ptt_hook_is_reinstalled_when_windows_drops_it(
    rig: Rig, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.voice import service as voice_service

    voice = rig.voice
    FakeHook.made.clear()
    monkeypatch.setattr(voice_service, "PttHook", FakeHook)
    monkeypatch.setattr(voice_service, "PTT_CHECK_S", 0.01)
    ctx.settings.patch({"voice": {"ptt_key": "right ctrl"}})
    await voice.start()  # хук и его сторож
    first = FakeHook.made[-1]
    await asyncio.sleep(0.05)
    assert voice._ptt is first  # жив и человек не печатает — не трогаем
    monkeypatch.setattr(ctx.platform, "idle_seconds", lambda: 1.0)  # человек печатает…
    first.quiet = voice_service.PTT_SILENT_S + 1  # …а хук давно ничего не слышал — Windows его сняла
    for _ in range(100):
        if voice._ptt is not first:
            break
        await asyncio.sleep(0.01)
    assert voice._ptt is not first and not first.running and voice._ptt is FakeHook.made[-1]
    second = FakeHook.made[-1]
    second.running = False  # поток хука умер
    for _ in range(100):
        if voice._ptt is not second:
            break
        await asyncio.sleep(0.01)
    assert voice._ptt is not second
