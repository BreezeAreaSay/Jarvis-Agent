"""WebSocket /ws: поток событий ядра для интерфейса (типы — docs/CONTRACTS.md).

Клиент подключается с ?token=…; первым приходит событие hello {version, settings, secrets, status}.
Сообщения клиента: {"type": "ping"} → pong;
{"type": "subscribe", "patterns": ["voice.*", …]} — сменить фильтр.
"""

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from jarvis import __version__

router = APIRouter()
log = logging.getLogger(__name__)


@router.websocket("/ws")
async def events(ws: WebSocket) -> None:
    ctx = ws.app.state.ctx
    token = ws.query_params.get("token", "")
    if token != ws.app.state.token:
        await ws.close(code=4401)
        return
    await ws.accept()
    sub = ctx.bus.subscribe()
    hello: dict[str, Any] = {
        "type": "hello",
        "data": {
            "version": __version__,
            "platform": ctx.platform.name,
            "settings": ctx.settings.value.model_dump(mode="json"),
            "secrets": ctx.secrets.status(),
            "voice": {"state": ctx.voice.state},
            "ai": {"configured": ctx.ai.configured},
        },
        "ts": 0,
    }
    await ws.send_text(json.dumps(hello, ensure_ascii=False))

    async def pump() -> None:
        async for event in sub:
            await ws.send_text(json.dumps(event.to_json(), ensure_ascii=False, default=str))

    sender = asyncio.create_task(pump())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "ping":
                await ws.send_text(json.dumps({"type": "pong", "data": {}, "ts": 0}))
            elif kind == "subscribe":
                patterns = tuple(str(p) for p in msg.get("patterns") or ("*",))
                sub.patterns = patterns
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket упал")
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
        sub.close()
