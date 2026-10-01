"""Контракт инструмента: одни и те же требования к каждому встроенному инструменту.

Определение и схемы, строгая проверка аргументов, preview без побочных эффектов и детерминированный,
эффекты в пределах объявленных, результат по схеме и сериализуемый, verify проходит, отмена — сразу.
Трасса, dry run и таймаут runtime проверяются в tests/unit/core/test_tool_runtime.py и eval.
"""

import asyncio
import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from jarvis.adapters.tools import HOST, builtin_tools
from jarvis.domain.tools import TargetKind
from jarvis.ports.tools import Tool, ToolContext

pytestmark = pytest.mark.anyio

# Аргументы, с которыми каждый инструмент работает над фикстурой (пути — от рабочей папки).
EXAMPLES: dict[str, dict[str, JsonValue]] = {
    "system.cwd": {},
    "filesystem.list": {"path": "."},
    "filesystem.stat": {"path": "docs/a.txt"},
    "filesystem.search": {"root": ".", "pattern": "*.txt"},
    "filesystem.read_text": {"path": "docs/a.txt"},
    "process.list": {"max_results": 5},
}
TOOLS = {tool.definition.id: tool for tool in builtin_tools()}


def test_every_builtin_tool_has_an_example() -> None:
    assert set(TOOLS) == set(EXAMPLES)


@pytest.fixture(params=sorted(EXAMPLES))
def tool(request: pytest.FixtureRequest) -> Tool:
    return TOOLS[request.param]


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("текст", encoding="utf-8")
    return ToolContext(target=HOST, working_directory=str(tmp_path.resolve()))


def arguments(tool: Tool) -> BaseModel:
    return tool.definition.input_model.model_validate(EXAMPLES[tool.definition.id])


def snapshot(root: Path) -> list[tuple[str, int, float]]:
    return sorted((str(path), path.stat().st_size, path.stat().st_mtime) for path in root.rglob("*"))


def test_definition(tool: Tool) -> None:
    definition = tool.definition
    assert re.fullmatch(r"[a-z]+\.[a-z_]+", definition.id)
    assert definition.summary
    assert len(definition.description.splitlines()) >= 2  # что делает и чего не делает
    assert definition.targets == frozenset({TargetKind.HOST})
    assert 0 < definition.timeout_s <= 60
    assert definition.untrusted_output
    json.dumps(definition.input_schema)
    json.dumps(definition.output_schema)


def test_unknown_arguments_are_rejected(tool: Tool) -> None:
    with pytest.raises(ValidationError):
        tool.definition.input_model.model_validate({**EXAMPLES[tool.definition.id], "force": True})


async def test_preview_is_pure_deterministic_and_within_declared_effects(
    tool: Tool, context: ToolContext
) -> None:
    root = Path(str(context.working_directory))
    before = snapshot(root)
    first = await tool.preview(arguments(tool), context)
    second = await tool.preview(arguments(tool), context)
    assert snapshot(root) == before
    assert first.fingerprint() == second.fingerprint()
    assert {effect.kind for effect in first.effects} <= tool.definition.effects
    assert not first.has_side_effects
    assert first.target == context.target
    tool.definition.input_model.model_validate(first.normalized_arguments)  # нормализованное — тоже по схеме


async def test_execute_matches_the_output_schema_and_verifies(tool: Tool, context: ToolContext) -> None:
    preview = await tool.preview(arguments(tool), context)
    normalized = tool.definition.input_model.model_validate(preview.normalized_arguments)
    output = await tool.execute(normalized, context)
    dumped = output.model_dump(mode="json")
    json.dumps(dumped)
    tool.definition.output_model.model_validate(dumped)
    verification = await tool.verify(normalized, output, context)
    assert verification.passed, verification.checks
    assert verification.checks


async def test_cancellation_returns_promptly(tool: Tool, context: ToolContext) -> None:
    preview = await tool.preview(arguments(tool), context)
    normalized = tool.definition.input_model.model_validate(preview.normalized_arguments)
    running = asyncio.create_task(tool.execute(normalized, context))
    await asyncio.sleep(0)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=2)
