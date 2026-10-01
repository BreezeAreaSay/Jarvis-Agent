"""Инструменты только для eval: управляемое поведение там, где настоящие инструменты слишком быстры.

`eval.sleep` ждёт заданное время — на нём проверяются таймаут вызова и отмена задачи посреди вызова.
Эффектов у него нет; в обычное приложение он не попадает.
"""

import asyncio

from pydantic import BaseModel, Field

from jarvis.domain.tools import TargetKind, ToolDefinition, ToolId, ToolPreview, ToolVerification
from jarvis.ports.tools import ToolContext

SLEEP_TIMEOUT_S = 1.0


class SleepArgs(BaseModel, frozen=True, extra="forbid"):
    seconds: float = Field(gt=0, le=60)


class SleepOutput(BaseModel, frozen=True, extra="forbid"):
    slept_s: float


class SleepTool:
    definition = ToolDefinition(
        id=ToolId("eval.sleep"),
        description=f"Подождать (только eval).\nТаймаут вызова — {SLEEP_TIMEOUT_S:g} с.",
        input_model=SleepArgs,
        output_model=SleepOutput,
        effects=frozenset(),
        targets=frozenset({TargetKind.HOST}),
        timeout_s=SLEEP_TIMEOUT_S,
        untrusted_output=False,
    )

    def __init__(self, started: asyncio.Event) -> None:
        self._started = started  # общий с авто-клиентом eval: «действие идёт» — можно отменять

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        assert isinstance(arguments, SleepArgs)
        return ToolPreview(
            summary=f"Подождать {arguments.seconds:g} с",
            normalized_arguments=arguments.model_dump(mode="json"),
            effects=[],
            target=context.target,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        assert isinstance(arguments, SleepArgs)
        self._started.set()
        await asyncio.sleep(arguments.seconds)
        return SleepOutput(slept_s=arguments.seconds)

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        assert isinstance(arguments, SleepArgs)
        assert isinstance(output, SleepOutput)
        return ToolVerification(passed=output.slept_s == arguments.seconds, checks=["время ожидания то же"])
