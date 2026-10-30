"""Маршруты API: фоновые задачи и хранилище заметок (docs/CONTRACTS.md, раздел 8)."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.api.deps import Ctx
from jarvis.background.repeat import describe, parse_repeat
from jarvis.background.service import MAX_BRIEF, Background, title_of
from jarvis.context import AppContext

router = APIRouter()


class TaskIn(BaseModel):
    brief: str
    deliverable: str = ""
    title: str = ""
    source: str = "ui"
    # повтор: {kind: daily, at, weekdays} | {kind: hours, hours}; null — найти в тексте («каждый вечер …»),
    # {} — не повторять; run_now — первый запуск сразу
    repeat: dict[str, Any] | None = None
    run_now: bool = True


class RepeatIn(BaseModel):
    repeat: dict[str, Any] | None = None  # null или {} — снять повторы


class ParseIn(BaseModel):
    text: str


class AnswerIn(BaseModel):
    text: str
    question_id: str = ""


def _service(ctx: AppContext) -> Background:
    service = getattr(ctx, "background", None)
    if not isinstance(service, Background):
        raise HTTPException(503, detail="Фоновые задачи недоступны")
    return service


def _known(service: Background, task_id: str) -> None:
    if task_id not in {c["id"] for c in service.cards()}:
        raise HTTPException(404, detail="Задача не найдена")


@router.get("/api/background")
async def background_list(ctx: Ctx) -> dict[str, Any]:
    return {"items": _service(ctx).cards()}


@router.post("/api/background")
async def background_create(body: TaskIn, ctx: Ctx) -> dict[str, Any]:
    source = body.source if body.source in ("ui", "text", "voice", "remote", "vault") else "ui"
    task = _service(ctx).create(
        body.brief,
        deliverable=body.deliverable,
        title=body.title,
        source=source,
        repeat=body.repeat,
        run_now=body.run_now,
    )
    return {"task": task.card()}


@router.post("/api/background/parse")
async def background_parse(body: ParseIn) -> dict[str, Any]:
    """Повтор в начале текста задачи («каждый вечер …») — показать в форме новой задачи до её создания."""
    repeat, rest = parse_repeat(body.text[:MAX_BRIEF])
    title = title_of(rest) if rest.strip() else ""
    return {"repeat": repeat, "repeat_text": describe(repeat), "title": title}


@router.get("/api/background/vault")
async def background_vault(ctx: Ctx, refresh: bool = False) -> dict[str, Any]:
    vault = _service(ctx).vault
    if refresh:
        await ctx.run_io(vault.vaults, True)
    return await ctx.run_io(vault.state)


@router.post("/api/background/vault/setup")
async def background_vault_setup(ctx: Ctx) -> dict[str, Any]:
    """Создать свою папку агента в хранилище (README, Инструкции, папки) — кнопка в настройках."""
    vault = _service(ctx).vault
    await ctx.run_io(vault.ensure)
    return await ctx.run_io(vault.state)


@router.get("/api/background/{task_id}")
async def background_get(task_id: str, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    return {"task": service.full(task_id)}


@router.post("/api/background/{task_id}/answer")
async def background_answer(task_id: str, body: AnswerIn, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    return {"task": service.answer(task_id, body.question_id, body.text).card()}


@router.post("/api/background/{task_id}/cancel")
async def background_cancel(task_id: str, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    return {"task": (await service.cancel(task_id)).card()}


@router.post("/api/background/{task_id}/retry")
async def background_retry(task_id: str, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    return {"task": service.retry(task_id).card()}


@router.post("/api/background/{task_id}/pause")
async def background_pause(task_id: str, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    return {"task": service.pause(task_id).card()}


@router.post("/api/background/{task_id}/resume")
async def background_resume(task_id: str, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    return {"task": service.resume(task_id).card()}


@router.put("/api/background/{task_id}/repeat")
async def background_repeat(task_id: str, body: RepeatIn, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    return {"task": service.set_repeat(task_id, body.repeat).card()}


@router.post("/api/background/{task_id}/notebooklm")
async def background_notebooklm(task_id: str, ctx: Ctx) -> dict[str, Any]:
    """Итог и источники задачи — файлом для NotebookLM: {path, link, urls}."""
    service = _service(ctx)
    _known(service, task_id)
    return await service.notebooklm(task_id)


@router.get("/api/background/{task_id}/tables")
async def background_tables(task_id: str, ctx: Ctx) -> dict[str, Any]:
    """Таблицы замеров задачи: [{path, name, columns, rows, total, numeric, link}]."""
    service = _service(ctx)
    _known(service, task_id)
    return {"items": await service.tables(task_id)}


@router.delete("/api/background/{task_id}")
async def background_delete(task_id: str, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    await service.delete(task_id)
    return {"ok": True}
