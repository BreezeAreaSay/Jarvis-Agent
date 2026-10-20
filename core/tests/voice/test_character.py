"""Характер Джарвиса: фразы из роликов → команда, действие и ответ голосом в его духе; варианты без повторов;
обращение из профиля; персона «Обычный» — как раньше; «Секунду, сэр» у долгих команд; напоминания; частые
ответы — заранее."""

import asyncio
import re
from itertools import pairwise
from typing import Any

import pytest

from jarvis.context import AppContext
from jarvis.dispatcher import OK_PHRASES
from jarvis.platform.base import SystemStats
from jarvis.services import ExecResult
from jarvis.voice import character as ch
from jarvis.voice.character import Character, with_address
from tests.conftest import EventLog


class Speech:
    """Что Jarvis сказал: (текст, перебивая ли прежнюю речь)."""

    def __init__(self) -> None:
        self.items: list[tuple[str, bool]] = []

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.items]

    def clear(self) -> None:
        self.items.clear()


@pytest.fixture
async def speech(ctx: AppContext) -> Speech:
    """Голос в список; цикл привязан, программы «Пуска» прочитаны, ИИ выключен (неизвестное — «не понял»)."""
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    await ctx.commands.refresh_apps()  # type: ignore[attr-defined]
    ctx.settings.patch({"ai": {"enabled": False}})
    out = Speech()

    async def say(text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        out.items.append((text, interrupt))

    ctx.tts.say = say  # type: ignore[method-assign]
    ctx.character.random.seed(7)
    return out


def calls(ctx: AppContext, name: str) -> list[tuple[Any, ...]]:
    return [args for call, args in ctx.platform.calls if call == name]  # type: ignore[attr-defined]


def variants(*groups: tuple[str, ...], address: str = "сэр") -> set[str]:
    return {with_address(v, address) for group in groups for v in group}


async def ask(ctx: AppContext, speech: Speech, phrase: str) -> tuple[dict[str, Any], str]:
    speech.clear()
    result = await ctx.dispatcher.handle(phrase, "voice")
    return result, " ".join(speech.texts)


def command_id(result: dict[str, Any]) -> str:
    return ",".join(result["result"]["command_ids"]) if result.get("kind") == "command" else result["kind"]


# --- фразы из роликов -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "command", "replies"),
    [
        ("покажись", "builtin.jarvis.show", ch.REPLIES["builtin.jarvis.show"]),
        ("скройся", "builtin.jarvis.hide", ch.REPLIES["builtin.jarvis.hide"]),
        ("спасибо, джарвис", "builtin.jarvis.thanks", ch.REPLIES["builtin.jarvis.thanks"]),
        ("открой стим", "builtin.app.steam.open", ch.REPLIES["builtin.app.steam.open"]),
        ("открой ютуб", "builtin.site.youtube", ch.REPLIES["builtin.site.youtube"]),
        ("сверни все окна", "builtin.windows.minimize_all", ch.REPLIES["builtin.windows.minimize_all"]),
        ("пауза", "builtin.media.pause", ch.MEDIA["paused"]),
        ("закрой музыку", "builtin.media.close", ch.REPLIES["builtin.media.close"]),
        ("запусти майнкрафт", "builtin.app.minecraft.open", ch.GAME),
    ],
)
async def test_phrases_from_videos_reply_in_character(
    ctx: AppContext, speech: Speech, phrase: str, command: str, replies: tuple[str, ...]
) -> None:
    result, said = await ask(ctx, speech, phrase)
    assert command_id(result) == command and result["result"]["ok"]
    assert said in variants(replies), said
    assert speech.items[0][1] is True  # ответ перебивает прежнюю речь — как и раньше


async def test_actions_behind_the_replies(ctx: AppContext, speech: Speech, events: EventLog) -> None:
    await ask(ctx, speech, "покажись")
    await ask(ctx, speech, "скройся")
    assert events.of("ui.show") == [{}] and events.of("ui.hide") == [{}]
    await ask(ctx, speech, "открой стим")
    assert calls(ctx, "launch")[-1] == ("steam.exe", "")
    await ask(ctx, speech, "открой ютуб")
    assert calls(ctx, "open_url")[-1] == ("https://www.youtube.com",)
    await ask(ctx, speech, "сверни все окна")
    assert calls(ctx, "window_action")[-1] == ("minimize_all", None)
    await ask(ctx, speech, "закрой музыку")
    assert calls(ctx, "close_app")[-1] == ("Spotify.exe",)


