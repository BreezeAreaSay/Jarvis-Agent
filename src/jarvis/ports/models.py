"""Порт бэкенда модели (03-contracts.md §3, ADR 0008, ADR 0023).

Адаптер переводит отрисованный промпт в API конкретного рантайма и обратно: имя модели, сэмплирование,
формат structured output и особенности сервера живут в нём. Ядро обращается к бэкенду только через
`ModelGateway` (core.models.gateway).
"""

from typing import Protocol

from jarvis.domain.models import BackendRequest, BackendResponse, BackendStatus, ModelInfo


class ModelBackend(Protocol):
    @property
    def info(self) -> ModelInfo: ...

    async def complete(self, request: BackendRequest) -> BackendResponse:
        """Один ответ модели. Сбои — ModelUnavailable, ModelTimeout, ModelRequestRejected."""
        ...

    async def describe(self) -> BackendStatus:
        """Что сервер сообщает о себе: модели, окно контекста, сборка (для `jarvis model check`)."""
        ...
