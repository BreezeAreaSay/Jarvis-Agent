"""Метрики из трассы: свёртка событий. Незнакомые виды событий пропускаются, поэтому новые события
(вызовы модели и инструментов) добавят свои счётчики, не ломая старые трассы."""

from collections.abc import Sequence
from datetime import datetime

from jarvis.domain.metrics import TaskMetrics
from jarvis.domain.states import ACTIVE_STATUSES, TaskStatus
from jarvis.domain.trace import EventKind, TraceEvent

# В CREATED задача ещё ждёт запуска; в WAITING_CONFIRMATION — решения человека.
_WORKING = ACTIVE_STATUSES - {TaskStatus.CREATED}


def compute_metrics(events: Sequence[TraceEvent]) -> TaskMetrics:
    if not events:
        return TaskMetrics(duration_ms=0, active_ms=0, transitions=0, failures=0, replans=0, finished=False)
    start = next((event.ts for event in events if event.kind is EventKind.TASK_CREATED), events[0].ts)
    finished = next((event.ts for event in events if event.kind is EventKind.TASK_FINISHED), None)
    end = finished or events[-1].ts

    active = 0.0
    transitions = replans = failures = 0
    state, since = TaskStatus.CREATED, start
    for event in events:
        if event.kind is EventKind.ERROR:
            failures += 1
        elif event.kind is EventKind.TASK_TRANSITION:
            transitions += 1
            target = TaskStatus(str(event.payload["to"]))
            # Перед `interrupted` процесс был мёртв неизвестное время — это не работа.
            if state in _WORKING and event.payload.get("reason") != "interrupted":
                active += _seconds(since, event.ts)
            if target is TaskStatus.REPLANNING:
                replans += 1
            state, since = target, event.ts
    return TaskMetrics(
        duration_ms=_ms(_seconds(start, end)),
        active_ms=_ms(active),
        transitions=transitions,
        failures=failures,
        replans=replans,
        finished=finished is not None,
    )


def _seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def _ms(seconds: float) -> int:
    return round(seconds * 1000)
