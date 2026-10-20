"""Заметки, задачи, календарь, напоминания и память о пользователе (таблицы notes, tasks, events, reminders,
memory).

Память — короткие записи о пользователе («Аня не ест мясо»). Вид записи: fact — подтверждённый факт,
assumption — предположение (ИИ должен уточнять, прежде чем на него опираться), temp — временное («сегодня
работаю из дома»), забывается через сутки. Похожие записи не дублируются (новая заменяет старую, в том числе
с противоположным смыслом — «ест мясо» / «не ест мясо»), но запись человека (source user) ИИ не
перезаписывает. Записей не больше 200 (лишними уходят старые записи ИИ), в системный промпт ИИ уходит сжатая
строка не длиннее ~1500 символов: подтверждённое, предположения и временное — отдельно, с датами.

Напоминания проверяет фоновая задача раз в ~5 секунд: сработавшее напоминание — событие reminder.fired, тост
«Напоминание» и голос «Напоминаю, сэр: …» (с персоной «Джарвис» — в его характере: «Сэр, напоминаю: …»,
jarvis.voice.character; если не включён тихий режим). Повтор — каждый день или неделю.
Напоминания, пропущенные, пока Jarvis был выключен (или компьютер спал), срабатывают один раз с пометкой
missed. События календаря с remind_min напоминают о себе так же.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from rapidfuzz import fuzz

from jarvis.ai.format import human_time, plural
from jarvis.db import new_id
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError

log = logging.getLogger(__name__)

MEMORY_LIMIT = 200
PROMPT_LIMIT = 1500
MEMORY_KINDS = ("fact", "assumption", "temp")
TEMP_TTL_S = 24 * 3600  # временная запись живёт сутки
TICK_S = 5.0
LATE_S = 120.0  # опоздание больше 2 минут — напоминание «пропущено» (Jarvis был выключен, ПК спал)
EVENT_STALE_S = 24 * 3600  # о событиях, начавшихся больше суток назад, уже не напоминаем
REPEATS = ("daily", "weekly")
_NEGATIONS = frozenset({"не", "ни", "нет", "not", "no", "never"})


class NotFound(UserFacingError):
    """Запись не найдена (API отвечает 404)."""


# --- память: сравнение фактов ------------------------------------------------------------------------


def _norm(text: str) -> str:
    t = text.casefold().replace("ё", "е")
    t = re.sub(r"[^\w\s-]", " ", t)
    return " ".join(t.split())


def same_fact(a: str, b: str) -> bool:
    """Два факта об одном и том же: почти совпадают или отличаются только отрицанием."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta = sorted(w for w in na.split() if w not in _NEGATIONS)
    tb = sorted(w for w in nb.split() if w not in _NEGATIONS)
    if ta and ta == tb:
        return True
    if fuzz.token_sort_ratio(na, nb) >= 90:
        return True
    shorter, longer = sorted((na, nb), key=len)
    return (
        len(shorter.split()) >= 2
        and len(shorter) / len(longer) >= 0.6
        and fuzz.token_set_ratio(na, nb) >= 100
    )


def _clean_fact(text: str) -> str:
    fact = " ".join(str(text).split()).strip(" .;")
    if len(fact) > 300:
        fact = fact[:299].rstrip() + "…"
    return fact[:1].upper() + fact[1:] if fact else ""


# --- строки таблиц → словари для API ---------------------------------------------------------------------


def _memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "text": row["text"],
        "source": row["source"],
        "kind": row.get("kind") or "fact",
        "expires": row.get("expires"),
        "created": row["created"],
    }


def _kind(value: Any) -> str:
    kind = str(value or "fact")
    if kind not in MEMORY_KINDS:
        raise UserFacingError(
            "Вид записи памяти: fact (факт), assumption (предположение) или temp (на сутки)"
        )
    return kind


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def _note(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "pinned": bool(row["pinned"]),
        "color": row["color"],
        "created": row["created"],
        "updated": row["updated"],
    }


def _task(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "done": bool(row["done"]),
        "due": row["due"],
        "position": row["position"],
        "created": row["created"],
        "completed": row["completed"],
    }


def _event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "start": row["start"],
        "end": row["end"],
        "all_day": bool(row["all_day"]),
        "notes": row["notes"],
        "remind_min": row["remind_min"],
        "reminded": bool(row["reminded"]),
        "created": row["created"],
    }


