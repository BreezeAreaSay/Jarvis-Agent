"""Агент «работает сам» (заглушка — полноценная версия: jarvis.ai, см. docs/CONTRACTS.md)."""

from jarvis.service_base import BaseService
from jarvis.services import Source, UserFacingError


class Agent(BaseService):
    async def run(self, task: str, source: Source = "text") -> str:
        raise UserFacingError("Агент ещё не подключён")

    async def stop_run(self) -> None:
        return None

    def resolve_approval(self, approval_id: str, allow: bool) -> bool:
        return False
