"""Вертикальный срез Session 3 на настоящей базе: задача → scripted-стадия → Tool Runtime →
filesystem.search → preview → политика → исполнение → проверка → трасса и аудит в SQLite → COMPLETED.
После закрытия базы всё читается заново — из другого соединения и через `jarvis trace`."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.adapters.sqlite import SqliteStorage
from jarvis.app.composition import database_path
from jarvis.cli.main import app as cli
from jarvis.domain.audit import AuditAction
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.tools import PolicyOutcome
from jarvis.domain.trace import EventKind
from jarvis.evals.engine import run_scenario
from jarvis.evals.scenario import load_scenarios

pytestmark = pytest.mark.anyio

SEARCH = Path(__file__).resolve().parents[2] / "evals" / "scenarios" / "tools" / "search.yaml"


async def test_search_slice_is_persisted_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    [scenario] = load_scenarios([SEARCH])
    database = database_path(tmp_path)
    with SqliteStorage(database) as storage:
        result = await run_scenario(scenario, JarvisConfig(), storage=storage)
    assert result.passed, result.problems
    assert result.status is TaskStatus.COMPLETED

    with SqliteStorage(database) as storage, storage.unit_of_work() as uow:
        task = uow.tasks.get(TaskId(result.task_id))
        events = uow.trace.list(task.id)
        audit = uow.audit.list(task_id=task.id)
        lease = uow.leases.get(task.id)
    assert task.status is TaskStatus.COMPLETED
    assert lease is None
    kinds = [event.kind for event in events]
    order = [
        EventKind.TOOL_PREVIEWED,
        EventKind.POLICY_DECIDED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FINISHED,
        EventKind.TOOL_VERIFIED,
    ]
    assert [kind for kind in kinds if kind in order] == order
    assert kinds.index(EventKind.TOOL_VERIFIED) < kinds.index(EventKind.TASK_FINISHED)
    finished = next(event for event in events if event.kind is EventKind.TOOL_FINISHED)
    assert finished.payload["status"] == "succeeded"
    assert finished.payload["untrusted"] is True
    assert [
        (record.action, record.decision, record.execution_status, record.verification_status)
        for record in audit
    ] == [
        (AuditAction.DECISION, PolicyOutcome.ALLOW, None, None),
        (AuditAction.RESULT, PolicyOutcome.ALLOW, "succeeded", "passed"),
    ]
    assert {record.tool_id for record in audit} == {"filesystem.search"}

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    output = CliRunner().invoke(cli, ["trace", result.task_id]).output
    assert "tool filesystem.search" in output
    assert "policy allow  (effect.read)" in output
    assert "verify passed" in output
    assert "инструментов 1" in output
