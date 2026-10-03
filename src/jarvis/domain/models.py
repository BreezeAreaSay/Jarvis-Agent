"""Модели: роли, возможности, промпт, запрос к бэкенду и запись вызова (03-contracts.md §3, ADR 0008,
ADR 0009, ADR 0023).

Ядро знает модель только через роль и возможности. Имя модели, рантайм, параметры сэмплирования и
формат API — в конфиге эндпоинта и в адаптере. Здесь — только данные, общие для ядра и адаптеров.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, NonNegativeInt, PositiveInt

from jarvis.domain.errors import ErrorInfo
from jarvis.domain.ids import TaskId


class ModelRole(StrEnum):
    """Роли появляются вместе со стадией, которая их вызывает (роутер — M7, планировщик — M8)."""

    EXECUTOR = "executor"  # выбирает следующее действие агента и формулирует ответ


class ModelCapabilities(BaseModel, frozen=True, extra="forbid"):
    """Что умеет модель на этом эндпоинте. Ядро решает по возможностям, а не по имени модели."""

    # Сервер ограничивает ответ JSON-схемой (грамматикой): схема уходит в запрос. Без этого схема
    # только описывается в промпте, а ответ проверяется и ремонтируется.
    structured_output: bool = False
    context_window: PositiveInt  # токенов на запрос и ответ вместе (n_ctx сервера)


class ModelInfo(BaseModel, frozen=True, extra="forbid"):
    endpoint: str  # ID эндпоинта из конфига: "main"
    model: str  # имя модели на сервере — только для журналов, ядро по нему не решает
    capabilities: ModelCapabilities


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
    latency_ms: NonNegativeInt | None = None
    prompt_ms: NonNegativeInt | None = None
    finish_reason: str | None = None
    error: ErrorInfo | None = None
    created_at: datetime
