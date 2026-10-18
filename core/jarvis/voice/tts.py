"""Синтез речи (заглушка — полноценная версия: jarvis.voice, см. docs/CONTRACTS.md)."""

from typing import Any

import numpy as np

from jarvis.service_base import BaseService


class Tts(BaseService):
    @property
    def speaking(self) -> bool:
        return False

    async def say(self, text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        return None

    def silence(self) -> None:
        return None

    async def synthesize(self, text: str) -> tuple[Any, int]:
        return np.zeros(0, dtype=np.float32), 22050

    async def play(self, samples: Any, rate: int, *, wait: bool = True) -> None:
        return None
