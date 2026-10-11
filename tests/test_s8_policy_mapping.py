"""AGENTS.md: «Каждое действие грамматики, инструмент рук и инструмент MCP отображается на строку таблицы
(тест)».

В репозитории тест есть только для MCP (tests/test_mcp.py) и для модулей files/system/audio/media
(tests/test_system.py). Этот тест прогоняет каждое действие грамматики и инструмент рук через настоящий
execute.run и настоящие функции pc (ОС подменена, policy.require — шпион, всегда «отказ», так что ничего
не исполняется) и проверяет: каждое действие спрашивает policy, и только строками таблицы.
"""

import time
from pathlib import Path
from typing import Any

import pytest

from jarvis import execute, hands
from jarvis.context import Context
from pc import apps, files, policy, procs, windows
from pc.result import Result
from pc.windows import WindowInfo

WIN = WindowInfo(77, "Калькулятор", 4242, "CalculatorApp.exe")


class FakeWinApi:
    def enum_windows(self) -> list[int]:
        return [WIN.hwnd]

    def is_visible(self, hwnd: int) -> bool:
        return True

    def ex_style(self, hwnd: int) -> int:
        return 0

    def owner(self, hwnd: int) -> int:
        return 0

    def is_cloaked(self, hwnd: int) -> bool:
        return False

    def class_name(self, hwnd: int) -> str:
        return "ApplicationFrameWindow"

    def get_title(self, hwnd: int) -> str:
        return WIN.title

    def pid_of(self, hwnd: int) -> int:
        return WIN.pid

    def exe_of(self, pid: int) -> str:
        return WIN.exe

    def is_elevated(self, pid: int) -> bool:
        return False

    def is_hung(self, hwnd: int) -> bool:
        return False

    def is_window(self, hwnd: int) -> bool:
        return hwnd == WIN.hwnd

    def foreground(self) -> int:
        return 0


class FakeFilesApi:
    def known_folder(self, folder_id: str) -> str:
        return r"C:\Users\me\Downloads"

    def is_dir(self, path: str) -> bool:
        return "." not in path.rsplit("\\", 1)[-1]

    def exists(self, path: str) -> bool:
        return True


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    asked: list[str] = []

    def require(action: str, caller: str, summary: str, details: str = "") -> Result | None:
        policy.check(action, caller)  # неизвестное действие — UnknownAction
        asked.append(action)
        return Result(False, "стоп")

    monkeypatch.setattr(policy, "require", require)
    monkeypatch.setattr(windows, "_api", FakeWinApi())
    monkeypatch.setattr(files, "_api", FakeFilesApi())
    monkeypatch.setattr(procs, "own_pids", lambda: {1})
    monkeypatch.setattr(
        procs, "find_processes", lambda name: [procs.ProcGroup("CalculatorApp.exe", 1, [WIN.pid])]
    )
    apps.set_inventory([{"name": "Калькулятор", "app_id": "Microsoft.WindowsCalculator_x!App"}])
    yield asked
    apps.set_inventory(None)


