"""«Приватность и данные»: что Jarvis хранит на этом компьютере, сколько это занимает, очистка по категориям и
ежедневная очистка по срокам из settings.privacy.

Очистка по срокам идёт раз в сутки (первая — через минуту после запуска): старая история команд, чаты без
новых сообщений (закреплённые не трогаем), старые сообщения голосового чата, скрытые результаты инструментов и
картинки в старых сообщениях (текст переписки остаётся), записи созвонов «Конспекта» и журналы. Временные
записи памяти с истёкшим сроком удаляются всегда. Итог — событие data.purged {removed, ts}.

Удалённое из базы затирается в файле (PRAGMA secure_delete в jarvis.db). Журналы интерфейса (ui.log,
updater.log) пишет Electron в свою папку — их ядро не видит.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis import paths
from jarvis.db import dumps, loads
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError
from jarvis.settings import SECRET_NAMES

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

DAY_S = 24 * 3600
START_DELAY_S = 60.0  # первая очистка — когда всё запустилось
CHECK_S = 3600.0  # раз в час проверяем, не пора ли
KV_LAST = "retention:last"
TOOL_PLACEHOLDER = "(результат инструмента удалён: истёк срок хранения)"
LOG_CAP = 5_000_000  # журнал больше — оставляем последние LOG_KEEP байт
LOG_KEEP = 1_000_000
ACTIVE_LOGS = ("core.log", "crash.log")  # открыты ядром всё время: только обрезаем, не удаляем
CLIPBOARD_KEY = "clipboard:items"
FILE_INDEX = "file_search_index.json"
VOICE_CACHE = "tts"  # cache/tts — звук частых ответов голосом (jarvis.voice.tts)


@dataclass(frozen=True)
class Category:
    id: str
    title: str
    hint: str
    clearable: bool = True


CATEGORIES: tuple[Category, ...] = (
    Category("history", "История команд", "Что вы говорили и что выполнил Jarvis"),
    Category("chats", "Чаты Jarvis AI", "Переписка с ИИ, результаты инструментов, приложенные картинки"),
    Category(
        "voice_chats", "Голосовой чат, Telegram и пульт", "Что вы спрашивали у Jarvis AI голосом и с телефона"
    ),
    Category("memory", "Память", "Что Jarvis запомнил о вас по вашей просьбе"),
    Category("notes", "Заметки", "В том числе конспекты созвонов"),
    Category("tasks", "Задачи", "Список дел"),
    Category("events", "Календарь", "События и напоминания о них"),
    Category("reminders", "Напоминания", "«Напомни через час…»"),
    Category("clipboard", "История буфера обмена", "Что вы копировали (плагин «Умный буфер»)"),
    Category("summaries", "Записи созвонов", "Звук для «Конспекта видео и созвонов»"),
    Category("screenshots", "Снимки экрана Jarvis", "Папка данных Jarvis; снимки в «Изображениях» — ваши"),
    Category("file_index", "Индекс «Поиска по ПК»", "Список путей к файлам — соберётся заново сам"),
    Category("voice_cache", "Готовые ответы голосом", "Звук частых ответов («Готово», «Открываю…»)"),
    Category("usage", "Учёт расходов ИИ", "Сколько токенов и денег ушло на ИИ"),
    Category("logs", "Журналы ядра", "Что делал Jarvis и какие были ошибки — для поиска причин сбоев"),
    Category("secrets", "Ключи API и токены", "Ключи ИИ, Telegram, умного дома, пароль пульта (зашифрованы)"),
    Category("models", "Голосовые модели", "Удаляются в «Голос и распознавание»", clearable=False),
)
CLEARABLE = tuple(c.id for c in CATEGORIES if c.clearable)
_TABLES = {"notes": "notes", "tasks": "tasks", "events": "events", "reminders": "reminders"}


def _files(folder: Path, pattern: str = "*") -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in folder.glob(pattern) if p.is_file()]


def _size(files: list[Path]) -> int:
    total = 0
    for f in files:
        with contextlib.suppress(OSError):
            total += f.stat().st_size
    return total


def _tree_size(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return _size([p for p in folder.rglob("*") if p.is_file()])


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _truncate(path: Path, keep: int = 0) -> bool:
    """Обрезать файл (журнал, который сейчас открыт на запись): keep — сколько последних байт оставить."""
    try:
        with open(path, "r+b") as f:
            tail = b""
            if keep:
                size = f.seek(0, 2)
                f.seek(max(0, size - keep))
                tail = f.read()
                cut = tail.find(b"\n")
                tail = tail[cut + 1 :] if 0 <= cut < len(tail) - 1 else tail
            f.seek(0)
            f.truncate()
            if tail:
                f.write(tail)
        return True
    except OSError:
        return False


class Retention(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self.clock: Callable[[], float] = time.time  # тесты подменяют время
        self.start_delay_s = START_DELAY_S
        self.check_s = CHECK_S
        self._task: asyncio.Task[None] | None = None

    # --- жизненный цикл -------------------------------------------------------------------------------
    async def start(self) -> None:
        self._task = self.ctx.spawn(self._loop(), "retention")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        await asyncio.sleep(self.start_delay_s)
        while True:
            try:
                if self.due():
                    await self.purge()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("очистка по срокам хранения упала")
            await asyncio.sleep(self.check_s)

    def due(self) -> bool:
        last = float(self.ctx.db.kv_get(KV_LAST, 0) or 0)
        return self.clock() - last >= DAY_S

    def last_run(self) -> float | None:
        last = float(self.ctx.db.kv_get(KV_LAST, 0) or 0)
        return last or None

    # --- папки ----------------------------------------------------------------------------------------
    @staticmethod
    def summaries_dir() -> Path:
        return paths.data_dir() / "summaries"

    @staticmethod
    def screenshots_dir() -> Path:
        return paths.data_dir() / "screenshots"

    @staticmethod
    def logs_dir() -> Path:
        return paths.logs_dir()

    # --- что хранится -----------------------------------------------------------------------------------
    def _count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        row = self.ctx.db.query_one(sql, params)
        return int(next(iter(row.values())) or 0) if row else 0

    def _chat_stats(self, kind: str) -> tuple[int, int]:
        chats = self._count("SELECT COUNT(*) FROM chats WHERE kind=?", (kind,))
        size = self._count(
            "SELECT SUM(LENGTH(CAST(m.content AS BLOB)) + IFNULL(LENGTH(CAST(m.meta AS BLOB)), 0) "
            "+ IFNULL(LENGTH(CAST(m.tool_calls AS BLOB)), 0)) "
            "FROM messages m JOIN chats c ON c.id = m.chat_id WHERE c.kind=?",
            (kind,),
        )
        return chats, size

    def _stats(self, cid: str) -> tuple[int, int]:
        db = self.ctx.db
        if cid == "history":
            return self._count("SELECT COUNT(*) FROM history"), self._count(
                "SELECT SUM(LENGTH(CAST(text AS BLOB)) + LENGTH(CAST(title AS BLOB)) "
                "+ LENGTH(CAST(detail AS BLOB))) FROM history"
            )
        if cid == "chats":
            return self._chat_stats("chat")
        if cid == "voice_chats":
            return self._chat_stats("voice")
        if cid == "memory":
            return self._count("SELECT COUNT(*) FROM memory"), self._count(
                "SELECT SUM(LENGTH(CAST(text AS BLOB))) FROM memory"
            )
        if cid in _TABLES:
            table = _TABLES[cid]
            row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            text_cols = {
                "notes": "LENGTH(CAST(title AS BLOB)) + LENGTH(CAST(body AS BLOB))",
                "tasks": "LENGTH(CAST(title AS BLOB))",
                "events": "LENGTH(CAST(title AS BLOB)) + LENGTH(CAST(notes AS BLOB))",
                "reminders": "LENGTH(CAST(text AS BLOB))",
            }[cid]
            return int(row["n"] if row else 0), self._count(f"SELECT SUM({text_cols}) FROM {table}")
        if cid == "clipboard":
            items = db.kv_get(CLIPBOARD_KEY, []) or []
            return (len(items), len(dumps(items).encode())) if isinstance(items, list) else (0, 0)
        if cid == "summaries":
            files = _files(self.summaries_dir(), "*.wav")
            return len(files), _size(files)
        if cid == "screenshots":
            files = _files(self.screenshots_dir())
            return len(files), _size(files)
        if cid == "file_index":
            files = _files(paths.cache_dir(), FILE_INDEX)
            return len(files), _size(files)
        if cid == "voice_cache":
            files = _files(paths.cache_dir() / VOICE_CACHE, "*.pcm")
            return len(files), _size(files)
        if cid == "usage":
            return self._count("SELECT COUNT(*) FROM usage"), 0
        if cid == "logs":
            files = _files(self.logs_dir())
            return len(files), _size(files)
        if cid == "secrets":
            status = self.ctx.secrets.status()
            return sum(1 for name in SECRET_NAMES if status.get(name)), 0
        if cid == "models":
            folder = paths.data_dir() / "models"
            count = len([p for p in folder.iterdir() if p.is_dir()]) if folder.is_dir() else 0
            return count, _tree_size(folder)
        raise UserFacingError(f"Неизвестная категория данных: {cid}")

    def inventory(self) -> dict[str, Any]:
        """Что хранится: {dir, db_bytes, purged_at, items: [{id, title, hint, count, bytes, clearable}]}."""
        items = []
        for cat in CATEGORIES:
            try:
                count, size = self._stats(cat.id)
            except UserFacingError:
                raise
            except Exception:
                log.exception("данные «%s» не посчитаны", cat.id)
                count, size = 0, 0
            items.append(
                {
                    "id": cat.id,
                    "title": cat.title,
                    "hint": cat.hint,
                    "count": count,
                    "bytes": size,
                    "clearable": cat.clearable,
                }
            )
        db_path = Path(self.ctx.db.path)
        db_files = [db_path, db_path.with_name(db_path.name + "-wal")]
        return {
            "dir": str(paths.data_dir()),
            "logs_dir": str(self.logs_dir()),
            "db_bytes": _size([f for f in db_files if f.exists()]),
            "purged_at": self.last_run(),
            "items": items,
        }

    # --- очистка по категориям --------------------------------------------------------------------------
    async def clear(self, scopes: list[str]) -> dict[str, int]:
        """Удалить данные выбранных категорий ("all" — все, кроме моделей). Вернёт, сколько удалено."""
        wanted: list[str] = []
        for scope in scopes:
            if scope == "all":
                wanted.extend(CLEARABLE)
            elif scope in CLEARABLE:
                wanted.append(scope)
            else:
                raise UserFacingError(f"Эти данные так не удалить: {scope}")
        out: dict[str, int] = {}
        for cid in dict.fromkeys(wanted):
            try:
                out[cid] = await self._clear_one(cid)
            except UserFacingError:
                raise
            except Exception as e:
                log.exception("данные «%s» не удалены", cid)
                raise UserFacingError(f"Не удалось удалить «{_title(cid)}»: {e}") from e
        log.info("данные удалены по просьбе: %s", out)
        self.ctx.bus.publish("data.cleared", {"cleared": out, "ts": self.clock()})
        return out

    async def _clear_one(self, cid: str) -> int:
        db = self.ctx.db
        if cid == "history":
            count = db.execute("DELETE FROM history").rowcount
            self.ctx.bus.publish("history.cleared", {})
            return count
        if cid in ("chats", "voice_chats"):
            kind = "chat" if cid == "chats" else "voice"
            ids = [r["id"] for r in db.query("SELECT id FROM chats WHERE kind=?", (kind,))]
            for chat_id in ids:
                self._delete_chat(chat_id)
            if kind == "voice":
                db.execute("DELETE FROM kv WHERE key LIKE 'voice_chat:%'")
            return len(ids)
        if cid == "memory":
            return int(self.ctx.notes.memory_clear()) if hasattr(self.ctx.notes, "memory_clear") else 0
        if cid in _TABLES:
            count = db.execute(f"DELETE FROM {_TABLES[cid]}").rowcount
            self.ctx.bus.publish(f"{cid}.changed", {"op": "clear"})
            return count
        if cid == "clipboard":
            return await self._clear_clipboard()
        if cid == "summaries":
            return sum(_unlink(f) for f in _files(self.summaries_dir(), "*.wav"))
        if cid == "screenshots":
            return sum(_unlink(f) for f in _files(self.screenshots_dir()))
        if cid == "file_index":
            return sum(_unlink(f) for f in _files(paths.cache_dir(), FILE_INDEX))
        if cid == "voice_cache":
            cache = getattr(getattr(self.ctx, "tts", None), "cache", None)
            if cache is not None:
                cache.clear()
            return sum(_unlink(f) for f in _files(paths.cache_dir() / VOICE_CACHE, "*.pcm"))
        if cid == "usage":
            return db.execute("DELETE FROM usage").rowcount
        if cid == "logs":
            return self.clear_logs()
        if cid == "secrets":
            status = self.ctx.secrets.status()
            names = [name for name in SECRET_NAMES if status.get(name)]
            for name in names:
                self.ctx.secrets.set(name, "")
            return len(names)
        raise UserFacingError(f"Эти данные так не удалить: {cid}")

    def _delete_chat(self, chat_id: str) -> None:
        """Чат целиком — через Jarvis AI: вместе с ним забываются тред Codex и сессии программ-провайдеров."""
        delete = getattr(self.ctx.ai, "delete_chat", None)
        if callable(delete):
            delete(chat_id)
            return
        if self.ctx.db.delete("chats", chat_id):
            self.ctx.db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            self.ctx.bus.publish("chat.deleted", {"id": chat_id})

    async def _clear_clipboard(self) -> int:
        items = self.ctx.db.kv_get(CLIPBOARD_KEY, []) or []
        plugins = getattr(self.ctx, "plugins", None)
        running = getattr(plugins, "is_running", None)
        if callable(running) and running("clipboard"):
            result = await plugins.action("clipboard", "clear", {"all": True})  # type: ignore[union-attr]
            return int(result.get("removed") or 0)
        self.ctx.db.delete("kv", CLIPBOARD_KEY, key="key")
        return len(items) if isinstance(items, list) else 0

    # --- журналы ------------------------------------------------------------------------------------------
    def logs(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted(_files(self.logs_dir()), key=lambda p: p.name):
            with contextlib.suppress(OSError):
                st = f.stat()
                out.append({"name": f.name, "size": st.st_size, "modified": st.st_mtime})
        return out

    def clear_logs(self) -> int:
        """Очистить журналы ядра: открытые (*.log) — обрезать до нуля, старые копии — удалить."""
        count = 0
        for f in _files(self.logs_dir()):
            done = _truncate(f) if f.suffix == ".log" else (_unlink(f) or _truncate(f))
            count += int(done)
        return count

    @staticmethod
    def _drop_log(f: Path) -> bool:
        """Старый журнал: удалить; открытые ядром и занятые другой программой (Windows) — обрезать."""
        if f.name in ACTIVE_LOGS:
            return _truncate(f)
        return _unlink(f) or _truncate(f)

    # --- очистка по срокам --------------------------------------------------------------------------------
    async def purge(self) -> dict[str, int]:
        """Удалить всё, у чего истёк срок хранения (settings.privacy). Вернёт, сколько удалено."""
        p = self.ctx.settings.value.privacy
        now = self.clock()
        removed: dict[str, int] = {}

        def cutoff(days: int) -> float | None:
            return now - days * DAY_S if days > 0 else None

        steps: list[tuple[str, Callable[[], int]]] = [
            ("memory", self._purge_memory),
            ("history", lambda: self._purge_history(cutoff(p.history_days))),
            ("chats", lambda: self._purge_chats(cutoff(p.chat_days))),
            ("voice_chats", lambda: self._purge_voice(cutoff(p.voice_chat_days))),
            ("tool_results", lambda: self._purge_tool_results(cutoff(p.tool_results_days))),
            ("summaries", lambda: self._purge_audio(cutoff(p.audio_days))),
            ("logs", lambda: self._purge_logs(cutoff(p.logs_days))),
        ]
        for name, step in steps:
            try:
                n = step()
            except Exception:
                log.exception("очистка «%s» упала", name)
                continue
            if n:
                removed[name] = n
        self.ctx.db.kv_set(KV_LAST, now)
        if removed:
            log.info("очистка по срокам хранения: %s", removed)
            self.ctx.bus.publish("data.purged", {"removed": removed, "ts": now})
        return removed

    def _purge_memory(self) -> int:
        purge = getattr(self.ctx.notes, "memory_purge_expired", None)
        return int(purge()) if callable(purge) else 0

    def _purge_history(self, before: float | None) -> int:
        if before is None:
            return 0
        return self.ctx.db.execute("DELETE FROM history WHERE ts < ?", (before,)).rowcount

    def _purge_chats(self, before: float | None) -> int:
        if before is None:
            return 0
        rows = self.ctx.db.query(
            "SELECT id FROM chats WHERE kind='chat' AND pinned=0 AND updated < ?", (before,)
        )
        for row in rows:
            self._delete_chat(row["id"])
        return len(rows)

    def _purge_voice(self, before: float | None) -> int:
        if before is None:
            return 0
        return self.ctx.db.execute(
            "DELETE FROM messages WHERE created < ? AND chat_id IN (SELECT id FROM chats WHERE kind='voice')",
            (before,),
        ).rowcount

    def _purge_tool_results(self, before: float | None) -> int:
        if before is None:
            return 0
        db = self.ctx.db
        count = db.execute(
            "UPDATE messages SET content=? WHERE role='tool' AND created < ? AND LENGTH(content) > ?",
            (TOOL_PLACEHOLDER, before, len(TOOL_PLACEHOLDER)),
        ).rowcount
        rows = db.query(
            "SELECT id, meta FROM messages WHERE role='user' AND created < ? AND meta LIKE '%\"images\"%'",
            (before,),
        )
        for row in rows:
            meta = loads(row["meta"], {}) or {}
            images = meta.pop("images", None)
            if not images:
                continue
            meta["images_removed"] = len(images) if isinstance(images, list) else 1
            db.update("messages", row["id"], {"meta": json.dumps(meta, ensure_ascii=False)})
            count += 1
        return count

    def _keep_audio(self) -> bool:
        state = self.ctx.settings.value.plugins.get("summary")
        return bool(state and state.config.get("keep_audio"))

    def _purge_audio(self, before: float | None) -> int:
        if before is None or self._keep_audio():
            return 0
        removed = 0
        for f in _files(self.summaries_dir(), "*.wav"):
            with contextlib.suppress(OSError):
                if f.stat().st_mtime < before:
                    removed += int(_unlink(f))
        return removed

    def _purge_logs(self, before: float | None) -> int:
        removed = 0
        for f in _files(self.logs_dir()):
            try:
                st = f.stat()
            except OSError:
                continue
            if before is not None and st.st_mtime < before:
                removed += int(self._drop_log(f))
            elif st.st_size > LOG_CAP:
                removed += int(_truncate(f, LOG_KEEP))
        return removed


def _title(cid: str) -> str:
    return next((c.title for c in CATEGORIES if c.id == cid), cid)
