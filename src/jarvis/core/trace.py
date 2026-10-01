"""Запись событий трассы (05-storage-and-trace.md §2)."""

from pydantic import JsonValue

from jarvis.domain.ids import TaskId, child_number
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.ports.clock import Clock
from jarvis.ports.storage import IdAllocator, UnitOfWorkFactory

SUMMARY_CHARS = 200


def shorten(text: str, limit: int = SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Tracer:
    def __init__(self, uow: UnitOfWorkFactory, ids: IdAllocator, clock: Clock) -> None:
        self._uow = uow
        self._ids = ids
        self._clock = clock

    def event(
        self,
        task_id: TaskId,
        kind: EventKind,
        payload: dict[str, JsonValue],
    ) -> TraceEvent:
        """Событие для контрольной точки: ID выдан, но запись — вместе с задачей."""
        event_id = self._ids.next_child_id(task_id, "ev")
        return TraceEvent(
            id=event_id,
            task_id=task_id,
            seq=child_number(event_id),
            ts=self._clock.now(),
            kind=kind,
            payload=payload,
        )

    def emit(
        self,
        task_id: TaskId,
        kind: EventKind,
        payload: dict[str, JsonValue],
    ) -> TraceEvent:
        """Журнальное событие: записывается сразу и не откатывается."""
        event = self.event(task_id, kind, payload)
        with self._uow() as uow:
            uow.trace.append([event])
            uow.commit()
        return event
