"""Хранилище в памяти.

Задачи и события хранятся сериализованными в JSON, а не ссылками на объекты: восстановление задачи из
хранилища проходит через ту же сериализацию, что понадобится SQLite. Для тестов атомарности можно
заставить следующий `commit()` упасть.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from jarvis.domain.errors import ConcurrentModification, StorageError, TaskNotFound
from jarvis.domain.ids import ChildKind, TaskId, child_id, task_id
from jarvis.domain.task import Task
from jarvis.domain.trace import TraceEvent


@dataclass
class _State:
    tasks: dict[TaskId, str] = field(default_factory=dict[TaskId, str])
    events: dict[TaskId, list[str]] = field(default_factory=dict[TaskId, list[str]])
    event_ids: set[str] = field(default_factory=set[str])
    last_task: int = 0
    last_child: dict[tuple[TaskId, ChildKind], int] = field(
        default_factory=dict[tuple[TaskId, ChildKind], int]
    )
    fail_after_commits: int | None = None


class InMemoryStorage:
    def __init__(self) -> None:
        self._state = _State()
        self.ids = InMemoryIdAllocator(self._state)

    def unit_of_work(self) -> "InMemoryUnitOfWork":
        return InMemoryUnitOfWork(self._state)

    def fail_commit(self, *, after: int = 0) -> None:
        """Пропустить `after` успешных `commit()`, а следующий уронить со StorageError без записи."""
        self._state.fail_after_commits = after


class InMemoryIdAllocator:
    def __init__(self, state: _State) -> None:
        self._state = state

    def next_task_id(self) -> TaskId:
        self._state.last_task += 1
        return task_id(self._state.last_task)

    def next_child_id(self, task_id: TaskId, kind: ChildKind) -> str:
        key = (task_id, kind)
        number = self._state.last_child.get(key, 0) + 1
        self._state.last_child[key] = number
        return child_id(task_id, kind, number)


class _Tasks:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.added: dict[TaskId, str] = {}
        self.saved: dict[TaskId, tuple[str, int]] = {}

    def add(self, task: Task) -> None:
        if task.id in self._state.tasks or task.id in self.added:
            raise StorageError(f"задача {task.id} уже существует", task_id=task.id)
        self.added[task.id] = task.model_dump_json()

    def get(self, task_id: TaskId) -> Task:
        if task_id in self.saved:
            return Task.model_validate_json(self.saved[task_id][0])
        raw = self.added.get(task_id) or self._state.tasks.get(task_id)
        if raw is None:
            raise TaskNotFound(f"задача {task_id} не найдена", task_id=task_id)
        return Task.model_validate_json(raw)

    def save(self, task: Task, expected_version: int) -> None:
        if task.version != expected_version + 1:
            raise ValueError(f"новая версия задачи должна быть {expected_version + 1}, а не {task.version}")
        current = self.get(task.id)
        if current.version != expected_version:
            raise ConcurrentModification(
                f"задача {task.id} изменена: версия {current.version}, ожидалась {expected_version}",
                task_id=task.id,
            )
        self.saved[task.id] = (task.model_dump_json(), expected_version)


class _Trace:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.appended: list[TraceEvent] = []

    def append(self, events: Sequence[TraceEvent]) -> None:
        pending = {event.id for event in self.appended}
        for event in events:
            if event.id in pending or event.id in self._state.event_ids:
                raise StorageError(f"событие {event.id} уже записано", event_id=event.id)
            pending.add(event.id)
        self.appended.extend(events)

    def list(self, task_id: TaskId, *, after_seq: int = 0) -> list[TraceEvent]:
        stored = [TraceEvent.model_validate_json(raw) for raw in self._state.events.get(task_id, [])]
        pending = [event for event in self.appended if event.task_id == task_id]
        return sorted(
            (event for event in [*stored, *pending] if event.seq > after_seq),
            key=lambda event: event.seq,
        )


class InMemoryUnitOfWork:
    def __init__(self, state: _State) -> None:
        self._state = state
        self._tasks = _Tasks(state)
        self._trace = _Trace(state)
        self._done = False

    @property
    def tasks(self) -> _Tasks:
        return self._tasks

    @property
    def trace(self) -> _Trace:
        return self._trace

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._done = True  # всё незакоммиченное отбрасывается вместе с объектом

    def commit(self) -> None:
        if self._done:
            raise StorageError("единица работы уже завершена")
        self._done = True
        state = self._state
        if state.fail_after_commits is not None:
            if state.fail_after_commits == 0:
                state.fail_after_commits = None
                raise StorageError("внедрённый сбой записи")
            state.fail_after_commits -= 1
        # Повторная проверка при записи: другая единица работы могла успеть записать то же самое.
        for key in self._tasks.added:
            if key in state.tasks:
                raise StorageError(f"задача {key} уже существует", task_id=key)
        for event in self._trace.appended:
            if event.id in state.event_ids:
                raise StorageError(f"событие {event.id} уже записано", event_id=event.id)
        for key, (_, expected) in self._tasks.saved.items():
            stored = state.tasks.get(key) or self._tasks.added.get(key)
            assert stored is not None
            if Task.model_validate_json(stored).version != expected:
                raise ConcurrentModification(f"задача {key} изменена другим писателем", task_id=key)
        state.tasks.update(self._tasks.added)
        state.tasks.update({key: raw for key, (raw, _) in self._tasks.saved.items()})
        for event in self._trace.appended:
            state.events.setdefault(event.task_id, []).append(event.model_dump_json())
            state.event_ids.add(event.id)
