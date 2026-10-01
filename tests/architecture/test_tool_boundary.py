"""Tool Runtime — единственная граница исполнения (ADR 0004, ADR 0022): проверки по графу импортов.

- ядро не знает конкретных инструментов, а Tool Runtime — CLI и сборки приложения;
- инструменты не знают ядра (TaskRunner, трассу) и хранилищ: записать трассу или аудит в обход
  runtime им нечем;
- политика не знает хранилищ;
- объекты инструментов создаёт только сборка приложения и держит только реестр, который видит
  только runtime: стадии, eval и CLI получают Tool Runtime, а не инструменты.
"""

import ast
import dataclasses
from functools import cache

import pytest

from jarvis.ports.tools import ToolContext
from tests.architecture.rules import SRC, python_files


@cache
def imports() -> dict[str, set[str]]:
    """Модуль jarvis → все модули, которые он импортирует (абсолютные имена)."""
    graph: dict[str, set[str]] = {}
    for file in python_files():
        relative = file.relative_to(SRC.parent).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        found: set[str] = set()
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                base = node.module
                if node.level:
                    package = parts if file.name == "__init__.py" else parts[:-1]
                    base = ".".join([*package[: len(package) - node.level + 1], node.module])
                found.add(base)
                found.update(f"{base}.{alias.name}" for alias in node.names)
        graph[module] = found
    return graph


def importers(target: str) -> set[str]:
    return {
        module
        for module, found in imports().items()
        if any(name == target or name.startswith(f"{target}.") for name in found)
        and not (module == target or module.startswith(f"{target}."))
    }


def offenders(source: str, *forbidden: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for module, found in imports().items():
        if module != source and not module.startswith(f"{source}."):
            continue
        bad = sorted(name for name in found if any(name == f or name.startswith(f"{f}.") for f in forbidden))
        if bad:
            result[module] = bad
    return result


def test_the_graph_sees_the_tool_modules() -> None:
    assert "jarvis.core.tools.runtime" in imports()
    assert "jarvis.adapters.tools.filesystem" in imports()


TOOL_STORAGE = (
    "jarvis.core",
    "jarvis.ports.storage",
    "jarvis.adapters.sqlite",
    "jarvis.adapters.memory",
    "sqlite3",
)
RULES = {
    "jarvis.core": ("jarvis.adapters",),  # ядро не знает конкретных инструментов
    "jarvis.core.tools": ("jarvis.cli", "jarvis.app", "jarvis.evals"),  # runtime не знает CLI
    "jarvis.adapters.tools": TOOL_STORAGE,  # инструменты не знают TaskRunner, трассу и хранилища
    "jarvis.core.policy": ("sqlite3", "jarvis.adapters", "jarvis.ports.storage"),  # политика — без хранилищ
}


@pytest.mark.parametrize("source", sorted(RULES))
def test_forbidden_dependencies(source: str) -> None:
    assert offenders(source, *RULES[source]) == {}


def test_only_the_composition_root_creates_tools() -> None:
    assert importers("jarvis.adapters.tools") == {"jarvis.app.composition"}


def test_only_the_runtime_holds_the_registry() -> None:
    assert importers("jarvis.core.tools.registry") == {"jarvis.app.composition", "jarvis.core.tools.runtime"}


def test_a_tool_gets_no_handle_to_storage_or_trace() -> None:
    assert {field.name for field in dataclasses.fields(ToolContext)} == {
        "target",
        "working_directory",
        "protected_roots",
    }
