"""Человекочитаемая трасса (05-storage-and-trace.md §2): шаблоны над событиями, без LLM.

Содержимое событий — сохранённые данные, а не инструкции: здесь оно только отображается. Управляющие
символы (например, escape-последовательности терминала в имени файла) показываются экранированными.
"""

import json
import re
from collections.abc import Sequence
from datetime import tzinfo

from jarvis.domain.metrics import TaskMetrics
from jarvis.domain.task import TaskSnapshot
from jarvis.domain.trace import EventKind, TraceEvent

_INDENT = " " * 10
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def render_timeline(
    task: TaskSnapshot, events: Sequence[TraceEvent], metrics: TaskMetrics, *, tz: tzinfo
) -> str:
    route = f"  route={task.route}" if task.route else ""
    lines = [f"TASK {task.id}  {task.status}{route}"]
    for event in events:
        lines.append("")
        lines.extend(_render_event(event, tz))
    lines += ["", _render_metrics(metrics)]
    return "\n".join(lines) + "\n"


def _render_event(event: TraceEvent, tz: tzinfo) -> list[str]:
    time = event.ts.astimezone(tz).strftime("%H:%M:%S")
    payload = event.payload
    match event.kind:
        case EventKind.TASK_CREATED:
            return [
                f"{time} CREATED",
                f"{_INDENT}request={_quote(payload.get('text'))}",
                f"{_INDENT}origin={payload.get('origin')}",
            ]
        case EventKind.TASK_TRANSITION:
            lines = [f"{time} {payload.get('to')}"]
            if "route" in payload:
                lines.append(f"{_INDENT}route={payload['route']}")
            lines.append(f"{_INDENT}reason={payload.get('reason')}")
            if "interruption" in payload:
                lines.append(f"{_INDENT}interruption={payload['interruption']}")
            return lines
        case EventKind.ERROR:
            return [f"{time} ! error", f"{_INDENT}{payload.get('category')}: {payload.get('message')}"]
        case EventKind.BUDGET_EXCEEDED:
            limit, value, maximum = payload.get("limit"), payload.get("value"), payload.get("maximum")
            return [f"{time} ! budget", f"{_INDENT}{limit}: {value} при максимуме {maximum}"]
        case EventKind.TASK_FINISHED:
            lines = [f"{time} FINISHED {payload.get('status')}"]
            if payload.get("answer") is not None:
                lines.append(f"{_INDENT}answer={_quote(payload.get('answer'))}")
            return lines
        case EventKind.TOOL_PREVIEWED:
            effects = payload.get("effects")
            items = effects if isinstance(effects, list) else []
            described = [f"{e.get('kind')} {e.get('resource')}" for e in items if isinstance(e, dict)]
            return [
                f"{time} tool {payload.get('tool')}  {payload.get('call_id')}",
                f"{_INDENT}{_clean(payload.get('summary'))}",
                f"{_INDENT}effects={_clean(', '.join(described) or 'нет')}",
            ]
        case EventKind.POLICY_DECIDED:
            rules = payload.get("rules")
            shown = ", ".join(str(rule) for rule in rules) if isinstance(rules, list) else ""
            return [
                f"{time} policy {payload.get('outcome')}  ({shown})",
                f"{_INDENT}{_clean(payload.get('reason'))}",
            ]
        case EventKind.APPROVAL_REQUESTED:
            return [
                f"{time} ? approval {payload.get('approval_id')}",
                f"{_INDENT}{_clean(payload.get('summary'))}",
                f"{_INDENT}expires_at={payload.get('expires_at')}",
            ]
        case EventKind.APPROVAL_RESOLVED:
            approval, status, via = payload.get("approval_id"), payload.get("status"), payload.get("via")
            return [f"{time} approval {approval} {status} via {via}"]
        case EventKind.TOOL_STARTED:
            return [f"{time} tool started  {payload.get('call_id')}"]
        case EventKind.TOOL_FINISHED:
            status, call, duration = payload.get("status"), payload.get("call_id"), payload.get("duration_ms")
            lines = [f"{time} tool {status}  {call}  {duration} мс"]
            if "output_bytes" in payload:
                trust = ", недоверенные данные" if payload.get("untrusted") else ""
                lines.append(f"{_INDENT}результат {payload.get('output_bytes')} байт{trust}")
            error = payload.get("error")
            if isinstance(error, dict):
                lines.append(f"{_INDENT}{error.get('category')}: {_clean(error.get('message'))}")
            return lines
        case EventKind.TOOL_VERIFIED:
            verdict = "passed" if payload.get("passed") else "FAILED"
            return [f"{time} verify {verdict}  {payload.get('call_id')}"]
        case _:  # вид события из более новой версии: показать как есть
            return [f"{time} {event.kind}", f"{_INDENT}{_clean(json.dumps(payload, ensure_ascii=False))}"]


def _render_metrics(metrics: TaskMetrics) -> str:
    state = "завершена" if metrics.finished else "не завершена"
    return (
        f"METRICS {state} · длительность {metrics.duration_ms / 1000:.1f} с · "
        f"активно {metrics.active_ms / 1000:.1f} с · переходов {metrics.transitions} · "
        f"ошибок {metrics.failures} · перепланирований {metrics.replans} · "
        f"вызовов модели {metrics.model_calls} · инструментов {metrics.tool_calls} · "
        f"токенов {metrics.prompt_tokens + metrics.completion_tokens}"
    )


def _quote(value: object) -> str:
    return f"«{_clean(value)}»"


def _clean(value: object) -> str:
    """Текст из трассы без управляющих символов: в терминал попадает только видимое."""
    return _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", str(value))
