"""Учёт бюджета в пределах одного такта (02-domain.md §4).

Runner создаёт `BudgetMeter` на каждый такт и передаёт его стадии; стадия списывает расход до того,
как его совершить. Превышение — `BudgetExceeded`; потраченное до него не теряется, потому что runner
забирает `usage` из счётчика и при исключении.
"""

from typing import Literal

from jarvis.domain.budget import Budget, BudgetLimit, BudgetUsage
from jarvis.domain.errors import BudgetExceeded

CountedLimit = Literal[
    BudgetLimit.STEPS, BudgetLimit.TOOL_CALLS, BudgetLimit.REPLANS, BudgetLimit.MODEL_CALLS
]


class BudgetMeter:
    def __init__(self, budget: Budget, usage: BudgetUsage) -> None:
        self._budget = budget
        self._usage = usage

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    def charge(self, limit: CountedLimit, amount: int = 1) -> None:
        """Списать расход заранее: действие не начинается, если превысит лимит."""
        if amount < 1:
            raise ValueError(f"списание должно быть положительным: {amount}")
        current = int(self._usage.value(limit))
        if current + amount > self._budget.maximum(limit):
            raise self._exceeded(limit, current)
        self._usage = self._usage.model_copy(update={limit.value: current + amount})

    def require_tokens(self) -> None:
        """Перед вызовом модели: токены известны только после него, поэтому проверяется остаток."""
        if self._usage.model_tokens >= self._budget.max_model_tokens:
            raise self._exceeded(BudgetLimit.MODEL_TOKENS, self._usage.model_tokens)

    def add_tokens(self, amount: int) -> None:
        if amount < 0:
            raise ValueError(f"число токенов не может быть отрицательным: {amount}")
        self._usage = self._usage.model_copy(update={"model_tokens": self._usage.model_tokens + amount})

    def record_failure(self) -> None:
        """Сбои считаются по факту; задача останавливается, когда их больше лимита."""
        failures = self._usage.failures + 1
        self._usage = self._usage.model_copy(update={"failures": failures})
        if failures > self._budget.max_failures:
            raise self._exceeded(BudgetLimit.FAILURES, failures)

    def remaining_time_s(self) -> float:
        return max(0.0, self._budget.max_wall_time_s - self._usage.active_time_s)

    def check_time(self) -> None:
        if self._usage.active_time_s >= self._budget.max_wall_time_s:
            raise self._exceeded(BudgetLimit.WALL_TIME, self._usage.active_time_s)

    def add_active_time(self, seconds: float) -> None:
        self._usage = self._usage.model_copy(
            update={"active_time_s": self._usage.active_time_s + max(0.0, seconds)}
        )

    def _exceeded(self, limit: BudgetLimit, value: float) -> BudgetExceeded:
        maximum = self._budget.maximum(limit)
        return BudgetExceeded(
            f"превышен лимит {limit.value}: {value} при максимуме {maximum}",
            limit=limit.value,
            value=value,
            maximum=maximum,
        )
