"""FakeTool — инструмент для unit-тестов Tool Runtime: без файловой системы, с управляемым поведением.

Эффекты вызова задаются шаблонами ресурса ("{path}" подставляется из аргументов), поведение preview,
execute и verify — полями. Счётчики показывают, что runtime на самом деле вызвал.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, JsonValue

from jarvis.domain.tools import (
    EffectKind,
    TargetKind,
    ToolDefinition,
    ToolEffect,
    ToolId,
    ToolPreview,
    ToolVerification,
)
from jarvis.ports.tools import ToolContext


class FakeArgs(BaseModel, frozen=True, extra="forbid"):
    path: str = "/data/a.txt"
    note: str = ""


class FakeOutput(BaseModel, frozen=True, extra="forbid"):
    value: str


@dataclass
class FakeTool:
    tool_id: str
    effects: Sequence[tuple[EffectKind, str]] = ((EffectKind.READ, "{path}"),)
    declared: frozenset[EffectKind] | None = None  # по умолчанию — виды из `effects`
    targets: frozenset[TargetKind] = frozenset({TargetKind.HOST})
    timeout_s: float = 5.0
    output: dict[str, JsonValue] = field(default_factory=lambda: {"value": "ok"})
    preview_error: BaseException | None = None
    execute_error: BaseException | None = None
    verify_error: BaseException | None = None
    verified: bool = True
    hang: bool = False  # execute не завершается сам
    drift_after: int | None = None  # preview после N-го видит другой ресурс (файл подменили)
    previews: int = 0
    executions: list[FakeArgs] = field(default_factory=list[FakeArgs])
    started: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def definition(self) -> ToolDefinition:
        declared = self.declared if self.declared is not None else frozenset(kind for kind, _ in self.effects)
        return ToolDefinition(
            id=ToolId(self.tool_id),
            description=f"{self.tool_id}: тестовый инструмент\nПодробности.",
            input_model=FakeArgs,
            output_model=FakeOutput,
            effects=declared,
            targets=self.targets,
            timeout_s=self.timeout_s,
        )

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        assert isinstance(arguments, FakeArgs)
        self.previews += 1
        if self.preview_error is not None:
            raise self.preview_error
        drifted = self.drift_after is not None and self.previews > self.drift_after
        suffix = f"#{self.previews}" if drifted else ""
        normalized = arguments.model_copy(update={"path": arguments.path.rstrip("/") or "/"})
        return ToolPreview(
            summary=f"{self.tool_id} {normalized.path}",
            normalized_arguments=normalized.model_dump(mode="json"),
            effects=[
                ToolEffect(kind=kind, resource=template.format(path=normalized.path + suffix))
                for kind, template in self.effects
            ],
            target=context.target,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        assert isinstance(arguments, FakeArgs)
        self.executions.append(arguments)
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.execute_error is not None:
            raise self.execute_error
        return _Raw.model_validate(self.output)

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        if self.verify_error is not None:
            raise self.verify_error
        return ToolVerification(passed=self.verified, checks=["значение получено"])


class _Raw(BaseModel, extra="allow"):
    """Результат как есть: проверять его по схеме — дело runtime, а не инструмента."""
