"""Повреждённая и чужая база, миграции, блокировки: понятная ошибка и никакого «ремонта»."""

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis.adapters.sqlite import Migration, SqliteStorage, storage_error
from jarvis.adapters.sqlite import migrate as migrate_module
from jarvis.adapters.sqlite.migrate import bundled_migrations, check_sequence
from jarvis.app.composition import build_app
from jarvis.domain.budget import BudgetUsage
from jarvis.domain.errors import StorageError
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import BudgetsSettings, JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, Task, TaskRequest
from tests.helpers import request

V1 = Migration(
    1,
    "base",
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,\n"
    "  applied_at TEXT NOT NULL);\n"
    "CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL);",
)
V2 = Migration(2, "tags", "ALTER TABLE notes ADD COLUMN tag TEXT;\nCREATE INDEX notes_tag ON notes (tag);")
BROKEN = Migration(2, "broken", "CREATE TABLE half (id INTEGER);\nTHIS IS NOT SQL;")


def versions(path: Path) -> list[int]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    conn.close()
    return [row[0] for row in rows]


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    return names


def test_new_database_gets_the_full_schema(tmp_path: Path) -> None:
    path = tmp_path / "data" / "jarvis.db"
    with SqliteStorage(path) as storage:
        assert storage.schema_version == len(bundled_migrations())
    assert {"schema_migrations", "tasks", "trace_events", "id_counters", "task_leases"} <= tables(path)
    assert versions(path) == [migration.version for migration in bundled_migrations()]
    with SqliteStorage(path):  # повторное открытие ничего не меняет и копий не делает
        pass
    assert not list(tmp_path.rglob("*.bak"))


def test_old_database_is_upgraded_with_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path, migrations=[V1]):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO notes (text) VALUES ('сохранится')")
    conn.close()

    with SqliteStorage(path, migrations=[V1, V2]) as storage:
        assert storage.schema_version == 2
    assert versions(path) == [1, 2]
    backup = path.with_name("jarvis.db.v1.bak")
    assert backup.is_file()
    assert versions(backup) == [1]  # копия — состояние до миграции
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT text, tag FROM notes").fetchall() == [("сохранится", None)]
    conn.close()


def test_failed_migration_rolls_back_and_explains(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path, migrations=[V1]):
        pass
    with pytest.raises(StorageError) as raised:
        SqliteStorage(path, migrations=[V1, BROKEN])
    assert "миграция 002 (broken) не применилась" in raised.value.message
    assert "осталась на версии 1" in raised.value.message
    assert versions(path) == [1]
    assert "half" not in tables(path)  # транзакция миграции откатилась целиком
    with SqliteStorage(path, migrations=[V1]) as storage:  # база осталась рабочей
        assert storage.schema_version == 1


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path, migrations=[V1, V2]):
        pass
    with pytest.raises(StorageError) as raised:
        SqliteStorage(path, migrations=[V1])
    assert "схема версии 2 новее, чем поддерживает эта версия Jarvis (1)" in raised.value.message


def test_file_that_is_not_a_database(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    path.write_bytes(b"not a database, just text\n" * 200)
    with pytest.raises(StorageError, match="не база SQLite"):
        SqliteStorage(path)
    assert path.read_bytes().startswith(b"not a database")  # файл не тронут


def test_foreign_sqlite_database(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE photos (id INTEGER)")
    conn.close()
    with pytest.raises(StorageError, match="не база Jarvis"):
        SqliteStorage(path)


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        ("DROP TABLE task_leases", "нет таблицы task_leases"),
        ("ALTER TABLE tasks DROP COLUMN outcome_json", "таблица tasks не совпадает"),
    ],
)
def test_damaged_schema_is_refused(tmp_path: Path, damage: str, problem: str) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute(damage)
    conn.close()
    with pytest.raises(StorageError) as raised:
        SqliteStorage(path)
    assert problem in raised.value.message
    assert "не будет использоваться" in raised.value.message


