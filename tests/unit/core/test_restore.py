"""Состояние задачи живёт в хранилище: новое приложение над тем же хранилищем продолжает её."""

import asyncio

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.core.budget import BudgetMeter
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task
from jarvis.evals.scripted import ScriptedStages
from tests.helpers import S, agent_prefix, make_app, request, scripted, step, transitions

pytestmark = pytest.mark.anyio


async def test_rebuilt_app_sees_the_same_task() -> None:
    storage = InMemoryStorage()
    clock = ManualClock()
    first, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.WAITING_CONFIRMATION, charge={"steps": 1, "tool_calls": 1}),
        storage=storage,
        clock=clock,
    )
    task_id = first.tasks.submit(request())
    waiting = await first.tasks.run_until_blocked(task_id)

    second, _ = scripted(storage=storage, clock=clock)
    assert second.tasks.get(task_id) == waiting
    assert second.tasks.trace(task_id) == first.tasks.trace(task_id)


class Hangs:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        budget.charge(BudgetLimit.STEPS)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("недостижимо")


async def test_interrupted_run_resumes_from_the_last_checkpoint() -> None:
    storage = InMemoryStorage()
    hangs = Hangs()
    first = make_app({**ScriptedStages(agent_prefix()).handlers(), S.EXECUTING: hangs}, storage=storage)
    task_id = first.tasks.submit(request())

    # Процесс «умирает» посреди такта: run_until_blocked отменяется снаружи.
    run = asyncio.create_task(first.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    interrupted = first.tasks.get(task_id)
    assert interrupted.status is S.EXECUTING
    assert interrupted.usage.steps == 0  # незавершённый такт не записан

    second, _ = scripted(
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1}),
        step(S.VERIFYING, S.COMPLETED, answer="продолжено"),
        storage=storage,
    )
    resumed = await second.tasks.run_until_blocked(task_id)

    assert resumed.status is TaskStatus.COMPLETED
    assert transitions(second, task_id) == [S.ROUTING, S.PLANNING, S.EXECUTING, S.VERIFYING, S.COMPLETED]
    event_ids = [event.id for event in second.tasks.trace(task_id)]
    assert len(event_ids) == len(set(event_ids))
    assert second.tasks.submit(request()) == "task_2"  # нумерация задач продолжается
