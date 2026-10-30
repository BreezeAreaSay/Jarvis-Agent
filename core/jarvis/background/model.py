"""Задача и её состояние: бриф, план, ход работы, вопросы, источники, итог.

Хранится JSON-файлом <данные>/background/<id>.json (своё состояние — файлами, без новых таблиц базы):
переживает перезапуск, и задача продолжается с плана и хода работы. Пишется атомарно (временный файл
и замена).
"""

import contextlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jarvis import paths
from jarvis.ai.format import one_line
from jarvis.background.repeat import describe

log = logging.getLogger(__name__)

# queued — ждёт очереди; running — работает; waiting — ждёт ответа человека; done / partial / failed — итог
# (как оценил его агент или ошибка); cancelled — отменил человек. Повторяющаяся задача между запусками —
# scheduled (ждёт следующего по расписанию) или paused (повторы на паузе: человек или три неудачи подряд)
STATUSES = ("queued", "running", "waiting", "done", "partial", "failed", "cancelled", "scheduled", "paused")
ACTIVE = frozenset({"queued", "running", "waiting"})
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_LOG = 400  # записей хода работы в состоянии (в заметке — все, что были)
MAX_SOURCES = 200
MAX_HISTORY = 60  # запусков повторяющейся задачи в состоянии
MAX_TABLES = 20
CARD_LOG = 3  # последних записей хода работы — в краткой карточке


@dataclass
class Question:
    """Вопрос человеку: неясное (kind=question) или разрешение на необратимое (kind=approval)."""

    id: str
    text: str
    kind: str = "question"
    options: list[str] = field(default_factory=list)
    detail: str = ""  # что именно будет сделано («Будет отправлено: …»)
    # разрешение: какое действие разрешается («Разрешить» пришло после паузы — снова не спрашивать)
    key: str = ""
    asked: float = field(default_factory=time.time)
    answer: str = ""
    answered: float = 0.0

    @property
    def open(self) -> bool:
        return not self.answered


@dataclass
class Entry:
    """Запись хода работы."""

    ts: float
    text: str
    kind: str = (
        "progress"  # progress — что сделано или найдено; action — действие; error — сбой; note — служебное
    )


@dataclass
class Task:
    id: str
    brief: str  # что сделать — словами человека
    title: str = ""  # коротко, для списка и имени заметки
    deliverable: str = ""  # какой нужен результат (отчёт, таблица, список…)
    status: str = "queued"
    source: str = "ui"  # ui | text | voice | remote | vault
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    plan: list[dict[str, Any]] = field(default_factory=list)  # [{text, done}]
    log: list[Entry] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)  # [{title, url}]
    summary: str = ""  # итог для человека
    note: str = ""  # заметка задачи — путь внутри хранилища
    result: str = ""  # заметка с результатом (если агент сделал отдельную) — путь внутри хранилища
    error: str = ""
    steps: int = 0  # действий сделано (за все заходы)
    worked_s: float = 0.0  # секунд работы (без ожидания ответа)
    runs: int = 0  # сколько раз задача запускалась (продолжение после перезапуска или ответа)
    approved: list[str] = field(default_factory=list)  # разрешённые человеком действия (ключи вопросов)
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    # повторяющаяся задача (jarvis.background.repeat): расписание, следующий запуск, номер запуска и с чего
    # он начался (лимиты действий и времени — на запуск), итоги прошлых запусков, неудачи подряд
    repeat: dict[str, Any] = field(default_factory=dict)
    next_run: float = 0.0
    cycle: int = 0
    cycle_started: float = 0.0
    cycle_steps: int = 0
    cycle_worked: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)  # [{cycle, ts, status, summary, steps}]
    fails: int = 0
    tables: list[str] = field(default_factory=list)  # таблицы замеров задачи — пути внутри хранилища

    # --- состояние ---------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def recurring(self) -> bool:
        return bool(self.repeat)

    def add_table(self, rel: str) -> None:
        if rel and rel not in self.tables:
            self.tables = [*self.tables, rel][-MAX_TABLES:]

    def add_history(self, status: str, summary: str) -> None:
        self.history.append(
            {
                "cycle": self.cycle,
                "ts": time.time(),
                "status": status,
                "summary": one_line(summary, 400),
                "steps": self.steps - self.cycle_steps,
            }
        )
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]

    def open_question(self) -> Question | None:
        return next((q for q in self.questions if q.open), None)

    def touch(self) -> None:
        self.updated = time.time()

    def add_log(self, text: str, kind: str = "progress") -> Entry:
        entry = Entry(time.time(), one_line(text, 600), kind)
        self.log.append(entry)
        if len(self.log) > MAX_LOG:
            self.log = self.log[-MAX_LOG:]
        self.touch()
        return entry

    def add_source(self, title: str, url: str) -> bool:
        url = (url or "").strip()
        if not url or any(s["url"] == url for s in self.sources) or len(self.sources) >= MAX_SOURCES:
            return False
        self.sources.append({"title": one_line(title or url, 160), "url": url})
        return True

    # --- JSON --------------------------------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def card(self) -> dict[str, Any]:
        """Краткая карточка — для списка и события task.updated."""
        question = self.open_question()
        progress = [asdict(e) for e in self.log if e.kind != "action"][-CARD_LOG:]
        done = sum(1 for s in self.plan if s.get("done"))
        return {
            "id": self.id,
            "title": self.title or one_line(self.brief, 80),
            "brief": one_line(self.brief, 300),
            "status": self.status,
            "source": self.source,
            "created": self.created,
            "updated": self.updated,
            "finished": self.finished,
            "plan_done": done,
            "plan_total": len(self.plan),
            "steps": self.steps,
            "progress": progress,
            "question": asdict(question) if question is not None else None,
            "summary": one_line(self.summary, 400),
            "error": self.error,
            "note": self.note,
            "result": self.result,
            "sources": len(self.sources),
            "repeat": self.repeat,
            "repeat_text": describe(self.repeat),
            "next_run": self.next_run,
            "cycle": self.cycle,
            "last_run": self.history[-1] if self.history else None,
            "tables": list(self.tables),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Task":
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in known}
        values["log"] = [Entry(**_only(Entry, e)) for e in data.get("log") or [] if isinstance(e, dict)]
        values["questions"] = [
            Question(**_only(Question, q)) for q in data.get("questions") or [] if isinstance(q, dict)
        ]
        task = cls(**values)
        if task.status not in STATUSES:
            task.status = "failed"
        return task


def _only(kind: Any, data: dict[str, Any]) -> dict[str, Any]:
    known = set(kind.__dataclass_fields__)
    return {k: v for k, v in data.items() if k in known}


class TaskStore:
    """Задачи на диске: по файлу на задачу."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = paths.sub_dir("background")
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def _path(self, task_id: str) -> Path:
        if not TASK_ID.match(task_id):
            raise ValueError(f"плохой id задачи: {task_id!r}")
        return self.root / f"{task_id}.json"

    def load_all(self) -> list[Task]:
        out: list[Task] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text("utf-8"))
                out.append(Task.from_json(data))
            except Exception:
                log.warning("задача %s не прочиталась — пропускаю", path.name, exc_info=True)
        out.sort(key=lambda t: t.created)
        return out

    def save(self, task: Task) -> None:
        path = self._path(task.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(task.to_json(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, task_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(task_id).unlink()
