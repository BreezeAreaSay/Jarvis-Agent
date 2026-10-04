"""Пути задачи через машину состояний со scripted-стадиями."""

import pytest

from jarvis.domain.budget import BudgetUsage
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Route
from tests.helpers import S, agent_prefix, approval_step, request, scripted, step, transitions

pytestmark = pytest.mark.anyio


async def test_agent_path_reaches_completed() -> None:
    app, script = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.EXECUTING, charge={"steps": 1, "tool_calls": 1}),
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1}),
        step(S.VERIFYING, S.COMPLETED, answer="ответ"),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.COMPLETED
    assert transitions(app, task_id) == [S.ROUTING, S.PLANNING, S.EXECUTING, S.VERIFYING, S.COMPLETED]
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "ответ"
    assert snapshot.outcome.error is None
    assert snapshot.route is Route.AGENT
    assert snapshot.budget == JarvisConfig().budgets.agent
    assert (snapshot.usage.steps, snapshot.usage.tool_calls) == (2, 1)
    assert script.remaining == 0


async def test_direct_path_uses_direct_budget() -> None:
    app, _ = scripted(
        step(S.ROUTING, S.EXECUTING, route="direct"),
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1, "tool_calls": 1}),
        step(S.VERIFYING, S.COMPLETED, answer="C:\\projects"),
    )
    task_id = app.tasks.submit(request("покажи текущую директорию"))
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.COMPLETED
    assert transitions(app, task_id) == [S.ROUTING, S.EXECUTING, S.VERIFYING, S.COMPLETED]
    assert snapshot.budget == JarvisConfig().budgets.direct


async def test_clarify_completes_from_routing_with_a_question() -> None:
    app, _ = scripted(step(S.ROUTING, S.COMPLETED, route="clarify", answer="Какой проект?"))
    task_id = app.tasks.submit(request("покажи стек проекта"))
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert transitions(app, task_id) == [S.ROUTING, S.COMPLETED]
    assert snapshot.route is Route.CLARIFY
    assert snapshot.outcome is not None
    assert snapshot.outcome.answer == "Какой проект?"


async def test_replanning_from_executing_and_verifying() -> None:
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.REPLANNING, charge={"steps": 1}),
        step(S.REPLANNING, S.EXECUTING, charge={"replans": 1}),
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1}),
        step(S.VERIFYING, S.REPLANNING),
        step(S.REPLANNING, S.EXECUTING, charge={"replans": 1}),
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1}),
        step(S.VERIFYING, S.COMPLETED),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.COMPLETED
    assert transitions(app, task_id).count(S.REPLANNING) == 2
    assert snapshot.usage.replans == 2


async def test_waiting_confirmation_blocks_the_run() -> None:
    app, script = scripted(*agent_prefix(), approval_step(charge={"steps": 1}))
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.WAITING_CONFIRMATION
    assert snapshot.outcome is None
    # Повторный запуск не вызывает стадий: задачу выводит из ожидания только решение клиента.
    assert await app.tasks.run_until_blocked(task_id) == snapshot
    assert script.remaining == 1  # шаг с вызовом ждёт решения человека


async def test_route_decision_starts_the_route_budget_from_zero() -> None:
    app, _ = scripted(
        step(S.ROUTING, S.PLANNING, route="agent", failures=1),
        step(S.PLANNING, S.FAILED, reason="стоп"),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.usage == BudgetUsage()  # расход маршрутизации учтён в её собственном бюджете
    assert snapshot.budget == JarvisConfig().budgets.agent


async def test_routing_cannot_call_a_model() -> None:
    """Router детерминированный (ADR 0026): у маршрутизации нулевой бюджет вызовов модели."""
    app, _ = scripted(step(S.ROUTING, S.PLANNING, route="agent", charge={"model_calls": 1}))
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)
    assert snapshot.status is S.BUDGET_EXCEEDED


async def test_verifying_can_fail_the_task() -> None:
    app, _ = scripted(
        *agent_prefix(),
        step(S.EXECUTING, S.VERIFYING, charge={"steps": 1}),
        step(S.VERIFYING, S.FAILED, reason="критерий провален, перепланирований нет"),
    )
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)
    assert snapshot.status is S.FAILED
    assert snapshot.outcome is not None
    assert snapshot.outcome.error is None  # штатный исход стадии, а не исключение


PREFIX_TO = {
    S.ROUTING: [],
    S.PLANNING: [step(S.ROUTING, S.PLANNING, route="agent")],
    S.EXECUTING: agent_prefix(),
    S.VERIFYING: [*agent_prefix(), step(S.EXECUTING, S.VERIFYING, charge={"steps": 1})],
    S.REPLANNING: [*agent_prefix(), step(S.EXECUTING, S.REPLANNING, charge={"steps": 1})],
}


@pytest.mark.parametrize("source", list(PREFIX_TO))
async def test_stage_can_fail_the_task_from_any_active_state(source: TaskStatus) -> None:
    app, _ = scripted(*PREFIX_TO[source], step(source, S.FAILED, reason="конец"))
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)

    assert snapshot.status is S.FAILED
    assert transitions(app, task_id)[-2:] == [source, S.FAILED]
