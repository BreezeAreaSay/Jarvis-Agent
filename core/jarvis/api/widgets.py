"""Маршруты API: данные виджетов и кнопки плеера (контракт — docs/CONTRACTS.md, раздел 5).

GET  /api/widgets/{kind}?…          — данные виджета (параметры запроса → params)
POST /api/widgets/{kind}/{action}   — действие из виджета: apps/launch {app_id}, recent/open {path},
                                      quick/run {text | url}, smarthome/<действие> {…}
POST /api/media/{action}            — play_pause | next | prev | stop | volume_up | volume_down | mute |
                                      unmute | volume (тело {level}); для громкости ответ {ok, volume, muted}
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.widgets.media import ACTIONS as MEDIA_ACTIONS
from jarvis.widgets.media import media_action
from jarvis.widgets.service import KINDS, Widgets

router = APIRouter()
OptionalBody = Annotated[dict[str, Any] | None, Body()]


def _widgets(ctx: AppContext) -> Widgets:
    service = ctx.widgets
    if not isinstance(service, Widgets):
        raise HTTPException(503, detail="Виджеты недоступны")
    return service


@router.get("/api/widgets/{kind}")
async def widget_data(kind: str, request: Request, ctx: Ctx) -> dict[str, Any]:
    if kind not in KINDS:
        raise HTTPException(404, detail=f"Неизвестный виджет: {kind}")
    params = {key: value for key, value in request.query_params.items() if key != "token"}
    return await ctx.widgets.data(kind, params)


@router.post("/api/widgets/{kind}/{action}")
async def widget_action(kind: str, action: str, ctx: Ctx, body: OptionalBody = None) -> dict[str, Any]:
    return await _widgets(ctx).action(kind, action, body or {})


@router.post("/api/media/{action}")
async def media(action: str, ctx: Ctx, body: OptionalBody = None) -> dict[str, Any]:
    if action not in MEDIA_ACTIONS:
        raise HTTPException(404, detail=f"Неизвестное действие: {action}")
    args = body or {}
    return await media_action(ctx, action, level=args.get("level"), step=args.get("step") or 10)
