"""Бюджеты задачи (02-domain.md §4).

Лимиты — включительные максимумы. Как расходуется бюджет, решает `core.budget.BudgetMeter`; здесь —
только данные.
"""

from enum import StrEnum

from pydantic import BaseModel, NonNegativeFloat, NonNegativeInt, PositiveFloat


class BudgetLimit(StrEnum):
    STEPS = "steps"
    TOOL_CALLS = "tool_calls"
    FAILURES = "failures"
    REPLANS = "replans"
    WALL_TIME = "wall_time"
    MODEL_CALLS = "model_calls"
    MODEL_TOKENS = "model_tokens"


class Budget(BaseModel, frozen=True, extra="forbid"):
    max_steps: NonNegativeInt
    max_tool_calls: NonNegativeInt
    max_failures: NonNegativeInt
    max_replans: NonNegativeInt
    max_wall_time_s: PositiveFloat
    max_model_calls: NonNegativeInt
    max_model_tokens: NonNegativeInt

    def maximum(self, limit: BudgetLimit) -> float:
        match limit:
            case BudgetLimit.STEPS:
                return self.max_steps
            case BudgetLimit.TOOL_CALLS:
                return self.max_tool_calls
            case BudgetLimit.FAILURES:
                return self.max_failures
            case BudgetLimit.REPLANS:
                return self.max_replans
            case BudgetLimit.WALL_TIME:
                return self.max_wall_time_s
            case BudgetLimit.MODEL_CALLS:
                return self.max_model_calls
            case BudgetLimit.MODEL_TOKENS:
                return self.max_model_tokens


class BudgetUsage(BaseModel, frozen=True, extra="forbid"):
    steps: NonNegativeInt = 0
    tool_calls: NonNegativeInt = 0
    failures: NonNegativeInt = 0
    replans: NonNegativeInt = 0
    active_time_s: NonNegativeFloat = 0.0  # без ожидания подтверждения
    model_calls: NonNegativeInt = 0
    model_tokens: NonNegativeInt = 0

    def value(self, limit: BudgetLimit) -> float:
        match limit:
            case BudgetLimit.STEPS:
                return self.steps
            case BudgetLimit.TOOL_CALLS:
                return self.tool_calls
            case BudgetLimit.FAILURES:
                return self.failures
            case BudgetLimit.REPLANS:
                return self.replans
            case BudgetLimit.WALL_TIME:
                return self.active_time_s
            case BudgetLimit.MODEL_CALLS:
                return self.model_calls
            case BudgetLimit.MODEL_TOKENS:
                return self.model_tokens
