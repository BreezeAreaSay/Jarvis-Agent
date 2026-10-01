"""Tool Runtime — единственная граница исполнения (04-security.md §3, ADR 0004, ADR 0022).

Конвейер одного вызова:

    инструмент из реестра → цель исполнения → аргументы по схеме → preview (без эффектов)
    → решение политики (аудит до исполнения) → подтверждение или отказ
    → для вызова с эффектами повторный preview (TOCTOU) → бюджет → execute с таймаутом и отменой
    → результат по схеме → verify → трасса и аудит → ToolOutcome

Ожидаемые исходы (отказ, нужно подтверждение, dry run) — значения; сбои — исключения ToolError.
Каждая запись трассы и аудита делается здесь, а не в инструменте: инструмент хранилища не видит.
"""

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, JsonValue, ValidationError

from jarvis.core.budget import BudgetMeter
from jarvis.core.policy import PolicyEngine
from jarvis.core.tools.registry import ToolRegistry
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.approvals import RESOLVED, ApprovalRequest, ApprovalStatus
from jarvis.domain.audit import (
    AuditAction,
    AuditRecord,
    arguments_hash,
    effect_kinds,
    resources,
    target_label,
)
from jarvis.domain.budget import BudgetLimit
from jarvis.domain.errors import (
    ApprovalRequired,
    InvalidToolArguments,
    JarvisError,
    ToolCancelled,
    ToolDenied,
    ToolError,
    ToolExecutionFailed,
    ToolPreviewFailed,
    ToolTimeout,
    ToolVerificationFailed,
    UnsupportedTarget,
)
from jarvis.domain.ids import TaskId
from jarvis.domain.task import Task
from jarvis.domain.tools import (
    ExecutionTarget,
    PolicyDecision,
    PolicyOutcome,
    ToolCall,
    ToolCallId,
    ToolDefinition,
    ToolId,
    ToolOutcome,
    ToolOutcomeKind,
    ToolPreview,
    ToolResult,
    ToolVerification,
)
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.ports.clock import Clock
from jarvis.ports.storage import UnitOfWorkFactory
from jarvis.ports.tools import Tool, ToolContext

_log = logging.getLogger(__name__)

# Полный результат попадает в трассу, только если он небольшой; иначе — размер (лимит события 4 КБ).
TRACE_OUTPUT_BYTES = 2048
ExecutionStatus = Literal["succeeded", "failed", "timed_out", "cancelled"]


