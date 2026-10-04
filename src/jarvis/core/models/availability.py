"""Состояние провайдеров моделей по итогам вызовов (ADR 0027).

Без проб перед запросом: состояние выводит Model Gateway из настоящих ответов и ошибок. Срок у каждого
состояния свой; истёк — провайдер снова кандидат. Состояния хранятся в хранилище (для CLI без демона:
следующая команда не бьёт в провайдера с исчерпанным лимитом), кроме MISCONFIGURED — оно живёт до
перезапуска процесса: после исправления конфига провайдер сразу снова кандидат.
"""

from datetime import timedelta

from jarvis.domain.errors import (
    ModelAuthRequired,
    ModelError,
    ModelLimitExceeded,
    ModelMisconfigured,
    ModelRateLimited,
    ModelTimeout,
    ModelUnavailable,
)
from jarvis.domain.providers import ProviderState, ProviderStatus
from jarvis.ports.clock import Clock
from jarvis.ports.storage import UnitOfWorkFactory

UNAVAILABLE_S = 30
RATE_LIMITED_S = 60
AUTH_REQUIRED_S = 3600
LIMIT_EXCEEDED_S = 3600
DEGRADED_S = 300

# Ошибки провайдера, после которых вызов переходит к следующему кандидату (fallback).
PROVIDER_FAILURES: tuple[type[ModelError], ...] = (
    ModelUnavailable,
    ModelTimeout,
    ModelAuthRequired,
    ModelRateLimited,
    ModelLimitExceeded,
    ModelMisconfigured,
)


class ProviderAvailability:
    def __init__(self, *, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock
        self._process: dict[str, ProviderStatus] = {}  # MISCONFIGURED — до перезапуска

    def status(self, provider: str) -> ProviderStatus | None:
        """Действующее состояние, мешающее вызову; None — провайдер готов (или срок состояния истёк)."""
        current = self._process.get(provider)
        if current is not None:
            return current
        with self._uow() as uow:
            stored = uow.provider_states.get(provider)
        if stored is None or stored.state is ProviderState.READY:
            return None
        if stored.valid_until is not None and stored.valid_until <= self._clock.now():
            return None
        return stored

    def record_success(self, provider: str, *, latency_s: float, degraded_after_s: float) -> ProviderStatus:
        self._process.pop(provider, None)
        if latency_s > degraded_after_s:
            status = self._status(provider, ProviderState.DEGRADED, f"ответ за {latency_s:.0f} с", DEGRADED_S)
        else:
            status = self._status(provider, ProviderState.READY, "успешный ответ", None)
            with self._uow() as uow:
                stored = uow.provider_states.get(provider)
            if stored is None or stored.state is ProviderState.READY:
                return status  # состояние не изменилось: лишней записи на каждый ответ нет
        self._store(status)
        return status

    def record_failure(self, provider: str, error: ModelError) -> ProviderStatus | None:
        """Состояние после ошибки провайдера; None — ошибка не говорит о провайдере (повтор не поможет)."""
        retry = error.details.get("retry_after_s")
        after = retry if isinstance(retry, int) and retry > 0 else None
        match error:
            case ModelAuthRequired():
                status = self._status(provider, ProviderState.AUTH_REQUIRED, error.category, AUTH_REQUIRED_S)
            case ModelRateLimited():
                status = self._status(
                    provider, ProviderState.RATE_LIMITED, error.category, after or RATE_LIMITED_S
                )
            case ModelLimitExceeded():
                status = self._status(
                    provider, ProviderState.LIMIT_EXCEEDED, error.category, after or LIMIT_EXCEEDED_S
                )
            case ModelMisconfigured():
                status = self._status(provider, ProviderState.MISCONFIGURED, error.category, None)
                self._process[provider] = status
                return status
            case ModelUnavailable() | ModelTimeout():
                status = self._status(
                    provider, ProviderState.UNAVAILABLE, error.category, after or UNAVAILABLE_S
                )
            case _:
                return None
        self._store(status)
        return status

    def _status(
        self, provider: str, state: ProviderState, reason: str, seconds: int | None
    ) -> ProviderStatus:
        now = self._clock.now()
        until = now + timedelta(seconds=seconds) if seconds is not None else None
        return ProviderStatus(
            provider=provider, state=state, reason=reason, valid_until=until, updated_at=now
        )

    def _store(self, status: ProviderStatus) -> None:
        with self._uow() as uow:
            uow.provider_states.put(status)
            uow.commit()
