"""Модели: роли, возможности, промпт, запрос к бэкенду и запись вызова (03-contracts.md §3, ADR 0008,
ADR 0009, ADR 0023).

Ядро знает модель только через роль и возможности. Имя модели, рантайм, параметры сэмплирования и
формат API — в конфиге эндпоинта и в адаптере. Здесь — только данные, общие для ядра и адаптеров.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, JsonValue, NonNegativeInt, PositiveInt, model_validator

from jarvis.domain.errors import ErrorInfo
from jarvis.domain.ids import TaskId
from jarvis.domain.privacy import DataClass
from jarvis.domain.providers import ProviderKind


class ModelRole(StrEnum):
    """Роли появляются вместе со стадией, которая их вызывает (роутер — M7, планировщик — M8)."""

    EXECUTOR = "executor"  # выбирает следующее действие агента и формулирует ответ


class ReasoningBehavior(StrEnum):
    """Как скрытые рассуждения модели соотносятся с лимитом вывода. Не каждый API сообщает число токенов
    рассуждений или даёт задать их бюджет, поэтому ядро знает только то, что влияет на расчёт бюджетов."""

    NONE = "none"  # ответ — это и есть ответ: рассуждений нет, они выключены или не расходуют лимит вывода
    SHARES_OUTPUT = "shares_output"  # рассуждения расходуют тот же лимит вывода, что и ответ


class ModelCapabilities(BaseModel, frozen=True, extra="forbid"):
    """Что умеет модель на этом эндпоинте. Ядро решает по возможностям, а не по имени модели;
    бюджеты промпта и ответа считает Model Gateway (ADR 0027)."""

    # Сервер ограничивает ответ JSON-схемой (грамматикой): схема уходит в запрос. Без этого схема
    # только описывается в промпте, а ответ проверяется и ремонтируется.
    structured_output: bool = False
    context_window: PositiveInt  # токенов на запрос и ответ вместе (n_ctx сервера)
    # Максимальный вывод эндпоинта за один ответ (лимит сервера или модели). Не задан — ограничивает
    # только окно контекста.
    max_output_tokens: PositiveInt | None = None
    reasoning_behavior: ReasoningBehavior = ReasoningBehavior.NONE
    # Сколько из лимита вывода уходит на рассуждения, если это известно или задаётся (необязательно):
    # без него при SHARES_OUTPUT ответу отдаётся весь max_output_tokens.
    reasoning_budget: PositiveInt | None = None

    @model_validator(mode="after")
    def _consistent_output(self) -> Self:
        if self.max_output_tokens is not None and self.max_output_tokens >= self.context_window:
            raise ValueError(
                f"max_output_tokens ({self.max_output_tokens}) должен быть меньше окна контекста "
                f"({self.context_window}): промпту нужно место"
            )
        shares = self.reasoning_behavior is ReasoningBehavior.SHARES_OUTPUT
        if self.reasoning_budget is not None and not shares:
            raise ValueError("reasoning_budget имеет смысл только при reasoning_behavior = shares_output")
        if shares and self.max_output_tokens is None and self.reasoning_budget is None:
            raise ValueError(
                "при reasoning_behavior = shares_output задайте max_output_tokens или reasoning_budget: "
                "иначе неизвестно, сколько вывода оставить на рассуждения"
            )
        if (
            self.reasoning_budget is not None
            and self.max_output_tokens is not None
            and self.reasoning_budget >= self.max_output_tokens
        ):
            raise ValueError("reasoning_budget должен быть меньше max_output_tokens: ответу нужно место")
        return self


class ModelInfo(BaseModel, frozen=True, extra="forbid"):
    endpoint: str  # ID эндпоинта из конфига: "main"
    model: str  # имя модели на сервере — только для журналов, ядро по нему не решает
    capabilities: ModelCapabilities
    kind: ProviderKind = ProviderKind.LOCAL_MODEL

    @property
    def remote(self) -> bool:
        """Промпт покидает компьютер: вызов только с разрешения политики приватности (ADR 0028)."""
        return self.kind is ProviderKind.REMOTE_MODEL_API


class Trust(StrEnum):
    TRUSTED = "trusted"  # написано Jarvis или пользователем: правила, запрос, описания инструментов
    DERIVED = "derived"  # написано моделью: прав не даёт
    UNTRUSTED = "untrusted"  # извне (вывод инструментов, файлы): только внутри блока DATA


class PromptSection(BaseModel, frozen=True, extra="forbid"):
    kind: Literal["system", "request", "tools", "history", "data"]
    trust: Trust
    title: str | None = None  # заголовок секции для модели
    source: str | None = None  # откуда данные: "tool:filesystem.read_text"
    ref: str | None = None  # ID для блока DATA: "task_1.call_2"
    sensitive: bool = False  # секрет, прочитанный с разрешения: модель видит, журнал — нет
    content: str


class Prompt(BaseModel, frozen=True, extra="forbid"):
    template_id: str = Field(min_length=1)  # "executor.v1": версия промпта вместе со схемой ответа
    sections: list[PromptSection]  # стабильные секции — первыми (кэш префикса на сервере)
    # Классы данных, которые несёт промпт, — по происхождению: запрос и все наблюдения задачи, в том
    # числе опущенные из-за окна (решения модели могли их пересказать). Их проверяет граница облака.
    data_classes: frozenset[DataClass] = frozenset({DataClass.LOCAL_METADATA})


class ChatMessage(BaseModel, frozen=True, extra="forbid"):
    role: Literal["system", "user", "assistant"]
    content: str


class BackendRequest(BaseModel, frozen=True, extra="forbid"):
    """Отрисованный промпт. Сэмплирование, имя модели и особенности сервера добавляет адаптер."""

    messages: list[ChatMessage]
    json_schema: dict[str, JsonValue] | None = None  # ограничить ответ схемой (structured_output)
    max_tokens: PositiveInt  # длина ответа — свойство роли, а не модели


class BackendResponse(BaseModel, frozen=True, extra="forbid"):
    text: str  # ответ без блока «размышлений»
    finish_reason: str | None = None  # как его назвал сервер — для журнала
    truncated: bool = False  # ответ оборван лимитом длины (адаптер переводит формат сервера)
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    # Токены скрытых рассуждений, если провайдер их сообщает отдельно; не сообщает — None, не выдумываем.
    reasoning_tokens: NonNegativeInt | None = None
    latency_ms: NonNegativeInt  # от запроса до ответа целиком
    prompt_ms: NonNegativeInt | None = None  # обработка промпта, если сервер отдаёт тайминги


class BackendStatus(BaseModel, frozen=True, extra="forbid"):
    """Что сервер сообщает о себе (`jarvis model check`)."""

    models: list[str]  # модели, которые сервер обслуживает
    context_window: PositiveInt | None = None  # n_ctx, если сервер его сообщает
    server: str | None = None  # сборка сервера, если известна


ModelCallStatus = Literal["ok", "invalid", "error", "cancelled"]


class ModelCallRecord(BaseModel, frozen=True, extra="forbid"):
    """Одна попытка вызова модели: основа отладки и replay (05-storage-and-trace.md §1)."""

    id: str  # task_42.mc_3
    task_id: TaskId
    role: ModelRole
    endpoint: str
    model: str
    template_id: str
    attempt: PositiveInt  # 1 — первый ответ, дальше — ремонт
    prompt_sha256: str  # хеш нормализованного промпта (без ID задачи)
    messages: list[ChatMessage]
    json_schema: bool  # схема ушла на сервер (ограниченное декодирование)
    response_text: str | None  # None — ответа нет (сбой связи, отмена)
    status: ModelCallStatus
    problems: list[str] = []  # почему ответ не принят
    prompt_tokens: NonNegativeInt | None = None
    completion_tokens: NonNegativeInt | None = None
    reasoning_tokens: NonNegativeInt | None = None
    latency_ms: NonNegativeInt | None = None
    prompt_ms: NonNegativeInt | None = None
    finish_reason: str | None = None
    error: ErrorInfo | None = None
    provider_kind: ProviderKind = ProviderKind.LOCAL_MODEL
    created_at: datetime