class ToolRuntime:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        uow: UnitOfWorkFactory,
        tracer: Tracer,
        clock: Clock,
        target: ExecutionTarget,
        approval_ttl_s: float,
        protected_roots: Sequence[str] = (),
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._uow = uow
        self._tracer = tracer
        self._clock = clock
        self._target = target
        self._approval_ttl_s = approval_ttl_s
        self._protected = tuple(protected_roots)

    @property
    def target(self) -> ExecutionTarget:
        return self._target

    def definitions(self) -> list[ToolDefinition]:
        return self._registry.definitions()

    async def call(
        self, task: Task, budget: BudgetMeter, tool_id: str, arguments: dict[str, JsonValue]
    ) -> ToolOutcome:
        """Новый вызов инструмента от стадии задачи."""
        call = ToolCall(
            id=ToolCallId(self._tracer.next_id(task.id, "call")),
            task_id=task.id,
            tool_id=ToolId(tool_id),
            arguments=arguments,
            target=self._target,
        )
        return await self._run(task, budget, call, approval=None)

    def resumable(self, task_id: TaskId) -> ApprovalRequest | None:
        """Решённый, но ещё не применённый запрос подтверждения: его вызов нужно довести."""
        with self._uow() as uow:
            approvals = uow.approvals.for_task(task_id)
        return next((item for item in approvals if item.status in RESOLVED), None)

    async def resume(self, task: Task, budget: BudgetMeter) -> ToolOutcome | None:
        """Довести вызов, по которому человек уже решил (после выхода из WAITING_CONFIRMATION)."""
        approval = self.resumable(task.id)
        if approval is None:
            return None
        return await self._run(task, budget, approval.call, approval=approval)

    async def _run(
        self, task: Task, budget: BudgetMeter, call: ToolCall, *, approval: ApprovalRequest | None
    ) -> ToolOutcome:
        tool = self._registry.get(call.tool_id)
        definition = tool.definition
        if call.target.kind not in definition.targets or call.target != self._target:
            raise UnsupportedTarget(
                f"{definition.id}: цель {call.target.kind} не поддерживается", tool_id=definition.id
            )
        if approval is not None and approval.status is not ApprovalStatus.APPROVED:
            return self._refused(task, approval)
        arguments = _validate(definition.input_model, call.arguments, definition.id)
        context = ToolContext(
            target=call.target,
            working_directory=task.request.working_directory,
            protected_roots=self._protected,
        )
        try:
            preview = await self._preview(tool, arguments, context, call)
        except Exception:
            if approval is not None:  # одобренный вызов не состоялся: решение израсходовано
                self._journal(task.id, [], used=approval)
            raise

        decision = self._policy.decide(call, preview)
        if approval is not None and decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
            if preview.fingerprint() == approval.preview_fingerprint:
                decision = PolicyDecision(
                    outcome=PolicyOutcome.ALLOW,
                    rules=sorted([*decision.rules, "approval.approved"]),
                    reason=f"одобрено человеком ({approval.resolved_via})",
                )
            else:  # между подтверждением и исполнением изменилось то, что будет затронуто
                decision = decision.model_copy(
                    update={
                        "rules": sorted([*decision.rules, "approval.stale"]),
                        "reason": "затронутое изменилось",
                    }
                )
        self._decided(task, call, preview, decision, approval=approval)

        if decision.outcome is PolicyOutcome.DENY:
            return ToolOutcome(kind=ToolOutcomeKind.DENIED, call=call, preview=preview, decision=decision)
        if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
            if task.request.dry_run:  # симуляция не спрашивает человека: показывает, что спросила бы
                return ToolOutcome(
                    kind=ToolOutcomeKind.DRY_RUN,
                    call=call,
                    preview=preview,
                    decision=decision,
                    would_execute=False,
                )
            created = self._request_approval(task, call, preview)
            return ToolOutcome(
                kind=ToolOutcomeKind.NEEDS_APPROVAL,
                call=call,
                preview=preview,
                decision=decision,
                approval_id=created.id,
            )

        try:
            budget.charge(BudgetLimit.TOOL_CALLS)  # исполнение и симуляция считаются; отказ — нет
            if not task.request.dry_run and preview.has_side_effects:
                # TOCTOU: перед эффектом preview повторяется; разошёлся — решение и подтверждение не годятся.
                again = await self._preview(tool, arguments, context, call, record=False)
                if again.fingerprint() != preview.fingerprint():
                    raise ToolPreviewFailed(
                        f"{definition.id}: затронутое изменилось между проверкой и исполнением",
                        tool_id=definition.id,
                    )
        except Exception:
            # Разрешённый вызов не исполнен: у решения в аудите должен быть итог.
            self._journal(
                task.id,
                [],
                self._audit(AuditAction.RESULT, task, call, preview, decision, execution="not_executed"),
            )
            raise
        if task.request.dry_run:
            self._journal(
                task.id,
                [],
                self._audit(AuditAction.RESULT, task, call, preview, decision, execution="dry_run"),
            )
            return ToolOutcome(
                kind=ToolOutcomeKind.DRY_RUN,
                call=call,
                preview=preview,
                decision=decision,
                would_execute=True,
            )
        # Исполняется ровно то, что видели политика и человек: нормализованные аргументы preview.
        normalized = _validate(definition.input_model, preview.normalized_arguments, definition.id)
        return await self._execute(task, tool, call, normalized, context, preview, decision)

    def _refused(self, task: Task, approval: ApprovalRequest) -> ToolOutcome:
        """Человек отказал или срок вышел: стадия получает отказ. Preview не повторяется — исход
        строится из того, что видел человек, и не зависит от того, что стало с ресурсом."""
        preview = ToolPreview(
            summary=approval.summary,
            normalized_arguments=approval.arguments,
            effects=approval.effects,
            target=approval.target,
        )
        reason = (
            "отказано человеком" if approval.status is ApprovalStatus.DENIED else "срок подтверждения истёк"
        )
        decision = PolicyDecision(
            outcome=PolicyOutcome.DENY, rules=[f"approval.{approval.status}"], reason=reason
        )
        self._decided(task, approval.call, preview, decision, approval=approval)
        return ToolOutcome(
            kind=ToolOutcomeKind.DENIED, call=approval.call, preview=preview, decision=decision
        )

    async def _preview(
        self, tool: Tool, arguments: BaseModel, context: ToolContext, call: ToolCall, *, record: bool = True
    ) -> ToolPreview:
        definition = tool.definition
        try:
            async with asyncio.timeout(definition.timeout_s):
                preview = await tool.preview(arguments, context)
        except TimeoutError:
            raise ToolTimeout(f"{definition.id}: preview не уложился в {definition.timeout_s} с") from None
        except ToolError:
            raise
        except Exception as exc:
            _log.warning("preview %s упал", definition.id, exc_info=True)
            raise ToolPreviewFailed(
                f"{definition.id}: preview не удался: {exc}", tool_id=definition.id
            ) from None
        undeclared = {effect.kind for effect in preview.effects} - definition.effects
        if undeclared or preview.target != call.target:
            raise ToolPreviewFailed(
                f"{definition.id}: preview объявил эффекты или цель вне определения инструмента",
                undeclared=[kind.value for kind in sorted(undeclared)],
            )
        if record:
            self._journal(
                call.task_id,
                [
                    (
                        EventKind.TOOL_PREVIEWED,
                        {
                            "call_id": call.id,
                            "tool": definition.id,
                            "target": target_label(call.target),
                            "summary": shorten(preview.summary),
                            "effects": [
                                {"kind": effect.kind.value, "resource": shorten(effect.resource, 300)}
                                for effect in preview.effects[:10]
                            ],
                            "arguments": _limited(preview.normalized_arguments, 800),
                        },
                    )
                ],
            )
        return preview

    async def _execute(
        self,
        task: Task,
        tool: Tool,
        call: ToolCall,
        arguments: BaseModel,
        context: ToolContext,
        preview: ToolPreview,
        decision: PolicyDecision,
    ) -> ToolOutcome:
        definition = tool.definition
        self._journal(task.id, [(EventKind.TOOL_STARTED, {"call_id": call.id, "tool": definition.id})])
        started = time.perf_counter()
        try:
            async with asyncio.timeout(definition.timeout_s):
                raw = await tool.execute(arguments, context)
            output = _validate(
                definition.output_model, raw.model_dump(mode="json"), definition.id, output=True
            )
        except TimeoutError:
            self._finished(task, call, preview, decision, "timed_out", started, error=ToolTimeout("таймаут"))
            raise ToolTimeout(
                f"{definition.id}: не уложился в {definition.timeout_s} с", tool_id=definition.id
            ) from None
        except asyncio.CancelledError:
            self._finished(task, call, preview, decision, "cancelled", started, error=ToolCancelled("отмена"))
            raise
        except ToolError as exc:
            self._finished(task, call, preview, decision, "failed", started, error=exc)
            raise
        except Exception as exc:
            _log.warning("инструмент %s упал", definition.id, exc_info=True)
            failure = ToolExecutionFailed(
                f"{definition.id}: {type(exc).__name__}: {exc}", tool_id=definition.id
            )
            self._finished(task, call, preview, decision, "failed", started, error=failure)
            raise failure from None

        result = ToolResult(
            output=output.model_dump(mode="json"), untrusted_content=definition.untrusted_output
        )
        try:
            verification = await tool.verify(arguments, output, context)
        except Exception as exc:
            verification = ToolVerification(
                passed=False, checks=[f"проверка упала: {type(exc).__name__}: {exc}"]
            )
        self._finished(
            task, call, preview, decision, "succeeded", started, result=result, verification=verification
        )
        if not verification.passed:
            raise ToolVerificationFailed(
                f"{definition.id}: постусловие не выполнено: {'; '.join(verification.checks)}",
                tool_id=definition.id,
            )
        return ToolOutcome(
            kind=ToolOutcomeKind.EXECUTED,
            call=call,
            preview=preview,
            decision=decision,
            result=result,
            verification=verification,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    def _decided(
        self,
        task: Task,
        call: ToolCall,
        preview: ToolPreview,
        decision: PolicyDecision,
        *,
        approval: ApprovalRequest | None,
    ) -> None:
        """Решение — в трассу и аудит до исполнения; решение человека расходуется в той же записи."""
        payload: dict[str, JsonValue] = {
            "call_id": call.id,
            "outcome": decision.outcome.value,
            "rules": list(decision.rules),
            "reason": shorten(decision.reason),
        }
        if decision.outcome is PolicyOutcome.DENY:
            payload["category"] = ToolDenied.category
        elif decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
            payload["category"] = ApprovalRequired.category
        if approval is not None:
            payload["approval_id"] = approval.id
        record = self._audit(
            AuditAction.DECISION,
            task,
            call,
            preview,
            decision,
            approval=approval,
            execution="not_executed" if decision.outcome is PolicyOutcome.DENY else None,
        )
        self._journal(task.id, [(EventKind.POLICY_DECIDED, payload)], record, used=approval)

    def _request_approval(self, task: Task, call: ToolCall, preview: ToolPreview) -> ApprovalRequest:
        now = self._clock.now()
        approval = ApprovalRequest(
            id=self._tracer.next_id(task.id, "appr"),
            task_id=task.id,
            call=call,
            summary=preview.summary,
            effects=preview.effects,
            target=preview.target,
            arguments=preview.normalized_arguments,
            preview_fingerprint=preview.fingerprint(),
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(seconds=self._approval_ttl_s),
        )
        event = (
            EventKind.APPROVAL_REQUESTED,
            {
                "approval_id": approval.id,
                "call_id": call.id,
                "summary": shorten(approval.summary),
                "expires_at": approval.expires_at.isoformat(),
            },
        )
        self._journal(task.id, [event], new_approval=approval)
        return approval

    def _finished(
        self,
        task: Task,
        call: ToolCall,
        preview: ToolPreview,
        decision: PolicyDecision,
        status: ExecutionStatus,
        started: float,
        *,
        result: ToolResult | None = None,
        verification: ToolVerification | None = None,
        error: JarvisError | None = None,
    ) -> None:
        finished: dict[str, JsonValue] = {
            "call_id": call.id,
            "tool": call.tool_id,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
        if result is not None:
            encoded = json.dumps(result.output, ensure_ascii=False)
            finished["output_bytes"] = len(encoded.encode("utf-8"))
            finished["untrusted"] = result.untrusted_content
            if len(encoded.encode("utf-8")) <= TRACE_OUTPUT_BYTES:
                finished["output"] = result.output
        if error is not None:
            finished["error"] = {"category": error.category, "message": shorten(error.message, 300)}
        events: list[tuple[EventKind, dict[str, JsonValue]]] = [(EventKind.TOOL_FINISHED, finished)]
        if verification is not None:
            checks: list[JsonValue] = [shorten(check, 200) for check in verification.checks[:10]]
            verified: dict[str, JsonValue] = {
                "call_id": call.id,
                "passed": verification.passed,
                "checks": checks,
            }
            events.append((EventKind.TOOL_VERIFIED, verified))
        record = self._audit(
            AuditAction.RESULT,
            task,
            call,
            preview,
            decision,
            execution=status,
            verification=None if verification is None else ("passed" if verification.passed else "failed"),
        )
        try:
            self._journal(task.id, events, record)
        except Exception:
            if status != "cancelled":
                raise
            # Отмена важнее записи итога: вызывающий должен получить CancelledError.
            _log.warning("не удалось записать отмену вызова %s", call.id, exc_info=True)

    def _audit(
        self,
        action: AuditAction,
        task: Task,
        call: ToolCall,
        preview: ToolPreview,
        decision: PolicyDecision,
        *,
        approval: ApprovalRequest | None = None,
        execution: str | None = None,
        verification: str | None = None,
    ) -> AuditRecord:
        return _audit_record(
            self._clock.now(),
            action,
            task,
            call,
            preview,
            decision,
            approval=approval,
            execution=execution,
            verification=verification,
        )

    def _journal(
        self,
        task_id: TaskId,
        events: Sequence[tuple[EventKind, dict[str, JsonValue]]],
        audit: AuditRecord | None = None,
        *,
        used: ApprovalRequest | None = None,
        new_approval: ApprovalRequest | None = None,
    ) -> list[TraceEvent]:
        """Журнальная запись: события, аудит и изменения подтверждения — одной транзакцией, сразу."""
        built = [self._tracer.event(task_id, kind, payload) for kind, payload in events]
        with self._uow() as uow:
            if built:
                uow.trace.append(built)
            if audit is not None:
                uow.audit.append(audit)
            if new_approval is not None:
                uow.approvals.add(new_approval)
            if used is not None:
                # Решение применяется ровно один раз: сравнение со статусом не даст исполнить дважды.
                uow.approvals.save(
                    used.model_copy(update={"status": ApprovalStatus.USED}), expected=used.status
                )
            uow.commit()
        return built


def _validate[M: BaseModel](model: type[M], data: object, tool_id: str, *, output: bool = False) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
        ]
        if output:
            raise ToolExecutionFailed(
                f"{tool_id}: результат не прошёл схему: {'; '.join(problems)}"
            ) from None
        raise InvalidToolArguments(f"{tool_id}: аргументы не прошли схему: {'; '.join(problems)}") from None


def _limited(value: dict[str, JsonValue], limit: int) -> dict[str, JsonValue]:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= limit:
        return value
    return {"truncated": shorten(encoded, limit // 2)}


def _audit_record(
    now: datetime,
    action: AuditAction,
    task: Task,
    call: ToolCall,
    preview: ToolPreview,
    decision: PolicyDecision,
    *,
    approval: ApprovalRequest | None,
    execution: str | None,
    verification: str | None,
) -> AuditRecord:
    """Запись аудита: кто, что, где, с каким решением и итогом. Аргументы — только хешем (могут
    содержать личные пути и имена), ресурсы — укороченными."""
    return AuditRecord(
        ts=now,
        action=action,
        actor="task",
        task_id=task.id,
        tool_call_id=call.id,
        tool_id=call.tool_id,
        target=target_label(call.target),
        effects=effect_kinds(preview.effects),
        resources=resources(preview.effects),
        arguments_hash=arguments_hash(preview.normalized_arguments),
        decision=decision.outcome,
        approval_id=approval.id if approval else None,
        approval_status=approval.status.value if approval else None,
        execution_status=execution,
        verification_status=verification,
        dry_run=task.request.dry_run,
    )