def test_corrupted_pages_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path) as storage:
        app = build_app(JarvisConfig(), stages={}, storage=storage, owner="A")
        for _ in range(50):
            app.tasks.submit(request("заполнить несколько страниц " * 20))
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    data = bytearray(path.read_bytes())
    page = 4096
    data[2 * page : len(data) - page] = b"\xa5" * (len(data) - 3 * page)  # испортить страницы данных
    path.write_bytes(bytes(data))
    with pytest.raises(StorageError) as raised, SqliteStorage(path) as broken:
        build_app(JarvisConfig(), stages={}, storage=broken).tasks.list_tasks()
    assert any(word in raised.value.message for word in ("повреждён", "не база SQLite", "ошибка базы"))


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,  # type: ignore[attr-defined,unused-ignore]
    reason="права на файл: root их игнорирует, а на Windows атрибуты папок другие",
)
def test_read_only_database(tmp_path: Path) -> None:
    path = tmp_path / "data" / "jarvis.db"
    with SqliteStorage(path):
        pass
    path.parent.chmod(0o500)
    path.chmod(0o400)
    try:
        with (
            pytest.raises(StorageError, match=r"нет прав на запись|не удалось открыть"),
            SqliteStorage(path) as db,
        ):
            build_app(JarvisConfig(), stages={}, storage=db).tasks.submit(request())
    finally:
        path.parent.chmod(0o700)
        path.chmod(0o600)


