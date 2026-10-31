"""«Отмени» — последнее обратимое действие Jarvis (громкость, звук, окна, напоминание, открытая программа), не
было — Ctrl+Z; «закрой его» — последняя открытая программа; напоминания на время словами, список и отмена."""

from datetime import datetime
from typing import Any

import pytest

from jarvis.commands import undo, when
from jarvis.commands.service import Commands
from jarvis.commands.when import parse_moment, parse_text
from jarvis.context import AppContext
from jarvis.services import ExecResult
from tests.commands.conftest import calls

FRIDAY = datetime(2026, 10, 30, 15, 0)  # пятница, 15:00


async def run(cmds: Commands, text: str) -> ExecResult:
    result = await cmds.run_text(text, "text")  # type: ignore[arg-type]
    assert result is not None, f"не распознано: {text!r}"
    return result


@pytest.mark.parametrize(
    ("text", "moment"),
    [
        ("завтра в 9", datetime(2026, 10, 31, 9, 0)),
        ("в 18:30", datetime(2026, 10, 30, 18, 30)),
        ("сегодня вечером", datetime(2026, 10, 30, 19, 0)),
        ("в пятницу в 10 утра", datetime(2026, 11, 6, 10, 0)),  # сегодня пятница, 10 утра прошло
        ("в пятницу вечером", datetime(2026, 10, 30, 19, 0)),
        ("в понедельник", datetime(2026, 11, 2, 9, 0)),
        ("послезавтра в полдень", datetime(2026, 11, 1, 12, 0)),
        ("31 октября в 16:00", datetime(2026, 10, 31, 16, 0)),
        ("29 октября", datetime(2027, 10, 29, 9, 0)),
        ("в эту субботу", datetime(2026, 10, 31, 9, 0)),
        ("в 7", datetime(2026, 10, 30, 19, 0)),  # «в 7» в три часа дня — вечер
        ("в 16", datetime(2026, 10, 30, 16, 0)),
        ("в девять тридцать вечера", datetime(2026, 10, 30, 21, 30)),
        ("в 11 ночи", datetime(2026, 10, 30, 23, 0)),
        ("в 2 дня", datetime(2026, 10, 31, 14, 0)),  # 14:00 сегодня прошло — завтра
        ("в двадцать один тридцать", datetime(2026, 10, 30, 21, 30)),
    ],
)
def test_moment_words(text: str, moment: datetime) -> None:
    assert parse_text(text, FRIDAY) == moment


@pytest.mark.parametrize(
    "text", ["купить хлеб", "в магазин", "завтра в магазин", "через 5 минут", "в 25", "сегодня в 9 утра", ""]
)
def test_not_a_moment(text: str) -> None:
    assert parse_text(text, FRIDAY) is None


@pytest.mark.parametrize(
    ("text", "moment"),
    [
        ("в следующую пятницу в 7 30 вечера", datetime(2026, 11, 6, 19, 30)),
        ("в следующий понедельник в девять тридцать утра", datetime(2026, 11, 2, 9, 30)),
        ("сегодня в 2 ночи", datetime(2026, 10, 31, 2, 0)),  # сказано днём — ближайшая ночь
        ("в 2 ночи", datetime(2026, 10, 31, 2, 0)),
        ("в полночь", datetime(2026, 10, 31, 0, 0)),
    ],
)
def test_moment_night_and_long_phrases(text: str, moment: datetime) -> None:
    assert parse_text(text, FRIDAY) == moment


def test_night_with_a_named_date_or_weekday_keeps_that_day() -> None:
    late = datetime(2026, 10, 30, 3, 0)  # пятница, 3 часа ночи
    assert when.moment(["30", "октября", "в", "2", "ночи"], late) == datetime(2026, 10, 30, 2, 0)  # прошло
    assert parse_text("30 октября в 2 ночи", late) is None
    assert parse_text("в пятницу в 2 ночи", late) == datetime(2026, 11, 6, 2, 0)  # следующая пятница
    assert parse_text("сегодня в 2 ночи", FRIDAY) == datetime(2026, 10, 31, 2, 0)  # сказано днём


def test_past_time_is_still_a_time_phrase() -> None:
    words = ["сегодня", "в", "9", "утра"]
    assert parse_moment(words, FRIDAY) is None and when.moment(words, FRIDAY) == datetime(2026, 10, 30, 9)
    assert when.longest_at(words, 0, FRIDAY) == 4  # слот берёт «в 9 утра» целиком, а не вечерние «в 9»
    assert when.moment(["29", "февраля"], FRIDAY) is None  # в 2026 и 2027 его нет


