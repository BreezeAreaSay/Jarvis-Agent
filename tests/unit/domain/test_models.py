"""Модели: настройки эндпоинтов (только этот компьютер), назначение ролей, действия и память агента."""

import pytest
from pydantic import ValidationError

from jarvis.domain.agent import (
    DECISION_CHARS,
    AgentState,
    AgentStep,
    FinishAction,
    Observation,
    ProposedAction,
    ToolAction,
)
from jarvis.domain.models import ModelRole
from jarvis.domain.settings import JarvisConfig, ModelsSettings, is_loopback_url

ENDPOINT = {"base_url": "http://127.0.0.1:8080/v1", "capabilities": {"context_window": 16384}}


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8080/v1", "http://localhost:1234/v1", "http://[::1]:8080", "https://127.10.0.1/v1/"],
)
def test_local_endpoints_are_accepted(url: str) -> None:
    settings = ModelsSettings.model_validate({"endpoints": {"main": {**ENDPOINT, "base_url": url}}})
    assert settings.endpoints["main"].base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.5:8080/v1",
        "https://api.example.com/v1",
        "http://127.0.0.1.example.com/v1",
        "http://user@127.0.0.1/v1",
        "ftp://127.0.0.1/v1",
        "127.0.0.1:8080",
        "http://localhost.example:80/v1",
    ],
)
def test_remote_or_malformed_endpoints_are_rejected(url: str) -> None:
    assert not is_loopback_url(url)
    with pytest.raises(ValidationError, match="base_url"):
        ModelsSettings.model_validate({"endpoints": {"main": {**ENDPOINT, "base_url": url}}})


def test_roles_point_to_known_endpoints() -> None:
    settings = ModelsSettings.model_validate({"endpoints": {"main": ENDPOINT}, "roles": {"executor": "main"}})
    assert settings.roles == {ModelRole.EXECUTOR: "main"}
    with pytest.raises(ValidationError, match="неизвестный эндпоинт"):
        ModelsSettings.model_validate({"endpoints": {"main": ENDPOINT}, "roles": {"executor": "other"}})
    with pytest.raises(ValidationError, match="roles"):
        ModelsSettings.model_validate({"endpoints": {"main": ENDPOINT}, "roles": {"planner": "main"}})


def test_endpoint_needs_declared_context_window_and_known_keys() -> None:
    with pytest.raises(ValidationError, match="capabilities"):
        ModelsSettings.model_validate({"endpoints": {"main": {"base_url": "http://127.0.0.1:8080/v1"}}})
    with pytest.raises(ValidationError, match="temprature"):
        ModelsSettings.model_validate({"endpoints": {"main": {**ENDPOINT, "sampling": {"temprature": 0.1}}}})
    with pytest.raises(ValidationError, match="ID эндпоинта"):
        ModelsSettings.model_validate({"endpoints": {"Main Model": ENDPOINT}})


def test_empty_config_has_no_model() -> None:
    assert JarvisConfig().models.roles == {}


def test_decision_is_short_and_actions_are_closed() -> None:
    tool = ToolAction(type="tool", tool="filesystem.list", arguments={"path": "."})
    ProposedAction(decision="д" * DECISION_CHARS, action=tool)
    with pytest.raises(ValidationError):
        ProposedAction(decision="д" * (DECISION_CHARS + 1), action=tool)
    with pytest.raises(ValidationError):
        ProposedAction.model_validate({"decision": "x", "action": {"type": "shell", "command": "rm -rf /"}})
    with pytest.raises(ValidationError):
        ProposedAction.model_validate(
            {"decision": "x", "action": {"type": "finish", "answer": "a", "evidence": [], "extra": 1}}
        )


def test_agent_state_tracks_pending_calls_and_answers() -> None:
    call = ProposedAction(decision="прочитаю", action=ToolAction(type="tool", tool="t", arguments={}))
    state = AgentState().with_step(AgentStep(proposal=call, call_id="task_1.call_1"))
    assert state.pending is not None
    assert state.executed_calls() == []
    state = state.with_observation(Observation(status="executed", summary="исполнен", data="{}"))
    assert state.pending is None
    assert state.executed_calls() == ["task_1.call_1"]
    with pytest.raises(ValueError, match="нет вызова"):
        state.with_observation(Observation(status="failed", summary="x"))
    finish = ProposedAction(
        decision="готово", action=FinishAction(type="finish", answer="ответ", evidence=["task_1.call_1"])
    )
    state = state.with_step(AgentStep(problems=["плохой JSON"])).with_step(AgentStep(proposal=finish))
    assert state.answer is not None
    assert state.answer.answer == "ответ"
