"""Грамматика (уровень 0, бюджет ≤0,3 с) под `jarvis run` не должна запускать PowerShell (Get-StartApps).

apps.enable_auto_refresh(True) включает jarvis run; grammar._app зовёт apps.resolve, а resolve при промахе
синхронно делает refresh() — PowerShell до 30 с прямо в grammar.match.
"""

import json
import time
from pathlib import Path

import pytest

from jarvis import grammar
from jarvis.context import Context
from pc import apps, subproc

FIXTURE = Path(__file__).parent / "fixtures" / "apps.json"


@pytest.fixture
def auto_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "jarvis.toml"))
    apps_fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calls: list[list[str]] = []

    def fake_run(argv, timeout, cwd=None, env=None):
        calls.append([str(a) for a in argv])
        time.sleep(0.5)  # Get-StartApps на живом ПК — 1–3 с
        return subproc.Completed(1, b"", b"")

    monkeypatch.setattr(subproc, "run", fake_run)
    monkeypatch.setattr(apps, "_last_auto_refresh", 0.0)
    apps.set_inventory(apps_fixture)
    apps.enable_auto_refresh(True)
    yield calls
    apps.enable_auto_refresh(False)
    apps.set_inventory(None)


def test_grammar_match_does_not_run_powershell(auto_refresh: list[list[str]]) -> None:
    t = time.perf_counter()
    hit = grammar.match("открой презентацию", Context())  # не приложение: уйдёт рукам
    ms = (time.perf_counter() - t) * 1000
    assert hit is None
    assert auto_refresh == [], f"grammar.match запустил {auto_refresh[0][0]}"
    assert ms < 300, f"grammar.match {ms:.0f} мс"
