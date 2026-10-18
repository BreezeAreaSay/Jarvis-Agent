"""Голосовой сервис (заглушка — полноценная версия: jarvis.voice, см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService
from jarvis.services import VoiceState


class Voice(BaseService):
    state: VoiceState = "off"

    async def set_enabled(self, enabled: bool) -> None:
        return None

    async def listen_now(self) -> None:
        return None

    async def stop_listening(self) -> None:
        return None

    def follow_up(self, seconds: float | None = None) -> None:
        return None

    async def ask_yes_no(self, timeout: float = 8.0) -> bool | None:
        return None

    async def capture_text(self, timeout: float = 15.0, *, punct: bool = True) -> str:
        return ""

    async def transcribe(self, samples: Any, rate: int, *, language: str = "ru") -> str:
        return ""
