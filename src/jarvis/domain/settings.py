"""Схема настроек (06-errors-and-config.md §2, ADR 0018).

Чистые pydantic-модели. Загрузку слоёв (файлы, окружение) делает `jarvis.config`; ядро получает готовый
объект. Разделы появляются вместе с потребителем.
"""

import ipaddress
import json
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
from jarvis.domain.routing import CloudMode, Route, RoutingLevel
from jarvis.domain.secrets import find_secrets, mask_secrets


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


def _points_here(host: str) -> bool:
    """Хост удалённого провайдера ведёт на этот компьютер или неоднозначен: localhost в любом виде
    (`localhost.`, `api.localhost`), петля и «любой адрес» (0.0.0.0, [::], [::ffff:127.0.0.1]), а также
    IP в нестандартной записи (`127.1`, `2130706433`, `0x7f000001`), которую резолвер понимает по-своему."""
    name = host.lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    if name.startswith("["):
        try:
            address = ipaddress.IPv6Address(name[1:-1])
        except ValueError:
            return True
        mapped = address.ipv4_mapped
        return (
            address.is_loopback
            or address.is_unspecified
            or (mapped is not None and (mapped.is_loopback or mapped.is_unspecified))
        )
    last = name.rsplit(".", 1)[-1]
    if not (last.isdigit() or last.startswith("0x")):
        return False  # имя хоста (у доменов верхнего уровня нет чисел)
    try:
        address = ipaddress.IPv4Address(name)  # только каноническая запись из четырёх чисел
    except ValueError:
        return True
    return address.is_loopback or address.is_unspecified


