"""Контракт хранилища. В M2 тот же набор пройдёт SQLite-реализация."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from jarvis.adapters.memory import InMemoryStorage
from jarvis.domain.budget import BudgetUsage
from jarvis.domain.errors import ConcurrentModification, StorageError, TaskNotFound
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import BudgetsSettings
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, Task, TaskRequest
from jarvis.domain.trace import EventKind, TraceEvent

NOW = datetime(2026, 1, 1, tzinfo=UTC)

STORAGES: list[Callable[[], InMemoryStorage]] = [InMemoryStorage]


@pytest.fixture(params=STORAGES, ids=lambda factory: factory.__name__)
def storage(request: pytest.FixtureRequest) -> InMemoryStorage:
    return request.param()


def new_task(task_id: TaskId) -> Task:
    return Task(
        id=task_id,
        version=1,
        request=TaskRequest(text="текст", origin=Origin.CLI),
        status=TaskStatus.CREATED,
        budget=BudgetsSettings().routing,
        usage=BudgetUsage(),
        created_at=NOW,
        updated_at=NOW,
    )


def new_event(storage: InMemoryStorage, task_id: TaskId) -> TraceEvent:
    event_id = storage.ids.next_child_id(task_id, "ev")
    return TraceEvent(
        id=event_id,
        task_id=task_id,
        seq=int(event_id.rsplit("_", 1)[1]),
        ts=NOW,
        kind=EventKind.TASK_CREATED,
        payload={},
    )


def bumped(task: Task, status: TaskStatus) -> Task:
    return task.model_copy(update={"version": task.version + 1, "status": status})


def add(storage: InMemoryStorage) -> Task:
    task = new_task(storage.ids.next_task_id())
    with storage.unit_of_work() as uow:
        uow.tasks.add(task)
        uow.commit()
    return task


def test_task_round_trip(storage: InMemoryStorage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow:
        assert uow.tasks.get(task.id) == task


def test_unknown_task_raises(storage: InMemoryStorage) -> None:
    with storage.unit_of_work() as uow, pytest.raises(TaskNotFound):
        uow.tasks.get(TaskId("task_99"))


def test_duplicate_task_raises(storage: InMemoryStorage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow, pytest.raises(StorageError):
        uow.tasks.add(task)


def test_save_checks_expected_version(storage: InMemoryStorage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow, pytest.raises(ConcurrentModification):
        uow.tasks.save(bumped(bumped(task, TaskStatus.ROUTING), TaskStatus.PLANNING), expected_version=2)
    with storage.unit_of_work() as uow, pytest.raises(ValueError, match="новая версия"):
        uow.tasks.save(task, expected_version=1)


def test_concurrent_writer_is_detected_at_commit(storage: InMemoryStorage) -> None:
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


def test_uncommitted_work_is_discarded(storage: InMemoryStorage) -> None:
    task = add(storage)
    with storage.unit_of_work() as uow:
        uow.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
        uow.trace.append([new_event(storage, task.id)])
        assert uow.tasks.get(task.id).status is TaskStatus.ROUTING  # своя запись видна до commit
    with storage.unit_of_work() as uow:
        assert uow.tasks.get(task.id) == task
        assert uow.trace.list(task.id) == []


def test_failed_commit_writes_nothing(storage: InMemoryStorage) -> None:
    task = add(storage)
    storage.fail_commit()
    with storage.unit_of_work() as uow:
        uow.tasks.save(bumped(task, TaskStatus.ROUTING), expected_version=1)
        uow.trace.append([new_event(storage, task.id)])
        with pytest.raises(StorageError):
            uow.commit()
    with storage.unit_of_work() as uow:
        assert uow.tasks.get(task.id) == task
        assert uow.trace.list(task.id) == []


def test_fault_injection_can_skip_commits(storage: InMemoryStorage) -> None:
    storage.fail_commit(after=1)
    add(storage)
    with pytest.raises(StorageError):
        add(storage)
    add(storage)


def test_trace_is_ordered_and_filterable(storage: InMemoryStorage) -> None:
    task = add(storage)
    events = [new_event(storage, task.id) for _ in range(3)]
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


def test_id_allocator_never_reuses_numbers(storage: InMemoryStorage) -> None:
    ids = storage.ids
    assert [ids.next_task_id() for _ in range(3)] == ["task_1", "task_2", "task_3"]
    assert ids.next_child_id(TaskId("task_1"), "ev") == "task_1.ev_1"
    assert ids.next_child_id(TaskId("task_1"), "ev") == "task_1.ev_2"
    assert ids.next_child_id(TaskId("task_1"), "call") == "task_1.call_1"
    assert ids.next_child_id(TaskId("task_2"), "ev") == "task_2.ev_1"

    storage.fail_commit()
    with storage.unit_of_work() as uow:
        uow.trace.append([new_event(storage, TaskId("task_1"))])  # task_1.ev_3
        with pytest.raises(StorageError):
            uow.commit()
    assert ids.next_child_id(TaskId("task_1"), "ev") == "task_1.ev_4"  # откат не вернул номер
