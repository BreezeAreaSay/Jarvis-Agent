"""Подтверждение человеком — состояние задачи, а не input() (04-security.md §6, ADR 0011)."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, JsonValue, model_validator

from jarvis.domain.ids import TaskId
from jarvis.domain.tools import ExecutionTarget, ToolCall, ToolEffect


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"  # срок вышел — как отказ
    WITHDRAWN = "withdrawn"  # задача ушла из ожидания без решения (отменена) — запрос недействителен
    USED = "used"  # решение применено: одобренный вызов исполнен или отказ передан стадии


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


# Решённые, но ещё не применённые: после выхода из WAITING_CONFIRMATION стадия доводит их вызов.
RESOLVED = frozenset({ApprovalStatus.APPROVED, ApprovalStatus.DENIED, ApprovalStatus.EXPIRED})
_CLOSED_WITHOUT_USE = frozenset({*RESOLVED, ApprovalStatus.WITHDRAWN})


class ApprovalRequest(BaseModel, frozen=True, extra="forbid"):
    id: str = Field(min_length=1)  # task_42.appr_1
    task_id: TaskId
    call: ToolCall  # ровно этот вызов, с этим ID и этими аргументами
    summary: str  # из preview: что будет сделано
    effects: list[ToolEffect]
    target: ExecutionTarget
    arguments: dict[str, JsonValue]  # нормализованные аргументы, которые видел человек
    preview_fingerprint: str  # перед исполнением preview повторяется и сверяется с этим отпечатком
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None
    resolved_via: str | None = None  # "cli", "eval-auto"; для истёкшего и отозванного — "runtime"

    @model_validator(mode="after")
    def _resolution(self) -> Self:
        if self.status in _CLOSED_WITHOUT_USE and (self.resolved_at is None or self.resolved_via is None):
            raise ValueError("у закрытого запроса есть время и канал закрытия")
        if self.expires_at <= self.created_at:
            raise ValueError("срок запроса должен быть позже его создания")
        return self

    def is_expired(self, now: datetime) -> bool:
        return self.status is ApprovalStatus.PENDING and now >= self.expires_at
