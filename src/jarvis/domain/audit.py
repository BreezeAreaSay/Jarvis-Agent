"""Аудит (04-security.md §8): кто, что, когда и какое действие пытался сделать.

Трасса показывает ход задачи; аудит — журнал действий над компьютером. Записи только добавляются.
Аргументы не пишутся целиком: только отпечаток и краткое описание из preview.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from jarvis.domain.ids import TaskId
from jarvis.domain.tools import EffectKind, PolicyOutcome, ToolCallId, ToolId


class AuditAction(StrEnum):
    DECISION = "decision"  # решение политики — до исполнения
    RESULT = "result"  # итог исполнения и проверки
    APPROVAL = "approval"  # человек решил по запросу подтверждения


class AuditRecord(BaseModel, frozen=True, extra="forbid"):
    ts: datetime
    action: AuditAction
    actor: str  # "task" (стадия задачи) или канал человека: "cli", "eval-auto"
    task_id: TaskId
    tool_call_id: ToolCallId
    tool_id: ToolId
    target: str  # "host:windows:local"
    effects: list[EffectKind]
    resources: list[str]  # затронутые ресурсы из preview (первые несколько)
    arguments_hash: str
    decision: PolicyOutcome | None = None
    approval_id: str | None = None
    approval_status: str | None = None
    execution_status: str | None = None  # succeeded, failed, timed_out, cancelled, dry_run, not_executed
    verification_status: str | None = None  # passed, failed
    dry_run: bool = False
