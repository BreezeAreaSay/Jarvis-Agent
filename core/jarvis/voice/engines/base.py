"""Общее для движков синтеза речи: параметры фразы, ошибка для человека, протокол движка."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

if TYPE_CHECKING:
    from jarvis.settings import Settings


class TtsEngineError(RuntimeError):
    """Движок не смог озвучить (нет ключа, сети, голоса); текст — для человека."""


@dataclass
class SynthOptions:
    voice: str  # голос движка ("" — из настроек)
    speed: float = 1.0
    pitch: float = 1.0
    settings: "Settings | None" = None


class Engine(Protocol):
    name: str
    title: str

    def available(self) -> str:
        """Пустая строка — можно звать; иначе причина, почему нет («нет ключа», «только Windows»)."""
        ...

    def voice(self, settings: "Settings") -> str:
        """Голос из настроек для этого движка."""
        ...

    def russian(self, voice: str) -> bool:
        """Голос русский: латиницу — русскими буквами, «%» — словом."""
        ...

    async def synthesize(self, text: str, opts: SynthOptions) -> tuple[np.ndarray, int]:
        """Текст → (float32 моно, частота воспроизведения; тон уже учтён)."""
        ...

    async def voices(self) -> list[dict[str, Any]]:
        """[{id, name, lang, gender}] для выбора в настройках."""
        ...

    def close(self) -> None: ...


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def gender_name(value: Any) -> str:
    text = str(value or "").casefold()
    if text.startswith(("f", "ж", "1")) or "female" in text:
        return "female"
    if text.startswith(("m", "м", "0")) or "male" in text:
        return "male"
    return ""
