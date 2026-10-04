"""Router и прямые команды (ADR 0026, ADR 0030): проверки по графу импортов и по тексту ядра.

- Router только выбирает стратегию: модели он не вызывает, инструментов не исполняет и не видит;
- у прямых команд нет пути к модели: стадия DIRECT не импортирует ни Gateway, ни порт моделей;
- ядро маршрутизации не знает названий программ — их знает инвентарь (адаптер).
"""

import ast

import pytest

from jarvis.adapters.inventory.names import KNOWN_ALIASES
from tests.architecture.rules import SRC
from tests.architecture.test_tool_boundary import imports, offenders

MODELS = ("jarvis.core.models", "jarvis.ports.models", "jarvis.adapters.models")
RULES = {
    "jarvis.core.routing": (
        *MODELS,
        "jarvis.core.tools",
        "jarvis.ports.tools",
        "jarvis.core.agent",
        "jarvis.core.direct",
    ),
    "jarvis.core.direct": MODELS,
}


def test_the_graph_sees_the_routing_modules() -> None:
    assert "jarvis.core.routing.router" in imports()
    assert "jarvis.core.direct.stage" in imports()


@pytest.mark.parametrize("source", sorted(RULES))
def test_routing_and_direct_commands_have_no_way_to_a_model(source: str) -> None:
    assert offenders(source, *RULES[source]) == {}


def test_the_router_core_knows_no_program_names() -> None:
    names = {name.casefold() for key, aliases in KNOWN_ALIASES.items() for name in (key, *aliases)}
    literals: set[str] = set()
    for file in (SRC / "core" / "routing").glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value.casefold())
    assert names & literals == set()
