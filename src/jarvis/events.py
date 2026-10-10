"""События одного запроса: их отдают core.handle и Brain.ask, показывают окно и CLI.

Порядок: Level → (Status | TextChunk | Items)* → ровно один Done последним.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

LevelName = Literal["grammar", "hands", "brain", "local"]
StatusKind = Literal["info", "tool", "warn"]


@dataclass(frozen=True)
class Level:
    """Какой уровень отвечает: grammar | hands | brain | local (без облака)."""

    level: LevelName
    reason: str
    model: str = ""


@dataclass(frozen=True)
class Status:
    """Строка состояния «⚙ …». key связывает начало и конец одного шага (спиннер → ✓/✗)."""

    text: str
    kind: StatusKind = "info"
    done: bool = False
    ok: bool = True
    key: str = ""


@dataclass(frozen=True)
class TextChunk:
    """Кусок текста ответа (поток мозга или ответ целиком)."""

    text: str


@dataclass(frozen=True)
class Items:
    """Результаты find: пути по порядку, номер = индекс + 1."""

    items: list[str]


@dataclass(frozen=True)
class Done:
    """Последнее событие запроса."""

    ok: bool
    text: str = ""
    level: LevelName = "grammar"
    reason: str = ""
    autohide: bool = False
    cancelled: bool = False
    timings: dict[str, Any] = field(default_factory=dict)


Event = Level | Status | TextChunk | Items | Done
