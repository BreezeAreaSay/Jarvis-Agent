"""События трассы (05-storage-and-trace.md §2).

`Tracer` выдаёт событию ID и время; записывает его тот, кто пишет контрольную точку, — вместе с
переходом, который событие объясняет.
"""

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from jarvis.domain.ids import ChildKind, TaskId, child_number
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.ports.clock import Clock
from jarvis.ports.storage import IdAllocator

SUMMARY_CHARS = 200


def shorten(text: str, limit: int = SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Tracer:
    def __init__(self, ids: IdAllocator, clock: Clock) -> None:
        self._ids = ids
        self._clock = clock

    def next_id(self, task_id: TaskId, kind: ChildKind) -> str:
        """ID дочерней сущности задачи (вызова, подтверждения): у каждого вида свой счётчик."""
        return self._ids.next_child_id(task_id, kind)

    def event(self, task_id: TaskId, kind: EventKind, payload: dict[str, JsonValue]) -> TraceEvent:
        event_id = self._ids.next_child_id(task_id, "ev")
        return TraceEvent(
            id=event_id,
            task_id=task_id,
            seq=child_number(event_id),
            ts=self._clock.now(),
            kind=kind,
            payload=payload,
        )


# Замеры и живые данные: у повторного прогона они свои, а решения, правила, пути и статусы — те же.
VOLATILE_KEYS = frozenset({"duration_ms", "expires_at", "output", "output_bytes", "output_sha256"})


def normalize_events(
    events: Sequence[TraceEvent], *, aliases: Mapping[str, str] | None = None
) -> list[dict[str, JsonValue]]:
    """Трасса без того, что различается между прогонами (ID, время, пропуски номеров, замеры
    вызовов инструментов), — для сравнения записанного прогона с повторным (replay) и прогонов на
    разных хранилищах. `aliases` заменяет в строках пути окружения прогона (временную папку) метками."""
    replacements = sorted((aliases or {}).items(), key=lambda item: -len(item[0]))
    return [
        {
            "n": position,
            "kind": event.kind.value,
            "v": event.v,
            "payload": _stable(event.payload, replacements),
        }
        for position, event in enumerate(events, start=1)
    ]


def _stable(value: JsonValue, replacements: Sequence[tuple[str, str]]) -> JsonValue:
    if isinstance(value, str):
        for original, alias in replacements:
            value = value.replace(original, alias)
        return value
    if isinstance(value, list):
        return [_stable(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _stable(item, replacements) for key, item in value.items() if key not in VOLATILE_KEYS}
    return value
