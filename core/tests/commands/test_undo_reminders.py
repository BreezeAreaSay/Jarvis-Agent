"""«Отмени» — последнее обратимое действие Jarvis (громкость, звук, окна, напоминание, открытая программа), не
было — Ctrl+Z; «закрой его» — последняя открытая программа; напоминания на время словами, список и отмена."""

from datetime import datetime
from typing import Any

import pytest

from jarvis.commands import undo
from jarvis.commands.service import Commands
from jarvis.commands.when import parse_text
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


async def test_reminder_at_list_and_cancel(cmds: Commands, ctx: AppContext) -> None:
    result = await run(cmds, "напомни мне завтра в 9 позвонить маме")
    assert result.ok and result.message.startswith("Напомню завтра в 9:00: позвонить маме")
    await run(cmds, "напомни забрать посылку в пятницу в 18:30")
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
    assert not missing.ok and "ничего не открывал" in missing.message
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
