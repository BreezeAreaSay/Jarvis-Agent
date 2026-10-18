"""Базовый класс сервиса ядра."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.context import AppContext


class BaseService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None
