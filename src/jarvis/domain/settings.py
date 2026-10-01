"""Схема настроек (06-errors-and-config.md §2, ADR 0018).

Чистые pydantic-модели. Загрузку слоёв (файлы, окружение) делает `jarvis.config`; ядро получает готовый
объект. Разделы появляются вместе с потребителем.
"""

from typing import Literal

from pydantic import BaseModel, PositiveFloat

from jarvis.domain.budget import Budget
from jarvis.domain.task import Route


class BudgetsSettings(BaseModel, frozen=True, extra="forbid"):
    """Стартовые значения из 02-domain.md §4; уточняются по eval."""

    routing: Budget = Budget(
        max_steps=0,
        max_tool_calls=0,
        max_failures=1,
        max_replans=0,
        max_wall_time_s=10,
        max_model_calls=3,
        max_model_tokens=8_000,
    )
    direct: Budget = Budget(
        max_steps=1,
        max_tool_calls=3,
        max_failures=1,
        max_replans=0,
        max_wall_time_s=15,
        max_model_calls=0,
        max_model_tokens=0,
    )
    chat: Budget = Budget(
        max_steps=1,
        max_tool_calls=0,
        max_failures=1,
        max_replans=0,
        max_wall_time_s=60,
        max_model_calls=3,
        max_model_tokens=16_000,
    )
    agent: Budget = Budget(
        max_steps=20,
        max_tool_calls=30,
        max_failures=5,
        max_replans=3,
        max_wall_time_s=300,
        max_model_calls=60,
        max_model_tokens=250_000,
    )

    def for_route(self, route: Route | None) -> Budget:
        """Бюджет фазы: до решения роутера и для уточняющего вопроса — routing."""
        match route:
            case Route.DIRECT:
                return self.direct
            case Route.CHAT:
                return self.chat
            case Route.AGENT:
                return self.agent
            case Route.CLARIFY | None:
                return self.routing


class RuntimeSettings(BaseModel, frozen=True, extra="forbid"):
    # Срок аренды задачи. Владелец продлевает её каждую треть срока; задача упавшего процесса
    # считается прерванной, когда срок истёк.
    lease_ttl_s: PositiveFloat = 30.0


class JarvisConfig(BaseModel, frozen=True, extra="forbid"):
    schema_version: Literal[1] = 1
    budgets: BudgetsSettings = BudgetsSettings()
    runtime: RuntimeSettings = RuntimeSettings()
