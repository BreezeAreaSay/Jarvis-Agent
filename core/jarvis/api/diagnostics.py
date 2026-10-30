"""Маршруты API: диагностика («Система» → «Диагностика», docs/CONTRACTS.md, раздел 1).

GET  /api/diagnostics               — {items: [{id, title, status, detail, hint, action}], report}
POST /api/diagnostics/crashes/reset — забыть аварийные остановки: части, где ядро падало, снова пробуются
"""

from typing import Any

from fastapi import APIRouter

from jarvis import diagnostics
from jarvis.api.deps import Ctx

router = APIRouter()


@router.get("/api/diagnostics")
async def diagnostics_info(ctx: Ctx) -> dict[str, Any]:
    return await ctx.run_io(diagnostics.collect, ctx)


@router.post("/api/diagnostics/crashes/reset")
async def diagnostics_reset_crashes(ctx: Ctx) -> dict[str, Any]:
    await ctx.run_io(diagnostics.reset_crashes)
    return await ctx.run_io(diagnostics.collect, ctx)
