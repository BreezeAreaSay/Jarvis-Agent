"""bench --live: фраза корпуса {"level": "hands", "tool": "ask_gpt"}, на которую руки верно ответили ask_gpt,
не должна считаться ошибкой уровня (в корпусе таких 7 из 60 hands-фраз)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jarvis import bench


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data))
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "jarvis.toml"))
    from pc import apps

    apps.set_inventory([])
    yield
    apps.set_inventory(None)


@dataclass
class Decision:
    kind: str = "tool"
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    timings: dict[str, Any] = field(default_factory=lambda: {"total_ms": 300.0})


class AskGptHands:
    def decide(self, text: str, ctx: Any) -> Decision:
        return Decision(tool="ask_gpt")


# строка прямо из bench/phrases.ru.jsonl (строка 114)
ITEM = {"text": "какая погода будет завтра", "level": "hands", "tool": "ask_gpt"}


def test_real_corpus_has_hands_ask_gpt_rows() -> None:
    rows = [it for it in bench.load_phrases() if it["level"] == "hands" and it.get("tool") == "ask_gpt"]
    assert len(rows) == 7


def test_correct_ask_gpt_from_hands_is_not_an_error() -> None:
    rep = bench.run([ITEM], "hands", live=True, hands=AskGptHands())
    assert rep["cases"][0]["level"] in ("hands", "brain")
    # руки ответили ровно то, что ожидает корпус, — ошибок быть не должно
    assert rep["errors"] == [], rep["errors"]
    assert rep["accuracy"]["level"] == {"ok": 1, "n": 1}