def _no_secret(value: JsonValue, field: str) -> None:
    """Ключ в конфиге — ошибка: только ссылка `api_key = "env:ИМЯ"`. Значение не повторяется."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    found = find_secrets(text)
    if found:
        raise ValueError(
            f"{field}: похоже на секрет ({', '.join(found)}) — ключ задаётся только ссылкой "
            'api_key = "env:ИМЯ"; значение здесь не показывается'
        )


def _shown(url: str) -> str:
    """Адрес для сообщения об ошибке: без параметров запроса (в них бывают ключи) и найденных секретов."""
    base, query, _ = url.partition("?")
    return mask_secrets(base) + ("?…" if query else "")


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
        _no_secret(value, "base_url")
        if _BASE_URL.fullmatch(value) is None:
            raise ValueError(f"нужен адрес вида http://127.0.0.1:8080/v1, а не {_shown(value)!r}")
        if not is_loopback_url(value):
            raise ValueError(
                f"сервер модели должен быть на этом компьютере (localhost, 127.0.0.1, [::1]): {_shown(value)}"
            )
        return value.rstrip("/")

    @field_validator("extra_body")
    @classmethod
    def _extra_without_secrets(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _no_secret(value, "extra_body")
        return value


# Ссылка на секрет: ключ лежит в переменной окружения, в конфиге — только её имя (ADR 0027).
SECRET_REF = re.compile(r"env:[A-Z_][A-Z0-9_]{0,127}")
_SECRET_REF_HINT = (
    "нужна ссылка вида env:ИМЯ_ПЕРЕМЕННОЙ (латиница в верхнем регистре, цифры, «_»): сам ключ в конфиге не "
    "хранится, значение здесь не показывается"
)


class RemoteEndpointSettings(BaseModel, frozen=True, extra="forbid"):
    """Провайдер модели в интернете с OpenAI-совместимым API (ADR 0027). Промпт уходит с компьютера —
    только через границу приватности (ADR 0028). Ядро не знает поставщика: только ID и возможности."""

    base_url: str  # "https://api.provider.example/v1"
    model: str = Field(min_length=1)  # имя модели у провайдера
    api_key: str  # ссылка на секрет: "env:JARVIS_SMART_API_KEY"
    request_timeout_s: float = Field(default=90.0, gt=0, le=600)
    max_response_bytes: int = Field(default=4 * 1024 * 1024, ge=4096, le=64 * 1024 * 1024)
    capabilities: ModelCapabilities
    sampling: SamplingSettings = SamplingSettings()
    # Провайдер не применяет JSON Schema, но выдаёт JSON-объект: response_format = json_object, а схема —
    # в промпте с проверкой и ремонтом.
    json_object: bool = False
    # Особенности провайдера: выключить рассуждения, параметры безопасности и т. п.
    extra_body: dict[str, JsonValue] = {}
    # Признаки исчерпанной квоты в ответе 429 сверх общих («quota», «balance», «billing» …).
    quota_markers: list[str] = []

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        _no_secret(value, "base_url")
        match = _BASE_URL.fullmatch(value)
        if match is None or match.group("scheme") != "https":
            raise ValueError(
                f"удалённый провайдер — только https-адрес вида https://host/v1, а не {_shown(value)!r}"
            )
        if _points_here(match.group("host")):
            raise ValueError(
                f"{_shown(value)}: сервер на этом компьютере (или IP не в виде 1.2.3.4) — это локальный "
                "эндпоинт ([models.endpoints]), а не удалённый"
            )
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def _reference(cls, value: str) -> str:
        if SECRET_REF.fullmatch(value) is None:
            raise ValueError(_SECRET_REF_HINT)  # значение не повторяется: это может быть сам ключ
        return value

    @field_validator("extra_body")
    @classmethod
    def _extra_without_secrets(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _no_secret(value, "extra_body")
        return value

    @property
    def api_key_env(self) -> str:
        return self.api_key.removeprefix("env:")


class RoutingSettings(BaseModel, frozen=True, extra="forbid"):
    """Цепочки провайдеров по уровням (ADR 0027): порядок — предпочтение человека по качеству, цене и
    задержке. Основная локальная модель (роль executor) добавляется в конец каждой цепочки сама."""

    mode: CloudMode = CloudMode.AUTO
    fast: list[str] = []  # только локальные эндпоинты
    local: list[str] = []  # только локальные эндпоинты
    smart: list[str] = []
    coding: list[str] = []
    max_providers: int = Field(default=3, ge=1, le=5)  # попыток на один вызов модели, последняя — локальная
    degraded_latency_s: float = Field(default=45.0, gt=0)  # ответ медленнее — провайдер DEGRADED

    def chain(self, level: RoutingLevel) -> list[str]:
        return list(getattr(self, level.value))


class CloudSettings(BaseModel, frozen=True, extra="forbid"):
    """Граница приватности облака (ADR 0028). По умолчанию облако выключено: после обновления Jarvis не
    начинает отправлять данные в сеть сам."""

    enabled: bool = False
    allow_local_metadata: bool = True
    allow_file_content: bool = False
    allow_source_code: bool = False
    allow_personal_data: bool = False
    allow_secrets: Literal[False] = (
        False  # секреты не уходят никогда: настройка существует, чтобы это сказать
    )
    private_roots: list[str] = []  # всё отсюда и задачи, начатые здесь, не уходят никогда
    personal_roots: list[str] = []  # личные папки сверх известных (Документы, Рабочий стол, Загрузки …)
    max_prompt_tokens: int = Field(default=32_000, ge=1024)


class ModelsSettings(BaseModel, frozen=True, extra="forbid"):
    endpoints: dict[str, EndpointSettings] = {}
    remote: dict[str, RemoteEndpointSettings] = {}
    roles: dict[ModelRole, str] = {}  # роль → ID локального эндпоинта; без назначения агент не запускается
    routing: RoutingSettings = RoutingSettings()
    repair_attempts: NonNegativeInt = Field(default=2, le=5)  # повторов после ответа не по схеме

    @model_validator(mode="after")
    def _known_endpoints(self) -> Self:
        for name in (*self.endpoints, *self.remote):
            if _ENDPOINT_ID.fullmatch(name) is None:
                raise ValueError(f"ID эндпоинта — строчные латинские буквы, цифры, «_» и «-»: {name!r}")
        shared = sorted(set(self.endpoints) & set(self.remote))
        if shared:
            raise ValueError(f"ID эндпоинта занят и локальным, и удалённым: {', '.join(shared)}")
        for role, name in self.roles.items():
            if name in self.remote:
                raise ValueError(
                    f"роли {role} назначен удалённый эндпоинт {name!r}: роль — только локальная модель"
                )
            if name not in self.endpoints:
                raise ValueError(f"роли {role} назначен неизвестный эндпоинт {name!r}")
        for level in RoutingLevel:
            for name in self.routing.chain(level):
                if name not in self.endpoints and name not in self.remote:
                    raise ValueError(f"в цепочке models.routing.{level} неизвестный эндпоинт {name!r}")
                if level in (RoutingLevel.FAST, RoutingLevel.LOCAL) and name in self.remote:
                    raise ValueError(
                        f"в цепочке models.routing.{level} удалённый эндпоинт {name!r}: уровни fast и local "
                        "работают только на этом компьютере"
                    )
        return self


class JarvisConfig(BaseModel, frozen=True, extra="forbid"):
    schema_version: Literal[1] = 1
    budgets: BudgetsSettings = BudgetsSettings()
    runtime: RuntimeSettings = RuntimeSettings()
    policy: PolicySettings = PolicySettings()
    models: ModelsSettings = ModelsSettings()
    cloud: CloudSettings = CloudSettings()
