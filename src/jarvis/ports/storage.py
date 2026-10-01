"""Порты хранилища (03-contracts.md §1).

Записи бывают двух видов. Журнальные (события трассы) пишутся сразу, своей короткой единицей работы.
Контрольная точка задачи (строка задачи + событие `task.transition`) пишется одной единицей работы в
конце такта. ID выдаёт `IdAllocator` — сразу и вне единицы работы, поэтому откат не возвращает номер.
"""

from collections.abc import Sequence
from types import TracebackType
from typing import Protocol, Self

from jarvis.domain.ids import ChildKind, TaskId
from jarvis.domain.task import Task
from jarvis.domain.trace import TraceEvent


class TaskRepository(Protocol):
    def add(self, task: Task) -> None:
        """Новая задача; ID уже занят — StorageError."""
        ...

    def get(self, task_id: TaskId) -> Task:
        """TaskNotFound, если задачи нет."""
        ...

    def save(self, task: Task, expected_version: int) -> None:
        """ConcurrentModification, если сохранённая версия не `expected_version`."""
        ...


class TraceRepository(Protocol):
    def append(self, events: Sequence[TraceEvent]) -> None: ...

    def list(self, task_id: TaskId, *, after_seq: int = 0) -> list[TraceEvent]:
        """События задачи по возрастанию `seq`."""
        ...


class UnitOfWork(Protocol):
    """Одна транзакция: всё, что записано через репозитории, применяется вместе на `commit()`.

    Выход из `with` без `commit()` отбрасывает записи.
    """

    @property
    def tasks(self) -> TaskRepository: ...

    @property
    def trace(self) -> TraceRepository: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class IdAllocator(Protocol):
    def next_task_id(self) -> TaskId: ...

    def next_child_id(self, task_id: TaskId, kind: ChildKind) -> str: ...