@pytest.mark.parametrize(
    ("text", "moment", "what"),
    [
        ("напомни в следующую пятницу в 7 вечера созвон", "в следующую пятницу в 7 вечера", "созвон"),
        ("напомни созвон в следующую пятницу в 7 30 вечера", "в следующую пятницу в 7 30 вечера", "созвон"),
        (
            "напомни позвонить маме в следующий понедельник в девять тридцать утра",
            "в следующий понедельник в девять тридцать утра",
            "позвонить маме",
        ),
        ("напомни мне сегодня в 2 ночи выключить сервер", "сегодня в 2 ночи", "выключить сервер"),
        ("напомни сегодня в 9 утра позвонить", "сегодня в 9 утра", "позвонить"),
    ],
)
def test_reminder_takes_the_whole_time(ctx: AppContext, text: str, moment: str, what: str) -> None:
    found = ctx.commands.match(text)  # type: ignore[attr-defined]
    assert found is not None and [p.command_id for p in found.parts] == ["builtin.timers.remind_at"]
    assert found.parts[0].slots == {"when": moment, "text": what}


@pytest.mark.parametrize(
    "text",
    [
        "напомни что у меня завтра в 9",
        "напомни во сколько у меня встреча в пятницу",
        "напомни мне где я оставил ключи завтра",
        "напомни через 10 минут что я хотел сделать",
    ],
)
def test_reminder_question_goes_to_ai(ctx: AppContext, text: str) -> None:
    assert ctx.commands.match(text) is None  # type: ignore[attr-defined]


async def test_reminder_in_the_past_is_explained(cmds: Commands, ctx: AppContext) -> None:
    result = await run(cmds, "напомни сегодня в 0 часов проверить почту")
    assert not result.ok and result.message == "Это время уже прошло: «сегодня в 0 часов»"
    assert ctx.notes.reminders_list(include_done=False) == []


async def test_reminder_at_list_and_cancel(cmds: Commands, ctx: AppContext) -> None:
    result = await run(cmds, "напомни мне завтра в 9 позвонить маме")
    assert result.ok and result.message.startswith("Напомню завтра в 9:00: позвонить маме")
    await run(cmds, "напомни мне забрать посылку в пятницу в 18:30")  # «мне» в текст не попадает
    texts = sorted(r["text"] for r in ctx.notes.reminders_list(include_done=False))
    assert texts == ["Забрать посылку", "Позвонить маме"]
    listing = await run(cmds, "какие у меня напоминания")
    assert listing.message.startswith("2 напоминания:") and "Позвонить маме" in listing.say
    # «напомни …» без времени — не команда: решает ИИ (может спросить, когда)
    assert ctx.commands.match("напомни купить хлеб") is None  # type: ignore[attr-defined]
    removed = await run(cmds, "отмени напоминание про посылку")
    assert removed.ok and "Забрать посылку" in removed.message
    last = await run(cmds, "удали напоминание")
    assert last.ok and "Позвонить маме" in last.message
    empty = await run(cmds, "какие у меня напоминания")
    assert empty.message == "Напоминаний нет"


async def test_undo_volume_mute_windows_and_reminder(cmds: Commands, ctx: AppContext) -> None:
    ctx.platform.volume = 40  # type: ignore[attr-defined]
    await run(cmds, "громкость на 80")
    result = await run(cmds, "отмени")
    assert result.ok and result.message == "Отменил: громкость снова 40%"
    assert calls(ctx, "volume_set")[-1] == (40,)
    await run(cmds, "выключи звук")
    await run(cmds, "отмени")
    assert calls(ctx, "mute_set")[-1] == (False,)
    await run(cmds, "сверни все окна")
    await run(cmds, "отмени")
    assert calls(ctx, "window_action")[-1] == ("undo_minimize_all", None)
    await run(cmds, "напомни через 10 минут проверить духовку")
    assert len(ctx.notes.reminders_list(include_done=False)) == 1
    await run(cmds, "отмени")
    assert ctx.notes.reminders_list(include_done=False) == []
    # шагов не осталось — как раньше: Ctrl+Z в активном окне
    plain = await run(cmds, "отмени")
    assert plain.message == "Отменил последнее действие в окне (Ctrl+Z)"
    assert calls(ctx, "press_keys")[-1] == ("ctrl+z",)


