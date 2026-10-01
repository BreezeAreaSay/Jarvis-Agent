"""`jarvis tasks | trace | cancel` над настоящей базой SQLite в JARVIS_HOME."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.app.composition import build_app, database_path
from jarvis.cli.main import app as cli
from jarvis.domain.settings import JarvisConfig
from jarvis.evals.scenario import ScriptStep
from jarvis.evals.scripted import ScriptedStages
from tests.helpers import S, request, step


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    return tmp_path


def seed(
    home: Path, steps: list[ScriptStep], *, owner: str = "seed", clock: ManualClock | None = None
) -> str:
    with SqliteStorage(database_path(home)) as storage:
        app = build_app(
            JarvisConfig(), stages=ScriptedStages(steps).handlers(), storage=storage, owner=owner, clock=clock
        )
        task_id = app.tasks.submit(request("сценарий"))
        asyncio.run(app.tasks.run_until_blocked(task_id))
        return task_id


DONE = [
    step(S.ROUTING, S.PLANNING, route="agent"),
    step(S.PLANNING, S.EXECUTING),
    step(S.EXECUTING, S.VERIFYING),
    step(S.VERIFYING, S.COMPLETED, answer="готово"),
]
WAITING = [step(S.ROUTING, S.EXECUTING, route="direct"), step(S.EXECUTING, S.WAITING_CONFIRMATION)]
FAILING = [step(S.ROUTING, S.PLANNING, route="agent"), step(S.PLANNING, S.EXECUTING, fail=True)]


def run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, list(args))
    return result.exit_code, result.output


def test_tasks_on_an_empty_home(home: Path) -> None:
    assert run("tasks") == (0, "Задач нет.\n")
    assert database_path(home).is_file()  # база создаётся при первом обращении


def test_tasks_table_and_filters(home: Path) -> None:
    for steps in (DONE, WAITING, FAILING):
        seed(home, steps)
    code, output = run("tasks")
    assert code == 0
    lines = output.splitlines()
    assert lines[0].split() == ["ID", "STATUS", "ROUTE", "UPDATED", "REASON"]
    assert [line.split()[:3] for line in lines[1:]] == [
        ["task_1", "COMPLETED", "agent"],
        ["task_2", "WAITING_CONFIRMATION", "direct"],
        ["task_3", "FAILED", "agent"],
    ]
    assert lines[3].split()[-1] == "scripted_failure"
    assert [line.split()[0] for line in run("tasks", "--status", "failed")[1].splitlines()[1:]] == ["task_3"]
    waiting_or_done = run("tasks", "-s", "waiting", "-s", "completed")[1].splitlines()[1:]
    assert [line.split()[0] for line in waiting_or_done] == ["task_1", "task_2"]
    assert [line.split()[0] for line in run("tasks", "--limit", "1")[1].splitlines()[1:]] == ["task_3"]
    assert run("tasks", "--status", "running")[1] == "Задач нет.\n"
    code, output = run("tasks", "--status", "nope")
    assert code == 2
    assert "неизвестный статус" in output


def test_trace_is_a_readable_timeline_or_json(home: Path) -> None:
    task_id = seed(home, FAILING)
    code, output = run("trace", task_id)
    assert code == 0
    assert output.startswith(f"TASK {task_id}  FAILED  route=agent\n")
    assert "route=agent" in output
    assert "! error\n          scripted_failure: scripted" in output
    assert "FINISHED FAILED" in output
    assert "METRICS завершена" in output

    code, output = run("trace", task_id, "--json")
    assert code == 0
    document = json.loads(output)
    assert document["task"]["status"] == "FAILED"
    assert [event["kind"] for event in document["events"]][-3:] == [
        "error",
        "task.transition",
        "task.finished",
    ]
    assert document["metrics"]["failures"] == 1


def test_trace_of_an_unknown_task(home: Path) -> None:
    code, output = run("trace", "task_404")
    assert code == 1
    assert "task_404 не найдена" in output


def test_cancel_waiting_and_finished_tasks(home: Path) -> None:
    waiting = seed(home, WAITING)
    done = seed(home, DONE)
    assert run("cancel", waiting) == (0, f"{waiting}: WAITING_CONFIRMATION → CANCELLED\n")
    assert run("cancel", waiting) == (0, f"{waiting} уже завершена: CANCELLED\n")
    assert run("cancel", done) == (0, f"{done} уже завершена: COMPLETED\n")
    assert "отменено пользователем" in run("trace", waiting)[1]


def test_cancel_refuses_a_task_held_by_a_live_process(home: Path) -> None:
    with SqliteStorage(database_path(home)) as storage:  # «другой процесс»: аренда живая ещё 30 с
        other = build_app(JarvisConfig(), stages={}, storage=storage, owner="other-process")
        task_id = other.tasks.submit(request("чужая задача"))
    code, output = run("cancel", task_id)
    assert code == 1
    assert "ведёт другой процесс (other-process" in output
    assert run("tasks")[1].splitlines()[1].split()[1] == "CREATED"


def test_commands_recover_tasks_of_dead_processes(home: Path) -> None:
    long_ago = ManualClock()
    with SqliteStorage(database_path(home)) as storage:  # процесс взял задачу год назад и пропал
        dead = build_app(JarvisConfig(), stages={}, storage=storage, owner="dead", clock=long_ago)
        task_id = dead.tasks.submit(request("брошенная задача"))
    long_ago.advance(timedelta(days=365).total_seconds())
    code, output = run("tasks")
    assert code == 0
    assert f"{task_id}: процесс, который вёл задачу, завершился — FAILED (interrupted)" in output
    assert output.splitlines()[-1].split()[1:2] == ["FAILED"]
    timeline = run("trace", task_id)[1]
    assert "reason=interrupted\n          interruption=owner_lost" in timeline


def test_broken_database_is_reported_without_a_traceback(home: Path) -> None:
    database_path(home).parent.mkdir(parents=True)
    database_path(home).write_bytes(b"this is not a database at all" * 100)
    for command in (["tasks"], ["trace", "task_1"], ["cancel", "task_1"]):
        code, output = run(*command)
        assert code == 1
        assert "не база SQLite" in output
        assert "Traceback" not in output