def test_locked_database_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path, busy_timeout_s=0.1) as storage:
        app = build_app(JarvisConfig(), stages={}, storage=storage, owner="A")
        blocker = sqlite3.connect(path, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")  # другой процесс держит запись
        try:
            with pytest.raises(StorageError, match="занята другим процессом"):
                app.tasks.submit(request())
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        app.tasks.submit(request())  # блокировка снята — работа продолжается


def test_error_translation_table() -> None:
    path = Path("x.db")
    cases = {
        "file is not a database": "не база SQLite",
        "database disk image is malformed": "повреждён",
        "attempt to write a readonly database": "нет прав на запись",
        "database is locked": "занята другим процессом",
        "unable to open database file": "не удалось открыть",
        "something else": "ошибка базы",
    }
    for text, expected in cases.items():
        assert expected in storage_error(sqlite3.OperationalError(text), path).message


def test_migration_numbers_must_be_contiguous() -> None:
    with pytest.raises(ValueError, match="подряд"):
        check_sequence([V1, Migration(3, "gap", "SELECT 1;")])


PARENT_CHILD = Migration(
    1,
    "tree",
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,\n"
    "  applied_at TEXT NOT NULL);\n"
    "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
    "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent (id));",
)
REBUILD_PARENT = Migration(
    2,
    "rebuild",
    "CREATE TABLE parent_new (id INTEGER PRIMARY KEY, label TEXT);\n"
    "INSERT INTO parent_new (id) SELECT id FROM parent;\n"
    "DROP TABLE parent;\n"
    "ALTER TABLE parent_new RENAME TO parent;",
)
ORPHANS = Migration(2, "orphans", "DELETE FROM parent;")


def seed_tree(path: Path) -> None:
    with SqliteStorage(path, migrations=[PARENT_CHILD]):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO parent (id) VALUES (1)")
        conn.execute("INSERT INTO child (id, parent_id) VALUES (10, 1)")
    conn.close()


def test_migration_can_rebuild_a_referenced_table(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    seed_tree(path)
    with SqliteStorage(path, migrations=[PARENT_CHILD, REBUILD_PARENT]) as storage:
        assert storage.schema_version == 2


def test_migration_that_breaks_links_is_rolled_back(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    seed_tree(path)
    with pytest.raises(StorageError, match="нарушены связи"):
        SqliteStorage(path, migrations=[PARENT_CHILD, ORPHANS])
    assert versions(path) == [1]


def test_upgrade_seen_late_does_not_touch_the_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Гонка из ревью: процесс B прочитал версию 1, пока A обновлял базу до 2. Раньше B затем снимал
    «копию v1» уже с обновлённой базы поверх настоящей. Теперь решение принимается под блокировкой."""
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path, migrations=[V1]):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO notes (text) VALUES ('до миграции')")
    conn.close()
    destructive = Migration(2, "wipe", "DELETE FROM notes;\nALTER TABLE notes ADD COLUMN tag TEXT;")
    with SqliteStorage(path, migrations=[V1, destructive]):  # процесс A обновил базу
        pass

    real = migrate_module.current_version
    reads = 0

    def stale_first_read(conn: sqlite3.Connection, where: Path) -> int:
        nonlocal reads
        reads += 1
        return 1 if reads == 1 else real(conn, where)  # B видит версию до обновления A

    monkeypatch.setattr(migrate_module, "current_version", stale_first_read)
    with SqliteStorage(path, migrations=[V1, destructive]) as late:  # процесс B
        assert late.schema_version == 2
    assert sorted(item.name for item in tmp_path.glob("*.bak")) == ["jarvis.db.v1.bak"]
    with sqlite3.connect(tmp_path / "jarvis.db.v1.bak") as conn:
        assert conn.execute("SELECT text FROM notes").fetchall() == [("до миграции",)]
    conn.close()


def test_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path, migrations=[V1]):
        pass
    older = tmp_path / "jarvis.db.v1.bak"
    older.write_bytes(b"backup from an earlier attempt")
    with SqliteStorage(path, migrations=[V1, V2]):
        pass
    assert older.read_bytes() == b"backup from an earlier attempt"
    assert versions(tmp_path / "jarvis.db.v1.2.bak") == [1]
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        ("DROP INDEX tasks_status_seq", "нет индекса tasks_status_seq"),
        (
            "ALTER TABLE task_leases RENAME TO old_leases;\n"
            "CREATE TABLE task_leases (task_id TEXT PRIMARY KEY, owner TEXT NOT NULL,"
            " expires_at TEXT NOT NULL) STRICT;\n"
            "DROP TABLE old_leases;",
            "таблица task_leases не совпадает",  # те же столбцы, но без внешнего ключа
        ),
    ],
)
def test_lost_constraints_are_detected(tmp_path: Path, damage: str, problem: str) -> None:
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.executescript(damage)
    conn.close()
    with pytest.raises(StorageError) as raised:
        SqliteStorage(path)
    assert problem in raised.value.message


def test_full_disk_is_reported_as_such(tmp_path: Path) -> None:
    with SqliteStorage(tmp_path / "jarvis.db") as storage:
        conn = storage.take()
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count = {pages}")  # «диск» заполнен
        storage.give(conn)  # следующая единица работы получит это соединение
        app = build_app(JarvisConfig(), stages={}, storage=storage, owner="A")

        def fill() -> None:
            for _ in range(200):
                app.tasks.submit(request("большой запрос " * 200))

        with pytest.raises(StorageError) as raised:
            fill()
    assert "нет места" in raised.value.message
    assert "rollback" not in raised.value.message


def test_session_3_database_is_upgraded_and_keeps_its_tasks(tmp_path: Path) -> None:
    """База Session 3 (миграции 001–002) с задачей: после 003 задача читается, рабочей памяти у неё нет."""
    path = tmp_path / "jarvis.db"
    with SqliteStorage(path, migrations=bundled_migrations()[:2]):
        pass
    task = Task(
        id=TaskId("task_1"),
        version=1,
        request=TaskRequest(text="старая задача", origin=Origin.EVAL),
        status=TaskStatus.CREATED,
        budget=BudgetsSettings().routing,
        usage=BudgetUsage(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with sqlite3.connect(path) as conn:  # строка в том виде, в каком её писала Session 3
        conn.execute(
            "INSERT INTO tasks (id, seq, version, status, route, request_json, budget_json, usage_json, "
            "outcome_json, created_at, updated_at) VALUES (?, 1, 1, ?, NULL, ?, ?, ?, NULL, ?, ?)",
            (
                task.id,
                task.status.value,
                task.request.model_dump_json(),
                task.budget.model_dump_json(),
                task.usage.model_dump_json(),
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )
    conn.close()
    with SqliteStorage(path) as storage:
        assert storage.schema_version == len(bundled_migrations())
        with storage.unit_of_work() as uow:
            assert uow.tasks.get(task.id) == task
            assert uow.model_calls.for_task(task.id) == []
    assert path.with_name("jarvis.db.v2.bak").is_file()
