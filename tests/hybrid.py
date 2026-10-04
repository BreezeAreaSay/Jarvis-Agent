"""Гибридное приложение для тестов: локальная scripted-модель и «облачные» scripted-провайдеры
(вид REMOTE_MODEL_API) без сети — что и кому Model Gateway отправляет, а что нет."""

from typing import Any

from jarvis.adapters.clock import ManualClock
from jarvis.adapters.memory import InMemoryStorage
from jarvis.app.composition import App, build_app
from jarvis.domain.models import ModelCapabilities, ModelRole
from jarvis.domain.providers import ProviderKind
from jarvis.domain.settings import JarvisConfig
from jarvis.domain.task import Origin, TaskRequest, TaskSnapshot
from jarvis.domain.trace import EventKind, TraceEvent
from jarvis.evals.models import ModelReply, ScriptedModel
from jarvis.ports.tools import Tool
from tests.fakes import FakeTool
from tests.helpers import FAKE_HOST

CAPS = ModelCapabilities(structured_output=True, context_window=16384)
CLOUD_CAPS = ModelCapabilities(structured_output=True, context_window=65536)
READER = "fake.read"


def finish(answer: str = "готово", *evidence: str) -> ModelReply:
    action = {"type": "finish", "answer": answer, "evidence": list(evidence)}
    return ModelReply.model_validate({"json": {"decision": "отвечаю", "action": action}})


def read(path: str) -> ModelReply:
    action = {"type": "tool", "tool": READER, "arguments": {"path": path}}
    return ModelReply.model_validate({"json": {"decision": "читаю", "action": action}})


def remote_settings(name: str) -> dict[str, Any]:
    return {
        "base_url": f"https://{name}.example/v1",
        "model": f"{name}-model",
        "api_key": "env:JARVIS_TEST_KEY",
        "capabilities": CLOUD_CAPS.model_dump(),
    }


class Hybrid:
    def __init__(
        self,
        local: list[ModelReply],
        remotes: dict[str, list[ModelReply]],
        *,
        cloud: dict[str, Any] | None = None,
        routing: dict[str, Any] | None = None,
        tools: list[Tool] | None = None,
    ) -> None:
        names = list(remotes)
        self.config = JarvisConfig.model_validate(
            {
                "cloud": {"enabled": True, **(cloud or {})},
                "models": {
                    "remote": {name: remote_settings(name) for name in names},
                    "routing": {"smart": names, "coding": names, **(routing or {})},
                },
            }
        )
        self.storage = InMemoryStorage()
        self.clock = ManualClock()
        self.local = ScriptedModel(local, capabilities=CAPS, endpoint="local")
        self.remotes = {
            name: ScriptedModel(
                replies, capabilities=CLOUD_CAPS, endpoint=name, kind=ProviderKind.REMOTE_MODEL_API
            )
            for name, replies in remotes.items()
        }
        self.reader = FakeTool(READER, output={"value": "def main(): pass"})
        self.app: App = build_app(
            self.config,
            models={ModelRole.EXECUTOR: self.local},
            remote_models=self.remotes,
            storage=self.storage,
            clock=self.clock,
            tools=tools if tools is not None else [self.reader],
            **FAKE_HOST,
        )

    async def run(self, text: str = "что-нибудь", **request: Any) -> TaskSnapshot:
        self.task_id = self.app.tasks.submit(TaskRequest(text=text, origin=Origin.EVAL, **request))
        return await self.app.tasks.run_until_blocked(self.task_id)

    async def run_resume(self) -> TaskSnapshot:
        return await self.app.tasks.run_until_blocked(self.task_id)

    def events(self, kind: EventKind) -> list[TraceEvent]:
        return [event for event in self.app.tasks.trace(self.task_id) if event.kind is kind]

    def called(self) -> list[str]:
        return [str(event.payload["endpoint"]) for event in self.events(EventKind.MODEL_CALLED)]
