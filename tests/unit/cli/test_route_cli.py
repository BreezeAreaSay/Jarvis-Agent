"""`jarvis route`: решение Router без исполнения; ленивые импорты CLI (холодный старт)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.adapters.inventory import StaticInventory
from jarvis.cli import route as route_cli
from jarvis.cli.main import app as cli
from jarvis.core.routing.router import Router
from jarvis.domain.inventory import AppEntry

TELEGRAM = AppEntry(
    id="telegram", name="Telegram Desktop", aliases=["телеграм"], target="C:/T.lnk", kind="shortcut"
)


@pytest.fixture(autouse=True)
def inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Инвентарь теста вместо приложений этого компьютера."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(route_cli, "build_router", lambda: Router(StaticInventory([TELEGRAM])))


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, ["route", *args])
    return result.exit_code, result.output


def test_a_direct_command_shows_its_decision_and_tool_without_running() -> None:
    code, output = run("открой", "телеграм")
    assert code == 0
    lines = output.splitlines()
    assert "strategy:    direct" in lines
    assert "intent:      app.launch" in lines
    assert "entities:    app=telegram «Telegram Desktop» (inventory.app.alias)" in lines
    assert "rules:       direct.launch.verb, inventory.app.alias" in lines
    assert 'tool:        app.launch {"app": "telegram"}' in lines
    assert "model_calls: 0" in lines
    assert any(line.startswith("router:      ") for line in lines)
    assert "исполнение:  нет — только решение (выполнить: jarvis run)" in lines


def test_a_question_goes_to_the_agent() -> None:
    code, output = run("Почему браузер постоянно падает?")
    assert code == 0
    assert "strategy:    agent" in output
    assert "level:       local" in output
    assert "model_calls: 1+ (агент на модели)" in output
    assert "tool:" not in output


def test_json_output(tmp_path: Path) -> None:
    code, output = run("--json", "--cwd", str(tmp_path), "покажи", "файлы")
    assert code == 0
    document = json.loads(output)
    assert document["strategy"] == "direct"
    assert document["intent"] == "fs.list"
    assert document["model_calls"] == 0
    assert document["tool"] == {
        "id": "filesystem.list",
        "arguments": {"path": str(tmp_path.resolve()), "max_entries": 200},
    }
    assert document["duration_ms"] >= 0


def test_control_characters_in_entities_are_escaped() -> None:
    code, output = run("открой", "папку", "src\x1b[31m")
    assert code == 0
    assert "\x1b" not in output


IMPORTS = """
import sys
import {module}
print(" ".join(sorted(name for name in sys.modules if name.startswith(("jarvis", "httpx")))))
"""


def loaded_by(module: str) -> set[str]:
    code = IMPORTS.format(module=module)
    finished = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    return set(finished.stdout.split())


def test_the_cli_entry_point_loads_no_command_modules() -> None:
    """`jarvis --version` не грузит ни приложение, ни конфиг, ни бенчмарк: команды загружаются лениво."""
    loaded = loaded_by("jarvis.cli.main")
    assert not {name for name in loaded if name.startswith(("jarvis.app", "jarvis.evals", "jarvis.config"))}
    assert "httpx" not in loaded


@pytest.mark.parametrize("module", ["jarvis.cli.run", "jarvis.cli.route"])
def test_run_and_route_do_not_load_the_benchmark_or_eval(module: str) -> None:
    loaded = loaded_by(module)
    assert not {name for name in loaded if name.startswith("jarvis.evals")}


def test_route_does_not_load_the_http_client() -> None:
    assert "httpx" not in loaded_by("jarvis.cli.route")
