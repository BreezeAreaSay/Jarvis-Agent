"""Метрики из трассы: свёртка событий. Незнакомые виды событий пропускаются, поэтому новые события
добавляют свои счётчики, не ломая старые трассы. Вызовы модели считаются по `model.called` (каждая
попытка, в том числе ремонт и сбой), токены — по данным сервера."""

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
    transitions = replans = failures = tool_calls = model_calls = prompt_tokens = completion_tokens = 0
    state: TaskStatus | None = TaskStatus.CREATED
    since = start
    for event in events:
        if event.kind is EventKind.ERROR:
            failures += 1
        elif event.kind is EventKind.TOOL_STARTED:
            tool_calls += 1
        elif event.kind is EventKind.MODEL_CALLED:
            model_calls += 1
            prompt_tokens += _count(event.payload.get("prompt_tokens"))
            completion_tokens += _count(event.payload.get("completion_tokens"))
        elif event.kind is EventKind.TASK_TRANSITION:
            transitions += 1
            target = _status(event.payload.get("to"))
            # Если того, кто вёл задачу, не стало (`owner_lost`), сколько он работал до этого, неизвестно.
            if state in _WORKING and event.payload.get("interruption") != "owner_lost":
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
        tool_calls=tool_calls,
        model_calls=model_calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finished=finished is not None,
    )


def _status(value: object) -> TaskStatus | None:
    """Статус из события; незнакомый (трасса новее кода) — None, а не исключение."""
    try:
        return TaskStatus(str(value))
    except ValueError:
        return None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def _ms(seconds: float) -> int:
    return round(seconds * 1000)
