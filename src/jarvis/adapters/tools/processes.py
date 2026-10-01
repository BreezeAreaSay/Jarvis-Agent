"""process.list — снимок списка процессов: pid, имя, исполняемый файл. Только чтение."""

import threading

import psutil
from pydantic import BaseModel, Field

from jarvis.adapters.tools._host import Stopped, in_thread
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

PROCESS_TABLE = "process-table"
MAX_PROCESSES = 2000


class ProcessListArgs(BaseModel, frozen=True, extra="forbid"):
    name: str | None = Field(default=None, min_length=1, description="Подстрока имени (без учёта регистра)")
    max_results: int = Field(default=200, ge=1, le=MAX_PROCESSES)


class ProcessInfo(BaseModel, frozen=True, extra="forbid"):
    pid: int
    name: str
    executable: str | None  # None — ОС не дала (нет прав или процесс системный)


class ProcessListOutput(BaseModel, frozen=True, extra="forbid"):
    processes: list[ProcessInfo]  # по возрастанию pid
    total: int  # подходящих под фильтр
    truncated: bool


class ProcessListTool:
    definition = ToolDefinition(
        id=ToolId("process.list"),
        description=(
            "Список запущенных процессов: pid, имя, путь к исполняемому файлу.\n"
            "Только чтение: остановить или запустить процесс этим инструментом нельзя."
        ),
        input_model=ProcessListArgs,
        output_model=ProcessListOutput,
        effects=frozenset({EffectKind.READ}),
        targets=frozenset({TargetKind.HOST}),
        timeout_s=15.0,
    )

    async def preview(self, arguments: BaseModel, context: ToolContext) -> ToolPreview:
        assert isinstance(arguments, ProcessListArgs)
        which = f" с «{arguments.name}» в имени" if arguments.name else ""
        return ToolPreview(
            summary=f"Показать запущенные процессы{which}",
            normalized_arguments=arguments.model_dump(mode="json"),
            effects=[ToolEffect(kind=EffectKind.READ, resource=PROCESS_TABLE)],
            target=context.target,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> BaseModel:
        assert isinstance(arguments, ProcessListArgs)

        def work(stop: threading.Event) -> ProcessListOutput:
            needle = arguments.name.casefold() if arguments.name else None
            found: list[ProcessInfo] = []
            for process in psutil.process_iter(["pid", "name", "exe"]):
                if stop.is_set():
                    raise Stopped
                info = process.info  # недоступные поля psutil отдаёт как None
                name = info.get("name") or ""
                if needle is not None and needle not in name.casefold():
                    continue
                found.append(ProcessInfo(pid=info["pid"], name=name, executable=info.get("exe") or None))
            found.sort(key=lambda item: item.pid)
            shown = found[: arguments.max_results]
            return ProcessListOutput(processes=shown, total=len(found), truncated=len(found) > len(shown))

        return await in_thread(work)

    async def verify(self, arguments: BaseModel, output: BaseModel, context: ToolContext) -> ToolVerification:
        assert isinstance(arguments, ProcessListArgs)
        assert isinstance(output, ProcessListOutput)
        pids = [process.pid for process in output.processes]
        needle = arguments.name.casefold() if arguments.name else None
        checks = {
            "не больше max_results": len(pids) <= arguments.max_results,
            "pid уникальны и по возрастанию": pids == sorted(set(pids)),
            "имена подходят под фильтр": needle is None
            or all(needle in process.name.casefold() for process in output.processes),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            return ToolVerification(passed=False, checks=[f"не выполнено: {name}" for name in failed])
        return ToolVerification(passed=True, checks=list(checks))
