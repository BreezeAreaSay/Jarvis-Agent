"""Задача, запрос, итог и результат стадии (02-domain.md §2–3).

Поля появляются вместе с первым потребителем: план, профиль, проект и признак заражения добавят
milestone, которые их используют.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, PositiveInt, field_validator, model_validator

from jarvis.domain.agent import AgentState
from jarvis.domain.budget import Budget, BudgetUsage
from jarvis.domain.errors import ErrorInfo, InvalidTransition
from jarvis.domain.ids import TaskId
from jarvis.domain.privacy import NEVER, DataClass
from jarvis.domain.routing import CloudMode, Route, RouteDecision
from jarvis.domain.states import TaskStatus, is_terminal


class Origin(StrEnum):
    EVAL = "eval"
    CLI = "cli"  # `jarvis run`; другие источники (replay, голос) появятся со своими командами


# Куда ведёт решение роутера (02-domain.md §3): из ROUTING стадия может уйти только так или в FAILED.
ROUTE_TARGETS: Mapping[Route, TaskStatus] = {
    Route.AGENT: TaskStatus.PLANNING,
    Route.DIRECT: TaskStatus.EXECUTING,
    Route.CHAT: TaskStatus.EXECUTING,
    Route.CLARIFY: TaskStatus.COMPLETED,
}


def check_route_target(route: Route | None, target: TaskStatus) -> None:
    """Результат ROUTING: маршрут обязателен и определяет следующее состояние."""
    if target is TaskStatus.FAILED:
        return
    if route is None:
        raise InvalidTransition(f"ROUTING → {target} без решения о маршруте")
    if ROUTE_TARGETS[route] is not target:
        raise InvalidTransition(f"маршрут {route} ведёт в {ROUTE_TARGETS[route]}, а не в {target}")


class TaskRequest(BaseModel, frozen=True, extra="forbid"):
    text: str = Field(min_length=1)
    origin: Origin
    working_directory: str | None = None  # папка клиента; от неё инструменты считают относительные пути
    dry_run: bool = False  # всё, кроме исполнения инструментов
    mode: CloudMode | None = None  # режим запуска; None — models.routing.mode из конфига
    # Классы данных, которые человек разрешил отправить в облако при запуске (`--allow-cloud`): до конца
    # задачи, любому провайдеру её уровня. Секреты и private_roots так не разрешаются.
    allow_cloud: list[DataClass] = []

    @field_validator("allow_cloud")
    @classmethod
    def _consentable(cls, value: list[DataClass]) -> list[DataClass]:
        never = sorted(item.value for item in value if item in NEVER)
        if never:
            raise ValueError(f"не разрешается ни настройкой, ни согласием: {', '.join(never)}")
        return value


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
    routing: RouteDecision | None = None  # решение Router: стратегия, интент, причины
    budget: Budget  # бюджет текущей фазы: routing до решения роутера, затем бюджет маршрута
    usage: BudgetUsage  # расход в пределах `budget`
    outcome: TaskOutcome | None = None
    state: AgentState | None = None  # рабочая память агента: шаги, действия, итоги вызовов
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
    routing: RouteDecision | None = None  # только из ROUTING: подробности решения
    answer: str | None = None  # ответ пользователю, попадает в итог
    state: AgentState | None = None  # новая рабочая память агента


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
    routing: RouteDecision | None = None
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
            routing=task.routing,
            budget=task.budget,
            usage=task.usage,
            outcome=task.outcome,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
