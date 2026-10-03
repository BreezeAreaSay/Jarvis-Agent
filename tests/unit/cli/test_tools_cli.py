"""`jarvis tools` и `jarvis tools show`: только описание, исполнения из CLI нет."""

import inspect
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.cli.main import app as cli


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    return tmp_path


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def test_tools_lists_every_builtin_tool_sorted() -> None:
    code, output = run("tools")
    assert code == 0
    ids = [line.split()[0] for line in output.splitlines()]
    assert ids == [
        "filesystem.list",
        "filesystem.read_text",
        "filesystem.search",
        "filesystem.stat",
        "process.list",
        "system.cwd",
    ]
    assert "system.cwd             нет" in output


def test_show_prints_the_definition_and_schemas() -> None:
    code, output = run("tools", "show", "filesystem.read_text")
    assert code == 0
    assert output.startswith("filesystem.read_text\n")
    assert "эффекты: read" in output
    assert "результат — недоверенные данные: да" in output
    arguments = output.split("\nаргументы:\n")[1].split("\nрезультат:\n")[0]
    assert json.loads(arguments)["required"] == ["path"]


def test_unknown_tool() -> None:
    code, output = run("tools", "show", "shell.execute")
    assert code == 2
    assert "нет инструмента shell.execute" in output


def test_there_is_no_way_to_execute_a_tool_from_the_cli() -> None:
    code, _ = run("tools", "run", "filesystem.list")
    assert code != 0
    commands = {command.name for command in cli.registered_commands}
    assert not {"exec", "execute", "tool"} & commands
    # `jarvis run` принимает только текст запроса: задачу ведёт агент, инструменты — через Tool Runtime.
    (run_command,) = [command for command in cli.registered_commands if command.name == "run"]
    assert run_command.callback is not None
    assert set(inspect.signature(run_command.callback).parameters) == {"text", "dry_run"}
