"""«Умная диктовка»: распознанный текст (заглушка голоса) правится ИИ и вставляется через платформу."""

from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.platform.fake import FakePlatform
from jarvis.plugins.dictation import Dictation, similar, split_stop
from tests.conftest import EventLog
from tests.plugins.conftest import FakeAi, FakeCommands, FakeVoice, enable, manager, wait_until

POLISHED = {"привет мир": "Привет, мир.", "как дела": "Как дела?"}


def pasted(ctx: AppContext) -> list[str]:
    platform = ctx.platform
    assert isinstance(platform, FakePlatform)
    return [args[0] for name, args in platform.calls if name == "paste_text"]


def plugin_of(ctx: AppContext) -> Dictation:
    plugin = manager(ctx).get("dictation")
    assert isinstance(plugin, Dictation)
    return plugin


def polish(messages: list[dict[str, Any]]) -> str:
    return POLISHED.get(messages[-1]["content"], messages[-1]["content"])


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("стоп диктовка", ("", True)),
        ("Хватит!", ("", True)),
        ("и это всё, стоп диктовка.", ("и это всё", True)),
        ("стоп-кран сорвали", ("стоп-кран сорвали", False)),
        ("продолжаем писать", ("продолжаем писать", False)),
    ],
)
def test_split_stop(heard: str, expected: tuple[str, bool]) -> None:
    assert split_stop(heard) == expected


def test_similar_guards_against_answers() -> None:
    assert similar("привет как дела", "Привет, как дела?")
    assert not similar("привет как дела", "Я — языковая модель и не могу ответить на этот вопрос подробно.")


async def test_dictation_pastes_polished_text(pctx: AppContext, pevents: EventLog) -> None:
    pctx.ai = FakeAi(configured=True, reply=polish)  # type: ignore[assignment]
    voice = pctx.voice
    assert isinstance(voice, FakeVoice)
    voice.phrases = ["привет мир", "как дела", "стоп диктовка"]
    await enable(pctx, "dictation")
    assert await manager(pctx).action("dictation", "toggle", {}) == {"ok": True, "active": True}
    plugin = plugin_of(pctx)
    await wait_until(lambda: not plugin.active)
    assert pasted(pctx) == ["Привет, мир. ", "Как дела? "]
    states = [(e["state"], e["text"]) for e in pevents.of("plugin.dictation.state")]
    assert states[0][0] == "listening"
    assert ("processing", "привет мир") in states and ("processing", "как дела") in states
    assert states[-1] == ("idle", "")
    assert plugin.status()["last_text"] == "Как дела?"
    assert len(pctx.ai.calls) == 2  # type: ignore[attr-defined]


async def test_dictation_without_ai_and_tail_stop(pctx: AppContext) -> None:
    voice = pctx.voice
    assert isinstance(voice, FakeVoice)
    voice.phrases = ["это последняя фраза стоп диктовка"]
    await enable(pctx, "dictation")
    await manager(pctx).action("dictation", "start", {})
    await wait_until(lambda: not plugin_of(pctx).active)
    assert pasted(pctx) == ["это последняя фраза "]


async def test_ai_answer_instead_of_edit_is_ignored(pctx: AppContext) -> None:
    pctx.ai = FakeAi(
        configured=True, reply="Конечно! Вот подробный рецепт борща на четыре порции и даже больше."
    )  # type: ignore[assignment]
    voice = pctx.voice
    assert isinstance(voice, FakeVoice)
    voice.phrases = ["купи хлеба", "хватит"]
    await enable(pctx, "dictation")
    await manager(pctx).action("dictation", "start", {})
    await wait_until(lambda: not plugin_of(pctx).active)
    assert pasted(pctx) == ["купи хлеба "]


async def test_dictation_stops_after_silence(pctx: AppContext) -> None:
    await enable(pctx, "dictation", config={"silence_s": 5})
    plugin = plugin_of(pctx)
    clock = [1000.0]
    plugin.clock = lambda: clock[0]
    voice = pctx.voice
    assert isinstance(voice, FakeVoice)

    def wait_whole_timeout(timeout: float) -> None:
        clock[0] += timeout

    voice.on_capture = wait_whole_timeout
    await manager(pctx).action("dictation", "start", {})
    await wait_until(lambda: not plugin.active)
    assert voice.captures == [5.0] and pasted(pctx) == []


async def test_voice_commands_and_toggle(pctx: AppContext) -> None:
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    voice = pctx.voice
    assert isinstance(voice, FakeVoice)
    voice.phrases = ["первая фраза"]
    voice.empty_delay = 5.0  # дальше тишина — диктовка ждёт, пока её не остановят
    await enable(pctx, "dictation", config={"silence_s": 60})
    plugin = plugin_of(pctx)
    result = await commands.get("plugin.dictation:start").handler({})
    assert result.ok and result.say == "Диктуйте." and plugin.active
    await wait_until(lambda: pasted(pctx) == ["первая фраза "])
    again = await commands.get("plugin.dictation:start").handler({})
    assert again.ok and again.message == "Диктовка уже идёт"
    assert await manager(pctx).action("dictation", "toggle", {}) == {"ok": True, "active": False}
    assert not plugin.active and plugin.status()["state"] == "idle"
    stopped = await commands.get("plugin.dictation:stop").handler({})
    assert stopped.message == "Диктовка не шла"
