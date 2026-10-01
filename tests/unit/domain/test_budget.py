import pytest

from jarvis.domain.budget import Budget, BudgetLimit, BudgetUsage
from jarvis.domain.settings import BudgetsSettings, JarvisConfig
from jarvis.domain.task import Route

BUDGET = Budget(
    max_steps=1,
    max_tool_calls=2,
    max_failures=3,
    max_replans=4,
    max_wall_time_s=5.5,
    max_model_calls=6,
    max_model_tokens=7,
)
USAGE = BudgetUsage(
    steps=10, tool_calls=20, failures=30, replans=40, active_time_s=50.5, model_calls=60, model_tokens=70
)


@pytest.mark.parametrize(
    ("limit", "maximum", "value"),
    [
        (BudgetLimit.STEPS, 1, 10),
        (BudgetLimit.TOOL_CALLS, 2, 20),
        (BudgetLimit.FAILURES, 3, 30),
        (BudgetLimit.REPLANS, 4, 40),
        (BudgetLimit.WALL_TIME, 5.5, 50.5),
        (BudgetLimit.MODEL_CALLS, 6, 60),
        (BudgetLimit.MODEL_TOKENS, 7, 70),
    ],
)
def test_every_limit_maps_to_its_fields(limit: BudgetLimit, maximum: float, value: float) -> None:
    assert BUDGET.maximum(limit) == maximum
    assert USAGE.value(limit) == value


def test_default_budgets_match_the_specification_table() -> None:
    table = {
        "routing": (0, 0, 1, 0, 10, 3, 8_000),
        "direct": (1, 3, 1, 0, 15, 0, 0),
        "chat": (1, 0, 1, 0, 60, 3, 16_000),
        "agent": (20, 30, 5, 3, 300, 60, 250_000),
    }
    budgets = JarvisConfig().budgets
    for route, values in table.items():
        budget: Budget = getattr(budgets, route)
        assert (
            budget.max_steps,
            budget.max_tool_calls,
            budget.max_failures,
            budget.max_replans,
            budget.max_wall_time_s,
            budget.max_model_calls,
            budget.max_model_tokens,
        ) == values, route


@pytest.mark.parametrize(
    ("route", "field"),
    [
        (None, "routing"),
        (Route.CLARIFY, "routing"),
        (Route.DIRECT, "direct"),
        (Route.CHAT, "chat"),
        (Route.AGENT, "agent"),
    ],
)
def test_budget_for_route(route: Route | None, field: str) -> None:
    budgets = BudgetsSettings()
    assert budgets.for_route(route) == getattr(budgets, field)


def test_budget_rejects_negative_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        Budget.model_validate({**BUDGET.model_dump(), "max_steps": -1})
    with pytest.raises(ValueError, match="max_stepz"):
        Budget.model_validate({**BUDGET.model_dump(), "max_stepz": 1})
