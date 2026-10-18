"""Маршруты API: сценарии (контракт — docs/CONTRACTS.md, раздел «Сценарии»).

GET    /api/scenarios                  — {items: Scenario[]}
POST   /api/scenarios                  — ScenarioIn → Scenario
GET    /api/scenarios/templates        — {items: ScenarioTemplate[]}
POST   /api/scenarios/from-template    — {template_id} → Scenario (включённый)
GET    /api/scenarios/{id}             — Scenario
PATCH  /api/scenarios/{id}             — часть ScenarioIn → Scenario
DELETE /api/scenarios/{id}             — {ok}
POST   /api/scenarios/{id}/run         — ExecResult (условия не проверяются)
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.api.deps import Ctx
from jarvis.context import AppContext
from jarvis.scenarios.model import ScenarioIn, ScenarioPatch
from jarvis.scenarios.service import ScenarioNotFoundError, Scenarios

router = APIRouter()


def _svc(ctx: AppContext) -> Scenarios:
    service = getattr(ctx, "scenarios", None)
    if not isinstance(service, Scenarios):
        raise HTTPException(503, detail="Сценарии недоступны")
    return service


class FromTemplateIn(BaseModel):
    template_id: str


@router.get("/api/scenarios")
async def list_scenarios(ctx: Ctx) -> dict[str, Any]:
    return {"items": _svc(ctx).list_all()}


@router.post("/api/scenarios")
async def create_scenario(body: ScenarioIn, ctx: Ctx) -> dict[str, Any]:
    return _svc(ctx).create(body)


@router.get("/api/scenarios/templates")
async def scenario_templates(ctx: Ctx) -> dict[str, Any]:
    return {"items": _svc(ctx).templates()}


@router.post("/api/scenarios/from-template")
async def scenario_from_template(body: FromTemplateIn, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).from_template(body.template_id)
    except ScenarioNotFoundError as e:
        raise HTTPException(404, detail=str(e)) from None


@router.get("/api/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).require(scenario_id)
    except ScenarioNotFoundError as e:
        raise HTTPException(404, detail=str(e)) from None


@router.patch("/api/scenarios/{scenario_id}")
async def patch_scenario(scenario_id: str, body: ScenarioPatch, ctx: Ctx) -> dict[str, Any]:
    try:
        return _svc(ctx).update(scenario_id, body)
    except ScenarioNotFoundError as e:
        raise HTTPException(404, detail=str(e)) from None


@router.delete("/api/scenarios/{scenario_id}")
async def delete_scenario(scenario_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        _svc(ctx).delete(scenario_id)
    except ScenarioNotFoundError as e:
        raise HTTPException(404, detail=str(e)) from None
    return {"ok": True}


@router.post("/api/scenarios/{scenario_id}/run")
async def run_scenario(scenario_id: str, ctx: Ctx) -> dict[str, Any]:
    try:
        result = await _svc(ctx).run(scenario_id, source="ui")
    except ScenarioNotFoundError as e:
        raise HTTPException(404, detail=str(e)) from None
    return result.to_json()
