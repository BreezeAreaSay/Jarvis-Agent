"""TaskService — публичный API ядра для CLI, eval и будущих клиентов (03-contracts.md §5)."""

from collections.abc import Collection
from dataclasses import dataclass

from jarvis.core.approvals import Approvals
from jarvis.core.leases import Leases
from jarvis.core.metrics import compute_metrics
from jarvis.core.runner import TaskRunner
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.approvals import ApprovalDecision, ApprovalRequest
from jarvis.domain.budget import BudgetUsage
from jarvis.domain.ids import TaskId
from jarvis.domain.metrics import TaskMetrics
from jarvis.domain.settings import BudgetsSettings
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Task, TaskRequest, TaskSnapshot
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.ports.clock import Clock
from jarvis.ports.storage import IdAllocator, UnitOfWorkFactory


@dataclass(frozen=True)
class TaskInspection:
    """Задача и её трасса из одного согласованного снимка хранилища."""

    task: TaskSnapshot
    events: list[TraceEvent]
    metrics: TaskMetrics


class TaskService:
    def __init__(
        self,
        *,
        runner: TaskRunner,
        uow: UnitOfWorkFactory,
        ids: IdAllocator,
        tracer: Tracer,
        budgets: BudgetsSettings,
        clock: Clock,
        leases: Leases,
        approvals: Approvals,
    ) -> None:
        self._runner = runner
        self._uow = uow
        self._ids = ids
        self._tracer = tracer
        self._budgets = budgets
        self._clock = clock
        self._leases = leases
        self._approvals = approvals

    def submit(self, request: TaskRequest) -> TaskId:
        """Создаёт задачу в CREATED вместе с арендой этого процесса; продвигает её `run_until_blocked`."""
        task_id = self._ids.next_task_id()
        now = self._clock.now()
        task = Task(
            id=task_id,
            version=1,
            request=request,
            status=TaskStatus.CREATED,
            budget=self._budgets.for_route(None),
            usage=BudgetUsage(),
            created_at=now,
            updated_at=now,
        )
        created = self._tracer.event(
            task_id,
            EventKind.TASK_CREATED,
            {"text": shorten(request.text), "origin": request.origin.value},
        )
        with self._uow() as uow:
            uow.tasks.add(task)
            uow.trace.append([created])
            uow.leases.put(self._leases.fresh(task_id), expected=None)
            uow.commit()
        return task_id

    async def run_until_blocked(self, task_id: TaskId) -> TaskSnapshot:
        """До терминального состояния или WAITING_CONFIRMATION."""
        return TaskSnapshot.of(await self._runner.run_until_blocked(task_id))

    def cancel(self, task_id: TaskId, reason: str) -> TaskSnapshot:
        """Состояние задачи после попытки отмены (TaskBusy — её ведёт другой живой процесс)."""
        return TaskSnapshot.of(self._runner.cancel(task_id, reason))

    def resolve_approval(self, approval_id: str, decision: ApprovalDecision, *, via: str) -> ApprovalRequest:
        """Записать решение человека по запросу подтверждения. Продолжает задачу `run_until_blocked`:
        после одобрения вызов исполняется, после отказа стадия получает отказ как результат.
        ApprovalClosed — запрос уже закрыт, истёк или задача его больше не ждёт."""
        return self._approvals.resolve(approval_id, decision, via=via)

    def approvals(self, task_id: TaskId) -> list[ApprovalRequest]:
        """Запросы подтверждения задачи в порядке создания."""
        return self._approvals.for_task(task_id)

    def recover_interrupted(self) -> list[TaskId]:
        """Задачи, чей процесс завершился посреди работы, → FAILED (`interrupted`)."""
        return self._runner.recover_interrupted()

    def get(self, task_id: TaskId) -> TaskSnapshot:
        with self._uow() as uow:
            return TaskSnapshot.of(uow.tasks.get(task_id))

    def list_tasks(
        self, *, statuses: Collection[TaskStatus] | None = None, limit: int | None = None
    ) -> list[TaskSnapshot]:
        """Новые задачи первыми."""
        with self._uow() as uow:
            return [TaskSnapshot.of(task) for task in uow.tasks.list(statuses=statuses, limit=limit)]

    def trace(self, task_id: TaskId) -> list[TraceEvent]:
        return self.inspect(task_id).events

    def inspect(self, task_id: TaskId) -> TaskInspection:
        with self._uow() as uow:
            task = uow.tasks.get(task_id)
            events = uow.trace.list(task_id)
        return TaskInspection(task=TaskSnapshot.of(task), events=events, metrics=compute_metrics(events))
