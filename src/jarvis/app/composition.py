"""Сборка приложения (01-structure.md §6).

В M1 стадии задачи передаёт вызывающий (eval и тесты — scripted-стадии); настоящие стадии появятся
со своими milestone. Хранилище — в памяти до появления SQLite (M2).
"""

from collections.abc import Mapping
from dataclasses import dataclass

from jarvis.adapters.clock import SystemClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.core.runner import StageHandler, TaskRunner
from jarvis.core.service import TaskService
from jarvis.core.trace import Tracer
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.ports.clock import Clock


@dataclass(frozen=True)
class App:
    config: JarvisConfig
    tasks: TaskService


def build_app(
    config: JarvisConfig,
    *,
    stages: Mapping[TaskStatus, StageHandler],
    storage: InMemoryStorage | None = None,
    clock: Clock | None = None,
) -> App:
    storage = storage if storage is not None else InMemoryStorage()
    clock = clock if clock is not None else SystemClock()
    tracer = Tracer(storage.unit_of_work, storage.ids, clock)
    runner = TaskRunner(
        stages=stages,
        uow=storage.unit_of_work,
        tracer=tracer,
        budgets=config.budgets,
        clock=clock,
    )
    tasks = TaskService(
        runner=runner,
        uow=storage.unit_of_work,
        ids=storage.ids,
        tracer=tracer,
        budgets=config.budgets,
        clock=clock,
    )
    return App(config=config, tasks=tasks)
