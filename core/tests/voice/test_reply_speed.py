"""Скорость ответа: от фразы (диспетчер получил текст) до первого звука ответа в динамиках — с облачным
голосом, который синтезирует фразу 0,4 с (как ElevenLabs по сети). Частые ответы характера готовы заранее —
первый звук сразу; без подготовки — ждём сеть. Цифры печатаются (pytest -s), записаны в docs/CONTRACTS.md."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest

from jarvis.context import AppContext
from jarvis.voice.tts import Tts
from tests.voice.helpers import FakeEngine, FakeSink, InstalledModels

NETWORK_S = 0.4  # столько «облачный» голос синтезирует одно предложение
INSTANT_S = 0.15  # первый звук не позже — «сразу»

PHRASES = (
    "сверни все окна",
    "покажись",
    "скройся",
    "спасибо",
    "пауза",
    "открой ютуб",
    "открой стим",
    "поставь таймер на 3 минуты",
    "выключи компьютер через 10 минут",
    "как дела",
    "я вернулся",
    "озвучь задачи на сегодня",
)


class TimedSink(FakeSink):
    """Динамики, которые помнят, когда пришёл первый звук."""

    def __init__(self) -> None:
        super().__init__(rate=16000)
        self.first_at: float | None = None

    def write(self, block: np.ndarray) -> None:
        if self.first_at is None and np.any(block):
            self.first_at = time.perf_counter()
        super().write(block)


class Rig:
    def __init__(self, ctx: AppContext, tts: Tts, sink: TimedSink, cloud: FakeEngine) -> None:
        self.ctx, self.tts, self.sink, self.cloud = ctx, tts, sink, cloud

    async def latency(self, phrase: str) -> float:
        """Секунды от фразы до первого звука ответа."""
        while self.tts.speaking:
            await asyncio.sleep(0.01)
        self.sink.first_at = None
        t0 = time.perf_counter()
        result = await self.ctx.dispatcher.handle(phrase, "voice")
        assert result["kind"] == "command", phrase
        deadline = t0 + 5
        while self.sink.first_at is None and time.perf_counter() < deadline:
            await asyncio.sleep(0.002)
        assert self.sink.first_at is not None, phrase
        return self.sink.first_at - t0


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[Rig]:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    await ctx.commands.refresh_apps()  # type: ignore[attr-defined]
    ctx.settings.patch({"ai": {"enabled": False}, "tts": {"engine": "elevenlabs", "effect": 0.0}})
    sink = TimedSink()
    cloud = FakeEngine("elevenlabs", delay=NETWORK_S)
    cloud.cacheable = True  # type: ignore[attr-defined]
    tts = Tts(
        ctx,
        sink_factory=lambda: sink,
        engines={"piper": FakeEngine("piper"), "elevenlabs": cloud},  # type: ignore[dict-item]
        models=InstalledModels(),  # type: ignore[arg-type]
        auto_download=False,
    )
    tts.warm_delay_s = 0.0
    ctx.tts = tts
    await tts.start()
    yield Rig(ctx, tts, sink, cloud)
    await tts.stop()


async def warm_up(rig: Rig) -> None:
    """Подготовка частых ответов (как через 3 с после запуска); сеть в тесте — мгновенная, чтобы не ждать."""
    rig.cloud.delay = 0.0
    rig.tts.add_stock(rig.ctx.dispatcher.stock_phrases)
    rig.tts.add_stock(rig.ctx.character.stock_phrases)
    task: Any = rig.tts._warm_task
    while task is None or not task.done():
        await asyncio.sleep(0.02)
        task = rig.tts._warm_task
    rig.cloud.delay = NETWORK_S


async def test_confirmations_start_instantly_from_ready_phrases(rig: Rig, capsys: Any) -> None:
    rig.tts.warm_max_chars = 10_000  # вся подготовка за раз (обычно — частями за несколько запусков)
    await warm_up(rig)
    numbers: dict[str, float] = {}
    for phrase in PHRASES:
        numbers[phrase] = await rig.latency(phrase)
    with capsys.disabled():
        print("\nот фразы до первого звука, облачный голос (0,4 с на предложение), ответы готовы заранее:")
        for phrase, seconds in numbers.items():
            print(f"  {phrase:<36} {seconds * 1000:6.0f} мс")
    slow = {p: s for p, s in numbers.items() if s > INSTANT_S}
    assert not slow, slow


async def test_without_ready_phrases_reply_waits_for_network(rig: Rig, capsys: Any) -> None:
    seconds = await rig.latency("сверни все окна")
    with capsys.disabled():
        print(f"\nбез подготовки: «сверни все окна» — {seconds * 1000:.0f} мс до первого звука")
    assert seconds >= NETWORK_S


async def test_first_launch_budget_covers_first_variants(rig: Rig) -> None:
    # кредиты облачного голоса — понемногу: за первый запуск — не больше warm_max_chars символов синтеза,
    # но первые варианты всех ответов успевают
    await warm_up(rig)
    synthesized = sum(len(text) for text, _ in rig.cloud.calls)
    assert synthesized <= rig.tts.warm_max_chars
    for phrase in ("Сворачиваю все окна, сэр.", "Я здесь, сэр.", "Таймер установлен, сэр.", "Готово, сэр."):
        assert rig.tts.is_ready(phrase), phrase
    calls = len(rig.cloud.calls)
    seconds = await rig.latency("сверни все окна")
    assert seconds < INSTANT_S and len(rig.cloud.calls) == calls  # из готовых — без сети
