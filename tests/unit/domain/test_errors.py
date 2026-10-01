import pytest

from jarvis.domain.errors import (
    BudgetExceeded,
    ConcurrentModification,
    ConfigError,
    Disposition,
    ErrorInfo,
    InvalidTransition,
    JarvisError,
    StorageError,
    TaskBusy,
    TaskCancelled,
    TaskNotFound,
    internal_error_info,
)


@pytest.mark.parametrize(
    ("error_type", "category", "disposition"),
    [
        (ConfigError, "config", Disposition.FATAL),
        (StorageError, "storage", Disposition.FATAL),
        (ConcurrentModification, "concurrent_modification", Disposition.FATAL),
        (TaskNotFound, "task_not_found", Disposition.FATAL),
        (TaskBusy, "task_busy", Disposition.FATAL),
        (InvalidTransition, "invalid_transition", Disposition.FATAL),
        (BudgetExceeded, "budget_exceeded", Disposition.STOP),
        (TaskCancelled, "task_cancelled", Disposition.STOP),
    ],
)
def test_categories_and_dispositions(
    error_type: type[JarvisError], category: str, disposition: Disposition
) -> None:
    error = error_type("сообщение", key="value")
    assert error.category == category
    assert error.disposition is disposition
    assert error.retryable is False
    assert error.to_info() == ErrorInfo(
        category=category,
        disposition=disposition,
        retryable=False,
        message="сообщение",
        details={"key": "value"},
    )


def test_unknown_exception_is_internal_and_fatal() -> None:
    info = internal_error_info(KeyError("x"))
    assert info.category == "internal"
    assert info.disposition is Disposition.FATAL
    assert info.message == "KeyError: 'x'"


def test_error_info_round_trips_through_json() -> None:
    info = BudgetExceeded("лимит", limit="steps", value=2, maximum=2).to_info()
    assert ErrorInfo.model_validate_json(info.model_dump_json()) == info
