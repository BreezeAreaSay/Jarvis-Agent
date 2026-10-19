"""Синтез речи: очередь фраз, interrupt/silence, wait, события, запасной движок и его пауза, память фраз
облачных движков, сигналы, прослушивание, счёт движка."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest

from jarvis import paths
from jarvis.context import AppContext
from jarvis.services import UserFacingError
from jarvis.voice import tts as tts_module
from jarvis.voice.engines import TtsEngineError
from jarvis.voice.player import Chunk, Player
from jarvis.voice.tts import Tts
from tests.conftest import EventLog
from tests.voice.helpers import FakeEngine, FakeSink, InstalledModels

Rig = tuple[Tts, FakeSink, dict[str, FakeEngine]]


async def wait_for(cond: Any, timeout: float = 3.0) -> None:
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
    sink = FakeSink(rate=16000, factor=0.05)
    engines = {"piper": FakeEngine("piper"), "edge": FakeEngine("edge"), "openai": FakeEngine("openai")}
    tts = Tts(
        ctx,
        sink_factory=lambda: sink,
        engines=engines,  # type: ignore[arg-type]
        models=InstalledModels(),  # type: ignore[arg-type]
        auto_download=False,
    )
    await tts.start()
    yield tts, sink, engines
    await tts.stop()


async def test_say_wait_plays_all_sentences(rig: Rig, events: EventLog) -> None:
    tts, sink, engines = rig
    await tts.say("Добрый день, сэр. Все системы в норме! Открыл Telegram.", wait=True)
    texts = [c[0] for c in engines["piper"].calls]
    assert texts == ["Добрый день, сэр.", "Все системы в норме!", "Открыл телеграм."]  # латиница — по-русски
    assert sink.samples >= 3 * int(0.3 * 16000) * 0.99
    await wait_for(lambda: not tts.speaking)
    await asyncio.sleep(0.05)
    states = events.of("tts.state")
    assert states[0]["speaking"] is True and "Добрый день" in states[0]["text"]
    assert states[-1] == {"speaking": False}
    levels = [e["level"] for e in events.of("tts.level")]
    assert levels and max(levels) > 0.3 and levels[-1] == 0.0


async def test_speaking_true_from_say_until_end(rig: Rig) -> None:
    tts, _, engines = rig
    engines["piper"].delay = 0.05
    assert not tts.speaking
    await tts.say("Проверка связи.")
    assert tts.speaking  # ещё синтезируется — уже «говорит» (голосовой сервис не слушает эхо)
    await wait_for(lambda: not tts.speaking)


async def test_interrupt_and_silence(rig: Rig, events: EventLog) -> None:
    tts, sink, engines = rig
    sink.factor = 1.0  # в реальном времени: 0,3 с на фразу
    long = "Первая фраза. Вторая фраза. Третья фраза. Четвёртая фраза."
    waiter = asyncio.ensure_future(tts.say(long, wait=True))
    await wait_for(lambda: sink.samples > 1000)
    await tts.say("Стоп.", interrupt=True, wait=True)
    await asyncio.wait_for(waiter, 2.0)  # прерванная фраза тоже «закончилась»
    played = sink.samples
    assert played < 3 * int(0.3 * 16000)  # первая не доиграла, остальные не звучали
    assert ("Стоп.", "v1") in engines["piper"].calls
    assert sink.aborted >= 1
    waiter = asyncio.ensure_future(tts.say("Длинная фраза. Ещё одна.", wait=True))
    await wait_for(lambda: tts.player.busy)
    tts.silence()
    await asyncio.wait_for(waiter, 2.0)
    await wait_for(lambda: not tts.speaking)
    await asyncio.sleep(0.05)
    assert events.of("tts.state")[-1] == {"speaking": False}


async def test_fallback_to_piper_with_error_event(rig: Rig, ctx: AppContext, events: EventLog) -> None:
    tts, sink, engines = rig
    ctx.settings.patch({"tts": {"engine": "openai"}})
    engines["openai"].fail = "нет ключа OpenAI"
    engines["openai"].ru = False
    await tts.say("Привет, рад вас слышать. Как у вас дела сегодня?", wait=True)
    await asyncio.sleep(0.05)
    errors = events.of("tts.error")
    assert len(errors) == 1 and "нет ключа OpenAI" in errors[0]["error"] and errors[0]["engine"] == "openai"
    assert [c[0] for c in engines["piper"].calls] == ["Привет, рад вас слышать.", "Как у вас дела сегодня?"]
    assert len(engines["openai"].calls) == 1  # упавший движок минуту не трогаем — сразу запасной
    assert sink.samples > 0


async def test_all_engines_fail(rig: Rig, ctx: AppContext, events: EventLog) -> None:
    tts, sink, engines = rig
    engines["piper"].fail = "голос не скачан"
    await tts.say("Привет.", wait=True)
    await asyncio.sleep(0.05)
    assert sink.samples == 0 and not tts.speaking
    assert any("Не удалось озвучить" in e["error"] for e in events.of("tts.error"))
    with pytest.raises(UserFacingError):
        await tts.synthesize("Привет.")


async def test_synthesize_returns_audio_without_playing(rig: Rig) -> None:
    tts, sink, _ = rig
    samples, rate = await tts.synthesize("Первое предложение. Второе предложение.")
    assert rate == 16000 and samples.dtype == np.float32
    assert len(samples) > 2 * int(0.3 * 16000)  # две фразы и пауза между ними
    assert sink.samples == 0
    empty, _ = await tts.synthesize("")
    assert len(empty) == 0


async def test_play_and_volume(rig: Rig, ctx: AppContext) -> None:
    tts, sink, _ = rig
    ctx.settings.patch({"tts": {"volume": 50}})
    await tts.play(np.full(8000, 0.8, dtype=np.float32), 8000, wait=True)
    assert sink.samples == 16000  # 8 кГц → частота устройства
    loud = max(float(np.max(np.abs(b))) for b in sink.blocks)
    assert abs(loud - 0.4) < 0.01


async def test_earcon_is_not_speech(rig: Rig, events: EventLog) -> None:
    tts, sink, _ = rig
    tts.play_earcon("wake")
    assert not tts.speaking
    await wait_for(lambda: sink.samples > 0)
    await asyncio.sleep(0.05)
    assert events.of("tts.state") == []
    tts.play_earcon("unknown")  # неизвестный — тихо ничего


async def test_voices_and_test(rig: Rig) -> None:
    tts, sink, engines = rig
    assert (await tts.voices("edge"))[0]["id"] == "v1"
    with pytest.raises(UserFacingError):
        await tts.voices("nope")
    await tts.test(None, "edge", "ru-RU-SvetlanaNeural")
    assert engines["edge"].calls[0][1] == "ru-RU-SvetlanaNeural"
    assert "Добрый день" in engines["edge"].calls[0][0]
    await wait_for(lambda: sink.samples > 0)
    engines["edge"].fail = "Edge TTS недоступен"
    with pytest.raises(UserFacingError, match="Edge TTS недоступен"):
        await tts.test("Проверка", "edge")  # прослушивание — без запасных: ошибку видно сразу


async def test_say_empty_and_status(rig: Rig) -> None:
    tts, sink, _ = rig
    await tts.say("   ", wait=True)
    await tts.say("👍", wait=True)
    assert sink.samples == 0 and not tts.speaking
    status = tts.status()
    assert status["engine"] == "piper" and status["speaking"] is False


async def test_phrase_cache_saves_cloud_credits(rig: Rig, ctx: AppContext) -> None:
    tts, sink, engines = rig
    engines["edge"].cacheable = True  # type: ignore[attr-defined]  # облачный движок: фразы помнятся
    ctx.settings.patch({"tts": {"engine": "edge"}})
    await tts.say("Готово, сэр.", wait=True)
    await tts.say("Готово, сэр.", wait=True)
    assert [c[0] for c in engines["edge"].calls] == ["Готово, сэр."]  # второй раз — из памяти
    assert sink.samples >= 2 * int(0.3 * 16000) * 0.99  # но прозвучала дважды
    assert len(tts.cache) == 1
    ctx.settings.patch({"tts": {"speed": 1.2}})
    await tts.say("Готово, сэр.", wait=True)
    assert len(engines["edge"].calls) == 2  # другой темп — другой звук
    long = "Очень длинное предложение " + "и ещё немного слов " * 9 + "в конце."
    assert 160 < len(long) <= 220
    await tts.say(long, wait=True)
    await tts.say(long, wait=True)
    assert len(engines["edge"].calls) == 4  # длинные фразы не помним
    ctx.settings.patch({"tts": {"engine": "piper"}})
    await tts.say("Готово, сэр.", wait=True)
    await tts.say("Готово, сэр.", wait=True)
    assert len(engines["piper"].calls) == 2  # локальный голос — без памяти


async def test_stock_phrases_are_ready_in_advance_and_kept_on_disk(
    rig: Rig, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    tts, _sink, engines = rig
    edge = engines["edge"]
    edge.cacheable = True  # type: ignore[attr-defined]
    tts.warm_delay_s = 0.0
    ctx.settings.patch({"tts": {"engine": "edge"}})
    tts.add_stock(lambda: ["Готово, сэр.", "Сейчас сделаю."])
    await wait_for(lambda: len(edge.calls) == 2)  # частые ответы подготовлены заранее
    await tts.say("Готово, сэр.", wait=True)
    assert len(edge.calls) == 2  # прозвучало сразу, без сети
    disk = paths.cache_dir() / "tts"
    assert len(list(disk.glob("*.pcm"))) == 2

    tts.cache.clear()  # как после перезапуска: память пуста, звук — с диска
    await tts.say("Сейчас сделаю.", wait=True)
    assert len(edge.calls) == 2
    await tts.say("Напоминаю, сэр: к врачу в пять.", wait=True)  # личное — только в памяти
    assert len(list(disk.glob("*.pcm"))) == 2
    tts.keep("Открываю Telegram.")  # название программы — можно и на диск
    await tts.say("Открываю Telegram.", wait=True)
    assert len(list(disk.glob("*.pcm"))) == 3

    monkeypatch.setattr(tts_module, "DISK_MAX_FILES", 2)
    tts.keep("Открываю Steam.")
    await tts.say("Открываю Steam.", wait=True)
    assert len(list(disk.glob("*.pcm"))) == 2  # старые фразы удаляются первыми
    # другой темп — другой звук: ответы готовятся заново
    ctx.settings.patch({"tts": {"speed": 1.2}})
    await wait_for(lambda: len(edge.calls) == 7)
    assert [c[0] for c in edge.calls[-2:]] == ["Готово, сэр.", "Сейчас сделаю."]


async def test_engine_cooldown_from_error_and_test_clears_it(rig: Rig, ctx: AppContext) -> None:
    tts, _, engines = rig

    class OutOfCredits(FakeEngine):
        async def synthesize(self, text: str, opts: Any) -> tuple[np.ndarray, int]:
            self.calls.append((text, opts.voice))
            if self.fail:
                raise TtsEngineError(self.fail, cooldown_s=3600.0)
            return await super().synthesize(text, opts)

    engines["openai"] = OutOfCredits("openai", fail="закончились кредиты")
    tts.engines["openai"] = engines["openai"]
    ctx.settings.patch({"tts": {"engine": "openai"}})
    await tts.say("Привет.", wait=True)
    assert tts._failed_until["openai"] - time.monotonic() > 3000  # ждём обновления кредитов, а не минуту
    assert [c[0] for c in engines["piper"].calls] == ["Привет."]  # пока говорит запасной
    await tts.say("Ещё раз.", wait=True)
    assert len(engines["openai"].calls) == 1  # упавший движок не дёргаем
    engines["openai"].fail = ""
    await tts.test("Проверка", "openai")  # прослушивание в настройках удалось — снова главный
    assert "openai" not in tts._failed_until
    await wait_for(lambda: not tts.speaking)
    await tts.say("Снова я.", wait=True)
    assert engines["openai"].calls[-1][0] == "Снова я."


async def test_account_of_engines(rig: Rig) -> None:
    tts, _, engines = rig
    assert await tts.account("piper") == {"supported": False}
    with pytest.raises(UserFacingError):
        await tts.account("nope")

    async def account() -> dict[str, Any]:
        return {"supported": True, "used": 10, "limit": 100}

    engines["edge"].account = account  # type: ignore[attr-defined]
    assert (await tts.account("edge"))["limit"] == 100


def test_player_stop_interrupts_and_calls_markers() -> None:
    sink = FakeSink(factor=1.0)  # в реальном времени: фрагмент на 1 с
    player = Player(lambda: sink)
    done: list[str] = []
    player.put(Chunk(0, np.zeros(16000, dtype=np.float32), 16000))
    player.put(Chunk(0, marker=lambda: done.append("m")))
    end = time.monotonic() + 1.0
    while not player.busy and time.monotonic() < end:
        time.sleep(0.005)
    time.sleep(0.05)
    t0 = time.monotonic()
    player.stop()
    assert done == ["m"]  # метка сброшенной фразы вызвана — say(wait=True) не повиснет
    while player.busy and time.monotonic() - t0 < 1.0:
        time.sleep(0.005)
    assert time.monotonic() - t0 < 0.2  # оборвано через блок, а не после целой фразы
    assert sink.samples < 16000 and sink.aborted == 1
    player.close()
