"""«Отмени»: громкость, окна, «свернуть все», напоминания и память возвращаются как было; необратимое —
«Это не отменить»; второй раз — «Нечего отменять». Команды проходят настоящие grammar.match и execute.run,
pc — фейк."""

import time
from pathlib import Path
from typing import Any

import pytest

from jarvis import execute, grammar, memory, reminders
from jarvis.context import TTL_S, Context, load_cli, save_cli
from pc import apps, audio, files, windows
from pc.result import Result, fail, ok
from pc.windows import WindowInfo

NOTE = WindowInfo(77, "Без имени — Блокнот", 4242, "Notepad.exe")


class FakePC:
    """Звук и окна: что вызвано и с какими аргументами; уровень и mute меняются как у настоящего pc.audio."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.level, self.muted = 40, False
        self.calls: list[tuple[Any, ...]] = []
        self.fail: set[str] = set()
        monkeypatch.setattr(audio, "volume", self.volume)
        monkeypatch.setattr(audio, "restore", self.restore)
        monkeypatch.setattr(windows, "window_action", self.window_action)
        monkeypatch.setattr(windows, "find_window", self.find_window)
        monkeypatch.setattr(windows, "close_target", self.close_target)
        monkeypatch.setattr(files, "open_target", self.open_target)
        apps.set_inventory([{"name": "Блокнот", "app_id": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"}])

    def _state(self, prev: dict[str, Any]) -> dict[str, Any]:
        return {"level": self.level, "muted": self.muted, "prev": prev}

    def volume(
        self, set: int | None = None, delta: int | None = None, mute: bool | None = None, caller: str = "user"
    ) -> Result:
        self.calls.append(("volume", set, delta, mute, caller))
        if "volume" in self.fail:
            return fail("Звук не отвечает")
        prev = {"level": self.level, "muted": self.muted}
        if mute is not None:
            self.muted = mute
        else:
            self.level = max(0, min(100, set if set is not None else self.level + (delta or 0)))
            if (set or 0) > 0 or (delta or 0) > 0:
                self.muted = False
        return ok(f"Громкость {self.level} %", self._state(prev))

    def restore(self, level: int, muted: bool, caller: str = "user") -> Result:
        self.calls.append(("restore", level, muted, caller))
        if "restore" in self.fail:
            return fail("Звук не отвечает")
        prev = {"level": self.level, "muted": self.muted}
        self.level, self.muted = level, muted
        return ok(f"Громкость {level} %", self._state(prev))

    def window_action(self, action: str, target: str | int | None, caller: str) -> Result:
        self.calls.append(("window_action", action, target, caller))
        if "window_action" in self.fail:
            return fail("Окна уже нет.")
        return ok({"restore": "Восстановил «Блокнот»", "undo_minimize_all": "Вернул окна"}.get(action, "ок"))

    def find_window(self, target: str | int) -> WindowInfo | None:
        return NOTE if target in (NOTE.hwnd, "Блокнот") else None

    def close_target(self, target: str | int, caller: str) -> Result:
        self.calls.append(("close_target", target, caller))
        return fail("Не закрылось") if "close_target" in self.fail else ok("Закрываю «Блокнот»")

    def open_target(self, target: str, kind: str | None, caller: str) -> Result:
        self.calls.append(("open_target", target, kind, caller))
        return ok("Открыл")

    def undo_calls(self) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == "restore" or (c[0] == "window_action" and "restore" in c[1])]


@pytest.fixture
def pc(monkeypatch: pytest.MonkeyPatch) -> FakePC:
    fake = FakePC(monkeypatch)
    yield fake
    apps.set_inventory(None)


@pytest.fixture
def ctx() -> Context:
    return Context(active_window=NOTE)


def say(text: str, ctx: Context) -> execute.Outcome:
    hit = grammar.match(text, ctx)
    assert hit is not None, f"грамматика не узнала {text!r}"
    return execute.run(hit.action, hit.args, text, ctx, "grammar")


# --- громкость ----------------------------------------------------------------------------------------


def test_volume_set_then_undo(pc: FakePC, ctx: Context) -> None:
    assert say("громкость 30", ctx).ok and pc.level == 30
    out = say("отмени", ctx)
    assert (out.kind, out.ok, out.text, out.autohide, out.action) == (
        "done", True, "Вернул громкость 40 %", True, "undo"
    )  # fmt: skip
    assert pc.calls[-1] == ("restore", 40, False, "user") and pc.level == 40
    again = say("отмени", ctx)
    assert (again.ok, again.text) == (False, "Нечего отменять")


@pytest.mark.parametrize("phrase", ["верни как было", "верни громкость", "отмена", "верни назад", "Отмени!"])
def test_mute_then_undo_phrases(pc: FakePC, ctx: Context, phrase: str) -> None:
    say("выключи звук", ctx)
    assert pc.muted
    out = say(phrase, ctx)
    assert out.ok and not pc.muted and pc.calls[-1] == ("restore", 40, False, "user")


def test_undo_returns_mute_too(pc: FakePC, ctx: Context) -> None:
    pc.muted = True
    say("громче", ctx)  # громче снимает mute
    assert not pc.muted
    out = say("отмени", ctx)
    assert out.text == "Вернул громкость 40 %, звук выключен" and pc.muted and pc.level == 40


def test_hands_volume_is_undoable(pc: FakePC, ctx: Context) -> None:
    out = execute.run("vol", {"delta": -10}, "сделай потише", ctx, "hands")
    assert out.ok and pc.level == 30
    assert say("отмени", ctx).ok and pc.level == 40


def test_undo_failure_keeps_record(pc: FakePC, ctx: Context) -> None:
    say("громкость 10", ctx)
    pc.fail.add("restore")
    out = say("отмени", ctx)
    assert not out.ok and out.text == "Звук не отвечает"
    pc.fail.clear()
    assert say("отмени", ctx).ok and pc.level == 40  # повтор после сбоя — работает


# --- окна ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "action"), [("сверни", "minimize"), ("разверни на весь экран", "maximize")]
)
def test_window_then_undo_restores_same_hwnd(pc: FakePC, ctx: Context, phrase: str, action: str) -> None:
    say(phrase, ctx)
    assert pc.calls[-1] == ("window_action", action, NOTE.hwnd, "user")
    out = say("верни окно", ctx)
    assert out.ok and out.text == "Восстановил «Блокнот»" and out.autohide
    assert pc.calls[-1] == ("window_action", "restore", NOTE.hwnd, "user")


def test_window_by_name_then_undo(pc: FakePC, ctx: Context) -> None:
    say("сверни блокнот", ctx)
    assert pc.calls[-1] == ("window_action", "minimize", "Блокнот", "user")
    say("отмени", ctx)
    assert pc.calls[-1] == ("window_action", "restore", NOTE.hwnd, "user")


def test_minimize_all_then_undo(pc: FakePC, ctx: Context) -> None:
    say("сверни все окна", ctx)
    out = say("верни все окна", ctx)
    assert out.ok and out.text == "Вернул окна"
    assert pc.calls[-1] == ("window_action", "undo_minimize_all", None, "user")


def test_restore_is_not_undoable(pc: FakePC, ctx: Context) -> None:
    say("восстанови окно", ctx)
    before = len(pc.calls)
    out = say("отмени", ctx)
    assert (out.ok, out.text) == (False, "Это не отменить") and pc.calls[before:] == []


# --- необратимое и свежесть ---------------------------------------------------------------------------


def test_nothing_to_undo(pc: FakePC, ctx: Context) -> None:
    out = say("отмени", ctx)
    assert (out.kind, out.ok, out.text, out.autohide) == ("done", False, "Нечего отменять", False)
    assert pc.calls == []


def test_irreversible_last_action(pc: FakePC, ctx: Context) -> None:
    say("громкость 30", ctx)
    say("закрой блокнот", ctx)  # последнее действие — необратимое: громкость не трогаем
    for _ in range(2):
        out = say("отмени", ctx)
        assert (out.ok, out.text) == (False, "Это не отменить")
    assert pc.undo_calls() == [] and pc.level == 30


def test_failed_action_does_not_replace_undo(pc: FakePC, ctx: Context) -> None:
    say("громкость 30", ctx)
    pc.fail.add("close_target")
    assert not say("закрой блокнот", ctx).ok
    assert say("отмени", ctx).ok and pc.level == 40


def test_answers_do_not_replace_undo(pc: FakePC, ctx: Context) -> None:
    say("громкость 30", ctx)
    for phrase in ("который час", "что ты умеешь", "какие напоминания", "что ты обо мне помнишь"):
        say(phrase, ctx)
    assert say("отмени", ctx).ok and pc.level == 40


def test_undo_expires_with_context_ttl(pc: FakePC, ctx: Context) -> None:
    say("громкость 30", ctx)
    assert ctx.undo is not None
    ctx.undo["ts"] = time.time() - TTL_S - 1
    assert say("отмени", ctx).text == "Нечего отменять" and pc.undo_calls() == []


def test_dry_run_records_nothing(pc: FakePC, ctx: Context) -> None:
    hit = grammar.match("громкость 30", ctx)
    assert hit is not None
    execute.run(hit.action, hit.args, "громкость 30", ctx, "grammar", dry=True)
    assert ctx.undo is None and pc.calls == []


def test_unknown_kind_is_not_undoable(pc: FakePC, ctx: Context) -> None:
    ctx.set_undo({"kind": "что-то новое"})
    assert say("отмени", ctx).text == "Это не отменить"
    ctx.set_undo({"kind": "volume", "level": "громко", "muted": False})
    assert say("отмени", ctx).text == "Это не отменить" and pc.undo_calls() == []


def test_hands_cannot_call_undo(pc: FakePC, ctx: Context) -> None:
    say("громкость 30", ctx)
    out = execute.run("undo", {}, "отмени", ctx, "hands")
    assert out.kind == "clarify" and pc.level == 30


# --- CLI: отмена между вызовами -----------------------------------------------------------------------


def test_cli_context_keeps_undo(pc: FakePC) -> None:
    ctx = load_cli()
    say("громкость 30", ctx)
    save_cli(ctx)
    ctx2 = load_cli()  # следующий вызов `jarvis ask "отмени"`
    assert ctx2.undo_record() is not None
    assert say("отмени", ctx2).ok and pc.level == 40
    save_cli(ctx2)
    assert say("отмени", load_cli()).text == "Нечего отменять"


# --- напоминания и память -------------------------------------------------------------------------------


def test_remind_then_undo_cancels_it(pc: FakePC, ctx: Context) -> None:
    out = say("напомни через 10 минут вынуть пиццу", ctx)
    assert out.ok and out.autohide and out.text.startswith("Напомню ") and out.text.endswith(": вынуть пиццу")
    assert [r.text for r in reminders.pending()] == ["вынуть пиццу"]
    undo = say("отмени", ctx)
    assert undo.ok and undo.text == "Отменил напоминание: вынуть пиццу" and reminders.pending() == []
    assert say("отмени", ctx).text == "Нечего отменять"


def test_cancel_reminder_then_undo_restores(pc: FakePC, ctx: Context) -> None:
    say("напомни через 10 минут вынуть пиццу", ctx)
    say("напомни через час позвонить маме", ctx)
    out = say("отмени все напоминания", ctx)
    assert out.ok and out.text == "Отменил 2 напоминания" and reminders.pending() == []
    back = say("верни как было", ctx)
    assert back.ok and back.text == "Вернул 2 напоминания"
    assert [r.text for r in reminders.pending()] == ["вынуть пиццу", "позвонить маме"]


def test_remember_then_undo_forgets(pc: FakePC, ctx: Context) -> None:
    out = say("запомни, что я пью кофе без сахара", ctx)
    assert (out.ok, out.text, out.autohide) == (True, "Запомнил: я пью кофе без сахара", True)
    undo = say("отмени", ctx)
    assert undo.ok and undo.text == "Забыл: я пью кофе без сахара" and memory.items() == []


def test_forget_all_then_undo_remembers_again(pc: FakePC, ctx: Context) -> None:
    say("запомни что меня зовут Дима", ctx)
    say("запомни что я пью кофе без сахара", ctx)
    out = say("забудь всё", ctx)
    assert out.ok and out.text == "Забыл 2 заметки" and memory.items() == []
    back = say("отмени", ctx)
    assert back.ok and back.text == "Вспомнил 2 заметки"
    assert [i.text for i in memory.items()] == ["меня зовут Дима", "я пью кофе без сахара"]


def test_undo_store_error_is_reported(pc: FakePC, ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    say("запомни что я пью кофе", ctx)

    def broken(ids: Any) -> list[Any]:
        raise memory.StoreError("Не удалось сохранить memory.json: занято")

    monkeypatch.setattr(memory, "forget_ids", broken)
    out = say("отмени", ctx)
    assert (out.ok, out.text) == (False, "Не удалось сохранить memory.json: занято")
    assert ctx.undo_record() is not None


def test_undo_does_not_touch_real_data_dir(pc: FakePC, ctx: Context, _isolated_data: Path) -> None:
    say("напомни через 10 минут вынуть пиццу", ctx)
    say("запомни что я пью кофе", ctx)
    assert sorted(p.name for p in _isolated_data.iterdir()) == ["memory.json", "reminders.json"]
