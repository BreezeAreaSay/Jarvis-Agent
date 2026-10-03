"""CLI агента по настоящему HTTP: `jarvis run`, `jarvis model check`, `jarvis trace --model-io`.

Модель — заглушка OpenAI-совместимого сервера; всё остальное настоящее: конфиг, SQLite в JARVIS_HOME,
адаптер на httpx, Gateway, агент, Tool Runtime, политика, файлы во временной папке.
"""

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jarvis.cli.main import app
from tests.integration.llm_stub import LlmStub, call, config_toml, finish


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "jarvis-home"
    (home / "config").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "notes.txt").write_text("Встреча в 15:30", encoding="utf-8")
    (workspace / "server.pem").write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    monkeypatch.chdir(workspace)
    return home


def configure(home: Path, stub: LlmStub, **options: Any) -> None:
    (home / "config" / "config.toml").write_text(config_toml(stub.base_url, **options), encoding="utf-8")


def test_run_without_a_model_explains_the_setup(home: Path) -> None:
    result = CliRunner().invoke(app, ["run", "привет"])
    assert result.exit_code == 2
    assert "Модель не настроена" in result.output


def test_run_lets_the_model_use_tools_and_answer(home: Path) -> None:
    with LlmStub(
        [call("filesystem.read_text", path="notes.txt"), finish("Встреча в 15:30.", "task_1.call_1")]
    ) as stub:
        configure(home, stub)
        result = CliRunner().invoke(app, ["run", "во", "сколько", "встреча?"])
        assert result.exit_code == 0, result.output
    assert "task_1" in result.output
    assert "· шаг 1: filesystem.read_text — модель: «действую»" in result.output
    assert "filesystem.read_text: succeeded" in result.output
    assert "\nВстреча в 15:30.\n" in result.output
    first, second = stub.requests
    assert first["response_format"]["type"] == "json_schema"
    assert first["max_tokens"] == 1024
    assert "во сколько встреча?" in first["messages"][1]["content"]
    assert "<<<DATA id=task_1.call_1" in second["messages"][1]["content"]

    trace = CliRunner().invoke(app, ["trace", "task_1"])
    assert "STEP 1  tool filesystem.read_text" in trace.output
    assert "COMPLETED" in trace.output
    model_io = CliRunner().invoke(app, ["trace", "task_1", "--model-io"])
    assert "── task_1.mc_1  executor ok" in model_io.output
    assert "[system]" in model_io.output
    assert '"tool": "filesystem.read_text"' in model_io.output


@pytest.mark.parametrize(("answer", "status"), [("y\n", "executed"), ("n\n", "denied")])
def test_run_asks_a_human_before_reading_a_secret(home: Path, answer: str, status: str) -> None:
    replies = [call("filesystem.read_text", path="server.pem"), finish("готово")]
    with LlmStub(replies) as stub:
        configure(home, stub)
        result = CliRunner().invoke(app, ["run", "покажи server.pem"], input=answer)
        assert result.exit_code == 0, result.output
    assert "Нужно подтверждение (task_1.appr_1)" in result.output
    assert "server.pem" in result.output
    data = stub.requests[1]["messages"][1]["content"]
    if status == "executed":
        assert "BEGIN CERTIFICATE" in data
    else:
        assert "отказано человеком" in data
        assert "BEGIN CERTIFICATE" not in data


def probe(body: dict[str, Any]) -> str:
    """Ответ на пробы `jarvis model check`: enum-проба или схема решения исполнителя."""
    schema = body["response_format"]["json_schema"]["schema"]
    if "answer" in schema["properties"]:
        return json.dumps({"answer": "синий"}, ensure_ascii=False)
    return finish("готово")


def test_model_check_passes_against_a_matching_server(home: Path) -> None:
    with LlmStub([probe, probe]) as stub:
        configure(home, stub)
        result = CliRunner().invoke(app, ["model", "check"])
    assert result.exit_code == 0, result.output
    assert "✓ окно контекста сервера: 16384" in result.output
    assert "✓ проба structured output: «синий»" in result.output
    assert "✓ схема решения исполнителя принята сервером" in result.output
    decision_schema = stub.requests[1]["response_format"]["json_schema"]["schema"]
    tools = [
        branch["properties"]["tool"]["const"]
        for branch in decision_schema["properties"]["action"]["anyOf"][:-1]
    ]
    assert "filesystem.read_text" in tools


def test_model_check_reports_what_does_not_match(home: Path) -> None:
    with LlmStub(["не JSON", "не JSON"], n_ctx=8192) as stub:
        configure(home, stub, context_window=16384)
        result = CliRunner().invoke(app, ["model", "check"])
    assert result.exit_code == 1
    assert "объявлено окно 16384, а сервер даёт 8192" in result.output
    assert "объявлен structured_output, но ответ не по схеме" in result.output


def test_model_check_reports_an_unreachable_server(home: Path) -> None:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    (home / "config" / "config.toml").write_text(config_toml(f"http://127.0.0.1:{port}/v1"), encoding="utf-8")
    result = CliRunner().invoke(app, ["model", "check"])
    assert result.exit_code == 1
    assert "сервер модели недоступен" in result.output


def test_remote_model_servers_are_refused_by_the_config(home: Path) -> None:
    (home / "config" / "config.toml").write_text(config_toml("https://api.example.com/v1"), encoding="utf-8")
    result = CliRunner().invoke(app, ["config", "check"])
    assert result.exit_code == 1
    assert "сервер модели должен быть на этом компьютере" in result.output
