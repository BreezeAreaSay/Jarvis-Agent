"""Аудит (04-security.md §8): кто, что, когда и какое действие пытался сделать.

Трасса показывает ход задачи; аудит — журнал действий над компьютером. Записи только добавляются.
Аргументы не пишутся целиком: только отпечаток и краткое описание из preview.
"""

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, JsonValue

from jarvis.domain.ids import TaskId
from jarvis.domain.tools import EffectKind, ExecutionTarget, PolicyOutcome, ToolCallId, ToolEffect, ToolId

AUDIT_RESOURCES = 5
AUDIT_RESOURCE_CHARS = 300


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


def target_label(target: ExecutionTarget) -> str:
    return f"{target.kind.value}:{target.os_family}:{target.name}"


def arguments_hash(arguments: dict[str, JsonValue]) -> str:
    """Отпечаток нормализованных аргументов: в аудите — он, а не сами аргументы."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def effect_kinds(effects: list[ToolEffect]) -> list[EffectKind]:
    return sorted({effect.kind for effect in effects})


def resources(effects: list[ToolEffect]) -> list[str]:
    """Первые затронутые ресурсы, укороченные: аудит не хранит длинных значений."""
    return [_clip(effect.resource) for effect in effects[:AUDIT_RESOURCES]]


def _clip(text: str) -> str:
    return text if len(text) <= AUDIT_RESOURCE_CHARS else text[: AUDIT_RESOURCE_CHARS - 1] + "…"
