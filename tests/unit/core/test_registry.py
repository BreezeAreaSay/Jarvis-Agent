"""Реестр инструментов: только регистрация, поиск и список."""

import pytest

from jarvis.core.tools.registry import ToolRegistry
from jarvis.domain.errors import ToolNotFound
from jarvis.domain.tools import ToolId
from tests.fakes import FakeTool


def test_lookup_and_sorted_definitions() -> None:
    registry = ToolRegistry([FakeTool("b.tool"), FakeTool("a.tool")])
    assert registry.get(ToolId("b.tool")).definition.id == "b.tool"
    assert [definition.id for definition in registry.definitions()] == ["a.tool", "b.tool"]


def test_unknown_tool_is_not_found() -> None:
    with pytest.raises(ToolNotFound):
        ToolRegistry([]).get(ToolId("filesystem.delete"))


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry([FakeTool("a.tool")])
    with pytest.raises(ValueError, match=r"a\.tool"):
        registry.register(FakeTool("a.tool"))
