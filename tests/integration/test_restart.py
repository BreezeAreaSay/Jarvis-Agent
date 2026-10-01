"""Перезапуск процесса: задача и трасса, записанные одним процессом, читаются другим без потерь;
задача убитого процесса становится FAILED (interrupted) — после того, как истечёт его аренда."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.app.composition import build_app, database_path
from jarvis.cli.main import app as cli
from jarvis.core.trace import normalize_events
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import JarvisConfig
from jarvis.evals.engine import run_scenario
from jarvis.evals.scenario import load_scenarios

pytestmark = pytest.mark.anyio

SCENARIOS = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
RUNTIME_SCENARIOS = sorted(scenario.id for scenario in load_scenarios([SCENARIOS]))

RUN_SCENARIO = """
import asyncio, sys
from pathlib import Path
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.domain.settings import JarvisConfig
from jarvis.evals.engine import run_scenario
from jarvis.evals.scenario import load_scenarios

database, scenarios, wanted = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
(scenario,) = [item for item in load_scenarios([scenarios]) if item.id == wanted]
with SqliteStorage(database) as storage:
    result = asyncio.run(run_scenario(scenario, JarvisConfig(), storage=storage))
print(result.task_id, result.passed)
"""

HANG_FOREVER = """
import asyncio, sys
from pathlib import Path
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.app.composition import build_app
from jarvis.domain.settings import JarvisConfig, RuntimeSettings
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, StageOutcome, TaskRequest
from jarvis.evals.scenario import ScriptStep
from jarvis.evals.scripted import ScriptedStages

class Hang:
    async def handle(self, task, budget):
        print("running", task.id, flush=True)
        await asyncio.Event().wait()

S = TaskStatus
steps = [
    ScriptStep(status=S.ROUTING, next=S.PLANNING, route="agent"),
    ScriptStep(status=S.PLANNING, next=S.EXECUTING),
]
stages = {**ScriptedStages(steps).handlers(), S.EXECUTING: Hang()}
config = JarvisConfig(runtime=RuntimeSettings(lease_ttl_s=float(sys.argv[2])))
with SqliteStorage(Path(sys.argv[1])) as storage:
    app = build_app(config, stages=stages, storage=storage, owner="doomed")
    task_id = app.tasks.submit(TaskRequest(text="долгая задача", origin=Origin.EVAL))
    asyncio.run(app.tasks.run_until_blocked(task_id))
"""


def python(*args: str) -> list[str]:
    return [sys.executable, "-c", *args]


@pytest.mark.parametrize("scenario_id", RUNTIME_SCENARIOS)
async def test_task_and_trace_survive_a_process_restart(tmp_path: Path, scenario_id: str) -> None:
    database = tmp_path / "jarvis.db"
    finished = subprocess.run(
        python(RUN_SCENARIO, str(database), str(SCENARIOS), scenario_id),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    task_id, passed = finished.stdout.split()
    assert passed == "True"

    # Новый процесс (этот) открывает базу заново и видит ту же задачу и ту же трассу,
    # что даёт прогон того же сценария в памяти.
    with SqliteStorage(database) as storage:
        restored = build_app(JarvisConfig(), stages={}, storage=storage).tasks.inspect(TaskId(task_id))
    (scenario,) = [item for item in load_scenarios([SCENARIOS]) if item.id == scenario_id]
    memory = InMemoryStorage()
    reference = await run_scenario(scenario, JarvisConfig(), storage=memory)
    expected = build_app(JarvisConfig(), stages={}, storage=memory).tasks.inspect(reference.task_id)

    assert normalize_events(restored.events) == normalize_events(expected.events)
    assert restored.task.status is expected.task.status
    assert restored.task.usage.model_copy(update={"active_time_s": 0}) == expected.task.usage.model_copy(
        update={"active_time_s": 0}
    )
    assert restored.task.outcome == expected.task.outcome


def test_killed_process_leaves_an_interrupted_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    database = database_path(tmp_path)
    ttl_s = 3.0  # с запасом на медленный старт CLI (антивирус на Windows CI)
    process = subprocess.Popen(
        python(HANG_FOREVER, str(database), str(ttl_s)), stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline().split()
        assert line[0] == "running"
        task_id = line[1]
    finally:
        process.kill()  # падение процесса: ни отмены, ни записи напоследок
        process.wait(timeout=30)
    killed_at = time.monotonic()

    runner = CliRunner()
    busy = runner.invoke(cli, ["cancel", task_id])  # аренда умершего процесса ещё жива
    assert busy.exit_code == 1, busy.output
    assert "ведёт другой процесс (doomed" in busy.output

    time.sleep(max(0.0, ttl_s + 0.3 - (time.monotonic() - killed_at)))
    listing = runner.invoke(cli, ["tasks"])
    assert listing.exit_code == 0
    assert f"{task_id}: процесс, который вёл задачу, завершился — FAILED (interrupted)" in listing.output
    document = json.loads(runner.invoke(cli, ["trace", task_id, "--json"]).output)
    assert document["task"]["status"] == "FAILED"
    assert document["task"]["outcome"]["error"]["category"] == "interrupted"
    transitions = [
        event["payload"]["to"] for event in document["events"] if event["kind"] == "task.transition"
    ]
    assert transitions == ["ROUTING", "PLANNING", "EXECUTING", "FAILED"]
    assert runner.invoke(cli, ["cancel", task_id]).output == f"{task_id} уже завершена: FAILED\n"
