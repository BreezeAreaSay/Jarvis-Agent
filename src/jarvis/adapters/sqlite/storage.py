"""Хранилище SQLite (ADR 0003, ADR 0021).

Один файл в режиме WAL, стандартный `sqlite3`, синхронно. Единица работы оптимистическая:
чтения идут в одной читающей транзакции (согласованный снимок), записи копятся и применяются на
`commit()` короткой транзакцией `BEGIN IMMEDIATE`, где каждое ожидание (версия задачи, аренда)
проверяется условием в самом запросе. Расхождение — `ConcurrentModification`, без частичной записи.
Ошибки SQLite превращаются в `StorageError` с объяснением; повреждённая база не «чинится».
"""

import json
import sqlite3
import time
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from jarvis.adapters.sqlite.migrate import Migration, bundled_migrations, migrate
from jarvis.domain.agent import AgentState
from jarvis.domain.approvals import ApprovalRequest, ApprovalStatus
from jarvis.domain.audit import AuditRecord
from jarvis.domain.budget import Budget, BudgetUsage
from jarvis.domain.errors import ApprovalNotFound, ConcurrentModification, StorageError, TaskNotFound
from jarvis.domain.ids import ChildKind, TaskId, child_id, child_number, task_id, task_number
from jarvis.domain.lease import Lease
from jarvis.domain.models import ModelCallRecord
from jarvis.domain.routing import Route, RouteDecision
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Task, TaskOutcome, TaskRequest
from jarvis.domain.trace import EventKind, TraceEvent

_TASK_COLUMNS = (
    "id, version, status, route, request_json, budget_json, usage_json, outcome_json, state_json, "
    "routing_json, created_at, updated_at"
)


def storage_error(exc: sqlite3.Error, path: Path) -> StorageError:
    """Понятное объяснение ошибки SQLite. Ничего не исправляет: только называет проблему."""
    text = str(exc).lower()
    if "file is not a database" in text:
        message = f"{path} — не база SQLite (файл повреждён или чужой)"
    elif "malformed" in text or "corrupt" in text:
        message = f"файл базы {path} повреждён; восстановите его из копии или удалите"
    elif "readonly" in text or "read-only" in text:
        message = f"нет прав на запись в базу {path}"
    elif "full" in text:
        message = f"нет места для базы {path}: освободите диск"
    elif "locked" in text or "busy" in text:
        message = f"база {path} занята другим процессом; повторите позже"
    elif "unable to open" in text:
        message = f"не удалось открыть базу {path}: проверьте путь и права"
    else:
        message = f"ошибка базы {path}: {exc}"
    return StorageError(message, path=str(path), sqlite_error=str(exc))


class SqliteStorage:
    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_s: float = 5.0,
        migrations: Sequence[Migration] | None = None,
    ) -> None:
        self.path = path
        self._busy_timeout_ms = round(busy_timeout_s * 1000)
        self._idle: list[sqlite3.Connection] = []
        self._closed = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"не удалось создать папку для базы {path}: {exc}", path=str(path)) from None
        with self.guard():
            conn = self._connect()
            try:
                self.schema_version = migrate(
                    conn, path, bundled_migrations() if migrations is None else migrations
                )
            except BaseException:
                conn.close()
                raise
            self._idle.append(conn)
        self.ids = SqliteIdAllocator(self)

    def unit_of_work(self) -> "SqliteUnitOfWork":
        return SqliteUnitOfWork(self)

    def close(self) -> None:
        self._closed = True
        while self._idle:
            self._idle.pop().close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def guard(self) -> Iterator[None]:
        try:
            yield
        except sqlite3.Error as exc:
            raise storage_error(exc, self.path) from None

    def take(self) -> sqlite3.Connection:
        """Соединение из пула; у каждой единицы работы своё — как у отдельного процесса."""
        return self._idle.pop() if self._idle else self._connect()

    def give(self, conn: sqlite3.Connection) -> None:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            if self._closed:  # хранилище закрыто, пока соединение было занято: файл не держим
                conn.close()
            else:
                self._idle.append(conn)

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: транзакции открываются только явно (BEGIN / BEGIN IMMEDIATE).
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=self._busy_timeout_ms / 1000)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            mode = self._enable_wal(conn)
            if mode != "wal":
                raise StorageError(f"база {self.path}: не удалось включить режим WAL ({mode})")
            conn.execute("PRAGMA synchronous = NORMAL")
        except BaseException:
            conn.close()
            raise
        return conn

    def _enable_wal(self, conn: sqlite3.Connection) -> str:
        """Перевод новой базы в WAL. Пока другое соединение переводит её же, SQLite отвечает
        «locked» сразу, не дожидаясь busy_timeout, — поэтому повтор с паузой в пределах того же срока.
        Режим WAL хранится в файле: у уже переведённой базы команда ничего не ждёт."""
        deadline = time.monotonic() + self._busy_timeout_ms / 1000
        while True:
            try:
                return str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)


