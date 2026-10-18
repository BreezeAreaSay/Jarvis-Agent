"""Сервис команд (заглушка — полноценная версия: jarvis.commands, см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService
from jarvis.services import CommandMatch, DynamicProvider, ExecResult, Source, UserFacingError


class Commands(BaseService):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._providers: dict[str, DynamicProvider] = {}

    def match(self, text: str) -> CommandMatch | None:
        return None

    async def execute(self, match: CommandMatch, source: Source) -> ExecResult:
        return ExecResult(False, match.text, "Команды ещё не подключены")

    async def run_text(self, text: str, source: Source) -> ExecResult | None:
        found = self.match(text)
        return None if found is None else await self.execute(found, source)

    async def run_command(
        self, command_id: str, slots: dict[str, Any] | None = None, source: Source = "ui"
    ) -> ExecResult:
        raise UserFacingError("Команды ещё не подключены")

    def register_provider(self, name: str, provider: DynamicProvider) -> None:
        self._providers[name] = provider

    def refresh_dynamic(self) -> None:
        return None

    def describe(self, limit: int = 300) -> list[dict[str, Any]]:
        return []
