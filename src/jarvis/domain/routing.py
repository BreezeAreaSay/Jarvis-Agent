"""Решение роутера: как исполнять задачу (ADR 0026).

Router ядра детерминированно выбирает стратегию исполнения и записывает решение с причинами. Модель
этого решения не принимает: она работает только внутри стратегии AGENT. Уровни smart и coding (облако,
ADR 0027) появятся с провайдерами V2.2.
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
    """Уровень модели для стратегии AGENT."""

    LOCAL = "local"  # основная локальная модель


class RouteDecision(BaseModel, frozen=True, extra="forbid"):
    """Что решил Router и почему. Хранится в задаче и пишется в трассу (`route.decided`)."""

    strategy: Route
    level: RoutingLevel | None = None  # AGENT: какая модель ведёт задачу
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
