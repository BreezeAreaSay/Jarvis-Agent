"""Маршруты API: каталог инструментов Jarvis AI (docs/CONTRACTS.md, раздел 4).

GET /api/ai/tools — {items: ToolInfo[], groups: [{id, title}], agent_confirm}. Разрешения меняются через
PATCH /api/settings {ai: {tool_policy: {<policy_key>: "allow" | "ask" | "deny" | "auto"}}}.
"""

from typing import Any

from fastapi import APIRouter

from jarvis.ai.catalog import describe
from jarvis.api.deps import Ctx

router = APIRouter()


@router.get("/api/ai/tools")
async def tools(ctx: Ctx) -> dict[str, Any]:
    return describe(ctx)
