"""Хранилище в памяти: та же семантика, что у SQLite, без файла.

Задачи, события и аренды хранятся сериализованными в JSON, а не ссылками на объекты: восстановление
задачи проходит через ту же сериализацию, что и в SQLite. Единица работы видит снимок состояния на
момент первого чтения, копит записи и на `commit()` заново сверяет ожидания — как транзакция SQLite.
Для тестов атомарности можно заставить `commit()` упасть.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from jarvis.domain.approvals import ApprovalRequest, ApprovalStatus
from jarvis.domain.audit import AuditRecord
from jarvis.domain.errors import ApprovalNotFound, ConcurrentModification, StorageError, TaskNotFound
from jarvis.domain.ids import ChildKind, TaskId, child_id, child_number, task_id, task_number
from jarvis.domain.lease import Lease
from jarvis.domain.models import ModelCallRecord
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Task
from jarvis.domain.trace import TraceEvent


@dataclass
class _State:
    tasks: dict[TaskId, str] = field(default_factory=dict[TaskId, str])
    events: dict[TaskId, list[str]] = field(default_factory=dict[TaskId, list[str]])
    event_ids: set[str] = field(default_factory=set[str])
    leases: dict[TaskId, str] = field(default_factory=dict[TaskId, str])
    approvals: dict[str, str] = field(default_factory=dict[str, str])
    audit: list[str] = field(default_factory=list[str])
    model_calls: dict[str, str] = field(default_factory=dict[str, str])
    last_task: int = 0
    last_child: dict[tuple[TaskId, ChildKind], int] = field(
        default_factory=dict[tuple[TaskId, ChildKind], int]
    )
    fail_after_commits: int | None = None


@dataclass(frozen=True)
class _Snapshot:
    """Состояние на момент первого чтения: события только дописываются, поэтому хватает их числа."""

    tasks: dict[TaskId, str]
    event_counts: dict[TaskId, int]
    leases: dict[TaskId, str]
    approvals: dict[str, str]
    audit_count: int
    model_calls: dict[str, str]


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


class InMemoryUnitOfWork:
    def __init__(self, state: _State) -> None:
        self._state = state
        self._snapshot: _Snapshot | None = None
        self._tasks = _Tasks(self)
        self._trace = _Trace(self)
        self._leases = _Leases(self)
        self._approvals = _Approvals(self)
        self._audit = _Audit(self)
        self._model_calls = _ModelCalls(self)
        self._done = False

    @property
    def tasks(self) -> "_Tasks":
        return self._tasks

    @property
    def trace(self) -> "_Trace":
        return self._trace

    @property
    def leases(self) -> "_Leases":
        return self._leases

    @property
    def approvals(self) -> "_Approvals":
        return self._approvals

    @property
    def audit(self) -> "_Audit":
        return self._audit

    @property
    def model_calls(self) -> "_ModelCalls":
        return self._model_calls

    def stored_audit(self) -> list[str]:
        return self._state.audit[: self.snapshot().audit_count]

    def snapshot(self) -> _Snapshot:
        if self._snapshot is None:
            state = self._state
            self._snapshot = _Snapshot(
                tasks=dict(state.tasks),
                event_counts={key: len(value) for key, value in state.events.items()},
                leases=dict(state.leases),
                approvals=dict(state.approvals),
                audit_count=len(state.audit),
                model_calls=dict(state.model_calls),
            )
        return self._snapshot

    def stored_events(self, task: TaskId) -> list[str]:
        return self._state.events.get(task, [])[: self.snapshot().event_counts.get(task, 0)]

    def event_exists(self, event_id: str) -> bool:
        return event_id in self._state.event_ids

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
        known = state.tasks.keys() | self._tasks.added.keys()
        for event in self._trace.appended:
            if event.id in state.event_ids:
                raise StorageError(f"событие {event.id} уже записано", event_id=event.id)
            if event.task_id not in known:
                raise StorageError(f"событие {event.id}: задачи {event.task_id} нет")
        for key in self._leases.changes:
            if key not in known:
                raise StorageError(f"аренда: задачи {key} нет", task_id=key)
        for approval in self._approvals.added.values():
            if approval.id in state.approvals:
                raise StorageError(f"запрос {approval.id} уже существует", approval_id=approval.id)
            if approval.task_id not in known:
                raise StorageError(f"запрос {approval.id}: задачи {approval.task_id} нет")
        for call in self._model_calls.added.values():
            if call.id in state.model_calls:
                raise StorageError(f"вызов модели {call.id} уже записан", model_call_id=call.id)
            if call.task_id not in known:
                raise StorageError(f"вызов модели {call.id}: задачи {call.task_id} нет")
        for key, (_, expected) in self._approvals.saved.items():
            stored = state.approvals.get(key)
            if key not in self._approvals.added and (
                stored is None or ApprovalRequest.model_validate_json(stored).status is not expected
            ):
                raise ConcurrentModification(f"запрос {key} изменён другим писателем", approval_id=key)
        for key, (_, expected) in self._tasks.saved.items():
            stored = state.tasks.get(key) or self._tasks.added.get(key)
            if stored is None or Task.model_validate_json(stored).version != expected:
                raise ConcurrentModification(f"задача {key} изменена другим писателем", task_id=key)
        for key, (_, expected) in self._leases.changes.items():
            current = state.leases.get(key)
            if _lease(current) != expected:
                raise ConcurrentModification(f"аренда задачи {key} изменилась", task_id=key)

        state.tasks.update(self._tasks.added)
        state.tasks.update({key: raw for key, (raw, _) in self._tasks.saved.items()})
        for event in self._trace.appended:
            state.events.setdefault(event.task_id, []).append(event.model_dump_json())
            state.event_ids.add(event.id)
        for key, (lease, _) in self._leases.changes.items():
            if lease is None:
                state.leases.pop(key, None)
            else:
                state.leases[key] = lease.model_dump_json()
        for key, approval in self._approvals.added.items():
            state.approvals[key] = approval.model_dump_json()
        for key, (approval, _) in self._approvals.saved.items():
            state.approvals[key] = approval.model_dump_json()
        state.audit.extend(record.model_dump_json() for record in self._audit.appended)
        state.model_calls.update(
            {key: call.model_dump_json() for key, call in self._model_calls.added.items()}
        )


def _lease(raw: str | None) -> Lease | None:
    return None if raw is None else Lease.model_validate_json(raw)


class _Tasks:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self.added: dict[TaskId, str] = {}
        self.saved: dict[TaskId, tuple[str, int]] = {}

    def add(self, task: Task) -> None:
        if task.id in self._uow.snapshot().tasks or task.id in self.added:
            raise StorageError(f"задача {task.id} уже существует", task_id=task.id)
        self.added[task.id] = task.model_dump_json()

    def get(self, task_id: TaskId) -> Task:
        raw = self._raw(task_id)
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
        first_expected = self.saved[task.id][1] if task.id in self.saved else expected_version
        self.saved[task.id] = (task.model_dump_json(), first_expected)

    def list(self, *, statuses: Collection[TaskStatus] | None = None, limit: int | None = None) -> list[Task]:
        ids = sorted({*self._uow.snapshot().tasks, *self.added}, key=task_number, reverse=True)
        found: list[Task] = []
        for key in ids:
            task = self.get(key)
            if statuses is None or task.status in statuses:
                found.append(task)
                if limit is not None and len(found) >= limit:
                    break
        return found

    def _raw(self, task_id: TaskId) -> str | None:
        if task_id in self.saved:
            return self.saved[task_id][0]
        return self.added.get(task_id) or self._uow.snapshot().tasks.get(task_id)


class _Trace:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self.appended: list[TraceEvent] = []

    def append(self, events: Sequence[TraceEvent]) -> None:
        pending = {event.id for event in self.appended}
        for event in events:
            if event.id in pending or self._uow.event_exists(event.id):
                raise StorageError(f"событие {event.id} уже записано", event_id=event.id)
            pending.add(event.id)
        self.appended.extend(events)

    def list(self, task_id: TaskId, *, after_seq: int = 0) -> list[TraceEvent]:
        stored = [TraceEvent.model_validate_json(raw) for raw in self._uow.stored_events(task_id)]
        pending = [event for event in self.appended if event.task_id == task_id]
        return sorted(
            (event for event in [*stored, *pending] if event.seq > after_seq),
            key=lambda event: event.seq,
        )


class _Leases:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        # задача → (новое значение или None для удаления, ожидаемое значение до этой единицы работы)
        self.changes: dict[TaskId, tuple[Lease | None, Lease | None]] = {}

    def get(self, task_id: TaskId) -> Lease | None:
        if task_id in self.changes:
            return self.changes[task_id][0]
        return _lease(self._uow.snapshot().leases.get(task_id))

    def put(self, lease: Lease, *, expected: Lease | None) -> None:
        self._change(lease.task_id, lease, expected)

    def delete(self, task_id: TaskId, *, expected: Lease) -> None:
        self._change(task_id, None, expected)

    def _change(self, task_id: TaskId, lease: Lease | None, expected: Lease | None) -> None:
        if self.get(task_id) != expected:
            raise ConcurrentModification(f"аренда задачи {task_id} изменилась", task_id=task_id)
        original = self.changes[task_id][1] if task_id in self.changes else expected
        self.changes[task_id] = (lease, original)


class _Approvals:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self.added: dict[str, ApprovalRequest] = {}
        self.saved: dict[str, tuple[ApprovalRequest, ApprovalStatus]] = {}

    def add(self, approval: ApprovalRequest) -> None:
        if approval.id in self.added or approval.id in self._uow.snapshot().approvals:
            raise StorageError(f"запрос {approval.id} уже существует", approval_id=approval.id)
        self.added[approval.id] = approval

    def get(self, approval_id: str) -> ApprovalRequest:
        if approval_id in self.saved:
            return self.saved[approval_id][0]
        if approval_id in self.added:
            return self.added[approval_id]
        raw = self._uow.snapshot().approvals.get(approval_id)
        if raw is None:
            raise ApprovalNotFound(f"запрос подтверждения {approval_id} не найден", approval_id=approval_id)
        return ApprovalRequest.model_validate_json(raw)

    def save(self, approval: ApprovalRequest, *, expected: ApprovalStatus) -> None:
        current = self.get(approval.id)
        if current.status is not expected:
            raise ConcurrentModification(
                f"запрос {approval.id}: статус {current.status}, ожидался {expected}", approval_id=approval.id
            )
        first = self.saved[approval.id][1] if approval.id in self.saved else expected
        self.saved[approval.id] = (approval, first)

    def for_task(self, task_id: TaskId) -> list[ApprovalRequest]:
        keys = {*self._uow.snapshot().approvals, *self.added}
        found = [self.get(key) for key in keys]
        return sorted(
            (item for item in found if item.task_id == task_id), key=lambda item: child_number(item.id)
        )


class _Audit:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self.appended: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.appended.append(record)

    def list(self, *, task_id: TaskId | None = None) -> list[AuditRecord]:
        stored = [AuditRecord.model_validate_json(raw) for raw in self._uow.stored_audit()]
        return [
            record for record in [*stored, *self.appended] if task_id is None or record.task_id == task_id
        ]


class _ModelCalls:
    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        self._uow = uow
        self.added: dict[str, ModelCallRecord] = {}

    def add(self, call: ModelCallRecord) -> None:
        if call.id in self.added or call.id in self._uow.snapshot().model_calls:
            raise StorageError(f"вызов модели {call.id} уже записан", model_call_id=call.id)
        self.added[call.id] = call

    def for_task(self, task_id: TaskId) -> list[ModelCallRecord]:
        stored = [
            ModelCallRecord.model_validate_json(raw) for raw in self._uow.snapshot().model_calls.values()
        ]
        found = [call for call in [*stored, *self.added.values()] if call.task_id == task_id]
        return sorted(found, key=lambda call: child_number(call.id))
