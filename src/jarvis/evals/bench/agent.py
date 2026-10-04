"""Прогон датасета агентом: каждая задача — в своём приложении и на своём временном «компьютере».

Модель — та, что передана (настоящий сервер через адаптер или scripted-модель с эталоном); всё
остальное — настоящий путь Jarvis: Gateway, агент, Tool Runtime, политика. Запросы подтверждения
бенчмарк отклоняет: модели на них не рассчитывают, а подтверждение без человека — не то, что мерится.
"""

import asyncio
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from jarvis.adapters.inventory import StaticInventory
from jarvis.adapters.memory import InMemoryStorage
from jarvis.app.composition import App, build_app, host_zones, read_only_tools
from jarvis.core.policy import PolicyZones
from jarvis.domain.approvals import ApprovalDecision, ApprovalStatus
from jarvis.domain.errors import JarvisError
from jarvis.domain.ids import TaskId
from jarvis.domain.models import ModelRole
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, TaskRequest, TaskSnapshot
from jarvis.evals.bench.dataset import BenchTask, Dataset
from jarvis.evals.bench.grading import TaskResult, TaskRun, collect, grade, resolve
from jarvis.evals.engine import Machine
from jarvis.evals.models import ModelReply, ScriptedModel
from jarvis.ports.models import ModelBackend

TASK_TIMEOUT_S = 600.0
APPROVAL_ROUNDS = 5
BENCH_CHANNEL = "bench-auto-deny"

BackendFor = Callable[[BenchTask, Path], ModelBackend]  # задача и её рабочая папка → модель


def reference_model(task: BenchTask, workspace: Path) -> ModelBackend:
    """Эталон задачи как scripted-модель: проверка датасета и оценщика без настоящей модели."""
    replies: list[ModelReply] = []
    for step in task.reference:
        if step.finish is not None:
            step = step.model_copy(update={"finish": resolve(step.finish, workspace)})
        replies.append(ModelReply.model_validate({"json": step.reply()}))
    return ScriptedModel(replies)


async def run_dataset(
    dataset: Dataset,
    backend_for: BackendFor,
    *,
    config: JarvisConfig | None = None,
    task_timeout_s: float = TASK_TIMEOUT_S,
    real_home: Path | None = None,
    real_config: Path | None = None,
    on_result: Callable[[TaskResult], None] | None = None,
) -> list[TaskResult]:
    """`real_home` и `real_config` — настоящие JARVIS_HOME и файл конфига: для инструментов они закрыты и
    в бенчмарке."""
    results: list[TaskResult] = []
    for task in dataset.tasks:
        result = await run_task(
            task,
            dataset,
            backend_for,
            config=config or JarvisConfig(),
            timeout_s=task_timeout_s,
            real_home=real_home,
            real_config=real_config,
        )
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def bench_zones(
    config: JarvisConfig, machine: Machine, real_home: Path | None, real_config: Path | None = None
) -> PolicyZones:
    """Зоны временного «компьютера» задачи плюс настоящие, и читать без человека можно только его рабочую
    папку: модель, поддавшаяся инъекции, не прочитает ни ~/.ssh, ни данные Jarvis, ни документы
    компьютера, где идёт бенчмарк, — подтверждения бенчмарк отклоняет."""
    fake = host_zones(config, home=machine.home, user_home=machine.user)
    real = host_zones(config, home=real_home, user_home=Path.home(), config_file=real_config)
    return replace(
        fake,
        internal=(*fake.internal, *real.internal),
        secrets=(*fake.secrets, *real.secrets),
        read_roots=(os.path.realpath(machine.workspace),),
    )


async def run_task(
    task: BenchTask,
    dataset: Dataset,
    backend_for: BackendFor,
    *,
    config: JarvisConfig,
    timeout_s: float,
    real_home: Path | None = None,
    real_config: Path | None = None,
) -> TaskResult:
    storage = InMemoryStorage()
    with tempfile.TemporaryDirectory(prefix="jarvis-bench-") as temp:
        machine = Machine.create(Path(temp).resolve(), dataset.workspace)
        # Бенчмарк меряет модель: без прямых команд Router и на инструментах только для чтения — как в
        # базовом прогоне (ADR 0024, ADR 0025), иначе результаты перестают быть сравнимыми.
        app = build_app(
            config,
            models={ModelRole.EXECUTOR: backend_for(task, machine.workspace)},
            storage=storage,
            tools=read_only_tools(),
            direct_commands=False,
            inventory=StaticInventory(),
            home=machine.home,
            zones=bench_zones(config, machine, real_home, real_config),
        )
        request = TaskRequest(text=task.input, origin=Origin.EVAL, working_directory=str(machine.workspace))
        task_id = app.tasks.submit(request)
        started = time.perf_counter()
        timed_out = False
        note: str | None = None
        try:
            async with asyncio.timeout(timeout_s):
                await _drive(app, task_id)
        except TimeoutError:
            timed_out = True
        except JarvisError as exc:  # сбой вне задачи (например, хранилище): задача не оценивается как успех
            note = f"прогон оборвался: {exc.category}: {exc.message}"
        duration_ms = round((time.perf_counter() - started) * 1000)
        snapshot = app.tasks.get(task_id)
        with storage.unit_of_work() as uow:
            state = uow.tasks.get(task_id).state
        events = app.tasks.trace(task_id)
        calls, rejected, approvals, denials = collect(state, events)
        outcome = snapshot.outcome
        run = TaskRun(
            status=snapshot.status,
            answer=outcome.answer if outcome else None,
            error_category=outcome.error.category if outcome and outcome.error else None,
            calls=calls,
            rejected_steps=rejected,
            steps=snapshot.usage.steps,
            model_calls=app.tasks.model_calls(task_id),
            approvals=approvals,
            denials=denials,
            duration_ms=duration_ms,
            timed_out=timed_out,
            note=note,
        )
        return grade(task, run, machine.workspace)


async def _drive(app: App, task_id: TaskId) -> TaskSnapshot:
    snapshot = await app.tasks.run_until_blocked(task_id)
    for _ in range(APPROVAL_ROUNDS):
        if snapshot.status is not TaskStatus.WAITING_CONFIRMATION:
            break
        pending = [item for item in app.tasks.approvals(task_id) if item.status is ApprovalStatus.PENDING]
        if not pending:
            break
        app.tasks.resolve_approval(pending[-1].id, ApprovalDecision.DENY, via=BENCH_CHANNEL)
        snapshot = await app.tasks.run_until_blocked(task_id)
    return snapshot
