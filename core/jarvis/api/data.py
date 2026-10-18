"""Маршруты API: «Приватность и данные» — что хранится, очистка, сроки хранения, журналы (docs/CONTRACTS.md,
раздел 7).

GET    /api/data/inventory  — {dir, logs_dir, db_bytes, purged_at, items: DataCategory[]}
POST   /api/data/clear      — {scopes: [id…] | ["all"]} → {ok, cleared: {id: сколько}}
POST   /api/data/purge      — очистить по срокам сейчас → {ok, removed: {…}}
GET    /api/logs            — {dir, items: [{name, size, modified}]}
DELETE /api/logs            — {ok, cleared}
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.retention import Retention

router = APIRouter()


def _svc(ctx: AppContext) -> Retention:
    service = getattr(ctx, "retention", None)
    if not isinstance(service, Retention):
        raise HTTPException(503, detail="Раздел данных недоступен")
    return service


class ClearIn(BaseModel):
    scopes: list[str] = Field(min_length=1)


@router.get("/api/data/inventory")
async def inventory(ctx: Ctx) -> dict[str, Any]:
    return await ctx.run_io(_svc(ctx).inventory)


@router.post("/api/data/clear")
async def clear(body: ClearIn, ctx: Ctx) -> dict[str, Any]:
    return {"ok": True, "cleared": await _svc(ctx).clear(body.scopes)}


@router.post("/api/data/purge")
async def purge(ctx: Ctx) -> dict[str, Any]:
    return {"ok": True, "removed": await _svc(ctx).purge()}


@router.get("/api/logs")
async def logs(ctx: Ctx) -> dict[str, Any]:
    service = _svc(ctx)
    return {"dir": str(service.logs_dir()), "items": await ctx.run_io(service.logs)}


@router.delete("/api/logs")
async def clear_logs(ctx: Ctx) -> dict[str, Any]:
    return {"ok": True, "cleared": await ctx.run_io(_svc(ctx).clear_logs)}
