"""Маршруты API: память, заметки, задачи, календарь, напоминания (docs/CONTRACTS.md, раздел 4).

Время — unix-секунды (float).
"""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.notes.service import Notes, NotFound

router = APIRouter()


def _notes(ctx: AppContext) -> Notes:
    notes = ctx.notes
    if not isinstance(notes, Notes):
        raise HTTPException(503, detail="Заметки недоступны")
    return notes


def _found[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return fn(*args, **kwargs)
    except NotFound as e:
        raise HTTPException(404, detail=str(e)) from e


def _deleted(removed: bool, what: str) -> dict[str, Any]:
    if not removed:
        raise HTTPException(404, detail=f"{what} не найдено")
    return {"ok": True}


# --- память -----------------------------------------------------------------------------------------------


class MemoryIn(BaseModel):
    text: str


@router.get("/api/memory")
async def memory_list(ctx: Ctx) -> dict[str, Any]:
    return {"items": _notes(ctx).memory_list()}


@router.post("/api/memory")
async def memory_add(body: MemoryIn, ctx: Ctx) -> dict[str, Any]:
    return _notes(ctx).memory_add(body.text, "user")


@router.delete("/api/memory/{memory_id}")
async def memory_delete(memory_id: str, ctx: Ctx) -> dict[str, Any]:
    return _deleted(_notes(ctx).memory_delete(memory_id), "Запись памяти")


# --- заметки ----------------------------------------------------------------------------------------------


class NoteIn(BaseModel):
    title: str = ""
    body: str = ""
    pinned: bool = False
    color: str = ""


class NotePatch(BaseModel):
    title: str | None = None
    body: str | None = None
    pinned: bool | None = None
    color: str | None = None


@router.get("/api/notes")
async def notes_list(ctx: Ctx, q: str = "") -> dict[str, Any]:
    return {"items": _notes(ctx).notes_list(q)}


@router.post("/api/notes")
async def note_add(body: NoteIn, ctx: Ctx) -> dict[str, Any]:
    return _notes(ctx).note_add(body.title, body.body, pinned=body.pinned, color=body.color)


@router.patch("/api/notes/{note_id}")
async def note_update(note_id: str, body: NotePatch, ctx: Ctx) -> dict[str, Any]:
    return _found(_notes(ctx).note_update, note_id, **body.model_dump(exclude_unset=True))


@router.delete("/api/notes/{note_id}")
async def note_delete(note_id: str, ctx: Ctx) -> dict[str, Any]:
    return _deleted(_notes(ctx).note_delete(note_id), "Заметка")


# --- задачи -----------------------------------------------------------------------------------------------


class TaskIn(BaseModel):
    title: str
    due: float | None = None
    done: bool = False


class TaskPatch(BaseModel):
    title: str | None = None
    done: bool | None = None
    due: float | None = None
    position: int | None = None


@router.get("/api/tasks")
async def tasks_list(ctx: Ctx) -> dict[str, Any]:
    return {"items": _notes(ctx).tasks_list()}


@router.post("/api/tasks")
async def task_add(body: TaskIn, ctx: Ctx) -> dict[str, Any]:
    notes = _notes(ctx)
    task = notes.task_add(body.title, body.due)
    return notes.task_update(task["id"], done=True) if body.done else task


@router.patch("/api/tasks/{task_id}")
async def task_update(task_id: str, body: TaskPatch, ctx: Ctx) -> dict[str, Any]:
    return _found(_notes(ctx).task_update, task_id, **body.model_dump(exclude_unset=True))


@router.delete("/api/tasks/{task_id}")
async def task_delete(task_id: str, ctx: Ctx) -> dict[str, Any]:
    return _deleted(_notes(ctx).task_delete(task_id), "Задача")


# --- календарь -----------------------------------------------------------------------------------------


class EventIn(BaseModel):
    title: str
    start: float
    end: float | None = None
    all_day: bool = False
    notes: str = ""
    remind_min: int | None = None


class EventPatch(BaseModel):
    title: str | None = None
    start: float | None = None
    end: float | None = None
    all_day: bool | None = None
    notes: str | None = None
    remind_min: int | None = None


@router.get("/api/events")
async def events_list(ctx: Ctx, start: float | None = None, end: float | None = None) -> dict[str, Any]:
    return {"items": _notes(ctx).events_list(start, end)}


@router.post("/api/events")
async def event_add(body: EventIn, ctx: Ctx) -> dict[str, Any]:
    return _notes(ctx).event_add(
        body.title, body.start, body.end, all_day=body.all_day, notes=body.notes, remind_min=body.remind_min
    )


@router.patch("/api/events/{event_id}")
async def event_update(event_id: str, body: EventPatch, ctx: Ctx) -> dict[str, Any]:
    return _found(_notes(ctx).event_update, event_id, **body.model_dump(exclude_unset=True))


@router.delete("/api/events/{event_id}")
async def event_delete(event_id: str, ctx: Ctx) -> dict[str, Any]:
    return _deleted(_notes(ctx).event_delete(event_id), "Событие")


# --- напоминания --------------------------------------------------------------------------------------


class ReminderIn(BaseModel):
    text: str
    at: float
    repeat: Literal["daily", "weekly"] | None = None


class ReminderPatch(BaseModel):
    text: str | None = None
    at: float | None = None
    repeat: Literal["daily", "weekly"] | None = None


@router.get("/api/reminders")
async def reminders_list(ctx: Ctx) -> dict[str, Any]:
    return {"items": _notes(ctx).reminders_list()}


@router.post("/api/reminders")
async def reminder_add(body: ReminderIn, ctx: Ctx) -> dict[str, Any]:
    return _notes(ctx).reminder_add(body.text, body.at, body.repeat)


@router.patch("/api/reminders/{reminder_id}")
async def reminder_update(reminder_id: str, body: ReminderPatch, ctx: Ctx) -> dict[str, Any]:
    return _found(_notes(ctx).reminder_update, reminder_id, **body.model_dump(exclude_unset=True))


@router.delete("/api/reminders/{reminder_id}")
async def reminder_delete(reminder_id: str, ctx: Ctx) -> dict[str, Any]:
    return _deleted(_notes(ctx).reminder_delete(reminder_id), "Напоминание")
