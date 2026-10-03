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


class ToolError(JarvisError):
    """Сбой вызова инструмента. По умолчанию — обратная связь стадии (агенту), а не конец задачи."""

    category = "tool_error"
    disposition = Disposition.FEEDBACK


class ToolNotFound(ToolError):
    category = "tool_not_found"


class InvalidToolArguments(ToolError):
    category = "invalid_tool_arguments"


class ToolPreviewFailed(ToolError):
    category = "tool_preview_failed"


class UnsupportedTarget(ToolError):
    category = "unsupported_target"


class ToolDenied(ToolError):
    """Категория отказа политики или человека. Отказ — исход-значение; исключение поднимает стадия,
    которой без этого вызова продолжать нельзя."""

    category = "tool_denied"


class ApprovalRequired(ToolError):
    """Категория вызова, которому нужно подтверждение человека (в трассе и аудите)."""

    category = "approval_required"
    disposition = Disposition.ASK_USER


class ToolExecutionFailed(ToolError):
    category = "tool_execution_failed"


class ToolTimeout(ToolError):
    category = "tool_timeout"


class ToolCancelled(ToolError):
    category = "tool_cancelled"
    disposition = Disposition.STOP


class ToolVerificationFailed(ToolError):
    category = "tool_verification_failed"
    disposition = Disposition.REPLAN


class ApprovalNotFound(JarvisError):
    category = "approval_not_found"


class ApprovalClosed(JarvisError):
    """Запрос уже решён, истёк или задача больше его не ждёт."""

    category = "approval_closed"


class ModelError(JarvisError):
    """Сбой обращения к модели: сервер недоступен, не ответил вовремя или отклонил запрос."""

    category = "model_error"


class ModelUnavailable(ModelError):
    """Сервер не принимает соединение или отвечает 5xx (модель ещё грузится)."""

    category = "model_unavailable"
    disposition = Disposition.RETRY
    retryable = True


class ModelTimeout(ModelError):
    category = "model_timeout"


class ModelRequestRejected(ModelError):
    """Сервер отклонил запрос (4xx) или ответил не по протоколу: повтор не поможет."""

    category = "model_request_rejected"


class ModelContextExceeded(ModelRequestRejected):
    """Промпт не поместился в окно контекста сервера: можно повторить с более коротким."""

    category = "model_context_exceeded"


class InvalidModelOutput(JarvisError):
    """Ответ модели не прошёл схему или семантическую проверку и после всех попыток ремонта."""

    category = "invalid_model_output"
    disposition = Disposition.FEEDBACK


class VerificationFailed(JarvisError):
    """Итог задачи не подтверждается тем, что действительно произошло."""

    category = "verification_failed"
    disposition = Disposition.REPLAN


class InvalidTransition(JarvisError):
    category = "invalid_transition"


class BudgetExceeded(JarvisError):
    category = "budget_exceeded"
    disposition = Disposition.STOP


class TaskCancelled(JarvisError):
    category = "task_cancelled"
    disposition = Disposition.STOP
