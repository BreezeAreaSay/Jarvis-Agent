"""Маршруты ядра: состояние, настройки, секреты, диспетчер фраз, история, подтверждения."""

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from jarvis import __version__, paths
from jarvis.api.deps import Ctx
from jarvis.settings import SECRET_NAMES

router = APIRouter()
STARTED = time.time()


@router.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@router.post("/api/shutdown")
async def shutdown(ctx: Ctx) -> dict[str, Any]:
    """Штатный выход по просьбе Electron: сервисы останавливаются и закрывают свои файлы."""
    if ctx.request_exit is None:
        raise HTTPException(409, detail="ядро запущено не сервером — выход недоступен")
    ctx.request_exit()
    return {"ok": True}


@router.get("/api/status")
async def status(ctx: Ctx) -> dict[str, Any]:
    return {
        "version": __version__,
        "platform": ctx.platform.name,
        "data_dir": str(paths.data_dir()),
        "uptime_s": round(time.time() - STARTED, 1),
        "voice": {"state": ctx.voice.state},
        "ai": {"configured": ctx.ai.configured},
        "ready": ctx.started.is_set(),
    }


@router.get("/api/settings")
async def get_settings(ctx: Ctx) -> dict[str, Any]:
    return {"settings": ctx.settings.value.model_dump(mode="json"), "secrets": ctx.secrets.status()}


@router.patch("/api/settings")
async def patch_settings(patch: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    try:
        new = ctx.settings.patch(patch)
    except ValidationError as e:
        raise HTTPException(422, detail=_validation_text(e)) from e
    return {"settings": new.model_dump(mode="json")}


class SecretIn(BaseModel):
    name: str
    value: str = ""


@router.put("/api/secrets")
async def put_secret(body: SecretIn, ctx: Ctx) -> dict[str, Any]:
    if body.name not in SECRET_NAMES:
        raise HTTPException(400, detail=f"Неизвестный секрет: {body.name}")
    ctx.secrets.set(body.name, body.value)
    return {"secrets": ctx.secrets.status()}


class DispatchIn(BaseModel):
    text: str
    source: Literal["text", "ui", "voice", "remote"] = "text"
    speak: bool | None = None


@router.post("/api/dispatch")
async def dispatch(body: DispatchIn, ctx: Ctx) -> dict[str, Any]:
    return await ctx.dispatcher.handle(body.text, body.source, speak=body.speak)


@router.get("/api/history")
async def history(ctx: Ctx, limit: int = 50) -> dict[str, Any]:
    items = ctx.db.query("SELECT * FROM history ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
    return {"items": items}


@router.delete("/api/history")
async def clear_history(ctx: Ctx) -> dict[str, Any]:
    ctx.db.execute("DELETE FROM history")
    ctx.bus.publish("history.cleared", {})
    return {"ok": True}


@router.get("/api/confirm")
async def pending_confirmations(ctx: Ctx) -> dict[str, Any]:
    return {"items": ctx.confirmations.pending()}


class ConfirmIn(BaseModel):
    allow: bool


@router.post("/api/confirm/{confirmation_id}")
async def resolve_confirmation(confirmation_id: str, body: ConfirmIn, ctx: Ctx) -> dict[str, Any]:
    if not ctx.confirmations.resolve(confirmation_id, body.allow):
        raise HTTPException(404, detail="Подтверждение уже не ждёт ответа")
    return {"ok": True}


def _validation_text(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', '')}")
    return "; ".join(parts) or str(e)
