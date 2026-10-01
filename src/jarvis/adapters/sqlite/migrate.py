"""Версии схемы: пронумерованные SQL-файлы `NNN_имя.sql`, каждый применяется своей транзакцией.

Новая база создаётся миграциями с нуля; старая догоняет недостающие (перед этим делается копия);
база новее, чем знает код, — ошибка. После миграций схема сверяется с эталоном, полученным теми же
миграциями на пустой базе в памяти: повреждённая схема не используется.
"""

import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from pathlib import Path

from jarvis.domain.errors import StorageError

_FILE = re.compile(r"(\d{3})_([a-z0-9_]+)\.sql")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def bundled_migrations() -> tuple[Migration, ...]:
    found: list[Migration] = []
    for entry in resources.files("jarvis.adapters.sqlite.migrations").iterdir():
        match = _FILE.fullmatch(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), match.group(2), entry.read_text(encoding="utf-8")))
    return check_sequence(found)


def check_sequence(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    if [migration.version for migration in ordered] != list(range(1, len(ordered) + 1)):
        raise ValueError(f"номера миграций должны идти подряд с 1: {[m.version for m in ordered]}")
    return ordered


def current_version(conn: sqlite3.Connection, path: Path) -> int:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "schema_migrations" in tables:
        return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    if tables:
        raise StorageError(
            f"{path}: это база SQLite, но не база Jarvis (нет schema_migrations)", path=str(path)
        )
    return 0


def migrate(conn: sqlite3.Connection, path: Path, migrations: Sequence[Migration]) -> int:
    """Довести схему до последней версии. Возвращает итоговую версию."""
    latest = migrations[-1].version if migrations else 0
    version = current_version(conn, path)
    if version > latest:
        raise StorageError(
            f"{path}: схема версии {version} новее, чем поддерживает эта версия Jarvis ({latest}); "
            "обновите Jarvis или укажите другой JARVIS_HOME",
            path=str(path),
            schema_version=version,
            supported=latest,
        )
    pending = [migration for migration in migrations if migration.version > version]
    if pending and version > 0:
        _backup(conn, path.with_name(f"{path.name}.v{version}.bak"))
    for migration in pending:
        _apply(conn, path, migration)
    if migrations:
        _check_schema(conn, path, migrations)
    return max(version, latest)


def _apply(conn: sqlite3.Connection, path: Path, migration: Migration) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Другой процесс мог применить эту миграцию, пока мы ждали блокировку.
        if current_version(conn, path) >= migration.version:
            conn.execute("COMMIT")
            return
        for statement in _statements(migration.sql):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        raise StorageError(
            f"{path}: миграция {migration.version:03d} ({migration.name}) не применилась: {exc}; "
            f"база осталась на версии {migration.version - 1}",
            path=str(path),
            migration=migration.version,
        ) from None


def _statements(sql: str) -> Iterator[str]:
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer
            buffer = ""
    rest = [line for line in buffer.splitlines() if line.strip() and not line.strip().startswith("--")]
    if rest:
        raise ValueError(f"незавершённый SQL в миграции: {rest[0][:60]}")


def _backup(conn: sqlite3.Connection, target: Path) -> None:
    with sqlite3.connect(target) as copy:
        conn.backup(copy)
    copy.close()


Signature = dict[str, tuple[tuple[str, str, int, int], ...]]


def _signature(conn: sqlite3.Connection) -> Signature:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        table: tuple(
            (row[1], row[2], row[3], row[5]) for row in conn.execute(f"PRAGMA table_info('{table}')")
        )
        for table in tables
    }


@cache
def _expected(migrations: tuple[Migration, ...]) -> Signature:
    with sqlite3.connect(":memory:", isolation_level=None) as conn:
        for migration in migrations:
            for statement in _statements(migration.sql):
                conn.execute(statement)
        signature = _signature(conn)
    conn.close()
    return signature


def _check_schema(conn: sqlite3.Connection, path: Path, migrations: Sequence[Migration]) -> None:
    expected = _expected(tuple(migrations))
    actual = _signature(conn)
    problems = [f"нет таблицы {table}" for table in expected if table not in actual]
    problems += [
        f"таблица {table} не совпадает со схемой"
        for table, columns in expected.items()
        if table in actual and actual[table] != columns
    ]
    if problems:
        raise StorageError(
            f"{path}: схема базы повреждена ({'; '.join(problems)}); база не будет использоваться",
            path=str(path),
        )
