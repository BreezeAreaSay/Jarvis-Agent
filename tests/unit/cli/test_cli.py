import json
from importlib.metadata import version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jarvis.cli.main import app
from jarvis.evals.scenario import load_scenarios

SCENARIOS = Path(__file__).resolve().parents[3] / "evals" / "scenarios"


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    return CliRunner()


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"jarvis {version('jarvis-agent')}"


def test_config_check_without_file(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0
    assert "действуют умолчания" in result.output


def test_config_check_reports_errors(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text("[budgets.agent]\nmax_stepz = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 1
    assert "budgets.agent.max_stepz" in result.output


def test_config_show_with_sources(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "config" / "config.toml"
    path.parent.mkdir()
    path.write_text("[budgets.agent]\nmax_steps = 12\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "show", "--sources"])
    assert result.exit_code == 0
    assert f"budgets.agent.max_steps = 12  [user:{path}]" in result.output
    assert "budgets.agent.max_replans = 3  [default]" in result.output
    assert f"# JARVIS_HOME: {tmp_path}" in result.output


def test_eval_runs_scenarios_and_writes_reports(runner: CliRunner, tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    result = runner.invoke(app, ["eval", str(SCENARIOS), "--report-dir", str(reports)])
    assert result.exit_code == 0, result.output
    count = len(load_scenarios([SCENARIOS]))
    assert f"Итого: {count} из {count}" in result.output
    (json_report,) = reports.glob("*-scripted.json")
    (markdown_report,) = reports.glob("*-scripted.md")
    data = json.loads(json_report.read_text(encoding="utf-8"))
    assert {item["id"] for item in data["results"]} >= {"runtime.agent_path", "runtime.cancel"}
    markdown = markdown_report.read_text(encoding="utf-8")
    assert f"Прошли {count} из {count} сценариев." in markdown
    assert "| `runtime.budget_steps` | ✓ | BUDGET_EXCEEDED | 2 |" in markdown


def test_eval_selects_scenarios_by_id(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["eval", str(SCENARIOS), "-s", "runtime.cancel", "--report-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "Итого: 1 из 1" in result.output
    unknown = runner.invoke(app, ["eval", str(SCENARIOS), "-s", "runtime.nope"])
    assert unknown.exit_code == 2


def test_eval_fails_on_a_failing_scenario(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "id: test.bad\ncategory: t\ninput: t\n"
        "script: [{status: ROUTING, next: COMPLETED, route: clarify}]\n"
        "expect: {status: FAILED}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["eval", str(path), "--report-dir", str(tmp_path / "reports")])
    assert result.exit_code == 1
    assert "✗ test.bad" in result.output


def test_eval_reports_missing_paths(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["eval", str(tmp_path / "missing")])
    assert result.exit_code == 2
