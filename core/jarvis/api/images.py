"""Маршруты API: картинки Jarvis AI (docs/CONTRACTS.md, раздел 4).

GET  /api/images/status   — {available, model, problem}: есть ли ключ OpenAI, чтобы рисовать
POST /api/images/generate — {prompt, format?: square|landscape|portrait, quality?: low|medium|high,
                            references?: [dataURL]} → {image: dataURL, info: {prompt, size, model, quality},
                            cost}
"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.ai.images import MAX_REFERENCES, SIZES, Images
from jarvis.api.deps import Ctx
from jarvis.context import AppContext

router = APIRouter()


def _images(ctx: AppContext) -> Images:
    service = getattr(ctx, "images", None)
    if not isinstance(service, Images):
        raise HTTPException(503, detail="Рисование картинок недоступно")
    return service


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    format: Literal["square", "landscape", "portrait"] = "square"
    quality: Literal["low", "medium", "high"] = "medium"
    references: list[str] = Field(default_factory=list, max_length=MAX_REFERENCES)


@router.get("/api/images/status")
async def status(ctx: Ctx) -> dict[str, Any]:
    return _images(ctx).status()


@router.post("/api/images/generate")
async def generate(body: GenerateIn, ctx: Ctx) -> dict[str, Any]:
    return await _images(ctx).generate(
        body.prompt, size=SIZES[body.format], quality=body.quality, references=body.references
    )
