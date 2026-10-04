"""Граница модели (ADR 0008, ADR 0023): проверки по исходникам и графу импортов.

- в ядре и портах нет модельной специфики: ни имён моделей и рантаймов, ни параметров сэмплирования,
  ни формата API — ядро решает по роли и возможностям;
- бэкенды моделей создаёт только сборка приложения;
- Model Gateway не видит инструментов, а бэкенд модели — ни инструментов, ни хранилищ: модель
  возвращает только данные, исполнить их может лишь Tool Runtime;
- агент вызывает инструменты только через Tool Runtime.
"""

import ast
import re

import pytest

from tests.architecture.rules import SRC, python_files
from tests.architecture.test_tool_boundary import importers, offenders

# Имена семейств моделей и рантаймов, параметры сэмплирования, детали OpenAI-совместимого API.
MODEL_SPECIFIC = re.compile(
    r"qwen|gemma|mistral|llama|deepseek|phi-?[0-9]|gguf|vulkan|ollama|lm ?studio|openai|"
    r"temperature|top_p|top_k|min_p|repeat_penalty|chat_template|response_format|chat/completions|/v1\b",
    re.IGNORECASE,
)


def model_specific(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.Name):
            text = node.id
        elif isinstance(node, ast.Attribute):
            text = node.attr
        elif isinstance(node, ast.arg):
            text = node.arg
        else:
            continue
        found += [f"{getattr(node, 'lineno', 0)}: {match.group()}" for match in MODEL_SPECIFIC.finditer(text)]
    return found


@pytest.mark.parametrize("layer", ["core", "ports"])
def test_core_knows_no_model_specifics(layer: str) -> None:
    problems = {
        str(file.relative_to(SRC)): found
        for file in python_files(layer)
        if (found := model_specific(file.read_text(encoding="utf-8")))
    }
    assert problems == {}


@pytest.mark.parametrize(
    "source",
    [
        'if info.model.startswith("qwen"): pass',
        "request = {'temperature': 0.2}",
        "def f(top_p: float) -> None: ...",
        "url = base + '/v1/chat/completions'",
        "body['response_format'] = schema",
    ],
)
def test_the_check_catches_model_specifics(source: str) -> None:
    assert model_specific(source)


def test_only_the_composition_root_creates_model_backends() -> None:
    assert importers("jarvis.adapters.models") == {"jarvis.app.composition"}


RULES = {
    "jarvis.core.models": ("jarvis.core.tools", "jarvis.ports.tools", "jarvis.adapters"),
    "jarvis.adapters.models": ("jarvis.ports.tools", "jarvis.ports.storage", "jarvis.core"),
    "jarvis.core.agent": ("jarvis.ports.tools", "jarvis.core.tools.registry", "jarvis.adapters"),
}


@pytest.mark.parametrize("source", sorted(RULES))
def test_model_side_cannot_reach_the_computer(source: str) -> None:
    assert offenders(source, *RULES[source]) == {}


def test_only_the_gateway_calls_a_model_backend() -> None:
    """Провайдера вызывает только Model Gateway — значит, каждый вызов проходит план, границу
    приватности и запись в журнал (ADR 0027, ADR 0028)."""
    callers = set()
    for file in python_files():
        if "jarvis/core" not in file.as_posix() and "jarvis/app" not in file.as_posix():
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "complete"
            ):
                callers.add(file.relative_to(SRC).as_posix())
    assert callers == {"core/models/gateway.py"}
