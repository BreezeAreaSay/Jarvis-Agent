"""Маршруты API: плагины (контракт — docs/CONTRACTS.md §5).

GET /api/plugins — список Plugin; PUT /api/plugins/{id} {enabled?, config?} — включить и настроить;
POST /api/plugins/{id}/action {action, args} — действие плагина ({ok, …}).
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.plugins.manager import Plugins

router = APIRouter()


class PluginUpdate(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class PluginActionIn(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)


def _manager(ctx: AppContext, plugin_id: str) -> Plugins:
    manager = ctx.plugins
    if not isinstance(manager, Plugins):
        raise HTTPException(503, detail="Плагины недоступны")
    if not manager.has(plugin_id):
        raise HTTPException(404, detail="Плагин не найден")
    return manager


@router.get("/api/plugins")
async def list_plugins(ctx: Ctx) -> dict[str, Any]:
    return {"items": ctx.plugins.list()}


@router.put("/api/plugins/{plugin_id}")
async def update_plugin(plugin_id: str, body: PluginUpdate, ctx: Ctx) -> dict[str, Any]:
    manager = _manager(ctx, plugin_id)
    return await manager.update(plugin_id, enabled=body.enabled, config=body.config)


@router.post("/api/plugins/{plugin_id}/action")
async def plugin_action(plugin_id: str, body: PluginActionIn, ctx: Ctx) -> dict[str, Any]:
    manager = _manager(ctx, plugin_id)
    return await manager.action(plugin_id, body.action, body.args)
