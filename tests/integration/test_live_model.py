"""Живая модель: только если задан JARVIS_TEST_LLM_URL (адрес llama-server, например
http://127.0.0.1:8080/v1). В CI пропускается.

Проверяется путь, а не ум модели: сервер принимает схему решения исполнителя, ответ проходит разбор,
задача доходит до конца или до ожидания человека, и каждый начатый вызов инструмента прошёл решение
политики раньше исполнения. Так тест годится и для настоящей модели на ПК, и для крошечной
модели со случайными весами.
"""

import os
from pathlib import Path

import pytest

from jarvis.adapters.memory import InMemoryStorage
from jarvis.adapters.models import OpenAICompatibleBackend
from jarvis.app.composition import build_app
from jarvis.domain.models import ModelRole
from jarvis.domain.settings import EndpointSettings, JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import Origin, TaskRequest
from jarvis.domain.trace import EventKind

URL = os.environ.get("JARVIS_TEST_LLM_URL")
CONTEXT = int(os.environ.get("JARVIS_TEST_LLM_CONTEXT", "8192"))

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not URL, reason="нет JARVIS_TEST_LLM_URL: живая модель не настроена"),
]


def backend() -> OpenAICompatibleBackend:
    settings = EndpointSettings.model_validate(
        {
            "base_url": URL,
            "request_timeout_s": 600,
            "capabilities": {"structured_output": True, "context_window": CONTEXT},
            "sampling": {"temperature": 0, "seed": 1},
        }
    )
    return OpenAICompatibleBackend("live", settings)


async def test_server_describes_itself() -> None:
    status = await backend().describe()
    assert status.models


async def test_agent_run_respects_the_runtime(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("Встреча в 15:30", encoding="utf-8")
    app = build_app(JarvisConfig(), models={ModelRole.EXECUTOR: backend()}, storage=InMemoryStorage())
    task_id = app.tasks.submit(
        TaskRequest(
            text="Какие файлы лежат в рабочей папке?", origin=Origin.EVAL, working_directory=str(tmp_path)
        )
    )
    snapshot = await app.tasks.run_until_blocked(task_id)
    assert snapshot.status in (
        TaskStatus.COMPLETED,
        TaskStatus.WAITING_CONFIRMATION,
        TaskStatus.BUDGET_EXCEEDED,
    ), snapshot.outcome
    events = app.tasks.trace(task_id)
    assert any(event.kind is EventKind.MODEL_CALLED for event in events)
    decided = {str(event.payload["call_id"]) for event in events if event.kind is EventKind.POLICY_DECIDED}
    started = [str(event.payload["call_id"]) for event in events if event.kind is EventKind.TOOL_STARTED]
    assert set(started) <= decided
