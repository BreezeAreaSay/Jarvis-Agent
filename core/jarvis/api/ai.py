"""Маршруты API: Jarvis AI — провайдеры, модели, проверка связи, расходы, ChatGPT по подписке,
программы-провайдеры (Claude Code, Gemini CLI, Qwen Code), сервер MCP Jarvis, чаты, агент
(docs/CONTRACTS.md, раздел 4).
"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from jarvis.ai.cli import CliBackend
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
    return {"items": await _ai(ctx).list_models()}


@router.post("/api/ai/test")
async def test(ctx: Ctx) -> dict[str, Any]:
    return await _ai(ctx).test_connection()


@router.get("/api/ai/usage")
async def usage(ctx: Ctx) -> dict[str, Any]:
    return _ai(ctx).usage()


# --- ChatGPT по подписке (Codex): компонент и вход ----------------------------------------------------------


class ChatGptLoginIn(BaseModel):
    device: bool = False  # вход кодом на другом устройстве (без браузера на этом ПК)


@router.get("/api/ai/chatgpt")
async def chatgpt_status(ctx: Ctx) -> dict[str, Any]:
    return await _ai(ctx).chatgpt.status()


@router.post("/api/ai/chatgpt/install")
async def chatgpt_install(ctx: Ctx) -> dict[str, Any]:
    return await _ai(ctx).chatgpt.install()


@router.post("/api/ai/chatgpt/login")
async def chatgpt_login(ctx: Ctx, body: ChatGptLoginIn | None = None) -> dict[str, Any]:
    return await _ai(ctx).chatgpt.login(device=bool(body and body.device))


@router.post("/api/ai/chatgpt/login/cancel")
async def chatgpt_login_cancel(ctx: Ctx) -> dict[str, Any]:
    return await _ai(ctx).chatgpt.cancel_login()


@router.post("/api/ai/chatgpt/logout")
async def chatgpt_logout(ctx: Ctx) -> dict[str, Any]:
    return await _ai(ctx).chatgpt.logout()


# --- программы-провайдеры: Claude Code, Gemini CLI, Qwen Code -----------------------------------------------


def _cli(ctx: AppContext, provider: str) -> CliBackend:
    backend = _ai(ctx).cli.get(provider)
    if backend is None:
        raise HTTPException(404, detail="Нет такой программы-провайдера")
    return backend


@router.get("/api/ai/cli/{provider}")
async def cli_status(provider: str, ctx: Ctx, refresh: bool = False) -> dict[str, Any]:
    return await _cli(ctx, provider).status(refresh=refresh)


@router.post("/api/ai/cli/{provider}/install")
async def cli_install(provider: str, ctx: Ctx) -> dict[str, Any]:
    return await _cli(ctx, provider).install()


@router.post("/api/ai/cli/{provider}/login")
async def cli_login(provider: str, ctx: Ctx) -> dict[str, Any]:
    return await _cli(ctx, provider).open_login()


# --- сервер MCP Jarvis (инструменты для программ-провайдеров) -----------------------------------------------


class _Asgi(Response):
    """Ответ, который целиком отдаёт ASGI-обработчик (SDK MCP пишет ответ сам)."""

    def __init__(self, handler: Any) -> None:
        super().__init__()
        self._handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._handler(scope, receive, send)


@router.api_route("/api/ai/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def mcp_endpoint(request: Request, ctx: Ctx) -> Response:
    """Streamable HTTP MCP: токен ядра проверил middleware, токен хода (X-Jarvis-Turn) — сервер MCP."""
    return _Asgi(_ai(ctx).mcp_server.handle)


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


class AgentScenarioIn(BaseModel):
    chat_id: str = ""
    message_id: str = ""


@router.post("/api/agent/scenario")
async def agent_scenario(body: AgentScenarioIn, ctx: Ctx) -> dict[str, Any]:
    """Удачный прогон агента (итог в чате с meta.recipe; пусто — последний) → сценарий с теми же шагами."""
    return {"scenario": ctx.agent.save_scenario(body.chat_id, body.message_id)}
