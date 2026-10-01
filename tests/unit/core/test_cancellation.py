"""Отмена: до запуска, посреди такта, в ожидании подтверждения; завершённая задача не меняется."""

import asyncio
import contextlib

import pytest

from jarvis.core.budget import BudgetMeter
from jarvis.core.runner import StageHandler
from jarvis.domain.errors import TaskBusy
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task
from jarvis.evals.scripted import ScriptedStages
from tests.helpers import S, agent_prefix, error_of, make_app, request, scripted, step, transitions

pytestmark = pytest.mark.anyio


async def test_cancel_before_start() -> None:
    app, script = scripted(step(S.ROUTING, S.COMPLETED))
    task_id = app.tasks.submit(request())
    app.tasks.cancel(task_id, "передумал")
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.CANCELLED
    assert transitions(app, task_id) == [S.CANCELLED]
    error = error_of(snapshot)
    assert error.category == "task_cancelled"
    assert error.message == "передумал"
    assert script.remaining == 1  # ни одна стадия не вызывалась


class Hangs:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.interrupted = False

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.interrupted = True
            raise
        raise AssertionError("недостижимо")


async def test_cancel_interrupts_the_running_tick() -> None:
    hangs = Hangs()
    app = make_app(with_stage(S.EXECUTING, hangs))
    task_id = app.tasks.submit(request())

    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    app.tasks.cancel(task_id, "Ctrl+C")
    snapshot = await asyncio.wait_for(run, timeout=5)

    assert hangs.interrupted
    assert snapshot.status is S.CANCELLED
    assert transitions(app, task_id)[-2:] == [S.EXECUTING, S.CANCELLED]


class ReturnsDespiteCancel:
    """Стадия, которая «проглатывает» отмену и возвращает результат: отмена всё равно важнее."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        self.started.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        return StageOutcome(next_status=TaskStatus.VERIFYING, reason="закончил")


async def test_cancellation_wins_over_the_stage_result() -> None:
    stage = ReturnsDespiteCancel()
    app = make_app(with_stage(S.EXECUTING, stage))
    task_id = app.tasks.submit(request())

    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(stage.started.wait(), timeout=5)
    app.tasks.cancel(task_id, "стоп")
    assert (await asyncio.wait_for(run, timeout=5)).status is S.CANCELLED


async def test_cancel_waiting_task() -> None:
    app, _ = scripted(*agent_prefix(), step(S.EXECUTING, S.WAITING_CONFIRMATION))
    task_id = app.tasks.submit(request())
    await app.tasks.run_until_blocked(task_id)
    app.tasks.cancel(task_id, "отклонить и остановить")

    assert app.tasks.get(task_id).status is S.CANCELLED
    assert transitions(app, task_id)[-2:] == [S.WAITING_CONFIRMATION, S.CANCELLED]


async def test_cancel_finished_task_changes_nothing() -> None:
    app, _ = scripted(step(S.ROUTING, S.COMPLETED, route="clarify"))
    task_id = app.tasks.submit(request())
    finished = await app.tasks.run_until_blocked(task_id)
    events = app.tasks.trace(task_id)

    app.tasks.cancel(task_id, "поздно")

    assert app.tasks.get(task_id) == finished
    assert app.tasks.trace(task_id) == events


async def test_same_task_cannot_run_twice_at_once() -> None:
    hangs = Hangs()
    app = make_app(with_stage(S.EXECUTING, hangs))
    task_id = app.tasks.submit(request())

    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    with pytest.raises(TaskBusy):
        await app.tasks.run_until_blocked(task_id)
    app.tasks.cancel(task_id, "стоп")
    await run


def with_stage(status: TaskStatus, stage: StageHandler) -> dict[TaskStatus, StageHandler]:
    """Scripted-путь до EXECUTING, а дальше — заданная стадия."""
    return {**ScriptedStages(agent_prefix()).handlers(), status: stage}


async def test_cancel_requested_during_shutdown_is_recorded() -> None:
    hangs = Hangs()
    app = make_app(with_stage(S.EXECUTING, hangs))
    task_id = app.tasks.submit(request())

    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    # Ctrl+C: клиент отменяет задачу, и в том же обороте цикла завершается сам прогон.
    app.tasks.cancel(task_id, "Ctrl+C")
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert app.tasks.get(task_id).status is S.CANCELLED


async def test_run_cancelled_without_a_cancel_request_keeps_the_checkpoint() -> None:
    hangs = Hangs()
    app = make_app(with_stage(S.EXECUTING, hangs))
    task_id = app.tasks.submit(request())

    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert hangs.interrupted
    assert app.tasks.get(task_id).status is S.EXECUTING
