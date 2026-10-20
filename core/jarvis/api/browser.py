"""Маршруты API: браузер Jarvis для агента — состояние, открыть окно (войти в сайты), отключиться
(docs/CONTRACTS.md, раздел 4)."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.ai.browser.service import Browser
from jarvis.api.deps import Ctx
from jarvis.context import AppContext

router = APIRouter()


class OpenIn(BaseModel):
    url: str = ""


def _browser(ctx: AppContext) -> Browser:
    browser = getattr(ctx, "browser", None)
    if not isinstance(browser, Browser):
        raise HTTPException(503, detail="Браузер Jarvis недоступен")
    return browser


@router.get("/api/browser")
async def browser_state(ctx: Ctx) -> dict[str, Any]:
    return _browser(ctx).state()


@router.post("/api/browser/open")
async def browser_open(body: OpenIn, ctx: Ctx) -> dict[str, Any]:
    """Кнопка «Открыть браузер Jarvis»: включить (если выключен), открыть окно или вывести его вперёд —
    человек входит в сайты, где агент будет работать."""
    browser = _browser(ctx)
    if not browser.enabled:
        ctx.settings.patch({"ai": {"browser": True}})
    await browser.connect(launch=True)
    url = body.url.strip()
    tabs = await browser.tabs()
    if url.startswith(("http://", "https://")):
        target = await browser.open_tab(url)
    elif tabs:
        target = str(tabs[-1]["targetId"])
    else:
        target = await browser.open_tab("about:blank")
    title = str(next((t.get("title") for t in await browser.tabs() if t["targetId"] == target), "") or "")
    await browser.activate(target, title)
    return browser.state()


@router.post("/api/browser/disconnect")
async def browser_disconnect(ctx: Ctx) -> dict[str, Any]:
    """Отключиться от браузера Jarvis (окна остаются; агент подключится снова, когда понадобится сайт)."""
    browser = _browser(ctx)
    await browser.disconnect(status="closed" if browser.enabled else "off")
    return browser.state()
