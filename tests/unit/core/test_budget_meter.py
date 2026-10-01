import pytest

from jarvis.core.budget import BudgetMeter, CountedLimit
from jarvis.domain.budget import Budget, BudgetLimit, BudgetUsage
from jarvis.domain.errors import BudgetExceeded

BUDGET = Budget(
    max_steps=2,
    max_tool_calls=3,
    max_failures=1,
    max_replans=1,
    max_wall_time_s=10,
    max_model_calls=2,
    max_model_tokens=100,
)


@pytest.mark.parametrize(
    ("limit", "maximum"),
    [
        (BudgetLimit.STEPS, 2),
        (BudgetLimit.TOOL_CALLS, 3),
        (BudgetLimit.REPLANS, 1),
        (BudgetLimit.MODEL_CALLS, 2),
    ],
)
def test_counted_limits_are_inclusive(limit: CountedLimit, maximum: int) -> None:
    meter = BudgetMeter(BUDGET, BudgetUsage())
    for _ in range(maximum):
        meter.charge(limit)
    with pytest.raises(BudgetExceeded) as raised:
        meter.charge(limit)
    assert raised.value.details == {"limit": limit.value, "value": maximum, "maximum": maximum}
    assert meter.usage.value(limit) == maximum  # отклонённое действие не списано


def test_charge_checks_the_whole_amount() -> None:
    meter = BudgetMeter(BUDGET, BudgetUsage(tool_calls=2))
    with pytest.raises(BudgetExceeded):
        meter.charge(BudgetLimit.TOOL_CALLS, 2)
    meter.charge(BudgetLimit.TOOL_CALLS, 1)
    assert meter.usage.tool_calls == 3
    with pytest.raises(ValueError, match="положительным"):
        meter.charge(BudgetLimit.TOOL_CALLS, 0)


def test_tokens_are_checked_before_a_call_and_counted_after() -> None:
    meter = BudgetMeter(BUDGET, BudgetUsage())
    meter.require_tokens()
    meter.add_tokens(150)  # ответ может превысить остаток: токены известны только после вызова
    assert meter.usage.model_tokens == 150
    with pytest.raises(BudgetExceeded) as raised:
        meter.require_tokens()
    assert raised.value.details["limit"] == "model_tokens"
    with pytest.raises(ValueError, match="отрицательным"):
        meter.add_tokens(-1)


def test_failures_stop_when_above_the_limit() -> None:
    meter = BudgetMeter(BUDGET, BudgetUsage())
    meter.record_failure()
    with pytest.raises(BudgetExceeded) as raised:
        meter.record_failure()
    assert raised.value.details == {"limit": "failures", "value": 2, "maximum": 1}
    assert meter.usage.failures == 2


def test_wall_time() -> None:
    meter = BudgetMeter(BUDGET, BudgetUsage(active_time_s=4))
    assert meter.remaining_time_s() == 6
    meter.check_time()
    meter.add_active_time(6)
    assert meter.remaining_time_s() == 0
    with pytest.raises(BudgetExceeded) as raised:
        meter.check_time()
    assert raised.value.details["limit"] == "wall_time"
