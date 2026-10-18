"""Маршруты API: серверы MCP и каталог реестра MCP (docs/CONTRACTS.md, раздел 4)."""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.mcp.service import Mcp, McpNotFound

router = APIRouter()


class McpServerIn(BaseModel):
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


def _mcp(ctx: AppContext) -> Mcp:
    mcp = ctx.mcp
    if not isinstance(mcp, Mcp):
        raise HTTPException(503, detail="MCP недоступен")
    return mcp


@router.get("/api/mcp/servers")
async def list_servers(ctx: Ctx) -> dict[str, Any]:
    return {"items": _mcp(ctx).servers()}


@router.post("/api/mcp/servers")
async def add_server(body: McpServerIn, ctx: Ctx) -> dict[str, Any]:
    return await _mcp(ctx).add(body.model_dump())


@router.put("/api/mcp/servers/{server_id}")
async def update_server(server_id: str, body: McpServerIn, ctx: Ctx) -> dict[str, Any]:
    try:
        return await _mcp(ctx).update(server_id, body.model_dump())
    except McpNotFound as e:
        raise HTTPException(404, detail=str(e)) from e


@router.delete("/api/mcp/servers/{server_id}")
async def delete_server(server_id: str, ctx: Ctx) -> dict[str, Any]:
    if not await _mcp(ctx).remove(server_id):
        raise HTTPException(404, detail="Сервер MCP не найден")
    return {"ok": True}


@router.post("/api/mcp/servers/{server_id}/reconnect")
async def reconnect_server(server_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return await _mcp(ctx).reconnect(server_id)
    except McpNotFound as e:
        raise HTTPException(404, detail=str(e)) from e


class AiDevIn(BaseModel):
    profiles: list[str] | None = None
    folder: str = ""


class ImportIn(BaseModel):
    keys: list[str] = Field(default_factory=list)


@router.get("/api/mcp/aidev")
async def aidev_state(ctx: Ctx) -> dict[str, Any]:
    return await _mcp(ctx).aidev_state()


@router.post("/api/mcp/aidev")
async def aidev_connect(body: AiDevIn, ctx: Ctx) -> dict[str, Any]:
    return await _mcp(ctx).aidev_connect(body.profiles, body.folder)


@router.get("/api/mcp/import")
async def import_candidates(ctx: Ctx) -> dict[str, Any]:
    return {"items": await _mcp(ctx).import_candidates()}


@router.post("/api/mcp/import")
async def import_servers(body: ImportIn, ctx: Ctx) -> dict[str, Any]:
    return await _mcp(ctx).import_servers(body.keys)


@router.get("/api/mcp/catalog")
async def catalog(ctx: Ctx, q: str = "", cursor: str = "") -> dict[str, Any]:
    return await _mcp(ctx).catalog_search(q, cursor)
