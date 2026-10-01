"""Конкуренция за одну базу SQLite: несколько соединений, потоков и процессов.

Проверяются итоги, которые не зависят от порядка планирования: уникальность ID, ровно одно
восстановление каждой задачи, согласованное чтение, ожидание короткой блокировки вместо ошибки.
"""

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.adapters.sqlite.migrate import bundled_migrations
from jarvis.app.composition import build_app
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.trace import EventKind
from jarvis.evals.scripted import ScriptedStages
from tests.helpers import S, budget_config, request, step

ALLOCATE = """
import json, sys
from pathlib import Path
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.domain.ids import TaskId
with SqliteStorage(Path(sys.argv[1])) as storage:
    tasks = [storage.ids.next_task_id() for _ in range(int(sys.argv[2]))]
    children = [storage.ids.next_child_id(TaskId("task_1"), "ev") for _ in range(int(sys.argv[2]))]
print(json.dumps([tasks, children]))
"""

RECOVER = """
import json, sys
from pathlib import Path
from jarvis.adapters.sqlite import SqliteStorage
from jarvis.app.composition import build_app
from jarvis.domain.settings import JarvisConfig
with SqliteStorage(Path(sys.argv[1])) as storage:
    recovered = build_app(JarvisConfig(), stages={}, storage=storage).tasks.recover_interrupted()
print(json.dumps(recovered))
"""


def spawn_all(script: str, *args: str, count: int) -> list[str]:
    processes = [
        subprocess.Popen([sys.executable, "-c", script, *args], stdout=subprocess.PIPE, text=True)
        for _ in range(count)
    ]
    outputs = [process.communicate(timeout=120)[0] for process in processes]
    assert [process.returncode for process in processes] == [0] * count
    return outputs


def test_id_allocation_from_interleaved_connections(tmp_path: Path) -> None:
    with SqliteStorage(tmp_path / "jarvis.db") as first, SqliteStorage(tmp_path / "jarvis.db") as second:
        allocated = [storage.ids.next_task_id() for _ in range(50) for storage in (first, second)]
    assert allocated == [f"task_{n}" for n in range(1, 101)]


def test_id_allocation_from_many_processes(tmp_path: Path) -> None:
    database = tmp_path / "jarvis.db"
    with SqliteStorage(database):
        pass
    outputs = spawn_all(ALLOCATE, str(database), "150", count=4)
    tasks = [item for output in outputs for item in json.loads(output)[0]]
    children = [item for output in outputs for item in json.loads(output)[1]]
    assert sorted(tasks, key=lambda value: int(value.split("_")[1])) == [f"task_{n}" for n in range(1, 601)]
    assert len(set(children)) == 600  # ни одного повтора, хотя процессы писали одновременно


def test_concurrent_first_open_migrates_once(tmp_path: Path) -> None:
    database = tmp_path / "fresh" / "jarvis.db"
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def open_database() -> None:
        barrier.wait()
        try:
            SqliteStorage(database).close()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_database) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    with sqlite3.connect(database) as conn:
        applied = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert applied == [migration.version for migration in bundled_migrations()]
    conn.close()


def test_each_interrupted_task_is_recovered_exactly_once(tmp_path: Path) -> None:
    database = tmp_path / "jarvis.db"
    long_ago = ManualClock()
    with SqliteStorage(database) as storage:
        dead = build_app(JarvisConfig(), stages={}, storage=storage, owner="dead", clock=long_ago)
        task_ids = [dead.tasks.submit(request()) for _ in range(20)]
    long_ago.advance(timedelta(days=1).total_seconds())  # аренды давно истекли

    outputs = spawn_all(RECOVER, str(database), count=4)  # четыре процесса восстанавливают разом
    recovered = [task for output in outputs for task in json.loads(output)]
    assert sorted(recovered) == sorted(task_ids)  # каждую задачу — ровно один процесс
    with SqliteStorage(database) as storage:
        tasks = build_app(JarvisConfig(), stages={}, storage=storage).tasks
        for task_id in task_ids:
            inspection = tasks.inspect(TaskId(task_id))
            assert inspection.task.status is S.FAILED
            finals = [event for event in inspection.events if event.kind is EventKind.TASK_FINISHED]
            assert len(finals) == 1


def test_readers_see_consistent_checkpoints_while_a_writer_works(tmp_path: Path) -> None:
    database = tmp_path / "jarvis.db"
    loops = 60
    steps = [
        step(S.ROUTING, S.PLANNING, route="agent"),
        step(S.PLANNING, S.EXECUTING),
        *[step(S.EXECUTING, S.REPLANNING), step(S.REPLANNING, S.EXECUTING)] * loops,
        step(S.EXECUTING, S.VERIFYING),
        step(S.VERIFYING, S.COMPLETED),
    ]
    config = budget_config(max_replans=loops * 2)
    with SqliteStorage(database) as storage:
        writer_app = build_app(config, stages=ScriptedStages(steps).handlers(), storage=storage, owner="W")
        task_id = writer_app.tasks.submit(request())
    done = threading.Event()
    mismatches: list[str] = []

    def write() -> None:
        try:
            with SqliteStorage(database) as storage:
                app = build_app(config, stages=ScriptedStages(steps).handlers(), storage=storage, owner="W")
                asyncio.run(app.tasks.run_until_blocked(task_id))
        finally:
            done.set()

    def read() -> None:
        with SqliteStorage(database) as storage:
            tasks = build_app(config, stages={}, storage=storage, owner="R").tasks
            while not done.is_set():
                inspection = tasks.inspect(task_id)
                moves = [e.payload["to"] for e in inspection.events if e.kind is EventKind.TASK_TRANSITION]
                if moves and moves[-1] != inspection.task.status.value:
                    mismatches.append(f"{inspection.task.status} vs {moves[-1]}")

    writer = threading.Thread(target=write)
    readers = [threading.Thread(target=read) for _ in range(2)]
    for thread in [*readers, writer]:
        thread.start()
    for thread in [writer, *readers]:
        thread.join(timeout=120)
    assert mismatches == []  # задача и трасса в каждом снимке согласованы
    with SqliteStorage(database) as storage:
        assert build_app(config, stages={}, storage=storage).tasks.get(task_id).status is S.COMPLETED


def test_short_lock_is_waited_out(tmp_path: Path) -> None:
    database = tmp_path / "jarvis.db"
    with SqliteStorage(database, busy_timeout_s=5) as storage:
        app = build_app(JarvisConfig(), stages={}, storage=storage, owner="A")
        blocker = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        release = threading.Timer(0.3, lambda: blocker.execute("ROLLBACK"))
        release.start()
        try:
            task_id = app.tasks.submit(request())  # ждёт освобождения базы, а не падает
        finally:
            release.join()
            blocker.close()
        assert app.tasks.get(task_id).status is S.CREATED
