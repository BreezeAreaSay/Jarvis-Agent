"""TaskService — публичный API ядра для CLI, eval и будущих клиентов (03-contracts.md §5)."""

from jarvis.core.runner import TaskRunner
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.budget import BudgetUsage
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import BudgetsSettings
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Task, TaskRequest, TaskSnapshot
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.ports.clock import Clock
from jarvis.ports.storage import IdAllocator, UnitOfWorkFactory


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
    ) -> None:
        self._runner = runner
        self._uow = uow
        self._ids = ids
        self._tracer = tracer
        self._budgets = budgets
        self._clock = clock

    def submit(self, request: TaskRequest) -> TaskId:
        """Создаёт задачу в CREATED; продвигает её `run_until_blocked`."""
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
            uow.commit()
        return task_id

    async def run_until_blocked(self, task_id: TaskId) -> TaskSnapshot:
        """До терминального состояния или WAITING_CONFIRMATION."""
        return TaskSnapshot.of(await self._runner.run_until_blocked(task_id))

    def cancel(self, task_id: TaskId, reason: str) -> None:
        self._runner.cancel(task_id, reason)

    def get(self, task_id: TaskId) -> TaskSnapshot:
        with self._uow() as uow:
            return TaskSnapshot.of(uow.tasks.get(task_id))

    def trace(self, task_id: TaskId) -> list[TraceEvent]:
        with self._uow() as uow:
            uow.tasks.get(task_id)  # TaskNotFound для несуществующей задачи
            return uow.trace.list(task_id)
