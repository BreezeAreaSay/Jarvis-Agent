"""Приветствие по возвращении: выключено по умолчанию; отошёл → вернулся — «С возвращением, сэр» и
напоминания; никогда над игрой, в звонке, в тихом режиме, пока Jarvis занят, во время «Фокуса» и при своём
сценарии; режим «только если есть что сказать»; короткие отлучки и сон компьютера."""

import asyncio
from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.voice import character as ch
from jarvis.voice.character import with_address
from jarvis.voice.presence import Presence
from tests.conftest import EventLog

NOW = 1_790_000_000.0
AWAY_S = 25 * 60.0  # дольше presence_away_min (20 минут по умолчанию)


class Rig:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.platform: Any = ctx.platform
        self.presence: Presence = ctx.presence
        self.now = NOW
        self.presence.clock = lambda: self.now
        ctx.notes.clock = lambda: self.now  # type: ignore[attr-defined]
        ctx.scenarios.clock = lambda: self.now
        self.said: list[str] = []

    async def leave_and_return(self, away_s: float = AWAY_S) -> str:
        """Человек отошёл на away_s (Jarvis это заметил) и вернулся; вернёт итог проверки при возвращении."""
        self.platform.idle = away_s
        self.now += away_s
        assert await self.presence.tick() == "away"
        self.now += 60
        self.platform.idle = 1.0
        return await self.presence.tick()


@pytest.fixture
async def rig(ctx: AppContext) -> Rig:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    r = Rig(ctx)

    async def say(text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        r.said.append(text)

    ctx.tts.say = say  # type: ignore[method-assign]
    return r


def greeted(text: str) -> bool:
    return any(text.startswith(with_address(g, "сэр")) for g in ch.GREET_BACK)


async def test_off_by_default(rig: Rig) -> None:
    assert rig.ctx.settings.value.voice.presence_greeting == "off"
    rig.platform.idle = 3600.0
    assert await rig.presence.tick() == ""
    rig.platform.idle = 0.0
    assert await rig.presence.tick() == "" and rig.said == []


async def test_greets_on_return_with_reminders(rig: Rig, events: EventLog) -> None:
    rig.ctx.settings.patch({"voice": {"presence_greeting": "always"}})
    rig.platform.idle = 0.0
    assert await rig.presence.tick() == ""  # человек за компьютером — ничего
    rig.ctx.notes.reminder_add("Сходить в банк", NOW + AWAY_S + 2 * 3600)  # type: ignore[attr-defined]
    assert await rig.leave_and_return() == "greeted"
    assert len(rig.said) == 1 and greeted(rig.said[0])
    assert "Напоминаю: " in rig.said[0] and "— сходить в банк." in rig.said[0]
    toast = events.of("toast")[-1]
    assert toast["title"] == "С возвращением" and "Сходить в банк" in toast["message"]
    assert events.of("presence.greeted") == [{"away_min": 26, "news": True}]
    assert rig.presence.away_since() == pytest.approx(NOW)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda r: setattr(r.platform, "fullscreen", True), "fullscreen"),
        (lambda r: setattr(r.platform, "mic_users", ["Zoom.exe"]), "call"),
        (lambda r: r.ctx.settings.patch({"voice": {"silent_mode": True}}), "silent"),
        (lambda r: setattr(r.ctx.confirmations, "waiting", lambda: True), "busy"),  # ждёт «да / нет»
    ],
)
async def test_never_talks_over_games_calls_and_work(
    rig: Rig, events: EventLog, setup: Any, reason: str
) -> None:
    rig.ctx.settings.patch({"voice": {"presence_greeting": "always"}})
    setup(rig)
    assert await rig.leave_and_return() == reason
    assert rig.said == [] and events.of("presence.skipped") == [{"reason": reason}]
    assert not [t for t in events.of("toast") if t["title"] == "С возвращением"]


async def test_quiet_while_jarvis_speaks_or_listens(rig: Rig) -> None:
    rig.ctx.settings.patch({"voice": {"presence_greeting": "always"}})
    tts: Any = rig.ctx.tts
    tts._pending = 1  # говорит
    assert await rig.leave_and_return() == "busy"
    tts._pending = 0
    rig.ctx.voice.state = "listening"
    assert await rig.leave_and_return() == "busy"


async def test_quiet_during_focus_and_with_own_scenario(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.ctx.settings.patch({"voice": {"presence_greeting": "always"}})

    class Focus:
        enabled = True

        @staticmethod
        def status() -> dict[str, Any]:
            return {"phase": "paused"}

    monkeypatch.setattr(rig.ctx.plugins, "get", lambda plugin_id: Focus())
    assert await rig.leave_and_return() == "focus"  # «Фокус» сам скажет «С возвращением»
    monkeypatch.undo()
    rig.ctx.scenarios.create(
        {
            "name": "Привет",
            "triggers": [{"type": "idle_return", "after_min": 15}],
            "actions": [{"type": "Jarvis.Briefing", "value": "missed"}],
        }
    )
    assert (
        await rig.leave_and_return() == "scenario"
    )  # свой сценарий «С возвращением» — дважды не здороваемся
    assert rig.said == []


async def test_news_mode_greets_only_with_something_to_say(rig: Rig) -> None:
    rig.ctx.settings.patch({"voice": {"presence_greeting": "news"}})
    assert await rig.leave_and_return() == "nothing" and rig.said == []
    rig.now += 3600  # здороваться можно не чаще раза в 20 минут
    rig.ctx.notes.reminder_add("Позвонить маме", rig.now + AWAY_S + 1800)  # type: ignore[attr-defined]
    assert await rig.leave_and_return() == "greeted"
    assert "позвонить маме" in rig.said[-1]


async def test_mentions_reminders_fired_while_away(rig: Rig) -> None:
    rig.ctx.settings.patch({"voice": {"presence_greeting": "news"}})
    notes: Any = rig.ctx.notes
    rig.platform.idle = AWAY_S
    rig.now += AWAY_S
    assert await rig.presence.tick() == "away"
    notes.reminder_add("Сделать розыгрыш в канале", rig.now - 30)
    await notes.tick()  # сработало без человека (и прозвучало в пустой комнате)
    await asyncio.sleep(0)
    rig.said.clear()
    rig.now += 60
    rig.platform.idle = 2.0
    assert await rig.presence.tick() == "greeted"
    assert "Пока вас не было, сработало напоминание: сделать розыгрыш в канале." in rig.said[0]


async def test_short_breaks_and_sleep(rig: Rig) -> None:
    rig.ctx.settings.patch({"voice": {"presence_greeting": "always", "presence_away_min": 10}})
    rig.platform.idle = 9 * 60.0  # отошёл на 9 минут — это не «ушёл»
    rig.now += 9 * 60
    assert await rig.presence.tick() == ""
    rig.platform.idle = 1.0
    assert await rig.presence.tick() == "" and rig.said == []
    assert await rig.leave_and_return(away_s=11 * 60) == "greeted"
    assert await rig.presence.tick() == ""  # раз на возвращение
    # компьютер спал: между проверками — час, а простой после пробуждения короткий
    rig.platform.idle = 0.0
    assert await rig.presence.tick() == ""
    rig.now += 3600
    rig.platform.idle = 2.0
    assert await rig.presence.tick() == "greeted"


async def test_background_loop_runs(running_ctx: AppContext) -> None:
    presence: Any = running_ctx.presence
    assert presence._task is not None and not presence._task.done()
