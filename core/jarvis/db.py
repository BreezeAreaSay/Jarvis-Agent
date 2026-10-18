"""SQLite ядра: история команд, чаты, память, заметки, задачи, календарь, напоминания, свои команды, столы.

Один файл <data>/jarvis.db (WAL). Запросы короткие, поэтому соединение одно, под замком, и вызывается прямо
из async-кода. Схема — список миграций MIGRATIONS: новая миграция дописывается в конец.
"""

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1 — начальная схема
    """
    CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        source TEXT NOT NULL,          -- voice | text | ui | remote | telegram | ai | plugin
        text TEXT NOT NULL,            -- что сказал или ввёл человек
        kind TEXT NOT NULL,            -- command | ai | unknown | system
        title TEXT NOT NULL DEFAULT '',-- что выполнено («Включить музыку»)
        status TEXT NOT NULL,          -- ok | error | pending | cancelled | denied
        detail TEXT NOT NULL DEFAULT '',
        icon TEXT NOT NULL DEFAULT '', -- ключ иконки (spotify, chrome, system…)
        command_id TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX history_ts ON history(ts);

    CREATE TABLE chats (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'chat',   -- chat | voice
        mode TEXT NOT NULL DEFAULT 'all',    -- chat | all | agent
        model TEXT NOT NULL DEFAULT '',
        pinned INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL,
        updated REAL NOT NULL
    );
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        role TEXT NOT NULL,                  -- system | user | assistant | tool
        content TEXT NOT NULL DEFAULT '',
        tool_calls TEXT,                     -- JSON (assistant)
        tool_call_id TEXT,                   -- (tool)
        name TEXT,
        meta TEXT,                           -- JSON: sources, cards, usage, images
        created REAL NOT NULL
    );
    CREATE INDEX messages_chat ON messages(chat_id, created);

    CREATE TABLE memory (
        id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'ai',   -- ai | user
        created REAL NOT NULL
    );

    CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        pinned INTEGER NOT NULL DEFAULT 0,
        color TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        updated REAL NOT NULL
    );

    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        due REAL,
        position INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL,
        completed REAL
    );

    CREATE TABLE events (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        start REAL NOT NULL,
        end REAL,
        all_day INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        remind_min INTEGER,
        reminded INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL
    );
    CREATE INDEX events_start ON events(start);

    CREATE TABLE reminders (
        id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        at REAL NOT NULL,
        repeat TEXT,                         -- NULL | daily | weekly
        done INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL
    );
    CREATE INDEX reminders_at ON reminders(done, at);

    CREATE TABLE commands (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,                  -- JSON команды (см. jarvis.commands.model)
        updated REAL NOT NULL
    );

    CREATE TABLE desks (
        id TEXT PRIMARY KEY,
        position INTEGER NOT NULL DEFAULT 0,
        data TEXT NOT NULL                   -- JSON стола: название, иконка, виджеты и их места
    );

    CREATE TABLE usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0
    );

    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2 — память: вид записи и срок жизни
    """
    ALTER TABLE memory ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact';  -- fact | assumption | temp
    ALTER TABLE memory ADD COLUMN expires REAL;                       -- temp: когда забыть (unix-время)
    """,
]


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # удалённое (чаты, память, история) затирается в файле, а не остаётся на диске до перезаписи
        self._conn.execute("PRAGMA secure_delete=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for index, script in enumerate(MIGRATIONS, start=1):
                if index <= current:
                    continue
                self._conn.execute("BEGIN")
                try:
                    self._run_script(script)
                    self._conn.execute(f"PRAGMA user_version = {index}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def _run_script(self, script: str) -> None:
        # executescript() сам делает COMMIT — выполняем операторы по одному внутри своей транзакции
        for statement in _split_sql(script):
            self._conn.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
            return int(cur.lastrowid or 0)

    def upsert(self, table: str, values: dict[str, Any], key: str = "id") -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        updates = ", ".join(f"{c}=excluded.{c}" for c in values if c != key)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({key}) DO UPDATE SET {updates}",
                list(values.values()),
            )

    def update(self, table: str, key_value: Any, values: dict[str, Any], key: str = "id") -> int:
        if not values:
            return 0
        sets = ", ".join(f"{c}=?" for c in values)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {table} SET {sets} WHERE {key}=?", [*values.values(), key_value]
            )
            return cur.rowcount

    def delete(self, table: str, key_value: Any, key: str = "id") -> int:
        with self._lock:
            return self._conn.execute(f"DELETE FROM {table} WHERE {key}=?", (key_value,)).rowcount

    # --- kv ---------------------------------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.upsert("kv", {"key": key, "value": dumps(value)}, key="key")


def _split_sql(script: str) -> list[str]:
    statements = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0] if "--" in line else line
        buf.append(stripped)
        if stripped.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.strip(";").strip():
                statements.append(stmt)
            buf = []
    rest = "\n".join(buf).strip()
    if rest:
        statements.append(rest)
    return statements
