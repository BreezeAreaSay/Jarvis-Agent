"""Каждый лимит бюджета останавливает задачу в BUDGET_EXCEEDED с событием budget.exceeded."""

import asyncio
import contextlib

import pytest
from pydantic import JsonValue

from jarvis.adapters.clock import ManualClock
from jarvis.app.composition import App
from jarvis.core import runner as runner_module
from jarvis.core.budget import BudgetMeter
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.ids import TaskId
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task, TaskChanges
from jarvis.domain.trace import EventKind
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


def exceeded_limits(app: App, task_id: TaskId) -> list[JsonValue]:
    events = app.tasks.trace(task_id)
    return [event.payload["limit"] for event in events if event.kind is EventKind.BUDGET_EXCEEDED]


@pytest.mark.parametrize(
    ("limit", "config_field"),
    [
        ("steps", "max_steps"),
        ("tool_calls", "max_tool_calls"),
        ("replans", "max_replans"),
        ("model_calls", "max_model_calls"),
    ],
)
async def test_counted_limit_stops_the_task(limit: str, config_field: str) -> None:
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.EXECUTING, charge={limit: 2}),
        step(S.EXECUTING, S.EXECUTING, charge={limit: 1}),
        config=budget_config(**{config_field: 2}),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.BUDGET_EXCEEDED
    assert transitions(app, task_id)[-1] is S.BUDGET_EXCEEDED
    assert exceeded_limits(app, task_id) == [limit]
    assert snapshot.usage.value(BudgetLimit(limit)) == 2  # отклонённое действие не списано
    error = error_of(snapshot)
    assert error.category == "budget_exceeded"


async def test_model_tokens_limit_stops_the_next_call() -> None:
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.EXECUTING, charge={"model_tokens": 150}),
        step(S.EXECUTING, S.EXECUTING, charge={"model_tokens": 1}),
        config=budget_config(max_model_tokens=100),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.BUDGET_EXCEEDED
    assert exceeded_limits(app, task_id) == ["model_tokens"]
    assert snapshot.usage.model_tokens == 150


async def test_failures_limit_stops_the_task() -> None:
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.EXECUTING, failures=1),
        step(S.EXECUTING, S.EXECUTING, failures=1),
        config=budget_config(max_failures=1),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.BUDGET_EXCEEDED
    assert exceeded_limits(app, task_id) == ["failures"]
    assert snapshot.usage.failures == 2


class SpendsClock:
    def __init__(self, clock: ManualClock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds
        self.calls = 0

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        self.calls += 1
        self.clock.advance(self.seconds)
        return StageOutcome(next_status=S.EXECUTING, reason="шаг", changes=TaskChanges())


class Goes:
    def __init__(self, target: TaskStatus, route: str | None = None) -> None:
        self.target = target
        self.route = route

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        changes = TaskChanges.model_validate({"route": self.route})
        return StageOutcome(next_status=self.target, reason="дальше", changes=changes)


async def test_wall_time_is_checked_before_each_tick() -> None:
    clock = ManualClock()
    executing = SpendsClock(clock, seconds=3)
    app = make_app(
        {
            S.ROUTING: Goes(S.PLANNING, "agent"),
            S.PLANNING: Goes(S.EXECUTING),
            S.EXECUTING: executing,
        },
        config=budget_config(max_wall_time_s=5),
        clock=clock,
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.BUDGET_EXCEEDED
    assert exceeded_limits(app, task_id) == ["wall_time"]
    assert executing.calls == 2  # 3 с + 3 с: третий такт не начинается
    assert snapshot.usage.active_time_s == 6


async def test_wall_time_interrupts_a_long_tick() -> None:
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.VERIFYING, hang=True),
        config=budget_config(max_wall_time_s=0.05),
    )
    task_id = app.tasks.submit(request())
    snapshot = await asyncio.wait_for(app.tasks.run_until_blocked(task_id), timeout=5)

    assert snapshot.status is S.BUDGET_EXCEEDED
    assert exceeded_limits(app, task_id) == ["wall_time"]
    assert snapshot.usage.active_time_s >= 0.05


async def test_usage_spent_before_an_exception_is_kept() -> None:
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1, "tool_calls": 2}, error="fatal"),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.FAILED
    assert (snapshot.usage.steps, snapshot.usage.tool_calls) == (1, 2)


async def test_routing_has_its_own_budget() -> None:
    app, _ = scripted(step(S.ROUTING, S.PLANNING, route="agent", charge={"model_calls": 4}))
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.BUDGET_EXCEEDED
    assert transitions(app, task_id) == [S.ROUTING, S.BUDGET_EXCEEDED]
    assert exceeded_limits(app, task_id) == ["model_calls"]
    assert snapshot.route is None  # решение роутера не применено


class AnswersLate:
    """Глотает отмену такта и всё равно возвращает результат."""

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        return StageOutcome(next_status=S.VERIFYING, reason="поздний ответ")


async def test_result_of_a_timed_out_tick_is_discarded() -> None:
    app = make_app(
        {**ScriptedStages(agent_prefix()).handlers(), S.EXECUTING: AnswersLate()},
        config=budget_config(max_wall_time_s=0.05),
    )
    task_id = app.tasks.submit(request())
    snapshot = await asyncio.wait_for(app.tasks.run_until_blocked(task_id), timeout=5)

    assert snapshot.status is S.BUDGET_EXCEEDED
    assert transitions(app, task_id)[-2:] == [S.EXECUTING, S.BUDGET_EXCEEDED]
    assert snapshot.usage.active_time_s >= 0.05


class Stubborn:
    """Не отменяется вовсе — runner не должен ждать её бесконечно."""

    def __init__(self) -> None:
        self.released = False

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        while not self.released:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.01)
        raise asyncio.CancelledError


async def test_stuck_stage_does_not_block_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "CANCEL_GRACE_S", 0.05)
    stubborn = Stubborn()
    app = make_app(
        {**ScriptedStages(agent_prefix()).handlers(), S.EXECUTING: stubborn},
        config=budget_config(max_wall_time_s=0.05),
    )
    task_id = app.tasks.submit(request())
    try:
        snapshot = await asyncio.wait_for(app.tasks.run_until_blocked(task_id), timeout=5)
    finally:
        stubborn.released = True
        await asyncio.sleep(0.05)
    assert snapshot.status is S.BUDGET_EXCEEDED
