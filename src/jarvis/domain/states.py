"""Машина состояний задачи (02-domain.md §3, ADR 0010).

Таблица переходов — данные. Runner проверяет по ней каждый результат стадии; недопустимый переход —
`InvalidTransition`, то есть ошибка программы.
"""

from collections.abc import Mapping
from enum import StrEnum

from jarvis.domain.errors import InvalidTransition


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    ROUTING = "ROUTING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    VERIFYING = "VERIFYING"
    REPLANNING = "REPLANNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


S = TaskStatus

TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED, S.BUDGET_EXCEEDED})

# Куда задачу может перевести стадия из каждого нетерминального состояния, помимо общих выходов.
_FORWARD: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    S.CREATED: frozenset({S.ROUTING}),
    S.ROUTING: frozenset({S.PLANNING, S.EXECUTING, S.COMPLETED}),
    S.PLANNING: frozenset({S.EXECUTING}),
    S.EXECUTING: frozenset({S.WAITING_CONFIRMATION, S.REPLANNING, S.VERIFYING}),
    S.WAITING_CONFIRMATION: frozenset({S.EXECUTING}),
    S.VERIFYING: frozenset({S.COMPLETED, S.REPLANNING, S.FAILED}),
    S.REPLANNING: frozenset({S.EXECUTING}),
}

# Любое нетерминальное состояние может завершиться сбоем, отменой или превышением бюджета.
_ALWAYS: frozenset[TaskStatus] = frozenset({S.FAILED, S.CANCELLED, S.BUDGET_EXCEEDED})

ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    status: (_FORWARD[status] | _ALWAYS) if status not in TERMINAL_STATUSES else frozenset()
    for status in TaskStatus
}

# Активные состояния: задачу кто-то ведёт и держит её аренду (02-domain.md §3). WAITING_CONFIRMATION
# аренды не держит, терминальные — тоже.
ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset(
    {S.CREATED, S.ROUTING, S.PLANNING, S.EXECUTING, S.VERIFYING, S.REPLANNING}
)

# Эти переходы выполняет только runner: стадия сообщает об отмене и превышении бюджета исключениями
# TaskCancelled и BudgetExceeded, чтобы в трассе и итоге всегда были причина и событие.
RUNNER_ONLY_TARGETS: frozenset[TaskStatus] = frozenset({S.CANCELLED, S.BUDGET_EXCEEDED})

# Состояния, в которых такт может закончиться без перехода: шаги агента внутри EXECUTING
# фиксируются событиями, а не переходами.
STAY_ALLOWED: frozenset[TaskStatus] = frozenset({S.EXECUTING})


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES


def is_allowed(source: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[source]


def check_transition(source: TaskStatus, target: TaskStatus) -> None:
    if not is_allowed(source, target):
        raise InvalidTransition(
            f"переход {source} → {target} не разрешён", source=source.value, target=target.value
        )
