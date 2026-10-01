from pathlib import Path

import pytest

from jarvis.domain.settings import JarvisConfig
from jarvis.domain.states import TaskStatus
from jarvis.evals.engine import run_scenario, run_scenarios
from jarvis.evals.scenario import Scenario, ScenarioError, load_scenarios

pytestmark = pytest.mark.anyio

SCENARIOS = Path(__file__).resolve().parents[3] / "evals" / "scenarios"


def scenario(**fields: object) -> Scenario:
    data: dict[str, object] = {
        "id": "test.case",
        "category": "test",
        "input": "текст",
        "script": [{"status": "ROUTING", "next": "COMPLETED", "route": "clarify"}],
        "expect": {"status": "COMPLETED"},
    }
    data.update(fields)
    return Scenario.model_validate(data)


async def test_repository_scenarios_pass() -> None:
    scenarios = load_scenarios([SCENARIOS])
    assert {item.id for item in scenarios} >= {
        "runtime.agent_path",
        "runtime.replan",
        "runtime.budget_steps",
        "runtime.cancel",
        "runtime.fatal_error",
    }
    report = await run_scenarios(scenarios, JarvisConfig())
    assert report.passed, [result.problems for result in report.results if not result.passed]


async def test_unmet_expectations_are_reported() -> None:
    result = await run_scenario(
        scenario(
            expect={
                "status": "FAILED",
                "transitions": ["ROUTING"],
                "usage": {"steps": 1},
                "error_category": "internal",
                "budget_limit": "steps",
            }
        ),
        JarvisConfig(),
    )
    assert not result.passed
    assert result.status is TaskStatus.COMPLETED
    assert len(result.problems) == 5


async def test_script_mismatch_and_leftover_steps_fail_the_scenario() -> None:
    result = await run_scenario(
        scenario(
            script=[
                {"status": "PLANNING", "next": "EXECUTING"},
                {"status": "EXECUTING", "next": "VERIFYING"},
            ],
            expect={"status": "FAILED", "error_category": "script_mismatch"},
        ),
        JarvisConfig(),
    )
    assert result.status is TaskStatus.FAILED
    assert result.problems == ["не проиграно шагов сценария: 1"]


async def test_hanging_scenario_times_out() -> None:
    result = await run_scenario(
        scenario(
            script=[{"status": "ROUTING", "next": "COMPLETED", "hang": True}],
            expect={"status": "COMPLETED"},
        ),
        JarvisConfig(),
        timeout_s=0.05,
    )
    assert not result.passed
    assert any("не завершился" in problem for problem in result.problems)


def test_scenario_budget_overrides_route_budgets() -> None:
    item = scenario(budget={"max_steps": 2})
    assert item.budget == {"max_steps": 2}


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("id: x.y\ncategory: c\ninput: t\nscript: []\nexpect: {status: COMPLETED}\nextra: 1\n", "extra"),
        (
            "id: x.y\ncategory: c\ninput: t\nscript: []\nexpect: {status: COMPLETED, usage: {stepz: 1}}\n",
            "stepz",
        ),
        ("id: bad\ncategory: c\ninput: t\nscript: []\nexpect: {status: COMPLETED}\n", "id"),
        ("id: [", "x.yaml"),
    ],
)
def test_invalid_scenarios_are_rejected(tmp_path: Path, content: str, fragment: str) -> None:
    path = tmp_path / "x.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ScenarioError) as raised:
        load_scenarios([path])
    assert fragment in raised.value.message


def test_duplicate_ids_and_missing_paths_are_rejected(tmp_path: Path) -> None:
    text = "id: x.y\ncategory: c\ninput: t\nscript: []\nexpect: {status: COMPLETED}\n"
    (tmp_path / "a.yaml").write_text(text, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ScenarioError, match="уже определён"):
        load_scenarios([tmp_path])
    with pytest.raises(ScenarioError, match="нет такого"):
        load_scenarios([tmp_path / "missing"])
