"""Формат сценария eval для механики ядра (M1).

Поля для рабочих папок, реестра, моделей и подтверждений появятся вместе с milestone, которые их
используют; неизвестное поле — ошибка, а не тихо проигнорированная настройка.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, NonNegativeFloat, NonNegativeInt, PositiveInt, model_validator

from jarvis.domain.budget import BudgetLimit, BudgetUsage
from jarvis.domain.errors import JarvisError
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Route

ChargeKind = Literal["steps", "tool_calls", "replans", "model_calls", "model_tokens"]


class ScenarioError(JarvisError):
    category = "scenario"


class ScriptStep(BaseModel, frozen=True, extra="forbid"):
    """Один такт scripted-стадии: что списать из бюджета, что сделать и куда перейти."""

    status: TaskStatus  # состояние, в котором проигрывается шаг
    next: TaskStatus
    reason: str = "scripted"
    route: Route | None = None
    answer: str | None = None
    charge: dict[ChargeKind, PositiveInt] = {}
    failures: NonNegativeInt = 0
    sleep_s: NonNegativeFloat = 0.0
    hang: bool = False  # такт не завершается сам: его прерывает отмена или лимит времени
    error: Literal["fatal", "internal"] | None = None


class ClientRules(BaseModel, frozen=True, extra="forbid"):
    """Что делает авто-клиент eval, пока задача выполняется."""

    cancel_on_hang: bool = False


class Expectation(BaseModel, frozen=True, extra="forbid"):
    status: TaskStatus
    transitions: list[TaskStatus] | None = None  # последовательность статусов «куда» из task.transition
    usage: dict[str, float] | None = None  # подмножество полей BudgetUsage
    error_category: str | None = None
    budget_limit: BudgetLimit | None = None

    @model_validator(mode="after")
    def _known_usage(self) -> Self:
        unknown = set(self.usage or {}) - set(BudgetUsage.model_fields)
        if unknown:
            raise ValueError(f"неизвестные поля usage: {sorted(unknown)}")
        return self


class Scenario(BaseModel, frozen=True, extra="forbid"):
    id: str = Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
    category: str
    input: str = Field(min_length=1)
    budget: dict[str, float] | None = None  # переопределение бюджета маршрутов direct, chat, agent
    client: ClientRules = ClientRules()
    script: list[ScriptStep]
    expect: Expectation


def load_scenarios(paths: Sequence[Path]) -> list[Scenario]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.yaml")))
        elif path.is_file():
            files.append(path)
        else:
            raise ScenarioError(f"нет такого файла или папки: {path}")
    scenarios: list[Scenario] = []
    seen: dict[str, Path] = {}
    for file in files:
        try:
            scenario = Scenario.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")))
        except (yaml.YAMLError, ValueError) as exc:
            raise ScenarioError(f"{file}: {exc}") from None
        if scenario.id in seen:
            raise ScenarioError(f"{file}: сценарий {scenario.id} уже определён в {seen[scenario.id]}")
        seen[scenario.id] = file
        scenarios.append(scenario)
    return scenarios
