"""«Что-нибудь ещё, сэр?»: после удачного дела голосом Jarvis спрашивает и слушает продолжение без «Джарвис»;
«нет», «всё» — «Как скажете, сэр» и больше не слушает. Не спрашивает, если слушать не будет (музыка, лимит
окон подряд, продолжение выключено), после «спасибо», выключения и с текста."""

import asyncio
from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.dispatcher import ASK_MORE, END_TALK_SAY
from jarvis.voice.character import with_address
from tests.ai.fakes import ScriptedLlm, no_sleep, reply


class Talk:
    """Что сказал Jarvis и какие окна продолжения открыл."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.follows: list[bool] = []  # question каждого окна
        self.can = True

    @property
    def text(self) -> str:
        return " ".join(self.said)

    def clear(self) -> None:
        self.said.clear()
        self.follows.clear()


@pytest.fixture
async def talk(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> Talk:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    await ctx.commands.refresh_apps()  # type: ignore[attr-defined]
    ctx.settings.patch({"ai": {"enabled": False}, "profile": {"address": "сэр"}})
    out = Talk()

    async def say(text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        out.said.append(text)

    monkeypatch.setattr(ctx.tts, "say", say)
    monkeypatch.setattr(ctx.voice, "can_follow", lambda *, question=False: out.can, raising=False)
    monkeypatch.setattr(
        ctx.voice, "follow_up", lambda seconds=None, *, question=False: out.follows.append(question)
    )
    return out


@pytest.fixture
def llm(ctx: AppContext) -> ScriptedLlm:
    ctx.secrets.set("ai_api_key", "sk-test")
    script = ScriptedLlm()
    ctx.ai.set_transport(script.transport)  # type: ignore[attr-defined]
    ctx.ai.llm.sleep = no_sleep  # type: ignore[attr-defined]
    return script


ASKS = {with_address(p, "сэр") for p in ASK_MORE}
ENDS = {with_address(p, "сэр") for p in END_TALK_SAY}


def asked(text: str) -> bool:
    return any(text.endswith(q) for q in ASKS)


async def test_asks_after_a_command_and_keeps_listening(ctx: AppContext, talk: Talk) -> None:
    await ctx.dispatcher.handle("сверни все окна", "voice")
    assert asked(talk.text) and talk.follows == [True]  # спросил и слушает без «Джарвис»
    talk.clear()
    await ctx.dispatcher.handle("включи музыку", "voice")  # ответ из окна продолжения — снова спрашивает
    assert asked(talk.text) and talk.follows == [True]


@pytest.mark.parametrize("answer", ["нет", "Нет, спасибо.", "всё", "это всё", "ничего", "больше ничего"])
async def test_no_ends_the_talk(ctx: AppContext, talk: Talk, answer: str) -> None:
    await ctx.dispatcher.handle("сверни все окна", "voice")
    talk.clear()
    result = await ctx.dispatcher.handle(answer, "voice")
    assert result["kind"] == "end" and talk.said and talk.said[0] in ENDS
    assert talk.follows == []  # больше не слушает без «Джарвис»


async def test_no_without_the_question_is_not_the_end(ctx: AppContext, talk: Talk) -> None:
    talk.can = False
    await ctx.dispatcher.handle("сверни все окна", "voice")
    assert not asked(talk.text) and talk.follows == []  # слушать не будет — не спрашивает
    result = await ctx.dispatcher.handle("нет", "voice")
    assert result["kind"] != "end"


@pytest.mark.parametrize("phrase", ["спасибо", "заблокируй компьютер", "выключи компьютер через 10 минут"])
async def test_no_question_when_the_talk_is_over(ctx: AppContext, talk: Talk, phrase: str) -> None:
    await ctx.dispatcher.handle(phrase, "voice")
    assert talk.said and not asked(talk.text) and talk.follows == []


async def test_setting_and_text_source(ctx: AppContext, talk: Talk) -> None:
    ctx.settings.patch({"voice": {"ask_more": False}})
    await ctx.dispatcher.handle("сверни все окна", "voice")
    assert talk.said and not asked(talk.text) and talk.follows == []
    ctx.settings.patch({"voice": {"ask_more": True}})
    talk.clear()
    await ctx.dispatcher.handle("сверни все окна", "text")  # с текста — молча, как всегда
    assert talk.said == [] and talk.follows == []


async def test_asks_after_an_ai_answer(ctx: AppContext, talk: Talk, llm: ScriptedLlm) -> None:
    ctx.settings.patch({"ai": {"enabled": True}})
    llm.add(reply("Столица Франции — Париж."))
    await ctx.dispatcher.handle("какая столица франции", "voice")
    assert talk.text.startswith("Столица Франции — Париж.") and asked(talk.text) and talk.follows == [True]
    talk.clear()
    llm.add(reply("О каком городе речь, сэр?"))  # Jarvis сам спросил — «Что-нибудь ещё?» не добавляет
    await ctx.dispatcher.handle("расскажи про этот город", "voice")
    assert talk.said == ["О каком городе речь, сэр?"] and talk.follows == [True]


def test_speech_without_address(ctx: AppContext) -> None:
    ctx.settings.patch({"profile": {"address": ""}})
    phrases: list[Any] = ctx.dispatcher.stock_phrases()  # type: ignore[attr-defined]
    assert "Что-нибудь ещё?" in phrases and "Как скажете." in phrases
