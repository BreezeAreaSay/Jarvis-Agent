"""Схема настроек (06-errors-and-config.md §2, ADR 0018).

Чистые pydantic-модели. Загрузку слоёв (файлы, окружение) делает `jarvis.config`; ядро получает готовый
объект. Разделы появляются вместе с потребителем.
"""

import ipaddress
import re
from typing import Literal, Self

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveFloat,
    field_validator,
    model_validator,
)

from jarvis.domain.budget import Budget
from jarvis.domain.models import ModelCapabilities, ModelRole
from jarvis.domain.routing import Route


class BudgetsSettings(BaseModel, frozen=True, extra="forbid"):
    """Стартовые значения из 02-domain.md §4; уточняются по eval."""

    # Router детерминированный (ADR 0026): у фазы маршрутизации нет вызовов модели вовсе.
    routing: Budget = Budget(
        max_steps=0,
        max_tool_calls=0,
        max_failures=1,
        max_replans=0,
        max_wall_time_s=10,
        max_model_calls=0,
        max_model_tokens=0,
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


class PolicySettings(BaseModel, frozen=True, extra="forbid"):
    # Папки, где запись допустима с подтверждением; вне их запись запрещена (Policy Engine v1).
    workspace_roots: list[str] = []
    approval_ttl_s: PositiveFloat = 1800.0  # срок запроса подтверждения


_BASE_URL = re.compile(
    r"(?P<scheme>https?)://(?P<host>\[[0-9a-fA-F:.]+\]|[^/:?#@\[\]\s]+)(?::(?P<port>[0-9]{1,5}))?(?:/[^?#\s]*)?"
)
_LOOPBACK_V4 = re.compile(r"127(?:\.[0-9]{1,3}){3}")
_ENDPOINT_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")


def is_loopback_url(url: str) -> bool:
    match = _BASE_URL.fullmatch(url)
    if match is None:
        return False
    port = match.group("port")
    if port is not None and not 0 < int(port) <= 65535:
        return False
    host = match.group("host").lower()
    if host == "localhost":
        return True
    if host.startswith("["):
        host = host[1:-1]
    elif _LOOPBACK_V4.fullmatch(host) is None:
        return False  # имя хоста, кроме localhost, может указывать куда угодно
    try:
        return ipaddress.ip_address(host).is_loopback  # 127.0.0.999 — не адрес
    except ValueError:
        return False


class SamplingSettings(BaseModel, frozen=True, extra="forbid"):
    """Параметры генерации: их читает только адаптер. None — умолчание сервера."""

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    seed: int | None = None
    stop: list[str] = []


class EndpointSettings(BaseModel, frozen=True, extra="forbid"):
    """Локальный сервер модели с OpenAI-совместимым API (llama.cpp `llama-server`)."""

    backend: Literal["openai_compat"] = "openai_compat"  # единственный вид бэкенда (ADR 0023)
    base_url: str  # "http://127.0.0.1:8080/v1"
    model: str = Field(default="local", min_length=1)  # имя модели на сервере
    request_timeout_s: PositiveFloat = 120.0
    capabilities: ModelCapabilities  # объявленные; `jarvis model check` сверяет их с сервером
    sampling: SamplingSettings = SamplingSettings()
    # Особенности сервера и шаблона чата, например {chat_template_kwargs = {enable_thinking = false}}.
    extra_body: dict[str, JsonValue] = {}

    @field_validator("base_url")
    @classmethod
    def _local(cls, value: str) -> str:
        # Промпт содержит файлы пользователя: он уходит только на этот компьютер (local-first).
        if _BASE_URL.fullmatch(value) is None:
            raise ValueError(f"нужен адрес вида http://127.0.0.1:8080/v1, а не {value!r}")
        if not is_loopback_url(value):
            raise ValueError(
                f"сервер модели должен быть на этом компьютере (localhost, 127.0.0.1, [::1]): {value}"
            )
        return value.rstrip("/")


class ModelsSettings(BaseModel, frozen=True, extra="forbid"):
    endpoints: dict[str, EndpointSettings] = {}
    roles: dict[ModelRole, str] = {}  # роль → ID эндпоинта; без назначения агент не запускается
    repair_attempts: NonNegativeInt = Field(default=2, le=5)  # повторов после ответа не по схеме

    @model_validator(mode="after")
    def _known_endpoints(self) -> Self:
        for name in self.endpoints:
            if _ENDPOINT_ID.fullmatch(name) is None:
                raise ValueError(f"ID эндпоинта — строчные латинские буквы, цифры, «_» и «-»: {name!r}")
        for role, name in self.roles.items():
            if name not in self.endpoints:
                raise ValueError(f"роли {role} назначен неизвестный эндпоинт {name!r}")
        return self


class JarvisConfig(BaseModel, frozen=True, extra="forbid"):
    schema_version: Literal[1] = 1
    budgets: BudgetsSettings = BudgetsSettings()
    runtime: RuntimeSettings = RuntimeSettings()
    policy: PolicySettings = PolicySettings()
    models: ModelsSettings = ModelsSettings()
