"""События трассы (05-storage-and-trace.md §2, ADR 0015).

Трасса — не лог рассуждений: только факты о задаче. Payload небольшой; большие данные — артефакты.
"""

import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, JsonValue, PositiveInt, model_validator

from jarvis.domain.ids import TaskId, child_number

MAX_PAYLOAD_BYTES = 4096


class EventKind(StrEnum):
    TASK_CREATED = "task.created"
    TASK_TRANSITION = "task.transition"
    BUDGET_EXCEEDED = "budget.exceeded"
    ERROR = "error"
    TASK_FINISHED = "task.finished"


class TraceEvent(BaseModel, frozen=True, extra="forbid"):
    id: str  # task_42.ev_31
    task_id: TaskId
    seq: PositiveInt  # порядок внутри задачи; совпадает с номером в id
    ts: datetime
    kind: EventKind
    v: PositiveInt = 1
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.id.startswith(f"{self.task_id}.ev_") or child_number(self.id) != self.seq:
            raise ValueError(f"ID события {self.id} не соответствует задаче {self.task_id} и seq {self.seq}")
        size = len(json.dumps(self.payload, ensure_ascii=False).encode("utf-8"))
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload события {self.kind} — {size} байт, больше {MAX_PAYLOAD_BYTES}")
        return self
