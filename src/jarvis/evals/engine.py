"""Прогон сценариев: временное приложение, авто-клиент, проверка ожиданий, отчёт."""

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel

from jarvis.app.composition import build_app
from jarvis.config import with_overrides
from jarvis.domain.budget import BudgetUsage
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, TaskRequest, TaskSnapshot
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.evals.scenario import Expectation, Scenario
from jarvis.evals.scripted import ScriptedStages

SCENARIO_TIMEOUT_S = 30.0


class ScenarioResult(BaseModel, frozen=True):
    id: str
    passed: bool
    status: TaskStatus
    transitions: list[TaskStatus]
    usage: BudgetUsage
    problems: list[str]
    duration_ms: int


class EvalReport(BaseModel, frozen=True):
    mode: str = "scripted"
    started_at: datetime
    results: list[ScenarioResult]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


async def run_scenarios(scenarios: Sequence[Scenario], config: JarvisConfig) -> EvalReport:
    started_at = datetime.now(UTC)
    results = [await run_scenario(scenario, config) for scenario in scenarios]
    return EvalReport(started_at=started_at, results=results)


async def run_scenario(
    scenario: Scenario, config: JarvisConfig, *, timeout_s: float = SCENARIO_TIMEOUT_S
) -> ScenarioResult:
    started = time.perf_counter()
    if scenario.budget:
        routes = ("direct", "chat", "agent")
        config = with_overrides(config, {"budgets": dict.fromkeys(routes, scenario.budget)})
    script = ScriptedStages(scenario.script)
    app = build_app(config, stages=script.handlers())
    task_id = app.tasks.submit(TaskRequest(text=scenario.input, origin=Origin.EVAL))

    problems: list[str] = []
    try:
        async with asyncio.timeout(timeout_s):
            run = asyncio.create_task(app.tasks.run_until_blocked(task_id))
            if scenario.client.cancel_on_hang:
                hung = asyncio.create_task(script.hung.wait())
                await asyncio.wait({run, hung}, return_when=asyncio.FIRST_COMPLETED)
                if hung.done():
                    app.tasks.cancel(task_id, "отмена клиентом eval")
                hung.cancel()
            await run
    except TimeoutError:
        problems.append(f"сценарий не завершился за {timeout_s} с")

    snapshot = app.tasks.get(task_id)
    events = app.tasks.trace(task_id)
    transitions = [
        TaskStatus(str(event.payload["to"])) for event in events if event.kind is EventKind.TASK_TRANSITION
    ]
    problems.extend(_check(scenario.expect, snapshot, transitions, events))
    if script.remaining:
        problems.append(f"не проиграно шагов сценария: {script.remaining}")
    return ScenarioResult(
        id=scenario.id,
        passed=not problems,
        status=snapshot.status,
        transitions=transitions,
        usage=snapshot.usage,
        problems=problems,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )


def _check(
    expect: Expectation,
    snapshot: TaskSnapshot,
    transitions: list[TaskStatus],
    events: list[TraceEvent],
) -> list[str]:
    problems: list[str] = []
    if snapshot.status is not expect.status:
        problems.append(f"статус {snapshot.status}, ожидался {expect.status}")
    if expect.transitions is not None and transitions != expect.transitions:
        actual = " → ".join(transitions)
        expected = " → ".join(expect.transitions)
        problems.append(f"переходы {actual}, ожидались {expected}")
    for field, expected_value in (expect.usage or {}).items():
        actual_value = getattr(snapshot.usage, field)
        if actual_value != expected_value:
            problems.append(f"usage.{field} = {actual_value}, ожидалось {expected_value}")
    if expect.error_category is not None:
        error = snapshot.outcome.error if snapshot.outcome else None
        category = error.category if error else None
        if category != expect.error_category:
            problems.append(f"категория ошибки {category}, ожидалась {expect.error_category}")
    if expect.budget_limit is not None:
        limits = [event.payload.get("limit") for event in events if event.kind is EventKind.BUDGET_EXCEEDED]
        if limits != [expect.budget_limit.value]:
            problems.append(f"события budget.exceeded {limits}, ожидалось [{expect.budget_limit.value}]")
    return problems
