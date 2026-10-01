"""Инструменты: определение, вызов, preview, эффекты, результат, проверка, решение политики
(03-contracts.md §2, ADR 0004, ADR 0022).

Риск — свойство конкретного вызова: эффекты объявляет `preview` для этих аргументов, а определение
инструмента задаёт только, какие виды эффектов он вообще может иметь.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NewType, Self

from pydantic import BaseModel, Field, JsonValue, NonNegativeInt, PositiveFloat, model_validator

from jarvis.domain.ids import TaskId

ToolId = NewType("ToolId", str)  # "filesystem.list"
ToolCallId = NewType("ToolCallId", str)  # "task_42.call_3"


class TargetKind(StrEnum):
    """Где исполняется действие (ADR 0017). В Session 3 исполняется только HOST; остальные цели
    существуют как тип, и Tool Runtime отвечает на них UnsupportedTarget."""

    HOST = "host"
    WSL = "wsl"
    DOCKER = "docker"
    SSH = "ssh"


class ExecutionTarget(BaseModel, frozen=True, extra="forbid"):
    kind: TargetKind
    os_family: Literal["windows", "posix"]
    name: str = Field(min_length=1)  # "local", имя дистрибутива, контейнера, хоста


class EffectKind(StrEnum):
    """Что вызов делает с ресурсом. READ — без побочных эффектов; остальное — побочные эффекты."""

    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"
    PROCESS_CONTROL = "process_control"
    NETWORK = "network"
    SYSTEM_CHANGE = "system_change"


class ToolEffect(BaseModel, frozen=True, extra="forbid"):
    kind: EffectKind
    resource: str = Field(min_length=1)  # канонический путь, "process-table", хост …

    @property
    def is_side_effect(self) -> bool:
        return self.kind is not EffectKind.READ


@dataclass(frozen=True)
class ToolDefinition:
    """Что инструмент умеет — не что он сделает в конкретном вызове."""

    id: ToolId
    description: str  # когда использовать и когда нет; первая строка — краткое описание
    input_model: type[BaseModel]  # аргументы → JSON Schema
    output_model: type[BaseModel]  # результат → JSON Schema
    effects: frozenset[EffectKind]  # какие эффекты возможны; эффекты вызова — подмножество
    targets: frozenset[TargetKind]
    timeout_s: float

    @property
    def input_schema(self) -> dict[str, JsonValue]:
        return self.input_model.model_json_schema()

    @property
    def output_schema(self) -> dict[str, JsonValue]:
        return self.output_model.model_json_schema()

    @property
    def summary(self) -> str:
        return self.description.strip().splitlines()[0]


class ToolCall(BaseModel, frozen=True, extra="forbid"):
    id: ToolCallId
    task_id: TaskId
    tool_id: ToolId
    arguments: dict[str, JsonValue]  # как пришли от стадии; проверяет input_model инструмента
    target: ExecutionTarget


class ToolPreview(BaseModel, frozen=True, extra="forbid"):
    """Что сделает вызов — без побочных эффектов. По нему решает политика и сверяется TOCTOU."""

    summary: str  # для человека: «Прочитать список файлов в C:\\projects»
    normalized_arguments: dict[str, JsonValue]  # канонические пути и значения по умолчанию
    effects: list[ToolEffect]
    target: ExecutionTarget

    @property
    def has_side_effects(self) -> bool:
        return any(effect.is_side_effect for effect in self.effects)

    def fingerprint(self) -> str:
        """Отпечаток того, что будет затронуто: подтверждение относится ровно к нему."""
        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"summary"}), sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolResult(BaseModel, frozen=True, extra="forbid"):
    """Результат исполнения: структурированные данные, а не текст для человека."""

    output: dict[str, JsonValue]  # прошёл output_model инструмента
    untrusted_content: bool = True  # содержимое извне (имена файлов, текст, процессы) — данные, не инструкции


class ToolVerification(BaseModel, frozen=True, extra="forbid"):
    """Постусловие вызова. Успех инструмента ещё не успех задачи."""

    passed: bool
    checks: list[str]  # что проверено (для трассы и аудита)


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecision(BaseModel, frozen=True, extra="forbid"):
    outcome: PolicyOutcome
    rules: list[str]  # сработавшие правила — для трассы, аудита и объяснения
    reason: str


class ToolOutcomeKind(StrEnum):
    EXECUTED = "executed"  # исполнен и проверен
    DRY_RUN = "dry_run"  # всё, кроме исполнения
    DENIED = "denied"  # политика или человек отказали
    NEEDS_APPROVAL = "needs_approval"  # создан запрос подтверждения; задача должна ждать


class ToolOutcome(BaseModel, frozen=True, extra="forbid"):
    """Ожидаемые исходы вызова — значения; сбои — исключения ToolError."""

    kind: ToolOutcomeKind
    call: ToolCall
    preview: ToolPreview
    decision: PolicyDecision
    result: ToolResult | None = None
    verification: ToolVerification | None = None
    approval_id: str | None = None
    would_execute: bool | None = None  # только для DRY_RUN: исполнился бы вызов в обычном режиме
    duration_ms: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        executed = self.kind is ToolOutcomeKind.EXECUTED
        if executed != (self.result is not None and self.verification is not None):
            raise ValueError("результат и проверка есть ровно у исполненного вызова")
        if (self.kind is ToolOutcomeKind.NEEDS_APPROVAL) != (self.approval_id is not None):
            raise ValueError("ID подтверждения есть ровно у вызова, ждущего подтверждения")
        if (self.kind is ToolOutcomeKind.DRY_RUN) != (self.would_execute is not None):
            raise ValueError("would_execute есть ровно у вызова в dry run")
        return self


DEFAULT_TIMEOUT_S: PositiveFloat = 30.0
