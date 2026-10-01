"""Повреждённая и чужая база, миграции, блокировки: понятная ошибка и никакого «ремонта»."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from jarvis.adapters.sqlite import Migration, SqliteStorage, storage_error
from jarvis.adapters.sqlite.migrate import bundled_migrations, check_sequence
from jarvis.app.composition import build_app
from jarvis.domain.errors import StorageError
from jarvis.domain.settings import JarvisConfig
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
