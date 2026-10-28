"""Маршруты API: фоновые задачи и хранилище заметок (docs/CONTRACTS.md, раздел 8)."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.api.deps import Ctx
from jarvis.background.service import Background
from jarvis.context import AppContext

router = APIRouter()


class TaskIn(BaseModel):
    brief: str
    deliverable: str = ""
    title: str = ""
    source: str = "ui"


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
    task = _service(ctx).create(body.brief, deliverable=body.deliverable, title=body.title, source=source)
    return {"task": task.card()}


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


@router.delete("/api/background/{task_id}")
async def background_delete(task_id: str, ctx: Ctx) -> dict[str, Any]:
    service = _service(ctx)
    _known(service, task_id)
    await service.delete(task_id)
    return {"ok": True}