async def test_undo_forgets_old_steps(
    cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    cmds.executor.undo.clock = lambda: clock["now"]  # type: ignore[attr-defined]
    await run(cmds, "громкость на 70")
    clock["now"] += undo.TTL_S + 1  # прошло больше пяти минут — «отмени» уже не про громкость
    await run(cmds, "отмени")
    assert calls(ctx, "press_keys")[-1] == ("ctrl+z",)


async def test_close_it_after_open(cmds: Commands, ctx: AppContext) -> None:
    missing = await run(cmds, "закрой его")
    assert not missing.ok and "какую программу закрыть" in missing.message
    await run(cmds, "открой спотифай")
    closed = await run(cmds, "закрой его")
    assert closed.ok and closed.message == "Закрыл Spotify"
    assert calls(ctx, "close_app")[-1] == ("Spotify.exe",)
    again = await run(cmds, "закрой его")
    assert not again.ok  # уже закрыли — второй раз не закрываем что попало
    # «отмени» после «открой …» тоже закрывает
    await run(cmds, "открой телеграм")
    await run(cmds, "отмени")
    assert calls(ctx, "close_app")[-1] == ("Telegram.exe",)


def test_journal_limits() -> None:
    journal = undo.UndoJournal(clock=lambda: 0.0)

    async def nothing() -> None:
        return None

    for i in range(undo.LIMIT + 5):
        journal.push(f"шаг {i}", nothing)
    steps: list[Any] = []
    while (step := journal.pop()) is not None:
        steps.append(step.title)
    assert len(steps) == undo.LIMIT and steps[0] == f"шаг {undo.LIMIT + 4}"


async def test_undo_does_not_close_an_app_that_was_already_running(cmds: Commands, ctx: AppContext) -> None:
    assert ctx.platform.is_running("Spotify.exe")  # окно Spotify открыто до команды
    await run(cmds, "открой блокнот")
    await run(cmds, "открой спотифай")
    result = await run(cmds, "отмени")  # не закрывает ни Spotify, ни открытый раньше Блокнот
    assert result.message == "Spotify был открыт и до команды — закрывать не стал"
    assert not calls(ctx, "close_app")
    closed = await run(cmds, "закрой его")  # сказано прямо — закрываем
    assert closed.ok and calls(ctx, "close_app") == [("Spotify.exe",)]


async def test_close_it_then_undo_does_not_close_again(cmds: Commands, ctx: AppContext) -> None:
    await run(cmds, "открой телеграм")
    await run(cmds, "закрой его")
    launches = len(calls(ctx, "launch"))
    result = await run(cmds, "отмени")  # «верни как было» — открыть снова, а не закрыть второй раз
    assert result.message == "Отменил: Telegram снова открыт"
    assert calls(ctx, "close_app") == [("Telegram.exe",)] and len(calls(ctx, "launch")) == launches + 1


async def test_site_or_folder_after_app_is_not_it(cmds: Commands, ctx: AppContext) -> None:
    await run(cmds, "открой телеграм")
    await run(cmds, "открой ютуб")
    result = await run(cmds, "закрой его")
    assert not result.ok and not calls(ctx, "close_app")  # «его» — сайт, а не Telegram
    await run(cmds, "открой телеграм")
    await run(cmds, "открой загрузки")
    assert not (await run(cmds, "закрой его")).ok


def test_close_it_and_undo_are_not_chained(ctx: AppContext) -> None:
    for text in ("открой блокнот и закрой его", "сверни все окна и отмени"):
        found = ctx.commands.match(text)  # type: ignore[attr-defined]
        ids = [p.command_id for p in found.parts] if found is not None else []
        assert "builtin.windows.close_last" not in ids and "builtin.keyboard.undo" not in ids, text
    found = ctx.commands.match("выключи его")  # type: ignore[attr-defined]
    assert found is None or found.parts[0].command_id != "builtin.windows.close_last"


async def test_reminder_cancel_by_word_form_and_recency(cmds: Commands, ctx: AppContext) -> None:
    notes: Any = ctx.notes
    real = notes.clock
    notes.clock = lambda: real() - 1200  # поставлено 20 минут назад
    notes.reminder_add("Позвонить маме", real() + 3600)
    notes.clock = real
    vague = await run(cmds, "удали напоминание")
    assert not vague.ok and "Какое напоминание удалить" in vague.message and "Позвонить маме" in vague.message
    assert len(notes.reminders_list(include_done=False)) == 1
    removed = await run(cmds, "удали напоминание про маму")  # «маму» ↔ «маме»
    assert removed.ok and notes.reminders_list(include_done=False) == []


async def test_reminder_cancel_all_needs_its_own_command(cmds: Commands, ctx: AppContext) -> None:
    await run(cmds, "напомни через 10 минут проверить духовку")
    await run(cmds, "напомни через час позвонить маме")
    refused = await run(cmds, "отмени напоминание про все")
    assert not refused.ok and "удали все напоминания" in refused.message
    assert len(ctx.notes.reminders_list(include_done=False)) == 2
