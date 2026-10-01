"""Жизненный цикл запроса подтверждения (04-security.md §6, ADR 0011, ADR 0022).

    PENDING ──решение человека──▶ APPROVED / DENIED ──стадия довела вызов──▶ USED
       │                                                                       ▲
       ├──срок вышел, задачу продолжили──▶ EXPIRED ────────────────────────────┘
       └──задача завершилась раньше, чем решение применили──▶ WITHDRAWN
          (из PENDING, APPROVED, DENIED, EXPIRED)

Человек только записывает решение; продолжает задачу runner (`run_until_blocked`), когда видит
решённый запрос. Каждый переход статуса — сравнение-и-запись: решение применяется ровно один раз,
а решение и отмена задачи не могут оба пройти.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from jarvis.core.trace import Tracer, shorten
from jarvis.domain.approvals import RESOLVED, ApprovalDecision, ApprovalRequest, ApprovalStatus
from jarvis.domain.audit import (
    AuditAction,
    AuditRecord,
    arguments_hash,
    effect_kinds,
    resources,
    target_label,
)
from jarvis.domain.errors import ApprovalClosed
from jarvis.domain.ids import TaskId
from jarvis.domain.states import TaskStatus, is_terminal
from jarvis.domain.trace import EventKind
from jarvis.ports.clock import Clock
from jarvis.ports.storage import UnitOfWorkFactory

# Канал, которым закрывает запросы сам Jarvis (истечение срока, отзыв при завершении задачи).
RUNTIME_CHANNEL = "runtime"

Explanation = tuple[EventKind, dict[str, JsonValue]]


@dataclass(frozen=True)
class Closing:
    """Что сделать с запросами задачи, которая выходит из WAITING_CONFIRMATION."""

    saves: list[tuple[ApprovalRequest, ApprovalStatus]]  # новый запрос и ожидаемый старый статус
    explanations: list[Explanation]
    audit: list[AuditRecord]


def resumption(approvals: Sequence[ApprovalRequest], now: datetime) -> ApprovalRequest | None:
    """Запрос, с которым ждущую задачу можно продолжать; None — задача ещё ждёт человека."""
    if any(item.status is ApprovalStatus.PENDING and not item.is_expired(now) for item in approvals):
        return None
    ready = [item for item in approvals if item.status in RESOLVED or item.is_expired(now)]
    return ready[-1] if ready else None


def resolved_event(approval: ApprovalRequest) -> Explanation:
    return (
        EventKind.APPROVAL_RESOLVED,
        {
            "approval_id": approval.id,
            "call_id": approval.call.id,
            "status": approval.status.value,
            "via": approval.resolved_via,
        },
    )


def audit_record(approval: ApprovalRequest) -> AuditRecord:
    assert approval.resolved_at is not None
    assert approval.resolved_via is not None
    return AuditRecord(
        ts=approval.resolved_at,
        action=AuditAction.APPROVAL,
        actor=approval.resolved_via,
        task_id=approval.task_id,
        tool_call_id=approval.call.id,
        tool_id=approval.call.tool_id,
        target=target_label(approval.target),
        effects=effect_kinds(approval.effects),
        resources=resources(approval.effects),
        arguments_hash=arguments_hash(approval.arguments),
        approval_id=approval.id,
        approval_status=approval.status.value,
    )


def closing(approvals: Sequence[ApprovalRequest], target: TaskStatus, now: datetime) -> Closing:
    """Что станет с запросами при переходе задачи в `target`. Продолжение из ожидания: истёкший
    запрос — EXPIRED (стадия получит отказ). Завершение задачи: всё, что не применено, — WITHDRAWN,
    чтобы у завершённой задачи не осталось ни ждущих, ни одобренных, но не исполненных вызовов."""
    saves: list[tuple[ApprovalRequest, ApprovalStatus]] = []
    for approval in approvals:
        unapplied = approval.status is ApprovalStatus.PENDING or approval.status in RESOLVED
        if is_terminal(target) and unapplied:
            status = ApprovalStatus.WITHDRAWN
        elif target is TaskStatus.EXECUTING and approval.is_expired(now):
            status = ApprovalStatus.EXPIRED
        else:
            continue
        update = {"status": status, "resolved_at": now, "resolved_via": RUNTIME_CHANNEL}
        saves.append((approval.model_copy(update=update), approval.status))
    return Closing(
        saves=saves,
        explanations=[resolved_event(closed) for closed, _ in saves],
        audit=[audit_record(closed) for closed, _ in saves],
    )


class Approvals:
    def __init__(self, *, uow: UnitOfWorkFactory, tracer: Tracer, clock: Clock) -> None:
        self._uow = uow
        self._tracer = tracer
        self._clock = clock

    def for_task(self, task_id: TaskId) -> list[ApprovalRequest]:
        with self._uow() as uow:
            return uow.approvals.for_task(task_id)

    def resolve(self, approval_id: str, decision: ApprovalDecision, *, via: str) -> ApprovalRequest:
        """Записать решение человека. Задачу не продолжает: это делает `run_until_blocked`.

        ApprovalClosed — запрос уже закрыт, истёк или задача его больше не ждёт; истёкший запрос
        при этом записывается как EXPIRED, и задача, продолжившись, получит отказ.
        """
        now = self._clock.now()
        with self._uow() as uow:
            approval = uow.approvals.get(approval_id)
            task = uow.tasks.get(approval.task_id)
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalClosed(
                f"запрос {approval_id} уже закрыт: {approval.status}", approval_id=approval_id
            )
        if task.status is not TaskStatus.WAITING_CONFIRMATION:
            raise ApprovalClosed(
                f"задача {task.id} не ждёт подтверждения ({task.status})", approval_id=approval_id
            )
        expired = approval.is_expired(now)
        if expired:
            status, channel = ApprovalStatus.EXPIRED, RUNTIME_CHANNEL
        else:
            status = (
                ApprovalStatus.APPROVED if decision is ApprovalDecision.APPROVE else ApprovalStatus.DENIED
            )
            channel = via
        resolved = approval.model_copy(update={"status": status, "resolved_at": now, "resolved_via": channel})
        kind, payload = resolved_event(resolved)
        with self._uow() as uow:
            uow.approvals.save(resolved, expected=ApprovalStatus.PENDING)
            uow.trace.append([self._tracer.event(task.id, kind, payload)])
            uow.audit.append(audit_record(resolved))
            uow.commit()
        if expired:
            raise ApprovalClosed(
                f"срок запроса {approval_id} истёк {shorten(approval.expires_at.isoformat())}",
                approval_id=approval_id,
            )
        return resolved