CASES: list[tuple[str, dict[str, Any], str, str]] = [
    # (инструмент, аргументы, текст команды, источник)
    ("open", {"target": "калькулятор", "kind": "app"}, "открой калькулятор", "grammar"),
    ("open", {"target": "загрузки", "kind": "folder"}, "открой загрузки", "grammar"),
    ("open", {"target": "https://example.com", "kind": "url"}, "открой https://example.com", "hands"),
    ("close", {"target": "калькулятор"}, "закрой калькулятор", "hands"),
    ("focus", {"target": "калькулятор"}, "переключись на калькулятор", "hands"),
    ("win", {"action": "minimize", "target": "калькулятор"}, "сверни калькулятор", "hands"),
    ("win", {"action": "minimize_all"}, "сверни все окна", "grammar"),
    ("find", {"query": "отчёт"}, "найди отчёт", "hands"),
    ("vol", {"set": 30}, "громкость 30", "hands"),
    ("media", {"action": "next"}, "следующий трек", "hands"),
    ("kill", {"name": "калькулятор"}, "убей процесс калькулятор", "hands"),
    ("open_found", {"index": 1}, "открой первый", "grammar"),
    ("lock", {}, "заблокируй", "grammar"),
]
# не действия ПК (AGENTS.md): в таблицу не входят. Напоминания, память и помощь — файлы Jarvis в <data> и
# ответы без действия на ПК (как clock); undo — действие ПК, его отображение — в UNDO_CASES ниже
NOT_PC_ACTIONS = {"reply", "clarify", "ask_gpt", "clock"}
NOT_PC_ACTIONS |= {"remind", "reminders", "cancel_reminder", "remember", "forget", "memory", "help"}
# отмена: запись ctx.undo → строки таблицы, которые спрашивает возврат «как было»
UNDO_CASES: list[tuple[dict[str, Any], set[str]]] = [
    ({"kind": "volume", "level": 30, "muted": False}, {"volume"}),
    ({"kind": "volume", "level": 0, "muted": True}, {"volume"}),
    ({"kind": "window", "action": "minimize", "hwnd": WIN.hwnd}, {"window"}),
    ({"kind": "window", "action": "maximize", "hwnd": WIN.hwnd}, {"window"}),
    ({"kind": "minimize_all"}, {"window"}),
]
# отмена напоминания и заметки — не действие ПК: policy не спрашивается
UNDO_NOT_PC: list[dict[str, Any]] = [
    {"kind": "reminder_add", "id": "abc"},
    {"kind": "reminder_cancel", "items": []},
    {"kind": "memory_add", "id": "abc"},
    {"kind": "memory_forget", "items": []},
    {"kind": "none", "action": "close"},
]


def test_cases_cover_every_grammar_action_and_hands_tool() -> None:
    hands_tools = {t["function"]["name"] for t in hands.TOOLS}
    covered = {c[0] for c in CASES} | {"undo"}
    assert (hands_tools | execute.GRAMMAR_ACTIONS) - NOT_PC_ACTIONS == covered


def test_hands_have_no_local_actions() -> None:
    """Напоминания, память, помощь и отмена — только у грамматики: рукам их не дать (их TOOLS не меняются)."""
    hands_tools = {t["function"]["name"] for t in hands.TOOLS}
    assert not hands_tools & execute.LOCAL_ACTIONS
    assert not execute.HANDS_ACTIONS & execute.LOCAL_ACTIONS


@pytest.mark.parametrize(("record", "rows"), UNDO_CASES, ids=[str(c[0]) for c in UNDO_CASES])
def test_undo_maps_to_existing_policy_rows(spy: list[str], record: dict[str, Any], rows: set[str]) -> None:
    ctx = Context()
    ctx.set_undo(record)
    out = execute.run("undo", {}, "отмени", ctx, "grammar")
    assert spy, f"undo {record}: policy не спрошена ({out})"
    assert set(spy) == rows and rows <= set(policy.TABLE)
    assert not out.ok and ctx.undo_record() is not None  # отказ — отмена не потеряна


@pytest.mark.parametrize("record", UNDO_NOT_PC, ids=[r["kind"] for r in UNDO_NOT_PC])
def test_undo_of_non_pc_actions_does_not_ask_policy(spy: list[str], record: dict[str, Any]) -> None:
    ctx = Context()
    ctx.set_undo(record)
    execute.run("undo", {}, "отмени", ctx, "grammar")
    assert spy == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("remind", {"text": "вынуть пиццу", "due": time.time() + 600}),
        ("reminders", {}),
        ("cancel_reminder", {"which": 1}),
        ("remember", {"text": "я пью кофе без сахара"}),
        ("forget", {"which": "кофе"}),
        ("memory", {}),
        ("help", {}),
    ],
)
def test_local_actions_do_not_ask_policy(spy: list[str], tool: str, args: dict) -> None:
    out = execute.run(tool, args, "команда", Context(), "grammar")
    assert spy == [], f"{tool}: не действие ПК, а спросил policy ({out})"


@pytest.mark.parametrize(("tool", "args", "text", "source"), CASES, ids=[f"{c[0]}:{c[2]}" for c in CASES])
def test_action_maps_to_policy_rows(spy: list[str], tool: str, args: dict, text: str, source: str) -> None:
    ctx = Context()
    ctx.set_found([r"C:\Users\me\отчёт.docx"])
    out = execute.run(tool, args, text, ctx, source)
    assert spy, f"{tool}: policy не спрошена ({out})"
    assert set(spy) <= set(policy.TABLE)
