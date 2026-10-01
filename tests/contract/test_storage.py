"""Контракт хранилища: один набор для InMemory и SQLite — поведение адаптеров не должно расходиться."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.domain.approvals import ApprovalRequest, ApprovalStatus
from jarvis.domain.audit import AuditAction, AuditRecord
from jarvis.domain.budget import BudgetUsage
from jarvis.domain.errors import (
    ApprovalNotFound,
    ConcurrentModification,
    Disposition,
    ErrorInfo,
    StorageError,
    TaskNotFound,
)
from jarvis.domain.ids import TaskId, child_number
from jarvis.domain.lease import Lease
from jarvis.domain.settings import BudgetsSettings
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, Route, Task, TaskOutcome, TaskRequest
from jarvis.domain.tools import (
    EffectKind,
    ExecutionTarget,
    PolicyOutcome,
    TargetKind,
    ToolCall,
    ToolCallId,
    ToolEffect,
    ToolId,
)
from jarvis.domain.trace import EventKind, TraceEvent

HOST = ExecutionTarget(kind=TargetKind.HOST, os_family="posix", name="local")

NOW = datetime(2026, 1, 1, 12, 30, 15, 123456, tzinfo=UTC)

Storage = InMemoryStorage | SqliteStorage


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    if request.param == "memory":
        yield InMemoryStorage()
        return
    with SqliteStorage(tmp_path / "jarvis.db") as sqlite:
        yield sqlite


def new_task(task_id: TaskId) -> Task:
    return Task(
        id=task_id,
        version=1,
        request=TaskRequest(text="текст «с кавычками» и \\ слэшем", origin=Origin.EVAL),
        status=TaskStatus.CREATED,
        budget=BudgetsSettings().routing,
        usage=BudgetUsage(),
        created_at=NOW,
        updated_at=NOW,
    )


def new_event(storage: Storage, task_id: TaskId, **payload: str) -> TraceEvent:
    event_id = storage.ids.next_child_id(task_id, "ev")
    return TraceEvent(
        id=event_id,
        task_id=task_id,
        seq=child_number(event_id),
        ts=NOW,
        kind=EventKind.TASK_CREATED,
        payload=dict(payload),
    )


def bumped(task: Task, status: TaskStatus) -> Task:
    return task.model_copy(update={"version": task.version + 1, "status": status})


def add(storage: Storage, *, leased: bool = False) -> Task:
    task = new_task(storage.ids.next_task_id())
    with storage.unit_of_work() as uow:
        uow.tasks.add(task)
        if leased:
            uow.leases.put(lease_for(task), expected=None)
        uow.commit()
    return task


def lease_for(task: Task, owner: str = "A", seconds: float = 30) -> Lease:
    return Lease(task_id=task.id, owner=owner, expires_at=NOW + timedelta(seconds=seconds))


# --- задачи


def test_task_round_trip_keeps_every_field(storage: Storage) -> None:
    task = add(storage)
    finished = task.model_copy(
        update={
            "version": 2,
            "status": TaskStatus.FAILED,
            "route": Route.AGENT,
            "usage": BudgetUsage(steps=3, active_time_s=1.25, model_tokens=900),
            "outcome": TaskOutcome(
                status=TaskStatus.FAILED,
                answer="ответ",
                error=ErrorInfo(
                    category="interrupted",
                    disposition=Disposition.FATAL,
                    retryable=False,
                    message="m",
                    details={"k": 1},
                ),
            ),
            "updated_at": NOW + timedelta(seconds=5),
        }
    )
    with storage.unit_of_work() as uow:
        uow.tasks.save(finished, expected_version=1)
        uow.commit()
    with storage.unit_of_work() as uow:
        assert uow.tasks.get(task.id) == finished


def test_unknown_task_raises(storage: Storage) -> None:
    with storage.unit_of_work() as uow, pytest.raises(TaskNotFound):
        uow.tasks.get(TaskId("task_99"))


def test_duplicate_task_raises(storage: Storage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow, pytest.raises(StorageError):
        uow.tasks.add(task)


def test_concurrent_adds_of_the_same_task_conflict(storage: Storage) -> None:
    task = new_task(storage.ids.next_task_id())
    first, second = storage.unit_of_work(), storage.unit_of_work()
    with first, second:
        first.tasks.add(task)
        second.tasks.add(task)
        first.commit()
        with pytest.raises(StorageError):
            second.commit()


def test_save_checks_expected_version(storage: Storage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow, pytest.raises(ConcurrentModification):
        uow.tasks.save(bumped(bumped(task, TaskStatus.ROUTING), TaskStatus.PLANNING), expected_version=2)
    with storage.unit_of_work() as uow, pytest.raises(ValueError, match="новая версия"):
        uow.tasks.save(task, expected_version=1)


def test_concurrent_writer_is_detected_at_commit(storage: Storage) -> None:
    task = add(storage)
    first, second = storage.unit_of_work(), storage.unit_of_work()
    with first, second:
        first.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
        second.tasks.save(bumped(task, TaskStatus.CANCELLED), expected_version=1)
        first.commit()
        with pytest.raises(ConcurrentModification):
            second.commit()
    with storage.unit_of_work() as uow:
        assert uow.tasks.get(task.id).status is TaskStatus.ROUTING


def test_uncommitted_work_is_discarded(storage: Storage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow:
        uow.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
        uow.trace.append([new_event(storage, task.id)])
        assert uow.tasks.get(task.id).status is TaskStatus.ROUTING  # своя запись видна до commit
        assert len(uow.trace.list(task.id)) == 1
    with storage.unit_of_work() as uow:
        assert uow.tasks.get(task.id) == task
        assert uow.trace.list(task.id) == []


def test_commit_is_all_or_nothing(storage: Storage) -> None:
    task = add(storage, leased=True)
    first, second = storage.unit_of_work(), storage.unit_of_work()
    with first, second:
        current = first.leases.get(task.id)
        assert current is not None
        first.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
        first.trace.append([new_event(storage, task.id)])
        first.leases.put(lease_for(task, owner="A", seconds=90), expected=current)
        second.leases.put(lease_for(task, owner="B", seconds=90), expected=current)
        second.commit()
        with pytest.raises(ConcurrentModification):
            first.commit()  # аренда изменилась — не записывается ни задача, ни событие
    with storage.unit_of_work() as uow:
        assert uow.tasks.get(task.id) == task
        assert uow.trace.list(task.id) == []


# --- список задач


def test_list_is_newest_first_with_filters(storage: Storage) -> None:
    tasks = [add(storage) for _ in range(4)]
    with storage.unit_of_work() as uow:
        uow.tasks.save(bumped(tasks[1], TaskStatus.ROUTING), expected_version=1)
        uow.commit()
    with storage.unit_of_work() as uow:
        assert [task.id for task in uow.tasks.list()] == ["task_4", "task_3", "task_2", "task_1"]
        assert [task.id for task in uow.tasks.list(limit=2)] == ["task_4", "task_3"]
        created = uow.tasks.list(statuses={TaskStatus.CREATED})
        assert [task.id for task in created] == ["task_4", "task_3", "task_1"]
        assert [task.id for task in uow.tasks.list(statuses={TaskStatus.ROUTING})] == ["task_2"]
        assert uow.tasks.list(statuses=set()) == []


def test_list_sees_own_writes(storage: Storage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow:
        uow.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
        uow.tasks.add(new_task(storage.ids.next_task_id()))
        assert [item.status for item in uow.tasks.list()] == [TaskStatus.CREATED, TaskStatus.ROUTING]
        assert [item.id for item in uow.tasks.list(statuses={TaskStatus.ROUTING}, limit=5)] == [task.id]


# --- трасса


def test_trace_is_ordered_and_filterable(storage: Storage) -> None:
    task = add(storage)
    events = [new_event(storage, task.id, text=f"событие {n} «кавычки»") for n in range(3)]
    with storage.unit_of_work() as uow:
        uow.trace.append([events[2], events[0]])
        uow.commit()
    with storage.unit_of_work() as uow:
        uow.trace.append([events[1]])
        uow.commit()
    with storage.unit_of_work() as uow:
        assert uow.trace.list(task.id) == events
        assert uow.trace.list(task.id, after_seq=1) == events[1:]
        assert uow.trace.list(TaskId("task_99")) == []


def test_duplicate_events_are_rejected(storage: Storage) -> None:
    task = add(storage)
    event = new_event(storage, task.id)
    with storage.unit_of_work() as uow, pytest.raises(StorageError):
        uow.trace.append([event, event])
    with storage.unit_of_work() as uow:
        uow.trace.append([event])
        uow.commit()
    with storage.unit_of_work() as uow, pytest.raises(StorageError):
        uow.trace.append([event])
    first, second = storage.unit_of_work(), storage.unit_of_work()
    other = new_event(storage, task.id)
    with first, second:
        first.trace.append([other])
        second.trace.append([other])
        first.commit()
        with pytest.raises(StorageError):
            second.commit()


def test_records_of_an_unknown_task_are_rejected(storage: Storage) -> None:
    ghost = TaskId("task_77")
    with storage.unit_of_work() as uow:
        uow.trace.append([new_event(storage, ghost)])
        with pytest.raises(StorageError):
            uow.commit()
    with storage.unit_of_work() as uow:
        uow.leases.put(Lease(task_id=ghost, owner="A", expires_at=NOW), expected=None)
        with pytest.raises(StorageError):
            uow.commit()


# --- аренды


def test_lease_round_trip_and_compare_and_set(storage: Storage) -> None:
    task = add(storage)
    first = lease_for(task)
    with storage.unit_of_work() as uow:
        assert uow.leases.get(task.id) is None
        uow.leases.put(first, expected=None)
        uow.commit()
    renewed = lease_for(task, seconds=60)
    with storage.unit_of_work() as uow:
        assert uow.leases.get(task.id) == first
        with pytest.raises(ConcurrentModification):
            uow.leases.put(renewed, expected=None)  # аренда уже есть
        uow.leases.put(renewed, expected=first)
        uow.commit()
    with storage.unit_of_work() as uow:
        with pytest.raises(ConcurrentModification):
            uow.leases.delete(task.id, expected=first)  # устаревшее ожидание
        uow.leases.delete(task.id, expected=renewed)
        assert uow.leases.get(task.id) is None
        uow.commit()
    with storage.unit_of_work() as uow:
        assert uow.leases.get(task.id) is None


def test_two_owners_racing_for_a_lease(storage: Storage) -> None:
    task = add(storage)
    first, second = storage.unit_of_work(), storage.unit_of_work()
    with first, second:
        assert first.leases.get(task.id) is None
        assert second.leases.get(task.id) is None
        first.leases.put(lease_for(task, owner="A"), expected=None)
        second.leases.put(lease_for(task, owner="B"), expected=None)
        first.commit()
        with pytest.raises(ConcurrentModification):
            second.commit()
    with storage.unit_of_work() as uow:
        assert uow.leases.get(task.id) == lease_for(task, owner="A")


def test_stale_delete_loses_to_a_renewal(storage: Storage) -> None:
    task = add(storage, leased=True)
    original = lease_for(task)
    first, second = storage.unit_of_work(), storage.unit_of_work()
    with first, second:
        first.leases.put(lease_for(task, seconds=90), expected=original)  # продление
        second.leases.delete(task.id, expected=original)  # «аренда истекла» — по старым данным
        first.commit()
        with pytest.raises(ConcurrentModification):
            second.commit()


# --- снимок и счётчики


def test_reads_inside_a_unit_of_work_are_a_consistent_snapshot(storage: Storage) -> None:
    task = add(storage)
    reader = storage.unit_of_work()
    with reader:
        assert reader.tasks.get(task.id).version == 1
        with storage.unit_of_work() as writer:  # контрольная точка: задача + событие перехода
            writer.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
            writer.trace.append([new_event(storage, task.id)])
            writer.commit()
        assert reader.trace.list(task.id) == []  # снимок: задача v1 и её события согласованы
        assert reader.tasks.get(task.id).version == 1
    with storage.unit_of_work() as fresh:
        assert fresh.tasks.get(task.id).version == 2
        assert len(fresh.trace.list(task.id)) == 1


def test_id_allocator_is_monotonic_and_scoped(storage: Storage) -> None:
    ids = storage.ids
    assert [ids.next_task_id() for _ in range(3)] == ["task_1", "task_2", "task_3"]
    assert ids.next_child_id(TaskId("task_1"), "ev") == "task_1.ev_1"
    assert ids.next_child_id(TaskId("task_1"), "ev") == "task_1.ev_2"
    assert ids.next_child_id(TaskId("task_1"), "call") == "task_1.call_1"
    assert ids.next_child_id(TaskId("task_2"), "ev") == "task_2.ev_1"


def test_ids_are_not_reused_after_a_failed_commit(storage: Storage) -> None:
    task = add(storage)
    first, second = storage.unit_of_work(), storage.unit_of_work()
    with first, second:
        first.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
        second.tasks.save(bumped(task, TaskStatus.CANCELLED), expected_version=1)
        second.trace.append([new_event(storage, task.id)])  # task_1.ev_1
        first.commit()
        with pytest.raises(ConcurrentModification):
            second.commit()
    assert storage.ids.next_child_id(task.id, "ev") == f"{task.id}.ev_2"  # откат не вернул номер


# --- подтверждения и аудит


def approval_for(
    storage: Storage, task: Task, status: ApprovalStatus = ApprovalStatus.PENDING
) -> ApprovalRequest:
    call = ToolCall(
        id=ToolCallId(storage.ids.next_child_id(task.id, "call")),
        task_id=task.id,
        tool_id=ToolId("test.write"),
        arguments={"path": "a.txt"},
        target=HOST,
    )
    return ApprovalRequest(
        id=storage.ids.next_child_id(task.id, "appr"),
        task_id=task.id,
        call=call,
        summary="Записать a.txt",
        effects=[ToolEffect(kind=EffectKind.WRITE, resource="/ws/a.txt")],
        target=HOST,
        arguments={"path": "/ws/a.txt"},
        preview_fingerprint="f" * 64,
        status=status,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def test_approval_round_trip_and_status_compare_and_set(storage: Storage) -> None:
    task = add(storage)
    first, second = approval_for(storage, task), approval_for(storage, task)
    with storage.unit_of_work() as uow:
        uow.approvals.add(second)
        uow.approvals.add(first)
        uow.commit()
    with storage.unit_of_work() as uow:
        assert uow.approvals.get(first.id) == first
        assert [item.id for item in uow.approvals.for_task(task.id)] == [first.id, second.id]
        with pytest.raises(StorageError):
            uow.approvals.add(first)
    approved = first.model_copy(
        update={"status": ApprovalStatus.APPROVED, "resolved_at": NOW, "resolved_via": "cli"}
    )
    with storage.unit_of_work() as uow:
        with pytest.raises(ConcurrentModification):
            uow.approvals.save(approved, expected=ApprovalStatus.APPROVED)
        uow.approvals.save(approved, expected=ApprovalStatus.PENDING)
        uow.commit()
    with storage.unit_of_work() as uow:
        assert uow.approvals.get(first.id) == approved
    with storage.unit_of_work() as uow, pytest.raises(ApprovalNotFound):
        uow.approvals.get("task_1.appr_99")


def test_two_resolutions_of_one_approval_conflict(storage: Storage) -> None:
    task = add(storage)
    pending = approval_for(storage, task)
    with storage.unit_of_work() as uow:
        uow.approvals.add(pending)
        uow.commit()
    approve = pending.model_copy(
        update={"status": ApprovalStatus.APPROVED, "resolved_at": NOW, "resolved_via": "a"}
    )
    deny = pending.model_copy(
        update={"status": ApprovalStatus.DENIED, "resolved_at": NOW, "resolved_via": "b"}
    )
    first, second = storage.unit_of_work(), storage.unit_of_work()
    with first, second:
        first.approvals.save(approve, expected=ApprovalStatus.PENDING)
        second.approvals.save(deny, expected=ApprovalStatus.PENDING)
        first.commit()
        with pytest.raises(ConcurrentModification):
            second.commit()
    with storage.unit_of_work() as uow:
        assert uow.approvals.get(pending.id).status is ApprovalStatus.APPROVED


def test_audit_is_append_only_and_ordered(storage: Storage) -> None:
    task = add(storage)
    other = add(storage)
    records = [
        AuditRecord(
            ts=NOW + timedelta(seconds=n),
            action=AuditAction.DECISION if n % 2 == 0 else AuditAction.RESULT,
            actor="task",
            task_id=owner.id,
            tool_call_id=ToolCallId(f"{owner.id}.call_{n + 1}"),
            tool_id=ToolId("filesystem.list"),
            target="host:posix:local",
            effects=[EffectKind.READ],
            resources=["/ws"],
            arguments_hash="h" * 64,
            decision=PolicyOutcome.ALLOW,
            execution_status="succeeded" if n % 2 else None,
        )
        for n, owner in enumerate([task, task, other, task])
    ]
    with storage.unit_of_work() as uow:
        for record in records[:2]:
            uow.audit.append(record)
        uow.commit()
    with storage.unit_of_work() as uow:
        for record in records[2:]:
            uow.audit.append(record)
        uow.commit()
    with storage.unit_of_work() as uow:
        assert uow.audit.list() == records
        assert uow.audit.list(task_id=task.id) == [records[0], records[1], records[3]]
