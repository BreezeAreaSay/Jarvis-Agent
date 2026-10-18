"""Маршруты API: команды, паки, программы «Пуска» (контракт — docs/CONTRACTS.md §2).

id команд бывают с точками и двоеточиями («builtin.media.play», «desks:abc», «plugin.pet:feed»), поэтому
в путях конвертер {command_id:path}, а постоянные маршруты (/actions, /test, /record, /build) объявлены
раньше.
"""

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.api.deps import Ctx
from jarvis.commands import catalog
from jarvis.commands.model import CommandIn
from jarvis.commands.service import CommandNotFoundError

if TYPE_CHECKING:
    from jarvis.commands.service import Commands

router = APIRouter()


def _svc(ctx: Any) -> "Commands":
    return ctx.commands


def _not_found(what: str = "Команда не найдена") -> HTTPException:
    return HTTPException(404, detail=what)


class TextIn(BaseModel):
    text: str


class RunIn(BaseModel):
    slots: dict[str, Any] | None = None


class EnabledIn(BaseModel):
    enabled: bool


class BuildIn(BaseModel):
    description: str


# --- постоянные маршруты ---------------------------------------------------------------------------------


@router.get("/api/commands")
async def list_commands(ctx: Ctx, q: str = "", category: str = "") -> dict[str, Any]:
    return _svc(ctx).list_commands(q, category)


@router.get("/api/commands/actions")
async def action_types() -> dict[str, Any]:
    return {"items": catalog.catalog_json()}


@router.post("/api/commands/test")
async def test_phrase(body: TextIn, ctx: Ctx) -> dict[str, Any]:
    match = _svc(ctx).match(body.text)
    if match is None:
        return {"match": None}
    return {"match": {"text": match.text, "parts": [asdict(p) for p in match.parts]}}


@router.post("/api/commands/record/start")
async def record_start(ctx: Ctx) -> dict[str, Any]:
    _svc(ctx).record_start()
    return {"ok": True}


@router.post("/api/commands/record/stop")
async def record_stop(ctx: Ctx) -> dict[str, Any]:
    return {"actions": _svc(ctx).record_stop()}


@router.post("/api/commands/build")
async def build_command(body: BuildIn, ctx: Ctx) -> dict[str, Any]:
    return await _svc(ctx).build(body.description)


@router.post("/api/commands")
async def create_command(body: CommandIn, ctx: Ctx) -> dict[str, Any]:
    return _svc(ctx).create(body)


# --- одна команда ----------------------------------------------------------------------------------------


@router.post("/api/commands/{command_id:path}/run")
async def run_command(command_id: str, ctx: Ctx, body: RunIn | None = None) -> dict[str, Any]:
    try:
        result = await _svc(ctx).run_command(command_id, (body.slots if body else None) or {}, "ui")
    except CommandNotFoundError:
        raise _not_found() from None
    return result.to_json()


@router.get("/api/commands/{command_id:path}")
async def get_command(command_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).info(command_id)
    except CommandNotFoundError:
        raise _not_found() from None


@router.put("/api/commands/{command_id:path}")
async def update_command(command_id: str, body: CommandIn, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).update(command_id, body)
    except CommandNotFoundError:
        raise _not_found() from None


@router.patch("/api/commands/{command_id:path}")
async def patch_command(command_id: str, body: EnabledIn, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).set_enabled(command_id, body.enabled)
    except CommandNotFoundError:
        raise _not_found() from None


@router.delete("/api/commands/{command_id:path}")
async def delete_command(command_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        _svc(ctx).delete(command_id)
    except CommandNotFoundError:
        raise _not_found() from None
    return {"ok": True}


# --- паки и программы ------------------------------------------------------------------------------------


@router.get("/api/packs")
async def list_packs(ctx: Ctx) -> dict[str, Any]:
    return _svc(ctx).packs_json()


@router.post("/api/packs/{pack_id}/install")
async def install_pack(pack_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).set_pack(pack_id, True)
    except CommandNotFoundError:
        raise _not_found("Пак не найден") from None


@router.delete("/api/packs/{pack_id}")
async def uninstall_pack(pack_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).set_pack(pack_id, False)
    except CommandNotFoundError:
        raise _not_found("Пак не найден") from None


@router.get("/api/apps")
async def list_apps(ctx: Ctx, refresh: int = 0) -> dict[str, Any]:
    return {"items": await _svc(ctx).apps_json(bool(refresh))}
