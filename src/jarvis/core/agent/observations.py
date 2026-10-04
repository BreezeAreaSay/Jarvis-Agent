"""Итог вызова инструмента как наблюдение в рабочей памяти задачи (ADR 0023).

Общий для стадий, которые вызывают инструменты: агентного исполнителя и прямых команд. `summary`
пишет Jarvis; вывод инструмента, текст ошибки и причина отказа — данные (`data`): модель видит их только
блоком DATA, а прочитанное из зоны секретов помечается и не оседает в журналах.
"""

import json

from jarvis.domain.agent import Observation
from jarvis.domain.errors import ToolError
from jarvis.domain.tools import ToolOutcome, ToolOutcomeKind

OBSERVATION_BYTES = 6000  # результат вызова в рабочей памяти и промпте; больше — обрезается


def observation_of(outcome: ToolOutcome) -> Observation:
    tool = outcome.call.tool_id
    match outcome.kind:
        case ToolOutcomeKind.EXECUTED:
            assert outcome.result is not None
            data, note = clip(json.dumps(outcome.result.output, ensure_ascii=False))
            # Прочитанное из зоны секретов (с разрешения человека) не оседает в журналах.
            secret = any(rule.startswith("zone.secrets") for rule in outcome.decision.rules)
            return Observation(
                status="executed",
                summary=f"{tool}: исполнен, проверка пройдена{note}",
                data=data,
                sensitive=secret,
            )
        case ToolOutcomeKind.DRY_RUN:
            would = "был бы исполнен" if outcome.would_execute else "потребовал бы подтверждения"
            return Observation(status="dry_run", summary=f"{tool}: dry run — не исполнялся ({would})")
        case ToolOutcomeKind.DENIED:
            rules = outcome.decision.rules
            if "approval.expired" in rules:
                verdict = "срок подтверждения истёк"
            elif "approval.denied" in rules:
                verdict = "отказано человеком"
            else:
                verdict = "отказано политикой"
            # Причина может содержать пути из аргументов — это данные, а не текст Jarvis.
            reason = f"{outcome.decision.reason} [{', '.join(rules)}]"
            return Observation(status="denied", summary=f"{tool}: {verdict}, не исполнен", data=reason)
        case ToolOutcomeKind.NEEDS_APPROVAL:
            raise AssertionError("ожидание подтверждения обрабатывает стадия")


def failed_observation(error: ToolError) -> Observation:
    return Observation(status="failed", summary=f"вызов не удался ({error.category})", data=error.message)


def clip(text: str) -> tuple[str, str]:
    encoded = text.encode("utf-8")
    if len(encoded) <= OBSERVATION_BYTES:
        return text, ""
    clipped = encoded[:OBSERVATION_BYTES].decode("utf-8", errors="ignore")
    return clipped, f"; результат обрезан: {OBSERVATION_BYTES} из {len(encoded)} байт"
