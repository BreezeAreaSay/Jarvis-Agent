"""Решение роутера: как исполнять задачу (ADR 0026).

Router ядра детерминированно выбирает стратегию исполнения, а для AGENT — уровень модели, и записывает
решение с причинами. Модель этого решения не принимает: она работает только внутри стратегии AGENT.
Router выбирает уровень, а не поставщика: конкретного провайдера внутри уровня выбирает Model Gateway
(ADR 0027).
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from jarvis.domain.intents import EntityKind, IntentId, ResolvedEntity


class Route(StrEnum):
    """Стратегия исполнения задачи."""

    DIRECT = "direct"  # распознанная команда: инструмент без модели
    AGENT = "agent"  # агентный цикл на модели
    CHAT = "chat"  # спящий: сливается с AGENT без инструментов (ADR 0026)
    CLARIFY = "clarify"  # уточняющий вопрос по шаблону


class RoutingLevel(StrEnum):
    """Уровень модели для стратегии AGENT (ADR 0026, ADR 0027)."""

    FAST = "fast"  # короткая свободная команда; только локальные модели
    LOCAL = "local"  # обычная задача агента, приватное, офлайн; только локальные модели
    SMART = "smart"  # глубокий анализ: облачные провайдеры по политике → локальная модель
    CODING = "coding"  # работа с кодом: облачные модели с сильным кодом по политике → локальная модель


# Уровни, на которых провайдер вне компьютера не вызывается никогда.
LOCAL_LEVELS = frozenset({RoutingLevel.FAST, RoutingLevel.LOCAL})


class CloudMode(StrEnum):
    """Режим запуска: параметр команды или `models.routing.mode` (ADR 0027)."""

    AUTO = "auto"  # уровень выбирает Router по признакам запроса; облако — по политике
    LOCAL_ONLY = "local_only"  # ни одного вызова провайдера вне компьютера
    SMART = "smart"  # задачи агента — на уровне smart
    CODING = "coding"  # задачи агента — на уровне coding


class RouteDecision(BaseModel, frozen=True, extra="forbid"):
    """Что решил Router и почему. Хранится в задаче и пишется в трассу (`route.decided`)."""

    strategy: Route
    level: RoutingLevel | None = None  # AGENT: какая модель ведёт задачу
    mode: CloudMode = CloudMode.AUTO  # действующий режим запуска
    intent: IntentId | None = None  # DIRECT: что исполнить
    entities: list[ResolvedEntity] = []  # DIRECT: разрешённые сущности; CLARIFY: кандидаты
    rules: list[str] = Field(min_length=1)  # ID сработавших правил: «direct.launch.verb», «agent.default»
    reason: str = Field(min_length=1)
    question: str | None = None  # CLARIFY: что спросить у человека

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.strategy is Route.DIRECT) != (self.intent is not None):
            raise ValueError("интент есть ровно у решения DIRECT")
        if (self.strategy is Route.AGENT) != (self.level is not None):
            raise ValueError("уровень модели есть ровно у решения AGENT")
        if (self.strategy is Route.CLARIFY) != (self.question is not None):
            raise ValueError("вопрос есть ровно у решения CLARIFY")
        return self

    def entity(self, kind: EntityKind) -> ResolvedEntity | None:
        return next((item for item in self.entities if item.kind is kind), None)
