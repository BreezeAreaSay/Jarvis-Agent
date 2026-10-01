"""Scripted-стадии: проигрывают сценарий вместо модели и инструментов.

Один общий список шагов на все состояния: каждый такт берёт следующий шаг и проверяет, что задача
в том состоянии, которого ждёт сценарий. Расход бюджета списывается через тот же `BudgetMeter`, что
будут использовать настоящие стадии.
"""

import asyncio
from collections import deque
from collections.abc import Sequence

from jarvis.core.budget import BudgetMeter, CountedLimit
from jarvis.core.runner import StageHandler
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.errors import JarvisError
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task, TaskChanges
from jarvis.evals.scenario import ChargeKind, ScriptStep

_COUNTED: dict[ChargeKind, CountedLimit] = {
    "steps": BudgetLimit.STEPS,
    "tool_calls": BudgetLimit.TOOL_CALLS,
    "replans": BudgetLimit.REPLANS,
    "model_calls": BudgetLimit.MODEL_CALLS,
}

# Состояния, у которых есть обработчик: у CREATED переход автоматический, WAITING_CONFIRMATION
# покидают решением клиента, из терминальных переходов нет.
STAGE_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.ROUTING,
    TaskStatus.PLANNING,
    TaskStatus.EXECUTING,
    TaskStatus.VERIFYING,
    TaskStatus.REPLANNING,
)


class ScriptMismatch(JarvisError):
    category = "script_mismatch"


class ScriptedFailure(JarvisError):
    category = "scripted_failure"


class ScriptedStages:
    def __init__(self, steps: Sequence[ScriptStep]) -> None:
        self._steps = deque(steps)
        self.hung = asyncio.Event()

    @property
    def remaining(self) -> int:
        return len(self._steps)

    def handlers(self) -> dict[TaskStatus, StageHandler]:
        return {status: _Stage(self, status) for status in STAGE_STATUSES}

    async def play(self, status: TaskStatus, budget: BudgetMeter) -> StageOutcome:
        if not self._steps:
            raise ScriptMismatch(f"сценарий закончился, а задача в {status}")
        step = self._steps.popleft()
        if step.status is not status:
            raise ScriptMismatch(f"сценарий ждёт {step.status}, а задача в {status}")
        for kind, amount in step.charge.items():
            if kind == "model_tokens":
                budget.require_tokens()
                budget.add_tokens(amount)
            else:
                budget.charge(_COUNTED[kind], amount)
        for _ in range(step.failures):
            budget.record_failure()
        if step.sleep_s:
            await asyncio.sleep(step.sleep_s)
        if step.hang:
            self.hung.set()
            await asyncio.Event().wait()
        if step.error == "fatal":
            raise ScriptedFailure(step.reason)
        if step.error == "internal":
            raise RuntimeError(step.reason)
        return StageOutcome(
            next_status=step.next,
            reason=step.reason,
            changes=TaskChanges(route=step.route, answer=step.answer),
        )


class _Stage:
    def __init__(self, script: ScriptedStages, status: TaskStatus) -> None:
        self._script = script
        self._status = status

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        return await self._script.play(self._status, budget)
