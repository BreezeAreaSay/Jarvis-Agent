"""Проверка на здравый смысл, а не бенчмарк: тысячи событий и сотни задач не должны приводить к
квадратичному росту времени записи или чтения. Пороги щедрые — ловится порядок, а не проценты."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.app.composition import App, build_app
from jarvis.domain.ids import TaskId, child_number
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import ACTIVE_STATUSES
from jarvis.domain.trace import EventKind, TraceEvent
from tests.helpers import request

EVENTS = 3000
TASKS = 300
NOW = datetime(2026, 1, 1, tzinfo=UTC)

Storage = InMemoryStorage | SqliteStorage


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    if request.param == "memory":
        yield InMemoryStorage()
        return
    with SqliteStorage(tmp_path / "jarvis.db") as sqlite:
        yield sqlite


def append_one(storage: Storage, task_id: TaskId, n: int) -> None:
    event_id = storage.ids.next_child_id(task_id, "ev")
    event = TraceEvent(
        id=event_id,
        task_id=task_id,
        seq=child_number(event_id),
        ts=NOW,
        kind=EventKind.ERROR,
        payload={"category": "c", "disposition": "fatal", "message": f"событие {n}"},
    )
    with storage.unit_of_work() as uow:  # одно событие — одна запись, как у контрольных точек
        uow.trace.append([event])
        uow.commit()


def timed(action: object) -> float:
    started = time.perf_counter()
    action()  # type: ignore[operator]
    return time.perf_counter() - started


def test_thousands_of_events_in_one_task(storage: Storage) -> None:
    app = build_app(JarvisConfig(), stages={}, storage=storage, owner="A")
    task_id = app.tasks.submit(request())
    chunk = EVENTS // 6
    durations = [timed(lambda: [append_one(storage, task_id, n) for n in range(chunk)]) for _ in range(6)]
    # Запись не замедляется с ростом трассы: последние куски не в разы дольше первого.
    assert max(durations[-2:]) < 4 * max(durations[0], 0.05), durations

    read = timed(lambda: app.tasks.inspect(task_id))
    assert len(app.tasks.inspect(task_id).events) == EVENTS + 1
    assert read < 1.0, read


def test_hundreds_of_tasks(storage: Storage) -> None:
    app: App = build_app(JarvisConfig(), stages={}, storage=storage, owner="A")
    created = timed(lambda: [app.tasks.submit(request()) for _ in range(TASKS)])
    assert created < 15.0, created

    assert timed(lambda: app.tasks.list_tasks(limit=20)) < 0.3
    assert timed(lambda: app.tasks.list_tasks(statuses=ACTIVE_STATUSES)) < 1.0
    assert len(app.tasks.list_tasks(statuses=ACTIVE_STATUSES)) == TASKS
    assert timed(lambda: app.tasks.recover_interrupted()) < 2.0  # все аренды живы: ничего не трогаем
    assert timed(lambda: app.tasks.trace(TaskId(f"task_{TASKS // 2}"))) < 0.1