async def test_thanks_is_only_a_reply(ctx: AppContext, speech: Speech, events: EventLog) -> None:
    before = len(events.of("toast"))
    result, said = await ask(ctx, speech, "спасибо")
    assert command_id(result) == "builtin.jarvis.thanks" and said
    assert len(events.of("toast")) == before  # «спасибо» — без карточки «Выполнено»


async def test_timer_and_shutdown_say_when(ctx: AppContext, speech: Speech) -> None:
    result, said = await ask(ctx, speech, "поставь таймер на 3 минуты")
    lead, tail = said.split(". ", 1)
    assert lead + "." in variants(ch.TIMER) and tail == "Сработает через 3 минуты."
    _, said = await ask(ctx, speech, "таймер 1 час")
    assert said.endswith("Сработает через 1 час.")
    result, said = await ask(ctx, speech, "выключи компьютер через 10 минут")
    assert command_id(result) == "builtin.system.shutdown_in" and not ctx.confirmations.pending()
    assert calls(ctx, "power")[-1] == ("shutdown", 600)
    assert said.endswith("Отключение через 10 минут.")
    assert said.removesuffix(" Отключение через 10 минут.") in variants(ch.SHUTDOWN_IN)


async def test_date_and_time_get_the_address(ctx: AppContext, speech: Speech) -> None:
    _, said = await ask(ctx, speech, "какой сегодня день")
    assert said.startswith("Сегодня ") and said.endswith(", сэр.")
    _, said = await ask(ctx, speech, "который час")
    assert said.startswith("Сейчас ") and said.endswith(", сэр.")


async def test_weather_in_character(ctx: AppContext, speech: Speech) -> None:
    async def data(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"city": "Москва", "temp": 19.2, "feels": 18, "condition": "ясно"}

    ctx.widgets.data = data  # type: ignore[method-assign]
    _, said = await ask(ctx, speech, "какая на улице погода")
    assert re.fullmatch(
        r"(Температура за бортом|За бортом|На улице сейчас) плюс 19 градусов, ясно, сэр\.", said
    )


async def test_music_replies_follow_what_happened(ctx: AppContext, speech: Speech) -> None:
    platform: Any = ctx.platform
    _, said = await ask(ctx, speech, "включи музыку")  # уже играет
    assert said == "Музыка уже играет, сэр."
    platform.media.status = "paused"
    _, said = await ask(ctx, speech, "включи музыку")
    assert said in variants(ch.MEDIA["playing"]) and platform.media.status == "playing"
    _, said = await ask(ctx, speech, "пауза")
    assert said in variants(ch.MEDIA["paused"])
    _, said = await ask(ctx, speech, "пауза")
    assert said == "Уже на паузе, сэр."


async def test_status_how_are_you(ctx: AppContext, speech: Speech) -> None:
    for phrase in ("как дела", "как системы", "статус систем"):
        result, said = await ask(ctx, speech, phrase)
        assert command_id(result) == "builtin.jarvis.status", phrase
        lead = next(v for v in variants(ch.STATUS_OK) if said.startswith(v))
        assert lead and "Процессор загружен на" in said and "память — на 46." in said
        assert "Температура видеокарты 54 градуса — в норме." in said  # nvidia-smi на заглушке — 54 °C
    platform: Any = ctx.platform
    platform.gpu_temp = None
    _, said = await ask(ctx, speech, "как дела")
    assert "Температур" not in said and "температур" not in said  # неизвестна — не выдумываем


async def test_status_mentions_trouble(ctx: AppContext, speech: Speech) -> None:
    stats = SystemStats(cpu=96, ram=93, disk=97, battery=9, charging=False, extra={"disk_free_gb": 4.2})
    ctx.platform.system_stats = lambda: stats  # type: ignore[method-assign]
    _, said = await ask(ctx, speech, "как дела")
    lead, _, details = said.partition(" Обратите внимание: ")
    assert lead in variants(ch.STATUS_BUSY)
    assert details == (
        "процессор загружен на 96 процентов; память занята на 93 процента; на системном диске осталось 4 "
        "гигабайт; заряд батареи — 9 процентов."
    )


# --- варианты, обращение, персона ---------------------------------------------------------------------------


async def test_variants_rotate_without_repeats(ctx: AppContext, speech: Speech) -> None:
    said = [(await ask(ctx, speech, "сверни все окна"))[1] for _ in range(8)]
    assert all(a != b for a, b in pairwise(said))
    assert len(set(said)) >= 2 and set(said) <= variants(ch.REPLIES["builtin.windows.minimize_all"])


