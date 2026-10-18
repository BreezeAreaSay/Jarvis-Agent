"""ИИ-сервис (заглушка — полноценная версия: jarvis.ai, см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService
from jarvis.services import AskResult, Source, UserFacingError


class Ai(BaseService):
    @property
    def configured(self) -> bool:
        return False

    async def ask(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        source: Source = "text",
        mode: str | None = None,
        images: list[str] | None = None,
    ) -> AskResult:
        return AskResult(chat_id or "", "", error="ИИ не настроен")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        raise UserFacingError("ИИ не настроен")
