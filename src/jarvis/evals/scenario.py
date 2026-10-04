"""Формат сценария eval: механика ядра (M1), инструменты (Session 3) и агент на модели (Session 4).

Сценарий ведут либо scripted-стадии (`script`), либо настоящие стадии агента со scripted-моделью
(`model`: реплики модели по порядку; пустой список — модель вызываться не должна, как у прямых команд).
Сценарий с инструментами получает свою временную рабочую папку (`files`) и временные данные Jarvis; в
аргументах вызовов и репликах модели `{workspace}` и `{jarvis_home}` заменяются их путями. Инвентарь
(`inventory`) — приложения и известные папки для прямых команд; запуск в eval только записывается.
Неизвестное поле — ошибка.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, JsonValue, NonNegativeInt, PositiveInt, model_validator

from jarvis.domain.approvals import ApprovalDecision
from jarvis.domain.budget import BudgetLimit, BudgetUsage
from jarvis.domain.errors import JarvisError
from jarvis.domain.intents import IntentId
from jarvis.domain.inventory import AppEntry, KnownFolder
from jarvis.domain.routing import Route
from jarvis.domain.states import TaskStatus
from jarvis.domain.tools import ToolOutcomeKind
from jarvis.domain.trace import EventKind
from jarvis.evals.models import ModelReply

ChargeKind = Literal["steps", "tool_calls", "replans", "model_calls", "model_tokens"]


class ScenarioError(JarvisError):
    category = "scenario"


FinalOutcome = Literal[ToolOutcomeKind.EXECUTED, ToolOutcomeKind.DRY_RUN, ToolOutcomeKind.DENIED]


class ToolStep(BaseModel, frozen=True, extra="forbid"):
    """Вызов инструмента через Tool Runtime. Если нужен человек, задача уходит в WAITING_CONFIRMATION,
    а после решения этот же шаг доводит вызов."""

    id: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = {}
    expect: FinalOutcome | None = None  # итог вызова; другой итог — расхождение со сценарием


class ScriptStep(BaseModel, frozen=True, extra="forbid"):
    """Один такт scripted-стадии: что списать из бюджета, что сделать и куда перейти."""

    status: TaskStatus  # состояние, в котором проигрывается шаг
    next: TaskStatus
    reason: str = "scripted"
    route: Route | None = None
    answer: str | None = None
    charge: dict[ChargeKind, PositiveInt] = {}
    failures: NonNegativeInt = 0
    hang: bool = False  # такт не завершается сам: его прерывает отмена или лимит времени
    fail: bool = False  # стадия падает с фатальной ошибкой (ScriptedFailure)
    tool: ToolStep | None = None

    @model_validator(mode="after")
    def _tool_in_executing(self) -> Self:
        if self.tool is not None and self.status is not TaskStatus.EXECUTING:
            raise ValueError("инструменты вызываются только в EXECUTING")
        return self


class ModelScript(BaseModel, frozen=True, extra="forbid"):
    """Scripted-модель роли executor: реплики по порядку и объявленные возможности. Без реплик любой
    вызов модели — ошибка сценария (прямая команда исполняется без модели)."""

    replies: list[ModelReply] = []
    structured_output: bool = True  # сервер применяет схему; False — схема только в промпте
    context_window: PositiveInt = 16384


class ScenarioInventory(BaseModel, frozen=True, extra="forbid"):
    """Инвентарь «компьютера» сценария. Папки — пути от домашней папки пользователя сценария, они
    создаются; цели приложений никогда не запускаются (запуск в eval записывается)."""

    apps: list[AppEntry] = []
    default_browser: str | None = None
    folders: dict[KnownFolder, str] = {}

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.default_browser is not None and all(app.id != self.default_browser for app in self.apps):
            raise ValueError(f"браузер по умолчанию {self.default_browser} не найден среди apps")
        for path in self.folders.values():
            if _outside(path):
                raise ValueError(f"папка инвентаря должна быть внутри домашней папки сценария: {path}")
        return self


class ClientRules(BaseModel, frozen=True, extra="forbid"):
    """Что делает авто-клиент eval, пока задача выполняется."""

    cancel_on_hang: bool = False  # отменить задачу, когда шаг завис или начал ждать eval.sleep
    approval: ApprovalDecision | None = None  # как отвечать на запросы подтверждения; None — не отвечать


# События вызова инструмента — для проверки порядка конвейера.
TOOL_EVENTS = frozenset(
    {
        EventKind.TOOL_PREVIEWED,
        EventKind.POLICY_DECIDED,
        EventKind.APPROVAL_REQUESTED,
        EventKind.APPROVAL_RESOLVED,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FINISHED,
        EventKind.TOOL_VERIFIED,
    }
)


class Expectation(BaseModel, frozen=True, extra="forbid"):
    status: TaskStatus
    transitions: list[TaskStatus] | None = None  # последовательность статусов «куда» из task.transition
    usage: dict[str, float] | None = None  # подмножество полей BudgetUsage
    error_category: str | None = None
    budget_limit: BudgetLimit | None = None
    tool_events: list[EventKind] | None = None  # события вызовов инструментов по порядку
    tool_outcomes: list[ToolOutcomeKind] | None = None  # итоги вызовов, полученные стадией
    rules: list[str] | None = None  # правила последнего решения политики
    found: list[str] | None = None  # пути из результата последнего исполненного вызова (от рабочей папки)
    observations: list[str] | None = None  # итоги вызовов агента по порядку: executed, denied, …
    answer_contains: list[str] | None = None  # подстроки ответа задачи
    # Строки, которые модель видела только внутри блоков DATA (недоверенные данные — не инструкции).
    data_only: list[str] | None = None
    strategy: Route | None = None  # решение Router
    intent: IntentId | None = None
    route_rules: list[str] | None = None  # правила решения Router (подмножество, по порядку не важно)
    # Что передано системе на запуск: «app:<цель>», «url:<адрес>», «folder:<путь>»; пути внутри
    # «компьютера» сценария — от его корня (user/Downloads, workspace/docs).
    launched: list[str] | None = None

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
    files: dict[str, str] = {}  # рабочая папка: путь → содержимое; путь с «/» на конце — папка
    dry_run: bool = False
    script: list[ScriptStep] | None = None
    model: ModelScript | None = None
    inventory: ScenarioInventory = ScenarioInventory()
    expect: Expectation

    @model_validator(mode="after")
    def _one_driver(self) -> Self:
        if (self.script is None) == (self.model is None):
            raise ValueError("сценарий ведёт ровно одно: script (scripted-стадии) или model (агент)")
        return self

    @model_validator(mode="after")
    def _relative_files(self) -> Self:
        for name in self.files:
            if _outside(name):
                raise ValueError(f"файл фикстуры должен быть внутри рабочей папки: {name}")
        return self


def _outside(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return name.startswith(("/", "\\")) or ":" in name or ".." in parts


ROUTING_KIND = "routing_dataset"  # набор фраз Router (evals/routing.py), а не сценарий


def is_routing_dataset(data: object) -> bool:
    return isinstance(data, dict) and data.get("kind") == ROUTING_KIND  # pyright: ignore[reportUnknownMemberType]


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
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
            if is_routing_dataset(data):
                continue
            scenario = Scenario.model_validate(data)
        except (yaml.YAMLError, ValueError) as exc:
            raise ScenarioError(f"{file}: {exc}") from None
        if scenario.id in seen:
            raise ScenarioError(f"{file}: сценарий {scenario.id} уже определён в {seen[scenario.id]}")
        seen[scenario.id] = file
        scenarios.append(scenario)
    return scenarios