class SqliteIdAllocator:
    """Счётчик увеличивается отдельной мгновенной записью — вне любой контрольной точки."""

    def __init__(self, storage: SqliteStorage) -> None:
        self._storage = storage

    def next_task_id(self) -> TaskId:
        return task_id(self._next("task", "task"))

    def next_child_id(self, task_id: TaskId, kind: ChildKind) -> str:
        return child_id(task_id, kind, self._next(task_id, kind))

    def _next(self, scope: str, kind: str) -> int:
        with self._storage.guard():
            conn = self._storage.take()
            try:
                rows = conn.execute(
                    "INSERT INTO id_counters (scope, kind, last) VALUES (?, ?, 1) "
                    "ON CONFLICT (scope, kind) DO UPDATE SET last = last + 1 RETURNING last",
                    (scope, kind),
                ).fetchall()
            finally:
                self._storage.give(conn)
            return int(rows[0][0])


class SqliteUnitOfWork:
    def __init__(self, storage: SqliteStorage) -> None:
        self._storage = storage
        self._conn: sqlite3.Connection | None = None
        self._done = False
        self._tasks = _Tasks(self)
        self._trace = _Trace(self)
        self._leases = _Leases(self)
        self._approvals = _Approvals(self)
        self._audit = _Audit(self)
        self._model_calls = _ModelCalls(self)

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

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._done = True
        self._close()

    def query(self, sql: str, parameters: Sequence[object] = ()) -> list[tuple[object, ...]]:
        """Чтение внутри одного снимка: первая выборка открывает читающую транзакцию."""
        if self._done:
            raise StorageError("единица работы уже завершена")
        with self._storage.guard():
            conn = self._open()
            if not conn.in_transaction:
                conn.execute("BEGIN")
            return conn.execute(sql, parameters).fetchall()

    def commit(self) -> None:
        if self._done:
            raise StorageError("единица работы уже завершена")
        self._done = True
        try:
            staged = (
                self._tasks.added,
                self._tasks.saved,
                self._trace.appended,
                self._leases.changes,
                self._approvals.added,
                self._approvals.saved,
                self._audit.appended,
                self._model_calls.added,
            )
            if any(staged):
                with self._storage.guard():
                    conn = self._open()
                    if conn.in_transaction:
                        conn.execute("COMMIT")  # конец читающего снимка; запись — своей транзакцией
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._write(conn)
                    except BaseException:
                        if conn.in_transaction:  # при «диск полон» SQLite уже откатил сам
                            conn.execute("ROLLBACK")
                        raise
                    conn.execute("COMMIT")
        finally:
            self._close()

    def _write(self, conn: sqlite3.Connection) -> None:
        tasks, trace, leases = self._tasks, self._trace, self._leases
        for key, task in tasks.added.items():
            final = tasks.saved[key][0] if key in tasks.saved else task
            try:
                conn.execute(
                    f"INSERT INTO tasks (seq, {_TASK_COLUMNS}) VALUES ({', '.join('?' * 13)})",
                    (task_number(final.id), *_task_row(final)),
                )
            except sqlite3.IntegrityError:
                raise StorageError(f"задача {key} уже существует", task_id=key) from None
        for key, (task, expected) in tasks.saved.items():
            if key in tasks.added:
                continue
            row = _task_row(task)
            changed = conn.execute(
                "UPDATE tasks SET version = ?, status = ?, route = ?, request_json = ?, budget_json = ?, "
                "usage_json = ?, outcome_json = ?, state_json = ?, routing_json = ?, created_at = ?, "
                "updated_at = ? "
                "WHERE id = ? AND version = ?",
                (*row[1:], key, expected),
            ).rowcount
            if changed != 1:
                raise ConcurrentModification(f"задача {key} изменена другим писателем", task_id=key)
        for event in trace.appended:
            try:
                conn.execute(
                    "INSERT INTO trace_events (id, task_id, seq, ts, kind, v, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    _event_row(event),
                )
            except sqlite3.IntegrityError as exc:
                if "foreign key" in str(exc).lower():
                    raise StorageError(f"событие {event.id}: задачи {event.task_id} нет") from None
                raise StorageError(f"событие {event.id} уже записано", event_id=event.id) from None
        for key, (lease, expected) in leases.changes.items():
            if not _apply_lease(conn, key, lease, expected):
                raise ConcurrentModification(f"аренда задачи {key} изменилась", task_id=key)
        for approval in self._approvals.added.values():
            final = (
                self._approvals.saved[approval.id][0] if approval.id in self._approvals.saved else approval
            )
            try:
                conn.execute(
                    "INSERT INTO approvals (id, task_id, seq, tool_call_id, status, request_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        final.id,
                        final.task_id,
                        child_number(final.id),
                        final.call.id,
                        final.status.value,
                        final.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "foreign key" in str(exc).lower():
                    raise StorageError(f"запрос {final.id}: задачи {final.task_id} нет") from None
                raise StorageError(f"запрос {final.id} уже существует", approval_id=final.id) from None
        for key, (approval, expected) in self._approvals.saved.items():
            if key in self._approvals.added:
                continue
            changed = conn.execute(
                "UPDATE approvals SET status = ?, request_json = ? WHERE id = ? AND status = ?",
                (approval.status.value, approval.model_dump_json(), key, expected.value),
            ).rowcount
            if changed != 1:
                raise ConcurrentModification(f"запрос {key} изменён другим писателем", approval_id=key)
        for record in self._audit.appended:
            conn.execute(
                "INSERT INTO audit_log (ts, task_id, tool_call_id, action, record_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.ts.isoformat(),
                    record.task_id,
                    record.tool_call_id,
                    record.action.value,
                    record.model_dump_json(),
                ),
            )
        for call in self._model_calls.added.values():
            try:
                conn.execute(
                    "INSERT INTO model_calls (id, task_id, seq, created_at, status, record_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        call.id,
                        call.task_id,
                        child_number(call.id),
                        call.created_at.isoformat(),
                        call.status,
                        call.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "foreign key" in str(exc).lower():
                    raise StorageError(f"вызов модели {call.id}: задачи {call.task_id} нет") from None
                raise StorageError(f"вызов модели {call.id} уже записан", model_call_id=call.id) from None

    def _open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._storage.take()
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            with self._storage.guard():
                self._storage.give(conn)  # с откатом незавершённой транзакции


def _apply_lease(conn: sqlite3.Connection, key: TaskId, lease: Lease | None, expected: Lease | None) -> bool:
    """Сравнить и записать: True, если в базе было ровно `expected`."""
    if expected is None:
        assert lease is not None  # удалить можно только известную аренду
        try:
            conn.execute(
                "INSERT INTO task_leases (task_id, owner, expires_at) VALUES (?, ?, ?)",
                (key, lease.owner, lease.expires_at.isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            if "foreign key" in str(exc).lower():
                raise StorageError(f"аренда: задачи {key} нет", task_id=key) from None
            return False
        return True
    match = (key, expected.owner, expected.expires_at.isoformat())
    if lease is None:
        sql = "DELETE FROM task_leases WHERE task_id = ? AND owner = ? AND expires_at = ?"
        return conn.execute(sql, match).rowcount == 1
    sql = (
        "UPDATE task_leases SET owner = ?, expires_at = ? WHERE task_id = ? AND owner = ? AND expires_at = ?"
    )
    return conn.execute(sql, (lease.owner, lease.expires_at.isoformat(), *match)).rowcount == 1


def _task_row(task: Task) -> tuple[object, ...]:
    return (
        task.id,
        task.version,
        task.status.value,
        task.route.value if task.route else None,
        task.request.model_dump_json(),
        task.budget.model_dump_json(),
        task.usage.model_dump_json(),
        task.outcome.model_dump_json() if task.outcome else None,
        task.state.model_dump_json() if task.state else None,
        task.routing.model_dump_json() if task.routing else None,
        task.created_at.isoformat(),
        task.updated_at.isoformat(),
    )


def _task_from(row: tuple[object, ...]) -> Task:
    key, version, status, route, request, budget, usage, outcome, state, routing, created, updated = row
    return Task(
        id=TaskId(str(key)),
        version=int(str(version)),
        request=TaskRequest.model_validate_json(str(request)),
        status=TaskStatus(str(status)),
        route=Route(str(route)) if route is not None else None,
        budget=Budget.model_validate_json(str(budget)),
        usage=BudgetUsage.model_validate_json(str(usage)),
        outcome=TaskOutcome.model_validate_json(str(outcome)) if outcome is not None else None,
        state=AgentState.model_validate_json(str(state)) if state is not None else None,
        routing=RouteDecision.model_validate_json(str(routing)) if routing is not None else None,
        created_at=datetime.fromisoformat(str(created)),
        updated_at=datetime.fromisoformat(str(updated)),
    )


def _event_row(event: TraceEvent) -> tuple[object, ...]:
    payload = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
    return (event.id, event.task_id, event.seq, event.ts.isoformat(), event.kind.value, event.v, payload)


def _event_from(row: tuple[object, ...]) -> TraceEvent:
    key, task, seq, ts, kind, v, payload = row
    return TraceEvent(
        id=str(key),
        task_id=TaskId(str(task)),
        seq=int(str(seq)),
        ts=datetime.fromisoformat(str(ts)),
        kind=EventKind(str(kind)),
        v=int(str(v)),
        payload=json.loads(str(payload)),
    )


class _Tasks:
    def __init__(self, uow: SqliteUnitOfWork) -> None:
        self._uow = uow
        self.added: dict[TaskId, Task] = {}
        self.saved: dict[TaskId, tuple[Task, int]] = {}

    def add(self, task: Task) -> None:
        if task.id in self.added or self._stored(task.id) is not None:
            raise StorageError(f"задача {task.id} уже существует", task_id=task.id)
        self.added[task.id] = task

    def get(self, task_id: TaskId) -> Task:
        task = self._visible(task_id)
        if task is None:
            raise TaskNotFound(f"задача {task_id} не найдена", task_id=task_id)
        return task

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
        self.saved[task.id] = (task, first_expected)

    def list(self, *, statuses: Collection[TaskStatus] | None = None, limit: int | None = None) -> list[Task]:
        where, parameters = "", []
        if statuses is not None:
            if not statuses:
                return []
            where = f"WHERE status IN ({', '.join('?' for _ in statuses)})"
            parameters = [status.value for status in statuses]
        staged = bool(self.added or self.saved)
        sql = f"SELECT {_TASK_COLUMNS} FROM tasks {where} ORDER BY seq DESC"
        if limit is not None and not staged:
            sql += f" LIMIT {int(limit)}"
        found = [_task_from(row) for row in self._uow.query(sql, parameters)]
        if not staged:
            return found
        # Свои незакоммиченные записи видны и в выборке.
        merged = {task.id: task for task in found}
        merged.update({key: self.get(key) for key in [*self.added, *self.saved]})
        ordered = sorted(merged.values(), key=lambda task: task_number(task.id), reverse=True)
        chosen = [task for task in ordered if statuses is None or task.status in statuses]
        return chosen if limit is None else chosen[:limit]

    def _visible(self, task_id: TaskId) -> Task | None:
        if task_id in self.saved:
            return self.saved[task_id][0]
        if task_id in self.added:
            return self.added[task_id]
        return self._stored(task_id)

    def _stored(self, task_id: TaskId) -> Task | None:
        rows = self._uow.query(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,))
        return _task_from(rows[0]) if rows else None


class _Trace:
    def __init__(self, uow: SqliteUnitOfWork) -> None:
        self._uow = uow
        self.appended: list[TraceEvent] = []

    def append(self, events: Sequence[TraceEvent]) -> None:
        pending = {event.id for event in self.appended}
        for event in events:
            exists = self._uow.query("SELECT 1 FROM trace_events WHERE id = ?", (event.id,))
            if event.id in pending or exists:
                raise StorageError(f"событие {event.id} уже записано", event_id=event.id)
            pending.add(event.id)
        self.appended.extend(events)

    def list(self, task_id: TaskId, *, after_seq: int = 0) -> list[TraceEvent]:
        rows = self._uow.query(
            "SELECT id, task_id, seq, ts, kind, v, payload_json FROM trace_events "
            "WHERE task_id = ? AND seq > ? ORDER BY seq",
            (task_id, after_seq),
        )
        stored = [_event_from(row) for row in rows]
        pending = [event for event in self.appended if event.task_id == task_id and event.seq > after_seq]
        return sorted([*stored, *pending], key=lambda event: event.seq)


class _Leases:
    def __init__(self, uow: SqliteUnitOfWork) -> None:
        self._uow = uow
        self.changes: dict[TaskId, tuple[Lease | None, Lease | None]] = {}

    def get(self, task_id: TaskId) -> Lease | None:
        if task_id in self.changes:
            return self.changes[task_id][0]
        rows = self._uow.query("SELECT owner, expires_at FROM task_leases WHERE task_id = ?", (task_id,))
        if not rows:
            return None
        owner, expires_at = rows[0]
        return Lease(task_id=task_id, owner=str(owner), expires_at=datetime.fromisoformat(str(expires_at)))

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
    def __init__(self, uow: SqliteUnitOfWork) -> None:
        self._uow = uow
        self.added: dict[str, ApprovalRequest] = {}
        self.saved: dict[str, tuple[ApprovalRequest, ApprovalStatus]] = {}

    def add(self, approval: ApprovalRequest) -> None:
        if approval.id in self.added or self._stored(approval.id) is not None:
            raise StorageError(f"запрос {approval.id} уже существует", approval_id=approval.id)
        self.added[approval.id] = approval

    def get(self, approval_id: str) -> ApprovalRequest:
        if approval_id in self.saved:
            return self.saved[approval_id][0]
        if approval_id in self.added:
            return self.added[approval_id]
        stored = self._stored(approval_id)
        if stored is None:
            raise ApprovalNotFound(f"запрос подтверждения {approval_id} не найден", approval_id=approval_id)
        return stored

    def save(self, approval: ApprovalRequest, *, expected: ApprovalStatus) -> None:
        current = self.get(approval.id)
        if current.status is not expected:
            raise ConcurrentModification(
                f"запрос {approval.id}: статус {current.status}, ожидался {expected}", approval_id=approval.id
            )
        first = self.saved[approval.id][1] if approval.id in self.saved else expected
        self.saved[approval.id] = (approval, first)

    def for_task(self, task_id: TaskId) -> list[ApprovalRequest]:
        rows = self._uow.query("SELECT id FROM approvals WHERE task_id = ? ORDER BY seq", (task_id,))
        keys = [str(row[0]) for row in rows]
        keys += [key for key, item in self.added.items() if item.task_id == task_id and key not in keys]
        return sorted((self.get(key) for key in keys), key=lambda item: child_number(item.id))

    def _stored(self, approval_id: str) -> ApprovalRequest | None:
        rows = self._uow.query("SELECT request_json FROM approvals WHERE id = ?", (approval_id,))
        return ApprovalRequest.model_validate_json(str(rows[0][0])) if rows else None


class _Audit:
    def __init__(self, uow: SqliteUnitOfWork) -> None:
        self._uow = uow
        self.appended: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.appended.append(record)

    def list(self, *, task_id: TaskId | None = None) -> list[AuditRecord]:
        if task_id is None:
            rows = self._uow.query("SELECT record_json FROM audit_log ORDER BY seq")
        else:
            rows = self._uow.query(
                "SELECT record_json FROM audit_log WHERE task_id = ? ORDER BY seq", (task_id,)
            )
        stored = [AuditRecord.model_validate_json(str(row[0])) for row in rows]
        return [
            *stored,
            *(record for record in self.appended if task_id is None or record.task_id == task_id),
        ]


class _ModelCalls:
    def __init__(self, uow: SqliteUnitOfWork) -> None:
        self._uow = uow
        self.added: dict[str, ModelCallRecord] = {}

    def add(self, call: ModelCallRecord) -> None:
        exists = self._uow.query("SELECT 1 FROM model_calls WHERE id = ?", (call.id,))
        if call.id in self.added or exists:
            raise StorageError(f"вызов модели {call.id} уже записан", model_call_id=call.id)
        self.added[call.id] = call

    def for_task(self, task_id: TaskId) -> list[ModelCallRecord]:
        rows = self._uow.query(
            "SELECT record_json FROM model_calls WHERE task_id = ? ORDER BY seq", (task_id,)
        )
        stored = [ModelCallRecord.model_validate_json(str(row[0])) for row in rows]
        pending = [call for call in self.added.values() if call.task_id == task_id]
        return sorted([*stored, *pending], key=lambda call: child_number(call.id))
