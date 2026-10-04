"""Провайдеры моделей: вид, состояние по итогам вызовов, разрешение на вызов (ADR 0027).

Ядро не знает брендов: провайдер — ID эндпоинта из конфига, его вид и возможности. Состояние
провайдера выводится из итогов настоящих вызовов, без проб перед каждым запросом, и действует до
`valid_until`.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ProviderKind(StrEnum):
    LOCAL_MODEL = "local_model"  # сервер на этом компьютере (только loopback)
    REMOTE_MODEL_API = "remote_model_api"  # API в интернете: промпт покидает компьютер


class ProviderState(StrEnum):
    READY = "ready"  # успешный ответ
    DEGRADED = "degraded"  # отвечает медленно: в конец цепочки, но не исключается
    UNAVAILABLE = "unavailable"  # нет соединения, таймаут, 5xx
    AUTH_REQUIRED = "auth_required"  # 401/403: ключ неверен или отозван
    RATE_LIMITED = "rate_limited"  # 429 без признака квоты: подождать
    LIMIT_EXCEEDED = "limit_exceeded"  # квота, баланс, лимит тарифа
    MISCONFIGURED = "misconfigured"  # нет ключа в окружении, неверный адрес или модель


class ProviderStatus(BaseModel, frozen=True, extra="forbid"):
    provider: str = Field(min_length=1)
    state: ProviderState
    reason: str  # категория ошибки или пояснение — без текста ответа провайдера
    valid_until: datetime | None = None  # None — до перезапуска процесса
    updated_at: datetime
