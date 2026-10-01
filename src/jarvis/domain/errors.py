"""Таксономия ошибок (06-errors-and-config.md §1, ADR 0014).

Исключения — для сбоев; ожидаемые исходы (отказ политики, нужно подтверждение) — значения. Каждое
исключение несёт категорию, признак повтора и диспозицию — что с ним делает ядро.
"""

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, JsonValue


class Disposition(StrEnum):
    RETRY = "retry"
    FEEDBACK = "feedback"
    REPLAN = "replan"
    ASK_USER = "ask_user"
    DEGRADE = "degrade"
    FATAL = "fatal"
    STOP = "stop"


class ErrorInfo(BaseModel, frozen=True):
    """Сериализуемый вид ошибки для трассы, аудита и итога задачи."""

    category: str
    disposition: Disposition
    retryable: bool
    message: str
    details: dict[str, JsonValue] = {}


class JarvisError(Exception):
    category: ClassVar[str] = "internal"
    disposition: ClassVar[Disposition] = Disposition.FATAL
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, **details: JsonValue) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, JsonValue] = details

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            category=self.category,
            disposition=self.disposition,
            retryable=self.retryable,
            message=self.message,
            details=self.details,
        )


def internal_error_info(exc: BaseException) -> ErrorInfo:
    """Неизвестное исключение — ошибка программы: FATAL, категория `internal`."""
    return ErrorInfo(
        category="internal",
        disposition=Disposition.FATAL,
        retryable=False,
        message=f"{type(exc).__name__}: {exc}",
    )


class ConfigError(JarvisError):
    category = "config"


class StorageError(JarvisError):
    category = "storage"


class ConcurrentModification(JarvisError):
    category = "concurrent_modification"


class TaskNotFound(JarvisError):
    category = "task_not_found"


class TaskBusy(JarvisError):
    category = "task_busy"


class LeaseLost(JarvisError):
    """Аренду задачи перехватил другой процесс: текущий прогон должен остановиться без записи."""

    category = "lease_lost"


class TaskInterrupted(JarvisError):
    """Процесс, который вёл задачу, завершился посреди работы (сбой, выход, потеря аренды)."""

    category = "interrupted"


class InvalidTransition(JarvisError):
    category = "invalid_transition"


class BudgetExceeded(JarvisError):
    category = "budget_exceeded"
    disposition = Disposition.STOP


class TaskCancelled(JarvisError):
    category = "task_cancelled"
    disposition = Disposition.STOP
