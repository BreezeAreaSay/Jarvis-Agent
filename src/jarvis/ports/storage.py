"""Порты хранилища (03-contracts.md §1, ADR 0021).

Единица работы оптимистическая: чтения видят одно согласованное состояние, записи копятся и
применяются вместе на `commit()`, который заново сверяет ожидания (версию задачи, аренду) и при
расхождении поднимает `ConcurrentModification`, ничего не записав. Так контрольная точка задачи
(строка задачи, событие перехода и объясняющие его события) и проверка аренды пишутся одной
транзакцией. ID выдаёт `IdAllocator` — сразу и вне единицы работы, поэтому откат не возвращает номер.
"""

from collections.abc import Collection, Sequence
from types import TracebackType
from typing import Protocol, Self

from jarvis.domain.ids import ChildKind, TaskId
from jarvis.domain.lease import Lease
from jarvis.domain.states import TaskStatus
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

    def list(self, *, statuses: Collection[TaskStatus] | None = None, limit: int | None = None) -> list[Task]:
        """Задачи с нужными статусами, новые первыми (по номеру задачи)."""
        ...


class TraceRepository(Protocol):
    def append(self, events: Sequence[TraceEvent]) -> None: ...

    def list(self, task_id: TaskId, *, after_seq: int = 0) -> list[TraceEvent]:
        """События задачи по возрастанию `seq`."""
        ...


class LeaseRepository(Protocol):
    """Аренды задач. Запись — сравнение с ожидаемым значением: это и есть защита от гонок."""

    def get(self, task_id: TaskId) -> Lease | None: ...

    def put(self, lease: Lease, *, expected: Lease | None) -> None:
        """Записать аренду, если сейчас записана `expected` (None — аренды нет)."""
        ...

    def delete(self, task_id: TaskId, *, expected: Lease) -> None:
        """Удалить аренду, если сейчас записана `expected`."""
        ...


class UnitOfWork(Protocol):
    """Одна транзакция: всё, что записано через репозитории, применяется вместе на `commit()`.

    Чтения внутри единицы работы видят её собственные записи. Выход из `with` без `commit()`
    отбрасывает записи. Единица работы одноразовая.
    """

    @property
    def tasks(self) -> TaskRepository: ...

    @property
    def trace(self) -> TraceRepository: ...

    @property
    def leases(self) -> LeaseRepository: ...

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
