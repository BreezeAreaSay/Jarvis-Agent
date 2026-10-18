"""Фикстуры тестов ИИ: настроенный провайдер (OpenAI с ключом) и модель по сценарию вместо сети."""

import pytest

from jarvis.context import AppContext
from tests.ai.fakes import ScriptedLlm, no_sleep


@pytest.fixture
def llm(ctx: AppContext) -> ScriptedLlm:
    ctx.secrets.set("ai_api_key", "sk-test")
    script = ScriptedLlm()
    ctx.ai.set_transport(script.transport)  # type: ignore[attr-defined]
    ctx.ai.llm.sleep = no_sleep  # type: ignore[attr-defined]
    return script
