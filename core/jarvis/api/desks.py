"""Маршруты API: столы (контракт — docs/CONTRACTS.md, раздел 5).

GET    /api/desks                               — {items: Desk[], current}
PUT    /api/desks                               — {items: Desk[]} сохранить порядок и раскладку всех столов
POST   /api/desks                               — {name?, preset?, icon?} → Desk (из заготовки)
GET    /api/desks/presets                       — {items: [{id, name, description, icon, aliases, widgets}]}
GET    /api/desks/widgets                       — каталог виджетов для «Добавить виджет»
GET    /api/desks/{id}                          — Desk
PATCH  /api/desks/{id}                          — {name?, icon?, widgets?, position?} → Desk
DELETE /api/desks/{id}                          — {ok} (последний стол удалить нельзя — 400)
POST   /api/desks/{id}/open                     — {ok} + событие desk.open
POST   /api/desks/{id}/widgets                  — {type, w?, h?, x?, y?, config?} → Desk (на свободное место)
PATCH  /api/desks/{id}/widgets/{widget_id}      — {config?, x?, y?, w?, h?} → Desk
DELETE /api/desks/{id}/widgets/{widget_id}      — Desk
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.desks.service import DeskNotFoundError, Desks

router = APIRouter()


def _desks(ctx: AppContext) -> Desks:
    service = ctx.desks
    if not isinstance(service, Desks):
        raise HTTPException(503, detail="Столы недоступны")
    return service


def _not_found() -> HTTPException:
    return HTTPException(404, detail="Стол не найден")


class DesksIn(BaseModel):
    items: list[dict[str, Any]]


class DeskCreate(BaseModel):
    name: str = ""
    preset: str = "empty"
    icon: str = ""


class DeskPatch(BaseModel):
    name: str | None = None
    icon: str | None = None
    widgets: list[dict[str, Any]] | None = None
    position: int | None = None


class WidgetIn(BaseModel):
    type: str
    w: int | None = None
    h: int | None = None
    x: int | None = None
    y: int | None = None
    config: dict[str, Any] | None = None


class WidgetPatch(BaseModel):
    config: dict[str, Any] | None = None
    x: int | None = None
    y: int | None = None
    w: int | None = None
    h: int | None = None


@router.get("/api/desks")
async def list_desks(ctx: Ctx) -> dict[str, Any]:
    desks = _desks(ctx)
    return {"items": desks.list(), "current": desks.current_id()}


@router.put("/api/desks")
async def save_desks(body: DesksIn, ctx: Ctx) -> dict[str, Any]:
    return {"items": _desks(ctx).save_all(body.items)}


@router.post("/api/desks")
async def create_desk(body: DeskCreate, ctx: Ctx) -> dict[str, Any]:
    return _desks(ctx).create(body.name, body.preset, body.icon)


@router.get("/api/desks/presets")
async def desk_presets(ctx: Ctx) -> dict[str, Any]:
    return {"items": _desks(ctx).presets()}


@router.get("/api/desks/widgets")
async def widget_catalog(ctx: Ctx) -> dict[str, Any]:
    return {"items": _desks(ctx).widget_types()}


@router.get("/api/desks/{desk_id}")
async def get_desk(desk_id: str, ctx: Ctx) -> dict[str, Any]:
    desk = _desks(ctx).get(desk_id)
    if desk is None:
        raise _not_found()
    return desk


@router.patch("/api/desks/{desk_id}")
async def patch_desk(desk_id: str, body: DeskPatch, ctx: Ctx) -> dict[str, Any]:
    try:
        return _desks(ctx).update(desk_id, body.model_dump(exclude_unset=True))
    except DeskNotFoundError as e:
        raise _not_found() from e


@router.delete("/api/desks/{desk_id}")
async def delete_desk(desk_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        _desks(ctx).delete(desk_id)
    except DeskNotFoundError as e:
        raise _not_found() from e
    return {"ok": True}


@router.post("/api/desks/{desk_id}/open")
async def open_desk(desk_id: str, ctx: Ctx) -> dict[str, Any]:
    desks = _desks(ctx)
    if desks.get(desk_id) is None or desks.open(desk_id) is None:
        raise _not_found()
    return {"ok": True}


@router.post("/api/desks/{desk_id}/widgets")
async def add_widget(desk_id: str, body: WidgetIn, ctx: Ctx) -> dict[str, Any]:
    try:
        return _desks(ctx).add_widget(desk_id, body.model_dump(exclude_none=True))
    except DeskNotFoundError as e:
        raise _not_found() from e


@router.patch("/api/desks/{desk_id}/widgets/{widget_id}")
async def patch_widget(desk_id: str, widget_id: str, body: WidgetPatch, ctx: Ctx) -> dict[str, Any]:
    try:
        return _desks(ctx).update_widget(desk_id, widget_id, body.model_dump(exclude_none=True))
    except DeskNotFoundError as e:
        raise HTTPException(404, detail="Стол или виджет не найден") from e


@router.delete("/api/desks/{desk_id}/widgets/{widget_id}")
async def delete_widget(desk_id: str, widget_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return _desks(ctx).remove_widget(desk_id, widget_id)
    except DeskNotFoundError as e:
        raise HTTPException(404, detail="Стол или виджет не найден") from e
