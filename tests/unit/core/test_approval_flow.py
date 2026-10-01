"""Подтверждение как состояние задачи: WAITING_CONFIRMATION → решение → продолжение через runner."""

import pytest

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.app.composition import App
from jarvis.domain.approvals import ApprovalDecision, ApprovalStatus
from jarvis.domain.audit import AuditAction
from jarvis.domain.errors import ApprovalClosed, ApprovalNotFound
from jarvis.domain.ids import TaskId
from jarvis.domain.settings import JarvisConfig, PolicySettings
from jarvis.domain.tools import ToolOutcomeKind
from jarvis.domain.trace import EventKind
from jarvis.evals.scripted import ScriptedStages
from tests.fakes import FakeTool
from tests.helpers import S, agent_prefix, approval_step, approval_tool, make_app, request, step, transitions

pytestmark = pytest.mark.anyio

TTL_S = 300.0
CONFIG = JarvisConfig(policy=PolicySettings(approval_ttl_s=TTL_S))


class Flow:
    def __init__(self, *, expect: str | None = None) -> None:
        self.tool: FakeTool = approval_tool()
        self.clock = ManualClock()
        self.storage = InMemoryStorage()
        approval = approval_step(path="/keys/a", expect=expect)
        self.script = ScriptedStages(
            [*agent_prefix(), approval, step(S.VERIFYING, S.COMPLETED, answer="готово")]
        )
        self.app: App = make_app(
            self.script.handlers, config=CONFIG, storage=self.storage, clock=self.clock, tools=[self.tool]
        )
        self.task_id: TaskId = self.app.tasks.submit(request())

    async def wait(self) -> str:
        snapshot = await self.app.tasks.run_until_blocked(self.task_id)
        assert snapshot.status is S.WAITING_CONFIRMATION
        [approval] = self.app.tasks.approvals(self.task_id)
        return approval.id

    def statuses(self) -> list[ApprovalStatus]:
        return [approval.status for approval in self.app.tasks.approvals(self.task_id)]

    def kinds(self) -> list[EventKind]:
        return [event.kind for event in self.app.tasks.trace(self.task_id)]


async def test_approved_call_runs_and_the_task_completes() -> None:
    flow = Flow(expect="executed")
    approval_id = await flow.wait()
    assert flow.tool.executions == []

    resolved = flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.APPROVE, via="cli")
    assert (resolved.status, resolved.resolved_via) == (ApprovalStatus.APPROVED, "cli")
    snapshot = await flow.app.tasks.run_until_blocked(flow.task_id)

    assert snapshot.status is S.COMPLETED
    assert transitions(flow.app, flow.task_id) == [
        S.ROUTING, S.PLANNING, S.EXECUTING, S.WAITING_CONFIRMATION, S.EXECUTING, S.VERIFYING, S.COMPLETED,
    ]  # fmt: skip
    assert [args.path for args in flow.tool.executions] == ["/keys/a"]
    assert flow.statuses() == [ApprovalStatus.USED]
    assert [outcome.kind for outcome in flow.script.outcomes] == [ToolOutcomeKind.EXECUTED]
    kinds = flow.kinds()
    assert kinds.index(EventKind.APPROVAL_REQUESTED) < kinds.index(EventKind.APPROVAL_RESOLVED)
    assert kinds.index(EventKind.APPROVAL_RESOLVED) < kinds.index(EventKind.TOOL_STARTED)


async def test_denied_call_gives_the_stage_a_controlled_result() -> None:
    flow = Flow(expect="denied")
    approval_id = await flow.wait()
    flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.DENY, via="cli")
    snapshot = await flow.app.tasks.run_until_blocked(flow.task_id)

    assert snapshot.status is S.COMPLETED  # стадия получила отказ значением и пошла дальше
    assert flow.tool.executions == []
    assert [(o.kind, o.decision.rules) for o in flow.script.outcomes] == [
        (ToolOutcomeKind.DENIED, ["approval.denied"])
    ]
    assert flow.statuses() == [ApprovalStatus.USED]
    with flow.storage.unit_of_work() as uow:
        audit = uow.audit.list(task_id=flow.task_id)
    assert [(r.action, r.actor, r.approval_status) for r in audit if r.action is AuditAction.APPROVAL] == [
        (AuditAction.APPROVAL, "cli", "denied")
    ]


