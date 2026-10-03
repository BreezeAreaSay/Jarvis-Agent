"""`jarvis bench`: эталон, модель из конфига (заглушка OpenAI-совместимого сервера), сравнение, ошибки.

Запуск настоящего llama-server — в tests/integration/test_live_bench.py (нужен сервер и модель).
"""

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.cli.main import app
from tests.integration.llm_stub import LlmStub, call, config_toml, finish

ROOT = Path(__file__).resolve().parents[2]
DATASET = str(ROOT / "benchmarks" / "agent" / "dataset.yaml")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "jarvis-home"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    return home


def only_folder(parent: Path) -> Path:
    (folder,) = parent.iterdir()
    return folder


def test_reference_run_writes_a_report(home: Path, tmp_path: Path) -> None:
    out = tmp_path / "results"
    tasks = ["-t", "a01_list_here", "-t", "h01_framework"]
    result = CliRunner().invoke(
        app, ["bench", "agent", "--reference", "--dataset", DATASET, *tasks, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "✓ a01_list_here" in result.output
    assert "Итого reference: успех 100.0 % (2 задач)" in result.output
    folder = only_folder(out)
    assert folder.name.endswith("-reference")
    data = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert [task["id"] for task in data["tasks"]] == ["a01_list_here", "h01_framework"]

    compared = CliRunner().invoke(app, ["bench", "compare", str(folder)])
    assert compared.exit_code == 0, compared.output
    assert "# Сравнение конфигураций" in compared.output
    assert "1. **reference** — проходит" in compared.output

    # папка сессии раскрывается в отчёты кандидатов (PowerShell не раскрывает `*`)
    comparison = tmp_path / "comparison.md"
    session = CliRunner().invoke(app, ["bench", "compare", str(out), "--out", str(comparison)])
    assert session.exit_code == 0, session.output
    assert "1. **reference** — проходит" in comparison.read_text(encoding="utf-8")
    empty = CliRunner().invoke(app, ["bench", "compare", str(tmp_path / "jarvis-home")])
    assert empty.exit_code == 2
    assert "отчёты не найдены" in empty.output


def test_configured_model_runs_through_http(home: Path, tmp_path: Path) -> None:
    """Модель из конфига пользователя: настоящий адаптер, HTTP, Gateway, агент и инструменты."""
    with LlmStub([call("system.cwd"), finish("Текущая директория — workspace.", "task_1.call_1")]) as stub:
        (home / "config" / "config.toml").write_text(config_toml(stub.base_url), encoding="utf-8")
        result = CliRunner().invoke(
            app,
            [
                "bench",
                "agent",
                "--dataset",
                DATASET,
                "-t",
                "a02_cwd",
                "--label",
                "stub",
                "--out",
                str(tmp_path / "results"),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "✓ a02_cwd" in result.output
    report = json.loads((only_folder(tmp_path / "results") / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["success_rate"] == 1.0
    assert report["tasks"][0]["model_calls"] == 2
    assert report["context_window"] == 16384
    assert "параметры, offload, память и скорость в отчёт не попали" in report["notes"][0]
    first, _ = stub.requests
    assert first["response_format"]["type"] == "json_schema"


def test_configured_run_needs_a_model(home: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["bench", "agent", "--dataset", DATASET, "--out", str(tmp_path)])
    assert result.exit_code == 2
    assert "роли executor не назначена модель" in result.output


def test_unknown_task(home: Path) -> None:
    result = CliRunner().invoke(
        app, ["bench", "agent", "--reference", "--dataset", DATASET, "-t", "z99_nope"]
    )
    assert result.exit_code == 2
    assert "нет задач: z99_nope" in result.output


def candidates_file(tmp_path: Path, server: str) -> Path:
    path = tmp_path / "candidates.toml"
    path.write_text(
        f"[defaults]\nserver = {json.dumps(server)}\nport = 18999\nstartup_timeout_s = 30\n"
        '[[candidates]]\nid = "broken"\nclass = "fast"\nhf = "org/repo-GGUF:Q4_K_M"\nquant = "Q4_K_M"\n',
        encoding="utf-8",
    )
    return path


def test_run_checks_the_server_binary_first(home: Path, tmp_path: Path) -> None:
    path = candidates_file(tmp_path, str(tmp_path / "no-such-llama-server"))
    result = CliRunner().invoke(app, ["bench", "run", str(path), "--dataset", DATASET])
    assert result.exit_code == 2
    assert "broken: llama-server не найден" in result.output


def test_run_rejects_a_broken_candidates_file(home: Path, tmp_path: Path) -> None:
    path = tmp_path / "candidates.toml"
    path.write_text('[[candidates]]\nid = "x"\nclass = "huge"\n', encoding="utf-8")
    result = CliRunner().invoke(app, ["bench", "run", str(path), "--dataset", DATASET])
    assert result.exit_code == 2


def test_server_that_fails_to_start_is_reported(home: Path, tmp_path: Path) -> None:
    """Сервер не поднялся (здесь — Python с аргументами llama-server): кандидат помечен, сессия завершена."""
    path = candidates_file(tmp_path, sys.executable)
    out = tmp_path / "results"
    result = CliRunner().invoke(
        app, ["bench", "run", str(path), "--dataset", DATASET, "--no-prefetch", "--out", str(out)]
    )
    assert result.exit_code == 1, result.output
    assert "broken: llama-server завершился с кодом" in result.output
    assert "Не запустились: broken" in result.output
    folder = only_folder(out) / "broken"
    assert (folder / "error.txt").exists()
    assert (folder / "server.log").exists()
