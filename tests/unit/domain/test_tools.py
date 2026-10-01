import pytest
from pydantic import BaseModel

from jarvis.domain.ids import TaskId
from jarvis.domain.paths import is_within, name_of
from jarvis.domain.tools import (
    EffectKind,
    ExecutionTarget,
    PolicyDecision,
    PolicyOutcome,
    TargetKind,
    ToolCall,
    ToolCallId,
    ToolDefinition,
    ToolEffect,
    ToolId,
    ToolOutcome,
    ToolOutcomeKind,
    ToolPreview,
    ToolResult,
    ToolVerification,
)

HOST = ExecutionTarget(kind=TargetKind.HOST, os_family="posix", name="local")


def preview(*effects: ToolEffect, **arguments: str) -> ToolPreview:
    return ToolPreview(summary="s", normalized_arguments=dict(arguments), effects=list(effects), target=HOST)


def test_side_effects_are_anything_but_reading() -> None:
    assert not preview(ToolEffect(kind=EffectKind.READ, resource="/a")).has_side_effects
    assert preview(ToolEffect(kind=EffectKind.CREATE, resource="/a")).has_side_effects
    assert not preview().has_side_effects


def test_fingerprint_covers_what_is_touched_but_not_wording() -> None:
    base = preview(ToolEffect(kind=EffectKind.WRITE, resource="/a"), path="/a")
    assert base.fingerprint() == base.model_copy(update={"summary": "другие слова"}).fingerprint()
    assert (
        base.fingerprint()
        != preview(ToolEffect(kind=EffectKind.WRITE, resource="/b"), path="/a").fingerprint()
    )
    assert (
        base.fingerprint()
        != preview(ToolEffect(kind=EffectKind.WRITE, resource="/a"), path="/b").fingerprint()
    )


class Args(BaseModel):
    path: str


def test_definition_exposes_schemas_and_summary() -> None:
    definition = ToolDefinition(
        id=ToolId("x.y"),
        description="Первая строка.\nПодробности.",
        input_model=Args,
        output_model=Args,
        effects=frozenset({EffectKind.READ}),
        targets=frozenset({TargetKind.HOST}),
        timeout_s=1,
    )
    assert definition.summary == "Первая строка."
    assert definition.input_schema["required"] == ["path"]


def test_outcome_shape_matches_its_kind() -> None:
    call = ToolCall(
        id=ToolCallId("task_1.call_1"),
        task_id=TaskId("task_1"),
        tool_id=ToolId("x"),
        arguments={},
        target=HOST,
    )
    allow = PolicyDecision(outcome=PolicyOutcome.ALLOW, rules=[], reason="")
    ToolOutcome(
        kind=ToolOutcomeKind.EXECUTED,
        call=call,
        preview=preview(),
        decision=allow,
        result=ToolResult(output={}),
        verification=ToolVerification(passed=True, checks=[]),
    )
    with pytest.raises(ValueError, match="исполненного"):
        ToolOutcome(kind=ToolOutcomeKind.EXECUTED, call=call, preview=preview(), decision=allow)
    with pytest.raises(ValueError, match="подтверждения"):
        ToolOutcome(kind=ToolOutcomeKind.NEEDS_APPROVAL, call=call, preview=preview(), decision=allow)
    with pytest.raises(ValueError, match="dry run"):
        ToolOutcome(kind=ToolOutcomeKind.DRY_RUN, call=call, preview=preview(), decision=allow)


@pytest.mark.parametrize(
    ("path", "root", "family", "inside"),
    [
        ("/home/u/.ssh/id_rsa", "/home/u/.ssh", "posix", True),
        ("/home/u/.ssh", "/home/u/.ssh", "posix", True),
        ("/home/u/.sshx/key", "/home/u/.ssh", "posix", False),  # префикс строки — не вложенность
        ("/home/U/.ssh", "/home/u/.ssh", "posix", False),
        ("C:\\Users\\Me\\.SSH\\id", "c:/users/me/.ssh", "windows", True),
        ("C:\\Users\\Me2", "C:\\Users\\Me", "windows", False),
        ("/", "/home", "posix", False),
    ],
)
def test_is_within(path: str, root: str, family: str, inside: bool) -> None:
    assert is_within(path, root, family) is inside  # type: ignore[arg-type]


def test_name_of() -> None:
    assert name_of("/a/b/Key.KDBX", "posix") == "Key.KDBX"
    assert name_of("C:\\a\\b.pem", "windows") == "b.pem"
    assert name_of("C:/a/c.pem", "windows") == "c.pem"
