from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from jarvis.adapters.clock import ManualClock
from jarvis.core.metrics import compute_metrics
from jarvis.domain.ids import TaskId
from jarvis.domain.metrics import TaskMetrics
from jarvis.domain.trace import EventKind, TraceEvent
from tests.helpers import S, agent_prefix, request, scripted, step

pytestmark = pytest.mark.anyio

START = datetime(2026, 1, 1, tzinfo=UTC)


def event(seq: int, seconds: float, kind: EventKind, **payload: JsonValue) -> TraceEvent:
    return TraceEvent(
        id=f"task_1.ev_{seq}",
        task_id=TaskId("task_1"),
        seq=seq,
        ts=START + timedelta(seconds=seconds),
        kind=kind,
        payload=payload,
    )


def move(seq: int, seconds: float, source: str, target: str) -> TraceEvent:
    return event(seq, seconds, EventKind.TASK_TRANSITION, **{"from": source, "to": target, "reason": "r"})


def test_metrics_are_folded_from_events() -> None:
    events = [
        event(1, 0, EventKind.TASK_CREATED, text="t", origin="eval"),
        move(2, 1, "CREATED", "ROUTING"),  # секунда ожидания запуска — не работа
        move(3, 2, "ROUTING", "PLANNING"),
        move(4, 3, "PLANNING", "EXECUTING"),
        move(5, 5, "EXECUTING", "WAITING_CONFIRMATION"),
        move(6, 65, "WAITING_CONFIRMATION", "EXECUTING"),  # минута ожидания человека — не работа
        move(7, 66, "EXECUTING", "REPLANNING"),
        move(8, 67, "REPLANNING", "EXECUTING"),
        event(9, 68, EventKind.ERROR, category="x", disposition="fatal", message="m"),
        move(10, 68, "EXECUTING", "FAILED"),
        event(11, 68, EventKind.TASK_FINISHED, status="FAILED", answer=None),
        event(12, 99, EventKind.TASK_CREATED),  # незнакомые и лишние события не ломают свёртку
    ]
    assert compute_metrics(events) == TaskMetrics(
        duration_ms=68_000, active_ms=7_000, transitions=8, failures=1, replans=1, finished=True
    )


@pytest.mark.parametrize(
    ("interruption", "active_ms"),
    [
        ("owner_lost", 2_000),  # процесс умер: 598 с простоя — не работа
        ("run_stopped", 600_000),  # прогон остановили посреди такта: время до остановки — работа
        (None, 600_000),  # обычный переход, даже с причиной «interrupted» в тексте
    ],
)
def test_interruption_cause_decides_the_last_interval(interruption: str | None, active_ms: int) -> None:
    payload: dict[str, JsonValue] = {"from": "EXECUTING", "to": "FAILED", "reason": "interrupted"}
    if interruption is not None:
        payload["interruption"] = interruption
    events = [
        event(1, 0, EventKind.TASK_CREATED),
        move(2, 0, "CREATED", "ROUTING"),
        move(3, 2, "ROUTING", "EXECUTING"),
        event(4, 600, EventKind.TASK_TRANSITION, **payload),
    ]
    metrics = compute_metrics(events)
    assert (metrics.duration_ms, metrics.active_ms) == (600_000, active_ms)


def test_unknown_status_from_a_newer_trace_is_tolerated() -> None:
    events = [
        event(1, 0, EventKind.TASK_CREATED),
        move(2, 1, "CREATED", "ROUTING"),
        move(3, 2, "ROUTING", "THINKING_HARD"),
        move(4, 5, "THINKING_HARD", "COMPLETED"),
    ]
    metrics = compute_metrics(events)
    assert (metrics.transitions, metrics.active_ms) == (3, 1_000)


def test_unfinished_task_counts_until_the_last_event() -> None:
    events = [event(1, 0, EventKind.TASK_CREATED), move(2, 2, "CREATED", "ROUTING")]
    metrics = compute_metrics(events)
    assert (metrics.duration_ms, metrics.active_ms, metrics.finished) == (2_000, 0, False)
    assert compute_metrics([]).duration_ms == 0


async def test_service_reports_metrics_with_the_trace() -> None:
    clock = ManualClock()
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.VERIFYING),
        step(S.VERIFYING, S.COMPLETED),
        clock=clock,
    )
    task_id = app.tasks.submit(request())
    await app.tasks.run_until_blocked(task_id)
    inspection = app.tasks.inspect(task_id)
    assert inspection.task.status is S.COMPLETED
    assert inspection.events == app.tasks.trace(task_id)
    assert inspection.metrics.transitions == 5
    assert inspection.metrics.finished
