"""system.cwd — рабочая папка задачи. Ничего не читает с диска, кроме разрешения пути: эффектов нет."""

import os
import threading
from pathlib import Path

from pydantic import BaseModel

from jarvis.adapters.tools._host import in_thread
from jarvis.domain.errors import ToolExecutionFailed
from jarvis.domain.tools import TargetKind, ToolDefinition, ToolId, ToolPreview, ToolVerification
from jarvis.ports.tools import ToolContext


class CwdArgs(BaseModel, frozen=True, extra="forbid"):
    pass


class CwdOutput(BaseModel, frozen=True, extra="forbid"):
    path: str  # канонический путь


class CwdTool:
    definition = ToolDefinition(
        id=ToolId("system.cwd"),
        description=(
            "Текущая рабочая папка задачи (канонический путь).\n"
            "От неё считаются относительные пути в аргументах других инструментов."
        ),
        input_model=CwdArgs,
        output_model=CwdOutput,
        effects=frozenset(),
        targets=frozenset({TargetKind.HOST}),
        timeout_s=5.0,
    )

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        return ToolPreview(
            summary="Узнать рабочую папку задачи", normalized_arguments={}, effects=[], target=context.target
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        def work(stop: threading.Event) -> CwdOutput:
            raw = context.working_directory or os.getcwd()
            try:
                return CwdOutput(path=str(Path(raw).resolve(strict=True)))
            except (OSError, RuntimeError) as exc:
                raise ToolExecutionFailed(f"рабочая папка недоступна: {raw}: {exc}") from None

        return await in_thread(work)

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        assert isinstance(output, CwdOutput)
        path = Path(output.path)
        passed = path.is_absolute() and await in_thread(lambda stop: path.is_dir())
        return ToolVerification(passed=passed, checks=["путь абсолютный и указывает на папку"])
