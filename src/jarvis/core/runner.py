"""TaskRunner — переводит задачу между состояниями (02-domain.md §3, ADR 0010, ADR 0021).

Прогон начинается с проверки аренды: задачу ведёт только её владелец. Каждый такт: проверить отмену
и бюджет → вызвать стадию текущего состояния → проверить её результат по таблице переходов →
записать контрольную точку. Контрольная точка — одна транзакция: строка задачи, событие перехода,
объясняющие его события (ошибка, превышение бюджета, завершение) и сравнение-и-запись аренды.
Поэтому задача и трасса не расходятся, а процесс, потерявший аренду, ничего не запишет.

Состояние задачи лежит в хранилище, а не в стеке вызовов. Активную задачу, чей процесс завершился,
не продолжают: её переводят в FAILED (`interrupted`) — неизвестно, что успел сделать прерванный такт.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import JsonValue

from jarvis.core import approvals
from jarvis.core.budget import BudgetMeter
from jarvis.core.leases import Holder, Leases
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.agent import AgentState
from jarvis.domain.approvals import ApprovalStatus
from jarvis.domain.budget import Budget, BudgetUsage
from jarvis.domain.errors import (
    BudgetExceeded,
    ConcurrentModification,
    ErrorInfo,
    InvalidTransition,
    JarvisError,
    LeaseLost,
    TaskBusy,
    TaskCancelled,
    TaskInterrupted,
    internal_error_info,
)
from jarvis.domain.ids import TaskId
from jarvis.domain.lease import Lease
from jarvis.domain.settings import BudgetsSettings
from jarvis.domain.states import (
    ACTIVE_STATUSES,
    RUNNER_ONLY_TARGETS,
    STAY_ALLOWED,
    TaskStatus,
    check_transition,
    is_terminal,
)
from jarvis.domain.task import Route, StageOutcome, Task, TaskOutcome, check_route_target
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.ports.clock import Clock
from jarvis.ports.storage import UnitOfWorkFactory

_log = logging.getLogger(__name__)

# Сколько ждать стадию после отмены такта, прежде чем продолжить без неё.
CANCEL_GRACE_S = 5.0

Explanation = tuple[EventKind, dict[str, JsonValue]]


class Interruption(StrEnum):
    """Почему задачу не довели до конца — в трассе как поле перехода, а не как текст причины."""

    RUN_STOPPED = "run_stopped"  # прогон этого процесса остановлен посреди такта (выход, Ctrl+C)
    OWNER_LOST = "owner_lost"  # тот, кто вёл задачу, пропал: процесс умер или его прогон оборвался


class StageHandler(Protocol):
    """Обработчик одного состояния. Статус задачи не меняет: возвращает `StageOutcome`."""

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome: ...


@dataclass
class _Run:
    lease: Lease | None = None  # аренда, которую держит прогон (ожидаемое значение для записи)
    tick: "asyncio.Task[StageOutcome] | None" = None
    cancel_reason: str | None = None
    lease_lost: bool = False


async def _stop(tick: "asyncio.Task[StageOutcome]") -> None:
    """Отменить такт и дождаться его — но не дольше CANCEL_GRACE_S."""
    tick.cancel()
    await asyncio.wait({tick}, timeout=CANCEL_GRACE_S)
    if not tick.done():
        _log.warning("стадия не завершилась за %s с после отмены такта", CANCEL_GRACE_S)
    elif not tick.cancelled():
        tick.exception()  # результат прерванного такта не нужен; исключение помечается полученным


def _blocked(task: Task) -> bool:
    return is_terminal(task.status) or task.status is TaskStatus.WAITING_CONFIRMATION


def _busy(task_id: TaskId, lease: Lease | None) -> TaskBusy:
    owner = lease.owner if lease else "?"
    until = lease.expires_at.isoformat() if lease else "?"
    return TaskBusy(
        f"задачу {task_id} ведёт другой процесс ({owner}, аренда до {until})",
        task_id=task_id,
        owner=owner,
        lease_until=until,
    )


class TaskRunner:
    def __init__(
        self,
        *,
        stages: Mapping[TaskStatus, StageHandler],
        uow: UnitOfWorkFactory,
        tracer: Tracer,
        budgets: BudgetsSettings,
        clock: Clock,
        leases: Leases,
    ) -> None:
        self._stages = dict(stages)
        self._uow = uow
        self._tracer = tracer
        self._budgets = budgets
        self._clock = clock
        self._leases = leases
        self._runs: dict[TaskId, _Run] = {}

    async def run_until_blocked(self, task_id: TaskId) -> Task:
        """Продвигает задачу, пока она не завершится или не станет ждать подтверждения.

        TaskBusy — задачу ведёт другой процесс; LeaseLost / ConcurrentModification — её перехватили
        посреди прогона (тогда этот процесс ничего больше не записывает).
        """
        if task_id in self._runs:
            raise TaskBusy(f"задача {task_id} уже выполняется в этом процессе", task_id=task_id)
        run = _Run()
        self._runs[task_id] = run
        try:
            with self._uow() as uow:
                task = uow.tasks.get(task_id)
                lease = uow.leases.get(task_id)
            if task.status is TaskStatus.WAITING_CONFIRMATION:
                return await self._resume(task, lease, run)
            if _blocked(task):
                return task
            holder = self._leases.holder(lease)
            if holder is Holder.BUSY:
                raise _busy(task_id, lease)
            run.lease = lease
            if holder is not Holder.MINE:
                return self._interrupted(task, run, "задачу вёл другой процесс, и он завершился")
            if task.status is not TaskStatus.CREATED:
                # Аренда наша, но задача уже в работе: прошлый прогон этого процесса оборвался
                # (сбой записи, отмена). Что успел сделать его такт, неизвестно — не продолжаем.
                return self._interrupted(task, run, "прошлый прогон этой задачи оборвался посреди работы")
            assert lease is not None  # MINE — значит, строка аренды есть
            run.lease = self._leases.renew(lease)  # до первой стадии убедиться, что аренду не перехватили
            return await self._drive(task, run)
        finally:
            del self._runs[task_id]

    async def _resume(self, task: Task, lease: Lease | None, run: _Run) -> Task:
        """Ждущая задача продолжается, когда по её запросу решили (или вышел срок). Переход в
        EXECUTING берёт аренду: из двух процессов, продолжающих задачу разом, пройдёт один."""
        with self._uow() as uow:
            ready = approvals.resumption(uow.approvals.for_task(task.id), self._clock.now())
        if ready is None:
            return task
        if self._leases.holder(lease) is Holder.BUSY:
            raise _busy(task.id, lease)
        run.lease = lease  # у ждущей задачи аренды нет; устаревшая строка будет заменена сравнением
        status = "истёк срок" if ready.status is ApprovalStatus.PENDING else ready.status.value
        task = self._checkpoint(
            task, run, TaskStatus.EXECUTING, f"подтверждение {ready.id}: {status}", usage=task.usage
        )
        return await self._drive(task, run)

    async def _drive(self, task: Task, run: _Run) -> Task:
        heartbeat = asyncio.create_task(self._heartbeat(run))
        try:
            while not _blocked(task):
                task = await self._tick(task, run)
            return task
        finally:
            heartbeat.cancel()
            await asyncio.wait({heartbeat})  # heartbeat сам ловит свои ошибки: результат не подменит

    def cancel(self, task_id: TaskId, reason: str) -> Task:
        """Отмена по запросу клиента; возвращает задачу после попытки.

        Задачу, которую ведёт этот процесс, прерывает посреди такта (запись сделает её прогон).
        Задачу с живой чужой аренды не трогает: TaskBusy. Активную задачу, чей процесс умер, не
        «отменяет», а восстанавливает как прерванную: сбой случился раньше решения пользователя.
        Остальные — CANCELLED сразу. Отмена завершённой задачи ничего не меняет.
        """
        run = self._runs.get(task_id)
        if run is not None:
            run.cancel_reason = reason
            if run.tick is not None and not run.tick.done():
                run.tick.cancel()
            return self._load(task_id)
        for attempt in range(2):
            with self._uow() as uow:
                task = uow.tasks.get(task_id)
                lease = uow.leases.get(task_id)
            if is_terminal(task.status):
                return task
            holder = self._leases.holder(lease)
            if holder is Holder.BUSY:
                raise _busy(task_id, lease)
            try:
                if task.status in ACTIVE_STATUSES and holder in (Holder.FREE, Holder.STALE):
                    return self._interrupted(
                        task, _Run(lease=lease), "процесс, который вёл задачу, завершился"
                    )
                return self._cancelled(task, _Run(lease=lease), reason, task.usage)
            except ConcurrentModification:
                if attempt:  # задача менялась дважды за время отмены — пусть клиент повторит
                    raise
        raise AssertionError("недостижимо")

    def _load(self, task_id: TaskId) -> Task:
        with self._uow() as uow:
            return uow.tasks.get(task_id)

    def recover_interrupted(self) -> list[TaskId]:
        """Активные задачи без живой аренды (их процесс завершился) → FAILED (`interrupted`)."""
        with self._uow() as uow:
            active = uow.tasks.list(statuses=ACTIVE_STATUSES)
            leases = {task.id: uow.leases.get(task.id) for task in active}
        recovered: list[TaskId] = []
        for task in active:
            lease = leases[task.id]
            if task.id in self._runs or self._leases.holder(lease) not in (Holder.FREE, Holder.STALE):
                continue
            try:
                self._interrupted(task, _Run(lease=lease), "процесс, который вёл задачу, завершился")
            except ConcurrentModification:
                continue  # владелец успел продлить аренду или задачу уже восстановил другой процесс
            recovered.append(task.id)
        return recovered

    async def _heartbeat(self, run: _Run) -> None:
        while True:
            await asyncio.sleep(self._leases.heartbeat_s)
            if run.lease is None:
                return
            try:
                run.lease = self._leases.renew(run.lease)
            except LeaseLost:
                run.lease_lost = True
                if run.tick is not None and not run.tick.done():
                    run.tick.cancel()
                return
            except Exception:  # база занята или иной сбой: следующая попытка через интервал
                _log.warning("не удалось продлить аренду задачи %s", run.lease.task_id, exc_info=True)

    def _check_lease(self, task: Task, run: _Run) -> None:
        if run.lease_lost:
            raise LeaseLost(f"аренду задачи {task.id} перехватили; прогон остановлен", task_id=task.id)

    async def _tick(self, task: Task, run: _Run) -> Task:
        self._check_lease(task, run)
        if run.cancel_reason is not None:
            return self._cancelled(task, run, run.cancel_reason, task.usage)
        if task.status is TaskStatus.CREATED:
            return self._checkpoint(task, run, TaskStatus.ROUTING, "задача принята", usage=task.usage)

        meter = BudgetMeter(task.budget, task.usage)
        try:
            meter.check_time()
        except BudgetExceeded as exc:
            return self._budget_exceeded(task, run, exc, meter.usage)

        handler = self._stages.get(task.status)
        if handler is None:
            error = internal_error_info(LookupError(f"нет обработчика для состояния {task.status}"))
            return self._failed(task, run, error, meter.usage)

        started = self._clock.monotonic()
        tick = asyncio.create_task(handler.handle(task, meter))
        run.tick = tick
        try:
            done, _ = await asyncio.wait({tick}, timeout=meter.remaining_time_s())
            if not done:  # такт не уложился в остаток времени; его результат не применяется
                await _stop(tick)
        except asyncio.CancelledError:
            # Отменили сам run_until_blocked (процесс завершается) — в том числе пока такт
            # останавливался. Итог записывается до ожидания такта: повторная отмена его не потеряет.
            tick.cancel()
            self._record_stop(task, run, meter, self._clock.monotonic() - started)
            await _stop(tick)
            raise
        finally:
            run.tick = None
        elapsed = self._clock.monotonic() - started

        if not done:
            self._check_lease(task, run)
            if run.cancel_reason is not None:
                meter.add_active_time(elapsed)
                return self._cancelled(task, run, run.cancel_reason, meter.usage)
            return self._budget_exceeded(task, run, meter.exhaust_time(elapsed), meter.usage)

        self._check_lease(task, run)  # такт прервал heartbeat: аренду перехватили
        meter.add_active_time(elapsed)
        if run.cancel_reason is not None:  # отмена важнее того, чем закончился такт
            return self._cancelled(task, run, run.cancel_reason, meter.usage)
        if tick.cancelled():
            return self._failed(task, run, internal_error_info(asyncio.CancelledError()), meter.usage)
        error = tick.exception()
        if error is not None:
            return self._on_error(task, run, error, meter.usage)
        return self._apply(task, run, tick.result(), meter.usage)

    def _record_stop(self, task: Task, run: _Run, meter: BudgetMeter, elapsed: float) -> None:
        """Итог прогона, остановленного посреди такта: что успел сделать такт, неизвестно, поэтому
        задачу не продолжают — CANCELLED, если клиент просил отмену, иначе FAILED (interrupted)."""
        if run.lease_lost:
            return
        meter.add_active_time(elapsed)
        try:
            if run.cancel_reason is not None:
                self._cancelled(task, run, run.cancel_reason, meter.usage)
            else:
                self._interrupted(
                    task,
                    run,
                    "прогон прерван: процесс завершает работу",
                    meter.usage,
                    cause=Interruption.RUN_STOPPED,
                )
        except Exception:
            # Отмена важнее итоговой записи: вызывающий должен увидеть CancelledError. Задача
            # остаётся в последней контрольной точке и станет FAILED (interrupted), когда истечёт
            # аренда этого процесса.
            _log.warning("не удалось записать итог прерванной задачи %s", task.id, exc_info=True)

    def _on_error(self, task: Task, run: _Run, error: BaseException, usage: BudgetUsage) -> Task:
        if isinstance(error, BudgetExceeded):
            return self._budget_exceeded(task, run, error, usage)
        if isinstance(error, TaskCancelled):
            return self._cancelled(task, run, error.message, usage)
        if isinstance(error, JarvisError):
            # Повтор, обратную связь модели и перепланирование обрабатывает сама стадия;
            # ошибка, дошедшая до runner'а, завершает задачу.
            return self._failed(task, run, error.to_info(), usage)
        _log.error("необработанное исключение в стадии %s задачи %s", task.status, task.id, exc_info=error)
        return self._failed(task, run, internal_error_info(error), usage)

    def _apply(self, task: Task, run: _Run, outcome: StageOutcome, usage: BudgetUsage) -> Task:
        target = outcome.next_status
        route = outcome.changes.route
        try:
            if target in RUNNER_ONLY_TARGETS:
                raise InvalidTransition(f"{target} выставляет runner; стадия сообщает об этом исключением")
            if target is task.status:
                if target not in STAY_ALLOWED:
                    raise InvalidTransition(f"такт в {task.status} должен сменить состояние")
            elif task.status is TaskStatus.ROUTING:
                check_route_target(route, target)
            if route is not None and task.status is not TaskStatus.ROUTING:
                raise InvalidTransition(f"маршрут решается только в ROUTING, а не в {task.status}")
            if target is not task.status:
                check_transition(task.status, target)
            if target is TaskStatus.WAITING_CONFIRMATION and not self._has_pending_approval(task.id):
                raise InvalidTransition("ожидание подтверждения без запроса: человеку нечего решать")
        except InvalidTransition as exc:
            return self._failed(task, run, exc.to_info(), usage)

        state = outcome.changes.state
        if target is task.status:
            changes: dict[str, Any] = {"usage": usage}
            if state is not None:
                changes["state"] = state
            return self._commit(task, run, changes, events=[])
        if route is not None:
            # Решение роутера: дальше действует бюджет маршрута, расход фазы считается с нуля.
            return self._checkpoint(
                task,
                run,
                target,
                outcome.reason,
                usage=BudgetUsage(),
                route=route,
                budget=self._budgets.for_route(route),
                answer=outcome.changes.answer,
                state=state,
            )
        return self._checkpoint(
            task, run, target, outcome.reason, usage=usage, answer=outcome.changes.answer, state=state
        )

    def _has_pending_approval(self, task_id: TaskId) -> bool:
        with self._uow() as uow:
            return any(item.status is ApprovalStatus.PENDING for item in uow.approvals.for_task(task_id))

    def _failed(self, task: Task, run: _Run, error: ErrorInfo, usage: BudgetUsage) -> Task:
        return self._checkpoint(
            task,
            run,
            TaskStatus.FAILED,
            error.message,
            usage=usage,
            error=error,
            explanations=[(EventKind.ERROR, _error_payload(error))],
        )

    def _interrupted(
        self,
        task: Task,
        run: _Run,
        message: str,
        usage: BudgetUsage | None = None,
        *,
        cause: Interruption = Interruption.OWNER_LOST,
    ) -> Task:
        error = TaskInterrupted(message, cause=cause.value).to_info()
        return self._checkpoint(
            task,
            run,
            TaskStatus.FAILED,
            "interrupted",
            usage=task.usage if usage is None else usage,
            error=error,
            explanations=[(EventKind.ERROR, _error_payload(error))],
            interruption=cause,
        )

    def _budget_exceeded(self, task: Task, run: _Run, error: BudgetExceeded, usage: BudgetUsage) -> Task:
        return self._checkpoint(
            task,
            run,
            TaskStatus.BUDGET_EXCEEDED,
            error.message,
            usage=usage,
            error=error.to_info(),
            explanations=[(EventKind.BUDGET_EXCEEDED, dict(error.details))],
        )

    def _cancelled(self, task: Task, run: _Run, reason: str, usage: BudgetUsage) -> Task:
        error = TaskCancelled(reason).to_info()
        return self._checkpoint(task, run, TaskStatus.CANCELLED, reason, usage=usage, error=error)

    def _checkpoint(
        self,
        task: Task,
        run: _Run,
        target: TaskStatus,
        reason: str,
        *,
        usage: BudgetUsage,
        route: Route | None = None,
        budget: Budget | None = None,
        answer: str | None = None,
        state: AgentState | None = None,
        error: ErrorInfo | None = None,
        explanations: Sequence[Explanation] = (),
        interruption: Interruption | None = None,
    ) -> Task:
        """Переход: задача, событие перехода и объясняющие его события — одной транзакцией."""
        check_transition(task.status, target)
        changes: dict[str, Any] = {"status": target, "usage": usage}
        if state is not None:
            changes["state"] = state
        if route is not None:
            changes["route"] = route
        if budget is not None:
            changes["budget"] = budget
        if is_terminal(target):
            changes["outcome"] = TaskOutcome(status=target, answer=answer, error=error)
        transition: dict[str, JsonValue] = {
            "from": task.status.value,
            "to": target.value,
            "reason": shorten(reason),
        }
        if route is not None:
            transition["route"] = route.value
        if interruption is not None:
            transition["interruption"] = interruption.value
        closing = approvals.Closing(saves=[], explanations=[], audit=[])
        if task.status is TaskStatus.WAITING_CONFIRMATION or is_terminal(target):
            # Запросы читаются здесь, а пишутся сравнением статуса в той же транзакции, что и переход:
            # если человек успел решить, запись не пройдёт (ConcurrentModification).
            with self._uow() as uow:
                closing = approvals.closing(uow.approvals.for_task(task.id), target, self._clock.now())
        kinds: list[Explanation] = [
            *explanations,
            *closing.explanations,
            (EventKind.TASK_TRANSITION, transition),
        ]
        if is_terminal(target):
            finished: dict[str, JsonValue] = {
                "status": target.value,
                "answer": shorten(answer) if answer is not None else None,
            }
            kinds.append((EventKind.TASK_FINISHED, finished))
        events = [self._tracer.event(task.id, kind, payload) for kind, payload in kinds]
        return self._commit(task, run, changes, events=events, closing=closing)

    def _commit(
        self,
        task: Task,
        run: _Run,
        changes: dict[str, Any],
        *,
        events: Sequence[TraceEvent],
        closing: approvals.Closing | None = None,
    ) -> Task:
        updated = Task.model_validate(
            {**task.model_dump(), **changes, "version": task.version + 1, "updated_at": self._clock.now()}
        )
        renewed: Lease | None = None
        with self._uow() as uow:
            uow.tasks.save(updated, expected_version=task.version)
            if events:
                uow.trace.append(events)
            if closing is not None:
                for approval, expected in closing.saves:
                    uow.approvals.save(approval, expected=expected)
                for record in closing.audit:
                    uow.audit.append(record)
            # Ограждение: запись пройдёт, только если аренда всё ещё та, что держит прогон (None —
            # аренды нет: так ждущая задача берёт её при продолжении).
            if updated.status in ACTIVE_STATUSES:
                renewed = self._leases.fresh(task.id)
                uow.leases.put(renewed, expected=run.lease)
            elif run.lease is not None:
                uow.leases.delete(task.id, expected=run.lease)
            uow.commit()
        run.lease = renewed
        return updated


def _error_payload(error: ErrorInfo) -> dict[str, JsonValue]:
    return {
        "category": error.category,
        "disposition": error.disposition.value,
        "message": shorten(error.message, 500),
    }
