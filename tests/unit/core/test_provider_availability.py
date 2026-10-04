"""Состояние провайдеров по итогам вызовов (ADR 0027): без проб, со сроком, переживает процесс."""

from pathlib import Path

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.core.models.availability import ProviderAvailability
from jarvis.domain.errors import (
    ModelAuthRequired,
    ModelError,
    ModelLimitExceeded,
    ModelMisconfigured,
    ModelRateLimited,
    ModelRequestRejected,
    ModelTimeout,
    ModelUnavailable,
)
from jarvis.domain.providers import ProviderState


@pytest.mark.parametrize(
    ("error", "state", "seconds"),
    [
        (ModelUnavailable("x"), ProviderState.UNAVAILABLE, 30),
        (ModelTimeout("x"), ProviderState.UNAVAILABLE, 30),
        (ModelAuthRequired("x"), ProviderState.AUTH_REQUIRED, 3600),
        (ModelRateLimited("x"), ProviderState.RATE_LIMITED, 60),
        (ModelRateLimited("x", retry_after_s=300), ProviderState.RATE_LIMITED, 300),
        (ModelLimitExceeded("x"), ProviderState.LIMIT_EXCEEDED, 3600),
    ],
)
def test_a_failure_sets_a_state_until_it_expires(
    error: ModelError, state: ProviderState, seconds: int
) -> None:
    clock = ManualClock()
    availability = ProviderAvailability(uow=InMemoryStorage().unit_of_work, clock=clock)
    status = availability.record_failure("cloud_a", error)
    assert status is not None
    assert status.state is state
    assert availability.status("cloud_a") == status
    clock.advance(seconds - 1)
    assert availability.status("cloud_a") is not None
    clock.advance(1)
    assert availability.status("cloud_a") is None  # срок истёк — снова кандидат


def test_misconfigured_lasts_until_restart_and_is_not_stored() -> None:
    storage = InMemoryStorage()
    clock = ManualClock()
    first = ProviderAvailability(uow=storage.unit_of_work, clock=clock)
    first.record_failure("cloud_a", ModelMisconfigured("нет ключа"))
    clock.advance(10_000)
    assert first.status("cloud_a") is not None
    restarted = ProviderAvailability(uow=storage.unit_of_work, clock=clock)
    assert restarted.status("cloud_a") is None  # после исправления конфига — сразу кандидат


def test_success_clears_the_state_and_slowness_degrades() -> None:
    clock = ManualClock()
    availability = ProviderAvailability(uow=InMemoryStorage().unit_of_work, clock=clock)
    availability.record_failure("cloud_a", ModelUnavailable("x"))
    availability.record_success("cloud_a", latency_s=1, degraded_after_s=45)
    assert availability.status("cloud_a") is None
    availability.record_success("cloud_a", latency_s=60, degraded_after_s=45)
    status = availability.status("cloud_a")
    assert status is not None
    assert status.state is ProviderState.DEGRADED


def test_a_rejected_request_says_nothing_about_the_provider() -> None:
    availability = ProviderAvailability(uow=InMemoryStorage().unit_of_work, clock=ManualClock())
    assert availability.record_failure("local", ModelRequestRejected("плохой запрос")) is None


def test_the_state_survives_a_new_cli_process(tmp_path: Path) -> None:
    clock = ManualClock()
    with SqliteStorage(tmp_path / "jarvis.db") as storage:
        ProviderAvailability(uow=storage.unit_of_work, clock=clock).record_failure(
            "cloud_a", ModelLimitExceeded("квота")
        )
    with SqliteStorage(tmp_path / "jarvis.db") as storage:
        status = ProviderAvailability(uow=storage.unit_of_work, clock=clock).status("cloud_a")
    assert status is not None
    assert status.state is ProviderState.LIMIT_EXCEEDED
