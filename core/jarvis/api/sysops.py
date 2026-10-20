"""Маршруты API: «Системные операции» — датчики и микшер громкости (docs/CONTRACTS.md, раздел 5).

GET  /api/system/sensors — {cpu_temp, gpu_temp, gpu_name, gpu_load, top: [{name, cpu, ram_mb}], updated}
GET  /api/system/mixer   — {master: {volume, muted}, items: [{app, title, level, muted, active}]}
POST /api/system/mixer   — {app, level} → то же, что GET (громкость одной программы)
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.widgets.sysops import SysOps

router = APIRouter()


def _sysops(ctx: AppContext) -> SysOps:
    service = getattr(ctx, "sysops", None)
    if not isinstance(service, SysOps):
        raise HTTPException(503, detail="«Системные операции» недоступны")
    return service


class AppVolumeIn(BaseModel):
    app: str
    level: int = Field(ge=0, le=100)


@router.get("/api/system/sensors")
async def sensors(ctx: Ctx) -> dict[str, Any]:
    return await _sysops(ctx).sensors()


@router.get("/api/system/mixer")
async def mixer(ctx: Ctx) -> dict[str, Any]:
    return await _sysops(ctx).mixer()


@router.post("/api/system/mixer")
async def set_app_volume(body: AppVolumeIn, ctx: Ctx) -> dict[str, Any]:
    return await _sysops(ctx).set_app_volume(body.app, body.level)