async def test_ready_variants_go_first(ctx: AppContext, speech: Speech) -> None:
    ready = "Убираю окна, сэр."
    ctx.tts.is_ready = lambda text: text == ready  # type: ignore[method-assign]
    said = [(await ask(ctx, speech, "сверни все окна"))[1] for _ in range(4)]
    assert said[0] == ready and said[2] == ready  # готовый звук — первым, но не дважды подряд
    assert said[1] != ready and said[3] != ready


async def test_address_from_profile(ctx: AppContext, speech: Speech) -> None:
    ctx.settings.patch({"profile": {"address": ""}})
    said = {(await ask(ctx, speech, "сверни все окна"))[1] for _ in range(6)}
    assert said <= {"Сворачиваю все окна.", "Убираю окна.", "Окна свёрнуты."}
    ctx.settings.patch({"profile": {"address": "босс"}})
    _, said_one = await ask(ctx, speech, "покажись")
    assert said_one in variants(ch.REPLIES["builtin.jarvis.show"], address="босс") and "босс" in said_one


async def test_plain_persona_sounds_as_before(ctx: AppContext, speech: Speech) -> None:
    ctx.settings.patch({"ai": {"persona": "plain"}})
    plain_ok = {ctx.dispatcher._fmt(p) for p in OK_PHRASES}  # type: ignore[attr-defined]
    _, said = await ask(ctx, speech, "сверни все окна")
    assert said in plain_ok
    _, said = await ask(ctx, speech, "открой стим")
    assert said == "Открываю Steam."
    _, said = await ask(ctx, speech, "поставь таймер на 3 минуты")
    assert said == "Поставил таймер на 3 минуты"
    _, said = await ask(ctx, speech, "как дела")
    assert said.startswith("Процессор загружен на") and "свободно 186 гигабайт" in said
    _, said = await ask(ctx, speech, "спасибо")
    assert said == "Пожалуйста."
    assert ctx.character.stock_phrases() == []


async def test_confirmations_off_keeps_answers_only(ctx: AppContext, speech: Speech) -> None:
    ctx.settings.patch({"voice": {"speak_confirmations": "off"}})
    _, said = await ask(ctx, speech, "сверни все окна")
    assert said == ""  # подтверждение выключено — и персонаж молчит
    _, said = await ask(ctx, speech, "как дела")
    assert said  # а ответ на вопрос звучит


async def test_text_source_is_silent(ctx: AppContext, speech: Speech) -> None:
    speech.clear()
    await ctx.dispatcher.handle("сверни все окна", "text")
    assert speech.items == []


def test_with_address() -> None:
    assert with_address("Готово, {address}.", "сэр") == "Готово, сэр."
    assert with_address("Готово, {address}.", "") == "Готово."
    assert with_address("{Address}, напоминаю: {text}.", "") == "Напоминаю: {text}."
    assert with_address("{Address}, напоминаю: {text}.", "сэр") == "Сэр, напоминаю: {text}."
    assert (
        with_address("Системы работают, {address}, но есть нюансы.", "")
        == "Системы работают, но есть нюансы."
    )
    assert with_address("Скрываюсь, {address}. Позовите.", "") == "Скрываюсь. Позовите."


def test_polite_only_for_short_statements(ctx: AppContext) -> None:
    c: Character = ctx.character
    assert c.polite("Сейчас 14 часов") == "Сейчас 14 часов, сэр."
    assert c.polite("Готово, сэр.") == "Готово, сэр."
    assert c.polite("Что дальше?") == "Что дальше?"
    assert c.polite("Первое. Второе.") == "Первое. Второе."
    assert c.polite("x" * 200) == "x" * 200


def test_game_and_games_mode_replies(ctx: AppContext) -> None:
    c: Character = ctx.character
    game = ExecResult(
        True, "Открыть CS2", command_ids=["builtin.apps.open"], data={"app": "CS2", "game": True}
    )
    assert c.spoken(game) in variants(ch.GAME)
    # «Игровой режим» (ветка игр): своя фраза команды уступает характеру
    mode = ExecResult(
        True, "Игровой режим", say="Игровой режим: окна свёрнуты.", command_ids=["builtin.games.mode"]
    )
    assert c.spoken(mode) in variants(ch.REPLIES["builtin.games.mode"])
    user = ExecResult(True, "Стрим", say="Стрим начался", command_ids=["user.stream"])
    assert c.spoken(user) is None  # своя фраза человека — его
    chain = ExecResult(True, "А и Б", command_ids=["builtin.volume.up", "builtin.media.next"])
    assert c.spoken(chain) is None
    failed = ExecResult(False, "Х", "Spotify не запущен", command_ids=["builtin.app.spotify.close"])
    assert c.spoken(failed) is None


