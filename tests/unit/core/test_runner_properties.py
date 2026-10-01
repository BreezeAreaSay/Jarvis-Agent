"""Свойство: при любых результатах стадий задача останавливается в допустимом состоянии, а трасса
содержит только допустимые переходы."""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from jarvis.core.budget import BudgetMeter
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.states import ALLOWED_TRANSITIONS, TERMINAL_STATUSES, TaskStatus
from jarvis.domain.task import Route, StageOutcome, Task, TaskChanges
from jarvis.domain.trace import EventKind
from jarvis.evals.scripted import STAGE_STATUSES
from tests.helpers import make_app, request

S = TaskStatus


class RandomStage:
    def __init__(self, targets: list[TaskStatus], routes: list[Route]) -> None:
        self.targets = targets
        self.routes = routes

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        if task.status is S.EXECUTING:
            budget.charge(BudgetLimit.STEPS)  # каждый цикл проходит через EXECUTING — прогон конечен
        target = self.targets.pop(0) if self.targets else S.FAILED
        route = self.routes.pop(0) if task.status is S.ROUTING and self.routes else None
        return StageOutcome(next_status=target, reason="случайный", changes=TaskChanges(route=route))


@settings(max_examples=150, deadline=None)
@given(
    targets=st.lists(st.sampled_from(list(TaskStatus)), max_size=40),
    routes=st.lists(st.sampled_from(list(Route)), max_size=3),
)
def test_any_stage_results_keep_the_task_consistent(targets: list[TaskStatus], routes: list[Route]) -> None:
    stage = RandomStage(list(targets), list(routes))
    app = make_app(dict.fromkeys(STAGE_STATUSES, stage))
    task_id = app.tasks.submit(request())
    snapshot = asyncio.run(app.tasks.run_until_blocked(task_id))

    assert snapshot.status in TERMINAL_STATUSES or snapshot.status is S.WAITING_CONFIRMATION
    assert (snapshot.outcome is not None) == (snapshot.status in TERMINAL_STATUSES)
    events = app.tasks.trace(task_id)
    assert [event.seq for event in events] == sorted({event.seq for event in events})
    previous = S.CREATED
    for event in events:
        if event.kind is EventKind.TASK_TRANSITION:
            target = TaskStatus(str(event.payload["to"]))
            assert event.payload["from"] == previous
            assert target in ALLOWED_TRANSITIONS[previous]
            previous = target
    assert previous is snapshot.status
    assert snapshot.usage.steps <= snapshot.budget.max_steps
