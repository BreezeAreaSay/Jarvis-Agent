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
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, JsonValue, ValidationError

from jarvis.core.approvals import RUNTIME_CHANNEL
from jarvis.core.budget import BudgetMeter
from jarvis.core.leases import Holder, Leases
from jarvis.core.policy import PolicyEngine
from jarvis.core.tools.provenance import DataClassifier
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
    LeaseLost,
    ToolCancelled,
    ToolDenied,
    ToolError,
    ToolExecutionFailed,
    ToolNotFound,
    ToolPreviewFailed,
    ToolTimeout,
    ToolVerificationFailed,
    UnsupportedTarget,
)
from jarvis.domain.ids import TaskId
from jarvis.domain.states import ACTIVE_STATUSES
from jarvis.domain.task import Task
from jarvis.domain.tools import (
    ExecutionTarget,
    Invoker,
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
from jarvis.ports.storage import UnitOfWork, UnitOfWorkFactory
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
        leases: Leases,
        protected_roots: Sequence[str] = (),
        classifier: DataClassifier | None = None,
    ) -> None:
        self._registry = registry
        self._classifier = classifier
        self._leases = leases
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
        self,
        task: Task,
        budget: BudgetMeter,
        tool_id: str,
        arguments: dict[str, JsonValue],
        *,
        invoker: Invoker = Invoker.MODEL,
    ) -> ToolOutcome:
        """Новый вызов инструмента от стадии задачи. `invoker` — кто его предложил: по умолчанию модель
        (самый строгий случай); прямую команду помечает только стадия DIRECT (ADR 0030)."""
        call = ToolCall(
            id=ToolCallId(self._tracer.next_id(task.id, "call")),
            task_id=task.id,
            tool_id=ToolId(tool_id),
            arguments=arguments,
            target=self._target,
            invoker=invoker,
        )
        return await self._run(task, budget, call, approval=None)

    def resumable(self, task_id: TaskId) -> ApprovalRequest | None:
        """Решённый, но ещё не применённый запрос подтверждения: его вызов нужно довести."""
        with self._uow() as uow:
            approvals = uow.approvals.for_task(task_id)
        resolved = [item for item in approvals if item.status in RESOLVED]
        return resolved[-1] if resolved else None

    async def resume(self, task: Task, budget: BudgetMeter) -> ToolOutcome | None:
        """Довести вызов, по которому человек уже решил (после выхода из WAITING_CONFIRMATION)."""
        approval = self.resumable(task.id)
        if approval is None:
            return None
        return await self._run(task, budget, approval.call, approval=approval)

    async def _run(
        self, task: Task, budget: BudgetMeter, call: ToolCall, *, approval: ApprovalRequest | None
    ) -> ToolOutcome:
        # Задача из хранилища, а не из рук стадии: режим dry run и рабочая папка — настоящие, и
        # вызывать инструменты может только прогон, который всё ещё ведёт задачу.
        with self._uow() as uow:
            task = self._owned(uow, task)
        tool = self._registry.get(call.tool_id)
        definition = tool.definition
        if not definition.model_visible and call.invoker is Invoker.MODEL:
            # Служебный инструмент (согласие на облако) модель не видит и вызвать не может.
            raise ToolNotFound(f"нет инструмента {call.tool_id}", tool_id=call.tool_id)
        if call.target.kind not in definition.targets or call.target != self._target:
            raise UnsupportedTarget(
                f"{definition.id}: цель {call.target.kind} не поддерживается", tool_id=definition.id
            )
        if approval is not None and approval.status is not ApprovalStatus.APPROVED:
            return self._refused(task, approval, approval.status)
        if approval is not None and self._clock.now() >= approval.expires_at:
            # Одобрение действует до срока запроса: вызов, одобренный давно, без человека не исполняется.
            return self._refused(task, approval, ApprovalStatus.EXPIRED)
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
        except BaseException as exc:
            # Разрешённый вызов не исполнен (в том числе отменён): у решения в аудите должен быть итог.
            record = self._audit(AuditAction.RESULT, task, call, preview, decision, execution="not_executed")
            self._record(task.id, [], record, cancelled=isinstance(exc, asyncio.CancelledError))
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
        normalized = definition.input_model.model_validate(preview.normalized_arguments)
        return await self._execute(task, tool, call, normalized, context, preview, decision)

    def _refused(self, task: Task, approval: ApprovalRequest, status: ApprovalStatus) -> ToolOutcome:
        """Человек отказал или срок вышел: стадия получает отказ. Preview не повторяется — исход
        строится из того, что видел человек, и не зависит от того, что стало с ресурсом."""
        preview = ToolPreview(
            summary=approval.summary,
            normalized_arguments=approval.arguments,
            effects=approval.effects,
            target=approval.target,
        )
        reason = "отказано человеком" if status is ApprovalStatus.DENIED else "срок подтверждения истёк"
        decision = PolicyDecision(outcome=PolicyOutcome.DENY, rules=[f"approval.{status}"], reason=reason)
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
        try:  # до политики: исполняться будут именно эти аргументы
            definition.input_model.model_validate(preview.normalized_arguments)
        except ValidationError:
            raise ToolPreviewFailed(
                f"{definition.id}: нормализованные аргументы preview не прошли схему", tool_id=definition.id
            ) from None
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
        self._journal(
            task.id, [(EventKind.TOOL_STARTED, {"call_id": call.id, "tool": definition.id})], fence=task
        )
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
            async with asyncio.timeout(definition.timeout_s):
                verification = await tool.verify(arguments, output, context)
        except TimeoutError:
            verification = ToolVerification(
                passed=False, checks=[f"проверка не уложилась в {definition.timeout_s} с"]
            )
        except asyncio.CancelledError:
            # Вызов уже исполнен: его итог записывается и при отмене посреди проверки.
            self._finished(
                task, call, preview, decision, "succeeded", started, result=result, unverified=True
            )
            raise
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
        # Классы данных результата — по происхождению (ADR 0028): их проверит граница облака.
        classes = (
            self._classifier.classify(definition, preview, result)
            if self._classifier is not None
            else definition.output_data
        )
        return ToolOutcome(
            kind=ToolOutcomeKind.EXECUTED,
            call=call,
            preview=preview,
            decision=decision,
            result=result,
            verification=verification,
            duration_ms=round((time.perf_counter() - started) * 1000),
            data_classes=classes,
        )

    def _owned(self, uow: UnitOfWork, task: Task) -> Task:
        """Задача в хранилище та же, что у стадии, активна, и её аренда — живая аренда этого процесса.
        Иначе её уже ведёт кто-то другой (или её восстановили как прерванную): LeaseLost."""
        stored = uow.tasks.get(task.id)
        lease = uow.leases.get(task.id)
        live = lease is not None and lease.is_live(self._clock.now())
        owned = self._leases.holder(lease) is Holder.MINE and live
        if stored.version != task.version or stored.status not in ACTIVE_STATUSES or not owned:
            raise LeaseLost(
                f"задачу {task.id} больше не ведёт этот прогон ({stored.status}): вызов не исполняется",
                task_id=task.id,
            )
        return stored

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
        self._journal(task.id, [(EventKind.POLICY_DECIDED, payload)], record, used=approval, fence=task)

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
        self._journal(task.id, [event], new_approval=approval, withdraw_older=True)
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
        unverified: bool = False,
    ) -> None:
        """Итог исполнения — в трассу и аудит. `unverified` — отмена пришла во время проверки."""
        finished: dict[str, JsonValue] = {
            "call_id": call.id,
            "tool": call.tool_id,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }
        if result is not None:
            encoded = json.dumps(result.output, ensure_ascii=False, sort_keys=True).encode("utf-8")
            finished["output_bytes"] = len(encoded)
            finished["output_sha256"] = hashlib.sha256(encoded).hexdigest()
            finished["untrusted"] = result.untrusted_content
            # Прочитанное из зоны секретов в трассу не попадает ни при каком размере.
            secret = any(rule.startswith("zone.secrets") for rule in decision.rules)
            if len(encoded) <= TRACE_OUTPUT_BYTES and not secret:
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
            verification="cancelled" if unverified else _verdict(verification),
        )
        self._record(task.id, events, record, cancelled=status == "cancelled" or unverified)

    def _record(
        self,
        task_id: TaskId,
        events: Sequence[tuple[EventKind, dict[str, JsonValue]]],
        audit: AuditRecord,
        *,
        cancelled: bool,
    ) -> None:
        """Итог вызова. При отмене запись — по возможности: вызывающий должен получить CancelledError."""
        try:
            self._journal(task_id, events, audit)
        except Exception:
            if not cancelled:
                raise
            _log.warning("не удалось записать итог отменённого вызова (задача %s)", task_id, exc_info=True)

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
        withdraw_older: bool = False,
        fence: Task | None = None,
    ) -> list[TraceEvent]:
        """Журнальная запись: события, аудит и изменения подтверждения — одной транзакцией, сразу.
        `fence` — запись допуска к действию (решение, начало исполнения): она проходит, только пока
        этот прогон ведёт задачу."""
        built = [self._tracer.event(task_id, kind, payload) for kind, payload in events]
        with self._uow() as uow:
            if fence is not None:
                self._owned(uow, fence)
            if withdraw_older:
                # Живым остаётся только новый запрос: старое решение не исполнится вместо нового.
                for older in uow.approvals.for_task(task_id):
                    if older.status is ApprovalStatus.PENDING or older.status in RESOLVED:
                        withdrawn = older.model_copy(
                            update={
                                "status": ApprovalStatus.WITHDRAWN,
                                "resolved_at": self._clock.now(),
                                "resolved_via": RUNTIME_CHANNEL,
                            }
                        )
                        uow.approvals.save(withdrawn, expected=older.status)
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


def _verdict(verification: ToolVerification | None) -> str | None:
    if verification is None:
        return None
    return "passed" if verification.passed else "failed"


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
