"""Сборка приложения (01-structure.md §6): единственное место, где создаются адаптеры.

Стадии задачи передаёт вызывающий (eval и тесты — scripted-стадии); настоящие стадии появятся со
своими milestone. Хранилище по умолчанию — в памяти; CLI открывает SQLite через `open_storage`.
"""

import os
import secrets
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jarvis.adapters.clock import SystemClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.core.leases import Leases
from jarvis.core.runner import StageHandler, TaskRunner
from jarvis.core.service import TaskService
from jarvis.core.trace import Tracer
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.ports.clock import Clock

Storage = InMemoryStorage | SqliteStorage


@dataclass(frozen=True)
class App:
    config: JarvisConfig
    tasks: TaskService
    owner: str  # кем этот процесс подписывает аренды задач


def database_path(home: Path) -> Path:
    """База лежит в данных Jarvis: JARVIS_HOME/data/jarvis.db."""
    return home / "data" / "jarvis.db"


def open_storage(home: Path) -> SqliteStorage:
    return SqliteStorage(database_path(home))


def process_owner() -> str:
    """Уникальный владелец аренд: хост и pid для человека, случайный токен — против повтора pid."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


def build_app(
    config: JarvisConfig,
    *,
    stages: Mapping[TaskStatus, StageHandler],
    storage: Storage | None = None,
    clock: Clock | None = None,
    owner: str | None = None,
) -> App:
    storage = storage if storage is not None else InMemoryStorage()
    clock = clock if clock is not None else SystemClock()
    owner = owner if owner is not None else process_owner()
    tracer = Tracer(storage.ids, clock)
    leases = Leases(uow=storage.unit_of_work, clock=clock, owner=owner, ttl_s=config.runtime.lease_ttl_s)
    runner = TaskRunner(
        stages=stages,
        uow=storage.unit_of_work,
        tracer=tracer,
        budgets=config.budgets,
        clock=clock,
        leases=leases,
    )
    tasks = TaskService(
        runner=runner,
        uow=storage.unit_of_work,
        ids=storage.ids,
        tracer=tracer,
        budgets=config.budgets,
        clock=clock,
        leases=leases,
    )
    return App(config=config, tasks=tasks, owner=owner)
