"""Версии схемы: пронумерованные SQL-файлы `NNN_имя.sql`, каждый применяется своей транзакцией.

Новая база создаётся миграциями с нуля; старая догоняет недостающие (перед этим делается копия);
база новее, чем знает код, — ошибка. После миграций схема сверяется с эталоном, полученным теми же
миграциями на пустой базе в памяти: повреждённая схема не используется.
"""

import os
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
    _refuse_newer(path, version, latest)
    if version < latest:
        version = _upgrade(conn, path, migrations)
    if migrations:
        _check_schema(conn, path, migrations)
    return version


def _refuse_newer(path: Path, version: int, latest: int) -> None:
    if version > latest:
        raise StorageError(
            f"{path}: схема версии {version} новее, чем поддерживает эта версия Jarvis ({latest}); "
            "обновите Jarvis или укажите другой JARVIS_HOME",
            path=str(path),
            schema_version=version,
            supported=latest,
        )


def _upgrade(conn: sqlite3.Connection, path: Path, migrations: Sequence[Migration]) -> int:
    """Все недостающие миграции — одной транзакцией под блокировкой записи.

    Версия перечитывается уже под блокировкой: другой процесс мог обновить базу, пока мы ждали.
    Копия базы снимается там же — до изменений и так, что параллельное обновление её не перезапишет.
    Связи (foreign keys) на время миграции выключены, чтобы миграция могла перестроить таблицу, и
    проверяются целиком перед COMMIT.
    """
    conn.execute("PRAGMA foreign_keys = OFF")  # вне транзакции: внутри неё не переключается
    try:
        conn.execute("BEGIN IMMEDIATE")
        start = current_version(conn, path)
        migration: Migration | None = None
        try:
            _refuse_newer(path, start, migrations[-1].version)
            pending = [item for item in migrations if item.version > start]
            if pending and start > 0:
                _backup(path, start)
            for migration in pending:
                for statement in _statements(migration.sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, datetime.now(UTC).isoformat()),
                )
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise StorageError(f"{path}: после миграции нарушены связи между таблицами: {broken[:3]}")
            conn.execute("COMMIT")
            return max(start, migrations[-1].version)
        except BaseException as exc:
            if conn.in_transaction:  # SQLite мог уже откатить транзакцию сам (например, диск полон)
                conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error) and migration is not None:
                raise StorageError(
                    f"{path}: миграция {migration.version:03d} ({migration.name}) не применилась: {exc}; "
                    f"база осталась на версии {start}",
                    path=str(path),
                    migration=migration.version,
                ) from None
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


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


def _backup(path: Path, version: int) -> Path:
    """Копия базы до миграции: `jarvis.db.vN.bak`; существующие копии не перезаписываются.

    Копию читает отдельное соединение: вызывающий держит блокировку записи, поэтому до COMMIT
    никто не изменит базу, а чтение в WAL видит её состояние до миграции.
    """
    target = path.with_name(f"{path.name}.v{version}.bak")
    number = 1
    while target.exists():
        number += 1
        target = path.with_name(f"{path.name}.v{version}.{number}.bak")
    partial = target.with_name(target.name + ".partial")
    source = sqlite3.connect(path)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
        finally:
            copy.close()
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    return target


Signature = dict[tuple[str, str], str]


def _signature(conn: sqlite3.Connection) -> Signature:
    """Схема как текст: таблицы и индексы с ограничениями (UNIQUE, внешние ключи, STRICT)."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
    )
    return {(str(kind), str(name)): " ".join(str(sql).split()) for kind, name, sql in rows}


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
    kinds = {"table": "таблицы", "index": "индекса"}
    problems = [f"нет {kinds[kind]} {name}" for kind, name in expected if (kind, name) not in actual]
    problems += [
        f"{'таблица' if kind == 'table' else 'индекс'} {name} не совпадает со схемой"
        for (kind, name), sql in expected.items()
        if (kind, name) in actual and actual[(kind, name)] != sql
    ]
    if problems:
        raise StorageError(
            f"{path}: схема базы повреждена ({'; '.join(problems)}); база не будет использоваться",
            path=str(path),
        )
