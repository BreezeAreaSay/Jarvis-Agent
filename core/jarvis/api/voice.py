"""Маршруты API: голос и синтез речи (контракт — docs/CONTRACTS.md, раздел 3)."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.voice import models as m
from jarvis.voice.audio import AudioError, list_devices
from jarvis.voice.service import Voice
from jarvis.voice.tts import Tts

router = APIRouter()


def _voice(ctx: AppContext) -> Voice | None:
    return ctx.voice if isinstance(ctx.voice, Voice) else None


def _tts(ctx: AppContext) -> Tts | None:
    return ctx.tts if isinstance(ctx.tts, Tts) else None


def _manager(ctx: AppContext) -> m.ModelManager:
    voice = _voice(ctx)
    return voice.models if voice is not None else m.manager_for(ctx)


def _status(ctx: AppContext) -> dict[str, Any]:
    voice = _voice(ctx)
    if voice is not None:
        return voice.status()
    s = ctx.settings.value.voice
    return {
        "state": ctx.voice.state,
        "enabled": s.enabled,
        "error": "",
        "wake_words": list(s.wake_words),
        "models_ready": False,
        "input_device": s.input_device,
    }


def _spec(ctx: AppContext, model_id: str) -> m.ModelSpec:
    spec = _manager(ctx).specs.get(model_id)
    if spec is None:
        raise HTTPException(404, detail=f"Неизвестная модель: {model_id}")
    return spec


# --- голос ----------------------------------------------------------------------------------------


@router.get("/api/voice")
async def voice_status(ctx: Ctx) -> dict[str, Any]:
    return _status(ctx)


class EnabledIn(BaseModel):
    enabled: bool


@router.post("/api/voice/enabled")
async def voice_enabled(body: EnabledIn, ctx: Ctx) -> dict[str, Any]:
    await ctx.voice.set_enabled(body.enabled)
    return _status(ctx)


class ListenIn(BaseModel):
    toggle: bool = False  # хоткей: если Jarvis уже слушает, отвечает или работает агент — остановить


@router.post("/api/voice/listen")
async def voice_listen(ctx: Ctx, body: ListenIn | None = None) -> dict[str, Any]:
    busy = ctx.voice.state in ("listening", "processing", "speaking") or bool(
        getattr(ctx.agent, "busy", False)
    )
    if body is not None and body.toggle and busy:
        await ctx.dispatcher.stop()
        return {"ok": True, "stopped": True}
    await ctx.voice.listen_now()
    return {"ok": True}


@router.post("/api/voice/stop")
async def voice_stop(ctx: Ctx) -> dict[str, Any]:
    await ctx.voice.stop_listening()
    return {"ok": True}


class CaptureIn(BaseModel):
    timeout: float = Field(15.0, ge=1.0, le=120.0)
    punct: bool = True


@router.post("/api/voice/capture")
async def voice_capture(ctx: Ctx, body: CaptureIn | None = None) -> dict[str, Any]:
    """Выслушать одну фразу без активатора и без выполнения (диктовка в поле ввода); "" — не дождались."""
    body = body or CaptureIn()
    text = await ctx.voice.capture_text(body.timeout, punct=body.punct)
    return {"text": text}


@router.get("/api/voice/devices")
async def voice_devices(ctx: Ctx) -> dict[str, Any]:
    try:
        return await ctx.run_io(list_devices)
    except AudioError as e:
        return {"inputs": [], "outputs": [], "error": str(e)}


@router.get("/api/voice/models")
async def voice_models(ctx: Ctx) -> dict[str, Any]:
    manager = _manager(ctx)
    return {"items": manager.catalog(manager.required_ids(ctx.settings.value, manager.installed))}


@router.post("/api/voice/models/{model_id}/download")
async def voice_model_download(model_id: str, ctx: Ctx) -> dict[str, Any]:
    spec = _spec(ctx, model_id)
    _manager(ctx).start(spec.id)
    return {"ok": True}


@router.delete("/api/voice/models/{model_id}")
async def voice_model_delete(model_id: str, ctx: Ctx) -> dict[str, Any]:
    spec = _spec(ctx, model_id)
    await _manager(ctx).delete(spec.id)
    return {"ok": True}


# --- синтез речи ----------------------------------------------------------------------------------


@router.get("/api/tts/voices")
async def tts_voices(ctx: Ctx, engine: str | None = None) -> dict[str, Any]:
    tts = _tts(ctx)
    if tts is None:
        return {"items": []}
    return {"items": await tts.voices(engine or None)}


@router.get("/api/tts/account")
async def tts_account(ctx: Ctx, engine: str | None = None) -> dict[str, Any]:
    """Счёт облачного движка: остаток кредитов ElevenLabs и модели; без счёта — {supported: false}."""
    tts = _tts(ctx)
    if tts is None:
        return {"supported": False}
    return await tts.account(engine or None)


class SayIn(BaseModel):
    text: str


@router.post("/api/tts/say")
async def tts_say(body: SayIn, ctx: Ctx) -> dict[str, Any]:
    await ctx.tts.say(body.text)
    return {"ok": True}


class TestIn(BaseModel):
    text: str | None = None
    engine: str | None = None
    voice: str | None = None


@router.post("/api/tts/test")
async def tts_test(ctx: Ctx, body: TestIn | None = None) -> dict[str, Any]:
    body = body or TestIn()
    tts = _tts(ctx)
    if tts is None:
        await ctx.tts.say(body.text or "Проверка голоса.", interrupt=True)
    else:
        await tts.test(body.text, body.engine or None, body.voice or None)
    return {"ok": True}


@router.post("/api/tts/stop")
async def tts_stop(ctx: Ctx) -> dict[str, Any]:
    ctx.tts.silence()
    return {"ok": True}
