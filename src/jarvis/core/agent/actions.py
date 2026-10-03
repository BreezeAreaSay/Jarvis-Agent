"""Схема и проверка решения исполнителя (ADR 0009, ADR 0023).

Схема строится из определений инструментов: у каждого инструмента — своя ветвь с его схемой
аргументов, у ответа — ветвь `finish`, где ссылаться можно только на исполненные вызовы. Сервер с
`structured_output` превращает схему в грамматику: модель не может назвать несуществующий
инструмент или сослаться на чужой вызов. Та же проверка повторяется после разбора (для серверов без
грамматики и на случай, если грамматика пропустила лишнее), а Tool Runtime всё равно проверяет
аргументы ещё раз: модель не получает обходного пути.
"""

from collections.abc import Sequence

from pydantic import JsonValue, ValidationError

from jarvis.core.models.gateway import StructuredOutput, problem_text
from jarvis.domain.agent import DECISION_CHARS, FinishAction, ProposedAction, ToolAction
from jarvis.domain.tools import ToolDefinition

_MAX_PROBLEMS = 5
_REF_DEPTH = 16


def clean_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Схема аргументов без `$defs` и `title`: ссылки раскрыты, лишние для модели поля убраны."""
    definitions = schema.get("$defs")
    defs: dict[str, JsonValue] = definitions if isinstance(definitions, dict) else {}

    def walk(node: JsonValue, depth: int) -> JsonValue:
        if depth > _REF_DEPTH:
            raise ValueError("схема аргументов слишком глубокая или рекурсивная")
        if isinstance(node, list):
            return [walk(item, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return walk(defs[ref.removeprefix("#/$defs/")], depth + 1)
        # «title» — подпись схемы (строка); аргумент с именем title — схема (объект), он остаётся.
        return {
            key: walk(value, depth + 1)
            for key, value in node.items()
            if key != "$defs" and not (key == "title" and isinstance(value, str))
        }

    cleaned = walk(schema, 0)
    assert isinstance(cleaned, dict)
    return cleaned


def decision_schema(definitions: Sequence[ToolDefinition], executed: Sequence[str]) -> dict[str, JsonValue]:
    branches: list[JsonValue] = [
        {
            "type": "object",
            "properties": {
                "type": {"const": "tool"},
                "tool": {"const": definition.id},
                "arguments": clean_schema(definition.input_schema),
            },
            "required": ["type", "tool", "arguments"],
            "additionalProperties": False,
        }
        for definition in definitions
    ]
    evidence: dict[str, JsonValue] = (
        {"type": "array", "items": {"enum": list[JsonValue](executed)}}
        if executed
        else {"type": "array", "maxItems": 0}
    )
    branches.append(
        {
            "type": "object",
            "properties": {
                "type": {"const": "finish"},
                "answer": {"type": "string", "minLength": 1},
                "evidence": evidence,
            },
            "required": ["type", "answer", "evidence"],
            "additionalProperties": False,
        }
    )
    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "minLength": 1, "maxLength": DECISION_CHARS},
            "action": {"anyOf": branches},
        },
        "required": ["decision", "action"],
        "additionalProperties": False,
    }


def decision_output(
    definitions: Sequence[ToolDefinition], executed: Sequence[str]
) -> StructuredOutput[ProposedAction]:
    known: dict[str, ToolDefinition] = {definition.id: definition for definition in definitions}

    def check(proposal: ProposedAction) -> list[str]:
        action = proposal.action
        if isinstance(action, ToolAction):
            definition = known.get(action.tool)
            if definition is None:  # имя из ответа не повторяется: объяснение — текст Jarvis
                return [f"action.tool: такого инструмента нет; доступны: {', '.join(sorted(known))}"]
            try:
                definition.input_model.model_validate(action.arguments)
            except ValidationError as exc:
                return [problem_text(error, "action.arguments.") for error in exc.errors()[:_MAX_PROBLEMS]]
            return []
        return _check_answer(action, executed)

    return StructuredOutput(model=ProposedAction, schema=decision_schema(definitions, executed), check=check)


def _check_answer(action: FinishAction, executed: Sequence[str]) -> list[str]:
    unknown = [ref for ref in action.evidence if ref not in executed]
    if not unknown:
        return []
    allowed = ", ".join(executed) or "исполненных вызовов нет — оставь evidence пустым"
    return [f"action.evidence: ссылок не на исполненные вызовы — {len(unknown)}; можно: {allowed}"]
