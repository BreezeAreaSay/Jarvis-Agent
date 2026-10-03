from datetime import UTC, datetime

import pytest

from jarvis.domain.budget import BudgetUsage
from jarvis.domain.errors import InvalidTransition, TaskCancelled
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import BudgetsSettings
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import (
    Origin,
    Route,
    Task,
    TaskOutcome,
    TaskRequest,
    TaskSnapshot,
    check_route_target,
)
from jarvis.domain.trace import MAX_PAYLOAD_BYTES, EventKind, TraceEvent

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_task(**changes: object) -> Task:
    data: dict[str, object] = {
        "id": "task_1",
        "version": 1,
        "request": TaskRequest(text="текст", origin=Origin.EVAL),
        "status": TaskStatus.CREATED,
        "budget": BudgetsSettings().routing,
        "usage": BudgetUsage(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(changes)
    return Task.model_validate(data)


def test_task_round_trips_through_json() -> None:
    task = make_task(
        status=TaskStatus.CANCELLED,
        route=Route.AGENT,
        usage=BudgetUsage(steps=2, active_time_s=1.5),
        outcome=TaskOutcome(status=TaskStatus.CANCELLED, error=TaskCancelled("стоп").to_info()),
    )
    assert Task.model_validate_json(task.model_dump_json()) == task


def test_terminal_task_requires_matching_outcome() -> None:
    with pytest.raises(ValueError, match="итог есть ровно"):
        make_task(status=TaskStatus.COMPLETED)
    with pytest.raises(ValueError, match="итог есть ровно"):
        make_task(status=TaskStatus.EXECUTING, outcome=TaskOutcome(status=TaskStatus.COMPLETED))
    with pytest.raises(ValueError, match="не совпадает"):
        make_task(status=TaskStatus.FAILED, outcome=TaskOutcome(status=TaskStatus.COMPLETED))
    with pytest.raises(ValueError, match="терминального"):
        TaskOutcome(status=TaskStatus.EXECUTING)


def test_snapshot_mirrors_task() -> None:
    task = make_task()
    snapshot = TaskSnapshot.of(task)
    # Запрос и рабочая память агента клиентам не нужны: они видят итог и трассу.
    assert snapshot.model_dump() == {
        key: value for key, value in task.model_dump().items() if key not in ("request", "state")
    }


def event(**changes: object) -> TraceEvent:
    data: dict[str, object] = {
        "id": "task_1.ev_3",
        "task_id": TaskId("task_1"),
        "seq": 3,
        "ts": NOW,
        "kind": EventKind.TASK_TRANSITION,
        "payload": {"from": "CREATED", "to": "ROUTING"},
    }
    data.update(changes)
    return TraceEvent.model_validate(data)


def test_event_id_must_match_task_and_seq() -> None:
    assert event().seq == 3
    with pytest.raises(ValueError, match="не соответствует"):
        event(seq=4)
    with pytest.raises(ValueError, match="не соответствует"):
        event(task_id="task_2")


def test_event_payload_is_limited() -> None:
    with pytest.raises(ValueError, match="байт"):
        event(payload={"text": "я" * MAX_PAYLOAD_BYTES})


@pytest.mark.parametrize(
    ("route", "target"),
    [
        (Route.AGENT, TaskStatus.PLANNING),
        (Route.DIRECT, TaskStatus.EXECUTING),
        (Route.CHAT, TaskStatus.EXECUTING),
        (Route.CLARIFY, TaskStatus.COMPLETED),
        (None, TaskStatus.FAILED),
        (Route.AGENT, TaskStatus.FAILED),
    ],
)
def test_route_decides_the_next_state(route: Route | None, target: TaskStatus) -> None:
    check_route_target(route, target)


@pytest.mark.parametrize(
    ("route", "target"),
    [
        (None, TaskStatus.PLANNING),
        (Route.AGENT, TaskStatus.EXECUTING),
        (Route.CHAT, TaskStatus.PLANNING),
        (Route.CLARIFY, TaskStatus.EXECUTING),
        (Route.DIRECT, TaskStatus.COMPLETED),
    ],
)
def test_route_and_next_state_must_agree(route: Route | None, target: TaskStatus) -> None:
    with pytest.raises(InvalidTransition):
        check_route_target(route, target)
