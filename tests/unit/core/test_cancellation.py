"""Отмена: до запуска, посреди такта, в ожидании подтверждения; завершённая задача не меняется."""

import asyncio
import contextlib

import pytest

from jarvis.adapters.memory import InMemoryStorage
from jarvis.core import runner as runner_module
from jarvis.core.budget import BudgetMeter
from jarvis.core.runner import StageHandler
from jarvis.domain.errors import StorageError, TaskBusy
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task
from jarvis.evals.scripted import ScriptedStages
from tests.helpers import (
    S,
    agent_prefix,
    budget_config,
    error_of,
    make_app,
    request,
    scripted,
    step,
    transitions,
)

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


async def test_run_stopped_mid_tick_marks_the_task_interrupted() -> None:
    hangs = Hangs()
    app = make_app(with_stage(S.EXECUTING, hangs))
    task_id = app.tasks.submit(request())

    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    run.cancel()  # процесс завершается, отмены задачи никто не просил
    with pytest.raises(asyncio.CancelledError):
        await run

    assert hangs.interrupted
    snapshot = app.tasks.get(task_id)
    assert snapshot.status is S.FAILED  # что успел сделать прерванный такт, неизвестно
    assert error_of(snapshot).category == "interrupted"
    assert transitions(app, task_id)[-2:] == [S.EXECUTING, S.FAILED]


@pytest.mark.parametrize("requested", [False, True])
async def test_storage_error_on_shutdown_does_not_mask_the_cancellation(requested: bool) -> None:
    storage = InMemoryStorage()
    hangs = Hangs()
    app = make_app(with_stage(S.EXECUTING, hangs), storage=storage)
    task_id = app.tasks.submit(request())

    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(hangs.started.wait(), timeout=5)
    storage.fail_commit()  # итоговую запись при выходе сделать не удастся
    if requested:
        app.tasks.cancel(task_id, "Ctrl+C")
    run.cancel()
    with pytest.raises(asyncio.CancelledError):  # вызывающий видит отмену, а не ошибку хранилища
        await run

    # Задача осталась в последней контрольной точке с арендой этого процесса: после истечения аренды
    # её переведёт в FAILED (interrupted) восстановление.
    assert app.tasks.get(task_id).status is S.EXECUTING


class CountingStage:
    """Стадия с «побочным эффектом»: считает, сколько раз её выполнили."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        self.calls += 1
        return StageOutcome(next_status=TaskStatus.VERIFYING, reason="эффект сделан")


async def test_aborted_run_is_not_resumed_by_the_same_process() -> None:
    storage = InMemoryStorage()
    stage = CountingStage()
    app = make_app(with_stage(S.EXECUTING, stage), storage=storage)
    task_id = app.tasks.submit(request())
    storage.fail_commit(after=4)  # аренда, ROUTING, PLANNING, EXECUTING; запись после такта падает
    with pytest.raises(StorageError):
        await app.tasks.run_until_blocked(task_id)
    assert stage.calls == 1

    snapshot = await app.tasks.run_until_blocked(task_id)  # тот же процесс, та же аренда
    assert stage.calls == 1  # такт с эффектом не повторён
    assert snapshot.status is S.FAILED
    assert error_of(snapshot).category == "interrupted"


class SlowToStop:
    """Глотает первую отмену и ещё какое-то время работает — runner ждёт его в _stop."""

    def __init__(self) -> None:
        self.stopping = asyncio.Event()

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stopping.set()
            await asyncio.sleep(30)
        raise AssertionError("недостижимо")


async def test_shutdown_while_a_timed_out_tick_stops_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "CANCEL_GRACE_S", 5.0)
    stage = SlowToStop()
    app = make_app(with_stage(S.EXECUTING, stage), config=budget_config(max_wall_time_s=0.05))
    task_id = app.tasks.submit(request())
    run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
    await asyncio.wait_for(stage.stopping.wait(), timeout=5)  # лимит времени истёк, такт останавливается
    run.cancel()  # и тут процесс завершается
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=10)
    snapshot = app.tasks.get(task_id)
    assert snapshot.status is S.FAILED  # итог записан, задача не висит активной до истечения аренды
    assert error_of(snapshot).category == "interrupted"