async def test_without_a_decision_the_task_keeps_waiting() -> None:
    flow = Flow()
    await flow.wait()
    events = flow.app.tasks.trace(flow.task_id)
    snapshot = await flow.app.tasks.run_until_blocked(flow.task_id)
    assert snapshot.status is S.WAITING_CONFIRMATION
    assert flow.app.tasks.trace(flow.task_id) == events  # ни стадий, ни записей
    assert flow.statuses() == [ApprovalStatus.PENDING]


async def test_a_request_is_resolved_once() -> None:
    flow = Flow()
    approval_id = await flow.wait()
    flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.APPROVE, via="cli")
    with pytest.raises(ApprovalClosed, match="уже закрыт"):
        flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.DENY, via="cli")
    with pytest.raises(ApprovalNotFound):
        flow.app.tasks.resolve_approval("task_1.appr_99", ApprovalDecision.APPROVE, via="cli")


async def test_late_answer_is_recorded_as_expired_and_the_call_is_refused() -> None:
    flow = Flow(expect="denied")
    approval_id = await flow.wait()
    flow.clock.advance(TTL_S + 1)
    with pytest.raises(ApprovalClosed, match="истёк"):
        flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.APPROVE, via="cli")
    assert flow.statuses() == [ApprovalStatus.EXPIRED]

    snapshot = await flow.app.tasks.run_until_blocked(flow.task_id)
    assert snapshot.status is S.COMPLETED
    assert flow.tool.executions == []
    assert flow.script.outcomes[0].decision.rules == ["approval.expired"]


async def test_unanswered_request_expires_when_the_task_is_resumed() -> None:
    flow = Flow(expect="denied")
    await flow.wait()
    flow.clock.advance(TTL_S + 1)
    snapshot = await flow.app.tasks.run_until_blocked(flow.task_id)
    assert snapshot.status is S.COMPLETED
    assert flow.tool.executions == []
    resolved = [
        e.payload for e in flow.app.tasks.trace(flow.task_id) if e.kind is EventKind.APPROVAL_RESOLVED
    ]
    assert [(p["status"], p["via"]) for p in resolved] == [("expired", "runtime")]


async def test_cancelling_a_waiting_task_withdraws_its_request() -> None:
    flow = Flow()
    approval_id = await flow.wait()
    assert flow.app.tasks.cancel(flow.task_id, "передумал").status is S.CANCELLED
    assert flow.statuses() == [ApprovalStatus.WITHDRAWN]
    with pytest.raises(ApprovalClosed):
        flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.APPROVE, via="cli")
    kinds = flow.kinds()
    assert kinds.index(EventKind.APPROVAL_RESOLVED) < kinds.index(EventKind.TASK_FINISHED)


async def test_approval_granted_before_a_cancel_is_never_applied() -> None:
    flow = Flow()
    approval_id = await flow.wait()
    flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.APPROVE, via="cli")
    flow.app.tasks.cancel(flow.task_id, "всё-таки нет")
    assert flow.statuses() == [ApprovalStatus.WITHDRAWN]
    assert (await flow.app.tasks.run_until_blocked(flow.task_id)).status is S.CANCELLED
    assert flow.tool.executions == []


async def test_waiting_task_resumed_by_another_process_takes_a_lease() -> None:
    flow = Flow(expect="executed")
    approval_id = await flow.wait()
    flow.app.tasks.resolve_approval(approval_id, ApprovalDecision.APPROVE, via="cli")
    other = make_app(
        flow.script.handlers, config=CONFIG, storage=flow.storage, clock=flow.clock, tools=[flow.tool]
    )
    snapshot = await other.tasks.run_until_blocked(flow.task_id)
    assert snapshot.status is S.COMPLETED
    assert len(flow.tool.executions) == 1
    with flow.storage.unit_of_work() as uow:
        assert uow.leases.get(flow.task_id) is None  # завершённая задача аренду отпустила


async def test_stage_may_not_wait_without_a_request() -> None:
    app = make_app(ScriptedStages([*agent_prefix(), step(S.EXECUTING, S.WAITING_CONFIRMATION)]).handlers)
    task_id = app.tasks.submit(request())
    snapshot = await app.tasks.run_until_blocked(task_id)
    assert snapshot.status is S.FAILED
    assert snapshot.outcome is not None
    assert snapshot.outcome.error is not None
    assert snapshot.outcome.error.category == "invalid_transition"
