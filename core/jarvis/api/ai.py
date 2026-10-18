"""Маршруты API: Jarvis AI — провайдеры, модели, проверка связи, расходы, чаты, агент (docs/CONTRACTS.md,
раздел 4).
"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.ai.service import Ai
from jarvis.api.deps import Ctx
from jarvis.context import AppContext

router = APIRouter()

Mode = Literal["chat", "all", "agent"]


def _ai(ctx: AppContext) -> Ai:
    ai = ctx.ai
    if not isinstance(ai, Ai):
        raise HTTPException(503, detail="Jarvis AI недоступен")
    return ai


def _chat(ai: Ai, chat_id: str) -> dict[str, Any]:
    chat = ai.chats.get(chat_id)
    if chat is None:
        raise HTTPException(404, detail="Чат не найден")
    return chat


# --- провайдеры и расходы ---------------------------------------------------------------------------------


@router.get("/api/ai/providers")
async def providers(ctx: Ctx) -> dict[str, Any]:
    return {"items": _ai(ctx).providers()}


@router.get("/api/ai/models")
async def models(ctx: Ctx) -> dict[str, Any]:
    return {"items": await _ai(ctx).llm.list_models()}


@router.post("/api/ai/test")
async def test(ctx: Ctx) -> dict[str, Any]:
    return await _ai(ctx).llm.test()


@router.get("/api/ai/usage")
async def usage(ctx: Ctx) -> dict[str, Any]:
    return _ai(ctx).usage()


# --- чаты -------------------------------------------------------------------------------------------------


class ChatIn(BaseModel):
    title: str = ""
    mode: Mode | None = None


class ChatPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    mode: Mode | None = None
    model: str | None = None


class MessageIn(BaseModel):
    text: str = ""
    images: list[str] = Field(default_factory=list)
    mode: Mode | None = None


@router.get("/api/chats")
async def list_chats(ctx: Ctx) -> dict[str, Any]:
    return {"items": _ai(ctx).chats.all()}


@router.post("/api/chats")
async def create_chat(body: ChatIn, ctx: Ctx) -> dict[str, Any]:
    return _ai(ctx).create_chat(body.title, body.mode)


@router.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    ai = _ai(ctx)
    _chat(ai, chat_id)
    return ai.get_chat(chat_id)


@router.patch("/api/chats/{chat_id}")
async def patch_chat(chat_id: str, body: ChatPatch, ctx: Ctx) -> dict[str, Any]:
    ai = _ai(ctx)
    _chat(ai, chat_id)
    return ai.chats.update(chat_id, **body.model_dump(exclude_none=True))


@router.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    ai = _ai(ctx)
    _chat(ai, chat_id)
    ai.delete_chat(chat_id)
    return {"ok": True}


@router.post("/api/chats/{chat_id}/messages")
async def post_message(chat_id: str, body: MessageIn, ctx: Ctx) -> dict[str, Any]:
    ai = _ai(ctx)
    _chat(ai, chat_id)
    return await ai.post_message(chat_id, body.text, images=body.images or None, mode=body.mode)


@router.post("/api/chats/{chat_id}/stop")
async def stop_chat(chat_id: str, ctx: Ctx) -> dict[str, Any]:
    ai = _ai(ctx)
    _chat(ai, chat_id)
    ai.stop_chat(chat_id)
    return {"ok": True}


# --- агент ------------------------------------------------------------------------------------------------


class AgentIn(BaseModel):
    task: str


@router.get("/api/agent")
async def agent_state(ctx: Ctx) -> dict[str, Any]:
    """Текущее состояние агента (для интерфейса после перезагрузки): то же, что событие agent.state."""
    state = getattr(ctx.agent, "state", None)
    if callable(state):
        return dict(state())
    return {"run_id": "", "task": "", "status": "idle", "step": 0, "total": 0, "text": ""}


@router.post("/api/agent/run")
async def agent_run(body: AgentIn, ctx: Ctx) -> dict[str, Any]:
    return {"run_id": await ctx.agent.run(body.task, "ui")}


@router.post("/api/agent/stop")
async def agent_stop(ctx: Ctx) -> dict[str, Any]:
    await ctx.agent.stop_run()
    return {"ok": True}
