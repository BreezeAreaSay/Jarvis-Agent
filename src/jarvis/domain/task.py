"""Задача, запрос, итог и результат стадии (02-domain.md §2–3).

Поля появляются вместе с первым потребителем: план, рабочая память агента, профиль, проект и
признак заражения добавят milestone, которые их используют.
"""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, PositiveInt, model_validator

from jarvis.domain.budget import Budget, BudgetUsage
from jarvis.domain.errors import ErrorInfo
from jarvis.domain.ids import TaskId
from jarvis.domain.states import TaskStatus, is_terminal


class Origin(StrEnum):
    CLI = "cli"
    EVAL = "eval"


class Route(StrEnum):
    DIRECT = "direct"
    AGENT = "agent"
    CHAT = "chat"
    CLARIFY = "clarify"


class TaskRequest(BaseModel, frozen=True, extra="forbid"):
    text: str = Field(min_length=1)
    origin: Origin


class TaskOutcome(BaseModel, frozen=True, extra="forbid"):
    status: TaskStatus
    answer: str | None = None
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _terminal(self) -> Self:
        if not is_terminal(self.status):
            raise ValueError(f"итог задачи бывает только у терминального статуса, а не {self.status}")
        return self


class Task(BaseModel, frozen=True, extra="forbid"):
    id: TaskId
    version: PositiveInt  # оптимистическая блокировка при сохранении
    request: TaskRequest
    status: TaskStatus
    route: Route | None = None
    budget: Budget  # бюджет текущей фазы: routing до решения роутера, затем бюджет маршрута
    usage: BudgetUsage  # расход в пределах `budget`
    outcome: TaskOutcome | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _outcome_matches_status(self) -> Self:
        if is_terminal(self.status) != (self.outcome is not None):
            raise ValueError("итог есть ровно у задачи в терминальном статусе")
        if self.outcome is not None and self.outcome.status != self.status:
            raise ValueError("статус итога не совпадает со статусом задачи")
        return self


class TaskChanges(BaseModel, frozen=True, extra="forbid"):
    """Что стадия просит изменить в задаче; None — не менять."""

    route: Route | None = None  # только из ROUTING: решение роутера
    answer: str | None = None  # ответ пользователю, попадает в итог


class StageOutcome(BaseModel, frozen=True, extra="forbid"):
    """Результат такта стадии. Статус меняет не стадия, а runner — после проверки по таблице."""

    next_status: TaskStatus
    reason: str = Field(min_length=1)
    changes: TaskChanges = TaskChanges()


class TaskSnapshot(BaseModel, frozen=True, extra="forbid"):
    """То, что видят клиенты ядра (CLI, eval)."""

    id: TaskId
    version: PositiveInt
    status: TaskStatus
    route: Route | None
    budget: Budget
    usage: BudgetUsage
    outcome: TaskOutcome | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, task: Task) -> Self:
        return cls(
            id=task.id,
            version=task.version,
            status=task.status,
            route=task.route,
            budget=task.budget,
            usage=task.usage,
            outcome=task.outcome,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
