"""Модель команды (формат — docs/CONTRACTS.md §2) и запись индекса: источник, пак, обработчик
динамической команды."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.services import ExecResult

CommandSource = Literal["builtin", "pack", "user", "dynamic"]

# при равной похожести побеждает команда человека, затем паки, динамические и встроенные
PRIORITY: dict[str, int] = {"user": 4, "pack": 3, "dynamic": 2, "builtin": 1}

CATEGORIES: dict[str, str] = {
    "user": "Мои команды",
    "volume": "Громкость",
    "media": "Медиа",
    "windows": "Окна",
    "apps": "Программы",
    "sites": "Сайты",
    "search": "Поиск",
    "keyboard": "Клавиатура",
    "mouse": "Мышь",
    "browser": "Браузер",
    "system": "Система",
    "folders": "Папки",
    "info": "Информация",
    "jarvis": "Джарвис",
    "timers": "Таймеры",
    "packs": "Паки команд",
    "plugins": "Плагины",
    "desks": "Столы",
    "smarthome": "Умный дом",
    "scenarios": "Сценарии",
}


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Action(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    value: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> str:
        return _to_str(v).strip()

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, v: Any) -> str:
        return _to_str(v)


class Command(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    category: str = "user"
    phrases: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    confirm: bool = False
    chain: bool = True
    icon: str = ""
    enabled: bool = True
    say: str = ""


class CommandIn(BaseModel):
    """Тело POST/PUT /api/commands: своя команда (id выдаёт ядро)."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    category: str | None = None
    phrases: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    confirm: bool = False
    chain: bool = True
    icon: str = ""
    enabled: bool = True
    say: str = ""


Handler = Callable[[dict[str, Any]], Awaitable[ExecResult]]


@dataclass
class Entry:
    """Команда в индексе: откуда она и (для динамических) чем выполняется."""

    command: Command
    source: CommandSource
    pack: str | None = None
    handler: Handler | None = None
    exclude: tuple[str, ...] = ()  # начала слов, при которых команда не подходит («завтр» для погоды)
    no_question: bool = False  # {text} с вопроса («что у меня», «во сколько») — не команда, а вопрос ИИ

    @property
    def id(self) -> str:
        return self.command.id

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.source, 0)

    def info(self) -> dict[str, Any]:
        """CommandInfo для API: команда + {source, pack?, editable}."""
        data = self.command.model_dump(mode="json")
        data["source"] = self.source
        data["editable"] = self.source == "user"
        if self.pack:
            data["pack"] = self.pack
        return data