# --- «Секунду, сэр» ----------------------------------------------------------------------------------------


async def test_slow_command_acknowledges_at_once(ctx: AppContext, speech: Speech) -> None:
    async def slow(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"city": "Москва", "temp": -3, "condition": "снег"}

    ctx.widgets.data = slow  # type: ignore[method-assign]
    ctx.character.ack_after_s = 0.05
    await ask(ctx, speech, "какая погода")
    (ack, ack_interrupt), (final, final_interrupt) = speech.items
    assert ack in variants(ch.ACK_WEATHER) and ack_interrupt is True
    assert "минус 3 градуса, снег" in final and final_interrupt is False  # итог — следом, не перебивая


async def test_fast_command_does_not_acknowledge(ctx: AppContext, speech: Speech) -> None:
    async def fast(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"city": "Москва", "temp": 5, "condition": "облачно"}

    ctx.widgets.data = fast  # type: ignore[method-assign]
    _, said = await ask(ctx, speech, "какая погода")
    assert len(speech.items) == 1 and speech.items[0][1] is True and "плюс 5 градусов" in said


async def test_no_acknowledgement_in_plain_persona_or_silent_mode(ctx: AppContext, speech: Speech) -> None:
    async def slow(kind: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"city": "Москва", "temp": 5, "condition": "облачно"}

    ctx.widgets.data = slow  # type: ignore[method-assign]
    ctx.character.ack_after_s = 0.02
    ctx.settings.patch({"ai": {"persona": "plain"}})
    await ask(ctx, speech, "какая погода")
    assert len(speech.items) == 1
    ctx.settings.patch({"ai": {"persona": "jarvis"}, "voice": {"silent_mode": True}})
    await ask(ctx, speech, "какая погода")
    assert speech.items == []


# --- напоминания --------------------------------------------------------------------------------------------


async def test_reminders_in_character(ctx: AppContext, speech: Speech) -> None:
    notes: Any = ctx.notes
    now = 1_790_000_000.0
    notes.clock = lambda: now
    notes.reminder_add("Позвонить маме", now - 2)
    notes.reminder_add("Таймер на 3 минуты", now - 1)
    notes.event_add("Созвон с командой", now + 600, remind_min=10)
    await notes.tick()
    call, timer, event = speech.texts

    def filled(group: tuple[str, ...], text: str) -> set[str]:
        return {with_address(v, "сэр").replace("{text}", text) for v in group}

    assert call in filled(ch.REMIND, "позвонить маме")  # «Сэр, напоминаю: позвонить маме.»
    assert timer in filled(ch.REMIND_TIMER, "таймер на 3 минуты")  # «Сэр, время вышло: таймер на 3 минуты.»
    assert event in filled(ch.REMIND_EVENT, "созвон с командой — через 10 минут")
    notes.reminder_add("Полить цветы", now - 3 * 3600)  # Jarvis был выключен — пропущено
    speech.clear()
    await notes.tick()
    assert speech.texts == ["Сэр, пока вас не было, сработало напоминание: полить цветы."]


def test_reminder_phrases_without_address_and_in_plain_persona(ctx: AppContext) -> None:
    c: Character = ctx.character
    ctx.settings.patch({"profile": {"address": ""}})
    phrase = c.reminder({"kind": "reminder"}, "Позвонить маме", missed=False)
    assert phrase in {
        "Напоминаю: позвонить маме.",
        "Позвольте напомнить: позвонить маме.",
        "Вы просили напомнить: позвонить маме.",
    }
    ctx.settings.patch({"profile": {"address": "сэр"}, "ai": {"persona": "plain"}})
    assert (
        c.reminder({"kind": "reminder"}, "Позвонить маме", missed=False) == "Напоминаю, сэр: Позвонить маме"
    )


# --- частые ответы заранее ---------------------------------------------------------------------------------


def test_stock_phrases_first_variants_first(ctx: AppContext) -> None:
    stock = ctx.character.stock_phrases()
    assert "Сворачиваю все окна, сэр." in stock and "Приятного просмотра, сэр." in stock
    assert all("{" not in p for p in stock) and len(stock) == len(set(stock))
    firsts = {with_address(g[0], "сэр") for g in ch._STOCK_GROUPS}
    assert set(stock[: len(firsts)]) == firsts  # первые варианты всех ответов — первыми
    ctx.settings.patch({"profile": {"address": "босс"}})
    assert "Сворачиваю все окна, босс." in ctx.character.stock_phrases()
