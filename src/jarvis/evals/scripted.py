"""Scripted-стадии: проигрывают сценарий вместо модели.

Один общий список шагов на все состояния: каждый такт берёт следующий шаг и проверяет, что задача
в том состоянии, которого ждёт сценарий. Расход бюджета списывается через тот же `BudgetMeter`, что
будут использовать настоящие стадии. Инструменты шаг вызывает только через Tool Runtime — как и
настоящая стадия: другой дороги к действиям на компьютере нет.
"""

import asyncio
from collections import deque
from collections.abc import Sequence

from jarvis.core.budget import BudgetMeter, CountedLimit
from jarvis.core.runner import StageHandler
from jarvis.core.tools.runtime import ToolRuntime
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.errors import JarvisError
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task, TaskChanges
from jarvis.domain.tools import ToolOutcome, ToolOutcomeKind
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
    def __init__(self, steps: Sequence[ScriptStep], *, hung: asyncio.Event | None = None) -> None:
        self._steps = deque(steps)
        self.hung = hung if hung is not None else asyncio.Event()
        self.outcomes: list[ToolOutcome] = []  # итоги вызовов инструментов — для проверок eval
        self._tools: ToolRuntime | None = None
        self._waiting: ScriptStep | None = None  # шаг, чей вызов ждёт решения человека

    @property
    def remaining(self) -> int:
        return len(self._steps) + (self._waiting is not None)

    def handlers(self, tools: ToolRuntime | None = None) -> dict[TaskStatus, StageHandler]:
        """Обработчики состояний; `tools` нужен сценариям с вызовами инструментов (фабрика стадий)."""
        self._tools = tools
        return {status: _Stage(self, status) for status in STAGE_STATUSES}

    async def play(self, task: Task, status: TaskStatus, budget: BudgetMeter) -> StageOutcome:
        if self._waiting is not None:
            return await self._resume(task, budget, self._waiting)
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
        if step.hang:
            self.hung.set()
            await asyncio.Event().wait()
        if step.fail:
            raise ScriptedFailure(step.reason)
        if step.tool is not None:
            outcome = await self._runtime().call(task, budget, step.tool.id, step.tool.arguments)
            return self._after_call(step, outcome)
        return _next(step)

    async def _resume(self, task: Task, budget: BudgetMeter, step: ScriptStep) -> StageOutcome:
        self._waiting = None
        outcome = await self._runtime().resume(task, budget)
        if outcome is None:
            raise ScriptMismatch("задача продолжена, но решения по вызову нет")
        return self._after_call(step, outcome)

    def _after_call(self, step: ScriptStep, outcome: ToolOutcome) -> StageOutcome:
        assert step.tool is not None
        if outcome.kind is ToolOutcomeKind.NEEDS_APPROVAL:
            self._waiting = step
            return StageOutcome(
                next_status=TaskStatus.WAITING_CONFIRMATION,
                reason=f"нужно подтверждение: {outcome.preview.summary}",
            )
        self.outcomes.append(outcome)
        if step.tool.expect is not None and outcome.kind is not step.tool.expect:
            raise ScriptMismatch(
                f"вызов {step.tool.id}: итог {outcome.kind}, сценарий ждёт {step.tool.expect}"
            )
        return _next(step)

    def _runtime(self) -> ToolRuntime:
        if self._tools is None:
            raise ScriptMismatch("шаг вызывает инструмент, а Tool Runtime стадиям не передан")
        return self._tools


def _next(step: ScriptStep) -> StageOutcome:
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
        return await self._script.play(task, self._status, budget)
