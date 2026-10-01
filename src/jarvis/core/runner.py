"""TaskRunner — переводит задачу между состояниями (02-domain.md §3, ADR 0010).

Один такт: загрузить задачу → проверить отмену и бюджет → вызвать стадию текущего состояния →
проверить её результат по таблице переходов → записать контрольную точку. Состояние задачи всегда
лежит в хранилище, а не в стеке вызовов, поэтому задачу можно продолжить новым runner'ом.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from jarvis.core.budget import BudgetMeter
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.budget import Budget, BudgetUsage
from jarvis.domain.errors import (
    BudgetExceeded,
    ErrorInfo,
    InvalidTransition,
    JarvisError,
    TaskBusy,
    TaskCancelled,
    internal_error_info,
)
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import BudgetsSettings
from jarvis.domain.states import STAY_ALLOWED, TaskStatus, check_transition, is_terminal
from jarvis.domain.task import Route, StageOutcome, Task, TaskOutcome
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.ports.clock import Clock
from jarvis.ports.storage import UnitOfWorkFactory

_log = logging.getLogger(__name__)


class StageHandler(Protocol):
    """Обработчик одного состояния. Статус задачи не меняет: возвращает `StageOutcome`."""

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome: ...


@dataclass
class _Run:
    tick: "asyncio.Task[StageOutcome] | None" = None
    cancel_reason: str | None = None


def _blocked(task: Task) -> bool:
    return is_terminal(task.status) or task.status is TaskStatus.WAITING_CONFIRMATION


class TaskRunner:
    def __init__(
        self,
        *,
        stages: Mapping[TaskStatus, StageHandler],
        uow: UnitOfWorkFactory,
        tracer: Tracer,
        budgets: BudgetsSettings,
        clock: Clock,
    ) -> None:
        self._stages = dict(stages)
        self._uow = uow
        self._tracer = tracer
        self._budgets = budgets
        self._clock = clock
        self._runs: dict[TaskId, _Run] = {}

    async def run_until_blocked(self, task_id: TaskId) -> Task:
        """Продвигает задачу, пока она не завершится или не станет ждать подтверждения."""
        if task_id in self._runs:
            raise TaskBusy(f"задача {task_id} уже выполняется", task_id=task_id)
        run = _Run()
        self._runs[task_id] = run
        try:
            task = self._load(task_id)
            while not _blocked(task):
                task = await self._tick(task, run)
            return task
        finally:
            del self._runs[task_id]

    def cancel(self, task_id: TaskId, reason: str) -> None:
        """Работающую здесь задачу прерывает посреди такта; остальные переводит в CANCELLED сразу.

        Отмена завершённой задачи ничего не меняет.
        """
        run = self._runs.get(task_id)
        if run is not None:
            run.cancel_reason = reason
            if run.tick is not None and not run.tick.done():
                run.tick.cancel()
            return
        task = self._load(task_id)
        if not is_terminal(task.status):
            self._cancelled(task, reason, task.usage)

    async def _tick(self, task: Task, run: _Run) -> Task:
        if run.cancel_reason is not None:
            return self._cancelled(task, run.cancel_reason, task.usage)
        if task.status is TaskStatus.CREATED:
            return self._checkpoint(task, TaskStatus.ROUTING, "задача принята", usage=task.usage)

        meter = BudgetMeter(task.budget, task.usage)
        try:
            meter.check_time()
        except BudgetExceeded as exc:
            return self._budget_exceeded(task, exc, meter.usage)

        handler = self._stages.get(task.status)
        if handler is None:
            error = InvalidTransition(f"нет обработчика для состояния {task.status}")
            return self._failed(task, error.to_info(), meter.usage)

        timeout = meter.remaining_time_s()
        started = self._clock.monotonic()
        tick = asyncio.create_task(handler.handle(task, meter))
        run.tick = tick
        try:
            done, _ = await asyncio.wait({tick}, timeout=timeout)
        except asyncio.CancelledError:
            # Отменили сам run_until_blocked (например, процесс завершается): такт прерывается,
            # задача остаётся в последней контрольной точке.
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
            raise
        finally:
            run.tick = None
        elapsed = self._clock.monotonic() - started

        if not done:
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
            meter.add_active_time(max(elapsed, timeout))
        else:
            meter.add_active_time(elapsed)

        if run.cancel_reason is not None:  # отмена важнее того, чем закончился такт
            return self._cancelled(task, run.cancel_reason, meter.usage)
        if not done:
            try:
                meter.check_time()
            except BudgetExceeded as exc:
                return self._budget_exceeded(task, exc, meter.usage)
        if tick.cancelled():
            return self._failed(task, internal_error_info(asyncio.CancelledError()), meter.usage)
        error = tick.exception()
        if error is not None:
            return self._on_error(task, error, meter.usage)
        return self._apply(task, tick.result(), meter.usage)

    def _on_error(self, task: Task, error: BaseException, usage: BudgetUsage) -> Task:
        if isinstance(error, BudgetExceeded):
            return self._budget_exceeded(task, error, usage)
        if isinstance(error, TaskCancelled):
            return self._cancelled(task, error.message, usage)
        if isinstance(error, JarvisError):
            # Повтор, обратную связь модели и перепланирование обрабатывает сама стадия;
            # ошибка, дошедшая до runner'а, завершает задачу.
            return self._failed(task, error.to_info(), usage)
        _log.error("необработанное исключение в стадии %s задачи %s", task.status, task.id, exc_info=error)
        return self._failed(task, internal_error_info(error), usage)

    def _apply(self, task: Task, outcome: StageOutcome, usage: BudgetUsage) -> Task:
        target = outcome.next_status
        route = outcome.changes.route
        try:
            if route is not None and task.status is not TaskStatus.ROUTING:
                raise InvalidTransition(f"маршрут решается только в ROUTING, а не в {task.status}")
            if target is task.status:
                if target not in STAY_ALLOWED:
                    raise InvalidTransition(f"такт в {task.status} должен сменить состояние")
            else:
                check_transition(task.status, target)
        except InvalidTransition as exc:
            return self._failed(task, exc.to_info(), usage)

        if target is task.status:
            return self._save(task, {"usage": usage})
        if route is not None:
            # Решение роутера: дальше действует бюджет маршрута, расход фазы считается с нуля.
            return self._checkpoint(
                task,
                target,
                outcome.reason,
                usage=BudgetUsage(),
                route=route,
                budget=self._budgets.for_route(route),
                answer=outcome.changes.answer,
            )
        return self._checkpoint(task, target, outcome.reason, usage=usage, answer=outcome.changes.answer)

    def _failed(self, task: Task, error: ErrorInfo, usage: BudgetUsage) -> Task:
        self._tracer.emit(
            task.id,
            EventKind.ERROR,
            {
                "category": error.category,
                "disposition": error.disposition.value,
                "message": shorten(error.message, 500),
            },
        )
        return self._checkpoint(task, TaskStatus.FAILED, error.message, usage=usage, error=error)

    def _budget_exceeded(self, task: Task, error: BudgetExceeded, usage: BudgetUsage) -> Task:
        self._tracer.emit(task.id, EventKind.BUDGET_EXCEEDED, dict(error.details))
        return self._checkpoint(
            task, TaskStatus.BUDGET_EXCEEDED, error.message, usage=usage, error=error.to_info()
        )

    def _cancelled(self, task: Task, reason: str, usage: BudgetUsage) -> Task:
        error = TaskCancelled(reason).to_info()
        return self._checkpoint(task, TaskStatus.CANCELLED, reason, usage=usage, error=error)

    def _checkpoint(
        self,
        task: Task,
        target: TaskStatus,
        reason: str,
        *,
        usage: BudgetUsage,
        route: Route | None = None,
        budget: Budget | None = None,
        answer: str | None = None,
        error: ErrorInfo | None = None,
    ) -> Task:
        """Переход: новое состояние задачи и событие `task.transition` пишутся вместе."""
        check_transition(task.status, target)
        changes: dict[str, Any] = {"status": target, "usage": usage}
        if route is not None:
            changes["route"] = route
        if budget is not None:
            changes["budget"] = budget
        if is_terminal(target):
            changes["outcome"] = TaskOutcome(status=target, answer=answer, error=error)
        event = self._tracer.event(
            task.id,
            EventKind.TASK_TRANSITION,
            {"from": task.status.value, "to": target.value, "reason": shorten(reason)},
        )
        updated = self._save(task, changes, event=event)
        if is_terminal(target):
            self._tracer.emit(
                task.id,
                EventKind.TASK_FINISHED,
                {"status": target.value, "answer": shorten(answer) if answer is not None else None},
            )
        return updated

    def _save(self, task: Task, changes: dict[str, Any], *, event: TraceEvent | None = None) -> Task:
        updated = Task.model_validate(
            {
                **task.model_dump(),
                **changes,
                "version": task.version + 1,
                "updated_at": self._clock.now(),
            }
        )
        with self._uow() as uow:
            uow.tasks.save(updated, expected_version=task.version)
            if event is not None:
                uow.trace.append([event])
            uow.commit()
        return updated

    def _load(self, task_id: TaskId) -> Task:
        with self._uow() as uow:
            return uow.tasks.get(task_id)
