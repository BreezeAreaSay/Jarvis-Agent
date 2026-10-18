"""Маршруты API: пульт в телефоне (контракт — docs/CONTRACTS.md, раздел 5).

GET  /api/remote                 — {lan: {enabled, port, urls, qr_svg, status, error},
                                    telegram: {enabled, status, bot, owner_id, pairing_code, pairing_expires,
                                               pairing_link, token_set, error}}
POST /api/remote/telegram/pair   — {pairing_code, expires, bot, link} — код для «/start <код>» в боте
POST /api/remote/token           — новый токен страницы пульта (старые ссылки перестают работать) → как GET
"""

from typing import Any

from fastapi import APIRouter, HTTPException

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.remote.service import Remote

router = APIRouter()


def _remote(ctx: AppContext) -> Remote:
    service = ctx.remote
    if not isinstance(service, Remote):
        raise HTTPException(503, detail="Пульт недоступен")
    return service


@router.get("/api/remote")
async def remote_info(ctx: Ctx) -> dict[str, Any]:
    return ctx.remote.info()


@router.post("/api/remote/telegram/pair")
async def telegram_pair(ctx: Ctx) -> dict[str, Any]:
    return _remote(ctx).pair()


@router.post("/api/remote/token")
async def regenerate_token(ctx: Ctx) -> dict[str, Any]:
    remote = _remote(ctx)
    remote.regenerate_token()
    return remote.info()