def _reminder(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "text": row["text"],
        "at": row["at"],
        "repeat": row["repeat"],
        "done": bool(row["done"]),
        "created": row["created"],
    }


def _repeat(value: Any) -> str | None:
    if value in (None, "", "none", "once"):
        return None
    if value not in REPEATS:
        raise UserFacingError("Повтор напоминания: daily (каждый день) или weekly (каждую неделю)")
    return str(value)


def _float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise UserFacingError(f"Неверное время: {name}") from None


def next_occurrence(at: float, repeat: str, now: float) -> float:
    """Следующий раз для повторяющегося напоминания: то же время на часах, позже now."""
    step = timedelta(days=1 if repeat == "daily" else 7)
    moment = datetime.fromtimestamp(at)
    behind = now - at
    if behind > step.total_seconds() * 2:  # давно пропущено — сразу ближе к сегодняшнему дню
        moment += step * int(behind // step.total_seconds() - 1)
    while moment.timestamp() <= now:
        moment += step
    return moment.timestamp()


class Notes(BaseService):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.clock: Callable[[], float] = time.time  # тесты подменяют время
        self.tick_s = TICK_S
        self._task: asyncio.Task[None] | None = None

    # --- жизненный цикл --------------------------------------------------------------------------------
    async def start(self) -> None:
        self._task = self.ctx.spawn(self._loop(), "notes-reminders")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("проверка напоминаний упала")
            await asyncio.sleep(self.tick_s)

    def _changed(self, kind: str, op: str, *, item: dict[str, Any] | None = None, item_id: str = "") -> None:
        data: dict[str, Any] = {"op": op}
        if item is not None:
            data["item"] = item
        if item_id:
            data["id"] = item_id
        self.ctx.bus.publish(f"{kind}.changed", data)

    # --- память --------------------------------------------------------------------------------------
    def memory_list(self) -> list[dict[str, Any]]:
        """Записи памяти, свежие первыми; временные с истёкшим сроком не показываются."""
        rows = self.ctx.db.query(
            "SELECT * FROM memory WHERE expires IS NULL OR expires > ? ORDER BY created DESC, rowid DESC",
            (self.clock(),),
        )
        return [_memory(r) for r in rows]

    def memory_get(self, memory_id: str) -> dict[str, Any]:
        row = self.ctx.db.query_one("SELECT * FROM memory WHERE id=?", (memory_id,))
        if row is None:
            raise NotFound("Запись памяти не найдена")
        return _memory(row)

    def _expires(self, kind: str, ts: float) -> float | None:
        return ts + TEMP_TTL_S if kind == "temp" else None

    def memory_add(self, text: str, source: str = "ai", *, kind: str = "fact") -> dict[str, Any]:
        """Запомнить запись. Похожая уже есть — новая версия заменяет её; но запись человека (source user)
        ИИ не перезаписывает: тогда вернётся она же с пометкой unchanged=True (события нет)."""
        fact = _clean_fact(text)
        if not fact:
            raise UserFacingError("Пустой факт — нечего запоминать")
        kind = _kind(kind)
        ts = self.clock()
        for item in self.memory_list():
            if same_fact(item["text"], fact):
                if item["source"] == "user" and source != "user":
                    return {**item, "unchanged": True}
                # тот же факт другими словами или с противоположным смыслом — новая версия заменяет старую
                values = {"text": fact, "source": source, "kind": kind, "expires": self._expires(kind, ts)}
                self.ctx.db.update("memory", item["id"], {**values, "created": ts})
                updated = {"id": item["id"], **values, "created": ts}
                self.ctx.bus.publish("memory.added", {"item": updated})
                return updated
        item = {
            "id": new_id(),
            "text": fact,
            "source": source,
            "kind": kind,
            "expires": self._expires(kind, ts),
            "created": ts,
        }
        self.ctx.db.insert("memory", item)
        self.ctx.bus.publish("memory.added", {"item": item})
        self._trim_memory()
        return item

    def memory_update(
        self, memory_id: str, *, text: str | None = None, kind: str | None = None
    ) -> dict[str, Any]:
        """Человек поправил запись: текст и/или вид. Поправленная запись — его (source user)."""
        current = self.memory_get(memory_id)
        values: dict[str, Any] = {"source": "user"}
        if text is not None:
            fact = _clean_fact(text)
            if not fact:
                raise UserFacingError("Пустой факт — удалите запись, если она больше не нужна")
            values["text"] = fact
        if kind is not None:
            values["kind"] = _kind(kind)
        new_kind = values.get("kind", current["kind"])
        ts = self.clock()
        if new_kind != current["kind"]:
            values["expires"] = self._expires(new_kind, ts)
        values["created"] = ts
        self.ctx.db.update("memory", memory_id, values)
        item = self.memory_get(memory_id)
        self.ctx.bus.publish("memory.updated", {"item": item})
        return item

    def memory_clear(self) -> int:
        """Забыть всё. Вернёт, сколько записей удалено."""
        count = self.ctx.db.execute("DELETE FROM memory").rowcount
        self.ctx.bus.publish("memory.cleared", {"count": count})
        return count

    def memory_purge_expired(self) -> int:
        """Удалить временные записи с истёкшим сроком (зовёт ежедневная очистка)."""
        rows = self.ctx.db.query(
            "SELECT id FROM memory WHERE expires IS NOT NULL AND expires <= ?", (self.clock(),)
        )
        for row in rows:
            self.ctx.db.delete("memory", row["id"])
            self.ctx.bus.publish("memory.deleted", {"id": row["id"]})
        return len(rows)

    def _trim_memory(self) -> None:
        # сверх лимита уходят сначала старые записи ИИ, записи человека — в последнюю очередь
        extra = self.ctx.db.query(
            "SELECT id FROM memory ORDER BY CASE WHEN source='user' THEN 0 ELSE 1 END, created DESC, "
            "rowid DESC LIMIT -1 OFFSET ?",
            (MEMORY_LIMIT,),
        )
        for row in extra:
            self.ctx.db.delete("memory", row["id"])
            self.ctx.bus.publish("memory.deleted", {"id": row["id"]})

    def memory_delete(self, memory_id: str) -> bool:
        removed = self.ctx.db.delete("memory", memory_id) > 0
        if removed:
            self.ctx.bus.publish("memory.deleted", {"id": memory_id})
        return removed

    def memory_find(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """Факты, похожие на запрос («забудь, что я не ем мясо»)."""
        q = _norm(query)
        if not q:
            return []
        scored = [(fuzz.WRatio(q, _norm(i["text"])), i) for i in self.memory_list()]
        if not scored:
            return []
        best = max(score for score, _ in scored)
        if best < 70:
            return []
        threshold = max(70.0, best - 5)
        return [item for score, item in sorted(scored, key=lambda x: -x[0]) if score >= threshold][:limit]

    def memory_prompt(self) -> str:
        """Память для системного промпта ИИ (не длиннее ~1500 символов): подтверждённое пользователем,
        предположения (уточнять) и временное — отдельными частями, свежие первыми, с датами."""
        groups: dict[str, list[str]] = {kind: [] for kind in MEMORY_KINDS}
        total = 0
        for item in self.memory_list():
            fact = item["text"].strip().rstrip(".")
            if not fact:
                continue
            if item["kind"] == "temp" and item["expires"]:
                until = datetime.fromtimestamp(item["expires"]).strftime("%d.%m %H:%M")
                entry = f"{fact} (до {until})"
            else:
                entry = f"{fact} ({_day(item['created'])})"
            if total + len(entry) + 2 > PROMPT_LIMIT:
                break
            groups[item["kind"]].append(entry)
            total += len(entry) + 2
        titles = {
            "fact": "Подтверждено пользователем",
            "assumption": "Предположения — уточняй, прежде чем на них опираться",
            "temp": "Временно",
        }
        return " ".join(f"{titles[kind]}: {'; '.join(items)}." for kind, items in groups.items() if items)

    # --- заметки ---------------------------------------------------------------------------------------
    def notes_list(self, query: str = "") -> list[dict[str, Any]]:
        notes = [
            _note(r) for r in self.ctx.db.query("SELECT * FROM notes ORDER BY pinned DESC, updated DESC")
        ]
        q = _norm(query or "")
        if not q:
            return notes
        out = []
        for note in notes:
            text = _norm(f"{note['title']} {note['body']}")
            if q in text or fuzz.partial_ratio(q, text) >= 80:
                out.append(note)
        return out

    def note_get(self, note_id: str) -> dict[str, Any]:
        row = self.ctx.db.query_one("SELECT * FROM notes WHERE id=?", (note_id,))
        if row is None:
            raise NotFound("Заметка не найдена")
        return _note(row)

    def note_add(
        self, title: str, body: str = "", *, pinned: bool = False, color: str = ""
    ) -> dict[str, Any]:
        ts = self.clock()
        row = {
            "id": new_id(),
            "title": (title or "").strip()[:200],
            "body": body or "",
            "pinned": 1 if pinned else 0,
            "color": color or "",
            "created": ts,
            "updated": ts,
        }
        self.ctx.db.insert("notes", row)
        item = _note(row)
        self._changed("notes", "add", item=item)
        return item

    def note_update(self, note_id: str, **fields: Any) -> dict[str, Any]:
        self.note_get(note_id)
        values: dict[str, Any] = {}
        if fields.get("title") is not None:
            values["title"] = str(fields["title"]).strip()[:200]
        if fields.get("body") is not None:
            values["body"] = str(fields["body"])
        if fields.get("pinned") is not None:
            values["pinned"] = 1 if fields["pinned"] else 0
        if fields.get("color") is not None:
            values["color"] = str(fields["color"])
        if values:
            values["updated"] = self.clock()
            self.ctx.db.update("notes", note_id, values)
        item = self.note_get(note_id)
        self._changed("notes", "update", item=item)
        return item

    def note_delete(self, note_id: str) -> bool:
        removed = self.ctx.db.delete("notes", note_id) > 0
        if removed:
            self._changed("notes", "delete", item_id=note_id)
        return removed

    # --- задачи ----------------------------------------------------------------------------------------
    def tasks_list(self, include_done: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks" + ("" if include_done else " WHERE done=0")
        rows = self.ctx.db.query(sql + " ORDER BY done, position, created")
        return [_task(r) for r in rows]

    def task_get(self, task_id: str) -> dict[str, Any]:
        row = self.ctx.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise NotFound("Задача не найдена")
        return _task(row)

    def task_add(self, title: str, due: float | None = None) -> dict[str, Any]:
        title = " ".join((title or "").split())[:300]
        if not title:
            raise UserFacingError("Пустая задача")
        last = self.ctx.db.query_one("SELECT MAX(position) AS p FROM tasks")
        position = int(last["p"]) + 1 if last and last["p"] is not None else 0
        row = {
            "id": new_id(),
            "title": title,
            "done": 0,
            "due": _float(due, "срок") if due is not None else None,
            "position": position,
            "created": self.clock(),
            "completed": None,
        }
        self.ctx.db.insert("tasks", row)
        item = _task(row)
        self._changed("tasks", "add", item=item)
        return item

    def task_update(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Изменить задачу: title, done, due (None — без срока), position."""
        current = self.task_get(task_id)
        values: dict[str, Any] = {}
        if fields.get("title") is not None:
            title = " ".join(str(fields["title"]).split())[:300]
            if not title:
                raise UserFacingError("Пустая задача")
            values["title"] = title
        if fields.get("done") is not None and bool(fields["done"]) != current["done"]:
            values["done"] = 1 if fields["done"] else 0
            values["completed"] = self.clock() if fields["done"] else None
        if "due" in fields:
            values["due"] = _float(fields["due"], "срок") if fields["due"] is not None else None
        if fields.get("position") is not None:
            values["position"] = int(fields["position"])
        if values:
            self.ctx.db.update("tasks", task_id, values)
        item = self.task_get(task_id)
        self._changed("tasks", "update", item=item)
        return item

    def task_delete(self, task_id: str) -> bool:
        removed = self.ctx.db.delete("tasks", task_id) > 0
        if removed:
            self._changed("tasks", "delete", item_id=task_id)
        return removed

    def task_find(self, query: str) -> dict[str, Any] | None:
        """Задача по id или по названию (нечётко; невыполненные — в первую очередь)."""
        query = (query or "").strip()
        if not query:
            return None
        row = self.ctx.db.query_one("SELECT * FROM tasks WHERE id=?", (query,))
        if row is not None:
            return _task(row)
        q = _norm(query)
        best: tuple[float, dict[str, Any]] | None = None
        for task in self.tasks_list():
            score = fuzz.WRatio(q, _norm(task["title"])) - (10 if task["done"] else 0)
            if score >= 70 and (best is None or score > best[0]):
                best = (score, task)
        return best[1] if best else None

    # --- календарь ---------------------------------------------------------------------------------------
    def events_list(self, start: float | None = None, end: float | None = None) -> list[dict[str, Any]]:
        """События, которые пересекают [start, end) (границы необязательны)."""
        where: list[str] = []
        params: list[Any] = []
        if end is not None:
            where.append("start < ?")
            params.append(end)
        if start is not None:
            where.append("COALESCE(end, start) >= ?")
            params.append(start)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY start"
        return [_event(r) for r in self.ctx.db.query(sql, params)]

    def event_get(self, event_id: str) -> dict[str, Any]:
        row = self.ctx.db.query_one("SELECT * FROM events WHERE id=?", (event_id,))
        if row is None:
            raise NotFound("Событие не найдено")
        return _event(row)

    def event_add(self, title: str, start: float, end: float | None = None, **kw: Any) -> dict[str, Any]:
        title = " ".join((title or "").split())[:300]
        if not title:
            raise UserFacingError("Пустое название события")
        start = _float(start, "начало")
        end_value = _float(end, "конец") if end is not None else None
        if end_value is not None and end_value < start:
            raise UserFacingError("Конец события раньше начала")
        remind = kw.get("remind_min")
        if remind is not None and int(remind) < 0:
            raise UserFacingError("Напоминание о событии: число минут не может быть отрицательным")
        row = {
            "id": new_id(),
            "title": title,
            "start": start,
            "end": end_value,
            "all_day": 1 if kw.get("all_day") else 0,
            "notes": str(kw.get("notes") or ""),
            "remind_min": int(remind) if remind is not None else None,
            "reminded": 0,
            "created": self.clock(),
        }
        self.ctx.db.insert("events", row)
        item = _event(row)
        self._changed("events", "add", item=item)
        return item

    def event_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        current = self.event_get(event_id)
        values: dict[str, Any] = {}
        if fields.get("title") is not None:
            title = " ".join(str(fields["title"]).split())[:300]
            if not title:
                raise UserFacingError("Пустое название события")
            values["title"] = title
        if fields.get("start") is not None:
            values["start"] = _float(fields["start"], "начало")
        if "end" in fields:
            values["end"] = _float(fields["end"], "конец") if fields["end"] is not None else None
        if fields.get("all_day") is not None:
            values["all_day"] = 1 if fields["all_day"] else 0
        if fields.get("notes") is not None:
            values["notes"] = str(fields["notes"])
        if "remind_min" in fields:
            remind = fields["remind_min"]
            if remind is not None and int(remind) < 0:
                raise UserFacingError("Напоминание о событии: число минут не может быть отрицательным")
            values["remind_min"] = int(remind) if remind is not None else None
        start = values.get("start", current["start"])
        end = values.get("end", current["end"])
        if end is not None and end < start:
            raise UserFacingError("Конец события раньше начала")
        if "start" in values or "remind_min" in values:
            values["reminded"] = 0
        if values:
            self.ctx.db.update("events", event_id, values)
        item = self.event_get(event_id)
        self._changed("events", "update", item=item)
        return item

    def event_delete(self, event_id: str) -> bool:
        removed = self.ctx.db.delete("events", event_id) > 0
        if removed:
            self._changed("events", "delete", item_id=event_id)
        return removed

    # --- напоминания ---------------------------------------------------------------------------------
    def reminders_list(self, include_done: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reminders" + ("" if include_done else " WHERE done=0")
        return [_reminder(r) for r in self.ctx.db.query(sql + " ORDER BY done, at")]

    def reminder_get(self, reminder_id: str) -> dict[str, Any]:
        row = self.ctx.db.query_one("SELECT * FROM reminders WHERE id=?", (reminder_id,))
        if row is None:
            raise NotFound("Напоминание не найдено")
        return _reminder(row)

    def reminder_add(self, text: str, at: float, repeat: str | None = None) -> dict[str, Any]:
        text = " ".join((text or "").split())[:500]
        if not text:
            raise UserFacingError("Пустое напоминание")
        row = {
            "id": new_id(),
            "text": text,
            "at": _float(at, "время напоминания"),
            "repeat": _repeat(repeat),
            "done": 0,
            "created": self.clock(),
        }
        self.ctx.db.insert("reminders", row)
        item = _reminder(row)
        self._changed("reminders", "add", item=item)
        return item

    def reminder_update(self, reminder_id: str, **fields: Any) -> dict[str, Any]:
        self.reminder_get(reminder_id)
        values: dict[str, Any] = {}
        if fields.get("text") is not None:
            text = " ".join(str(fields["text"]).split())[:500]
            if not text:
                raise UserFacingError("Пустое напоминание")
            values["text"] = text
        if fields.get("at") is not None:
            values["at"] = _float(fields["at"], "время напоминания")
            values["done"] = 0
        if "repeat" in fields:
            values["repeat"] = _repeat(fields["repeat"])
        if values:
            self.ctx.db.update("reminders", reminder_id, values)
        item = self.reminder_get(reminder_id)
        self._changed("reminders", "update", item=item)
        return item

    def reminder_delete(self, reminder_id: str) -> bool:
        removed = self.ctx.db.delete("reminders", reminder_id) > 0
        if removed:
            self._changed("reminders", "delete", item_id=reminder_id)
        return removed

    # --- срабатывание ------------------------------------------------------------------------------------
    async def tick(self) -> list[dict[str, Any]]:
        """Сработать всё, чему пора (вызывается планировщиком; в тестах — напрямую с подменённым clock)."""
        now = self.clock()
        fired: list[dict[str, Any]] = []
        for row in self.ctx.db.query("SELECT * FROM reminders WHERE done=0 AND at <= ? ORDER BY at", (now,)):
            item = _reminder(row)
            missed = now - item["at"] > LATE_S
            if item["repeat"] in REPEATS:
                self.ctx.db.update(
                    "reminders", item["id"], {"at": next_occurrence(item["at"], item["repeat"], now)}
                )
            else:
                self.ctx.db.update("reminders", item["id"], {"done": 1})
            self._changed("reminders", "update", item=self.reminder_get(item["id"]))
            fired.append(await self._fire({**item, "kind": "reminder"}, item["text"], missed=missed))
        due = self.ctx.db.query(
            "SELECT * FROM events WHERE remind_min IS NOT NULL AND reminded=0 "
            "AND start - remind_min * 60 <= ? ORDER BY start",
            (now,),
        )
        for row in due:
            event = _event(row)
            self.ctx.db.update("events", event["id"], {"reminded": 1})
            if now - event["start"] > EVENT_STALE_S:
                continue  # давно прошло — молча отмечаем
            self._changed("events", "update", item={**event, "reminded": True})
            fired.append(await self._fire_event(event, now))
        return fired

    async def _fire_event(self, event: dict[str, Any], now: float) -> dict[str, Any]:
        start, title = event["start"], event["title"]
        minutes = round((start - now) / 60)
        if minutes >= 1:
            message = f"{title} — через {minutes} {plural(minutes, ('минуту', 'минуты', 'минут'))}"
        elif minutes > -5:
            message = f"{title} — начинается сейчас"
        else:
            message = f"{title} — началось {human_time(start)}"
        expected = start - (event["remind_min"] or 0) * 60
        return await self._fire(
            {**event, "kind": "event", "text": message}, message, missed=now - expected > LATE_S
        )

    async def _fire(self, item: dict[str, Any], message: str, *, missed: bool) -> dict[str, Any]:
        data = {**item, "missed": missed}
        self.ctx.bus.publish("reminder.fired", {"item": data})
        toast = f"{message} (пропущено, {human_time(item['at'])})" if missed and "at" in item else message
        self.ctx.bus.publish(
            "toast",
            {"title": "Напоминание", "message": toast, "status": "info", "icon": "timer", "ts": time.time()},
        )
        settings = self.ctx.settings.value
        if not settings.voice.silent_mode:
            address = settings.profile.address.strip()
            phrase = f"Напоминаю, {address}: {message}" if address else f"Напоминаю: {message}"
            character = getattr(self.ctx, "character", None)
            if character is not None:  # персона «Джарвис»: «Сэр, напоминаю: …», «Сэр, время вышло: …»
                with contextlib.suppress(Exception):
                    phrase = character.reminder(item, message, missed=missed) or phrase
            try:
                await asyncio.wait_for(self.ctx.tts.say(phrase), 10.0)
            except Exception:
                log.exception("напоминание не озвучено")
        return data
