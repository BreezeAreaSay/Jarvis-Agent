"""jarvis bench на синтетическом корпусе: фейковые роутер и грамматика, фейковые руки и фейковый сервер."""

import importlib
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis import bench


def install(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    package, _, attr = name.rpartition(".")
    monkeypatch.setattr(importlib.import_module(package), attr, mod, raising=False)
    return mod


@dataclass
class Hit:
    action: str
    args: dict[str, Any]
    confidence: float = 1.0


@dataclass
class Route:
    level: str
    reason: str
    text: str
    deep: bool = False
    hit: Hit | None = None
    local_only: bool = False


@dataclass
class Decision:
    kind: str = "tool"
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    timings: dict[str, Any] = field(default_factory=dict)


CORPUS = [
    {"text": "громкость 30", "level": "grammar", "tool": "vol", "args": {"set": 30}},
    {
        "text": "сверни его",
        "level": "grammar",
        "tool": "win",
        "args": {"action": "minimize", "target": "@cur"},
        "ctx": {"window": {"title": "Telegram", "exe": "Telegram.exe"}},
    },
    {"text": "открой телегу", "level": "hands", "tool": "open", "args": {"target": "телегу"}},
    {"text": "закрой процесс стима", "level": "hands", "tool": "kill", "args": {"name": "стим"}},
    {"text": "покажи дискорд", "level": "hands", "tool": "focus", "args": {"target": "дискорд"}},
    {"text": "почему тормозит комп", "level": "brain"},
    {
        "text": "закрой его",
        "level": "hands",
        "tool": "close",
        "args": {"target": "@cur"},
        "ctx": {"last": "Telegram", "found": [r"C:\Users\me\отчёт.docx"]},
    },
]

# что «вернут» фейковые грамматика и роутер
GRAMMAR = {
    "громкость 30": Hit("vol", {"set": 30}),
    "сверни его": Hit("win", {"action": "minimize", "target": "@cur"}),
    "закрой процесс стима": Hit("close", {"target": "стима"}),  # ложное срабатывание
}
HEURISTIC = {"почему тормозит комп": "heur:question"}
# что «ответят» руки
HANDS = {
    "открой телегу": Decision(
        tool="open", args={"target": "телега", "kind": "app"}, timings={"total_ms": 300.0}
    ),
    "покажи дискорд": Decision(tool="open", args={"target": "дискорд"}, timings={"total_ms": 500.0}),
    "закрой его": Decision(tool="close", args={"target": "@cur"}, timings={"total_ms": 400.0}),
}


@pytest.fixture
def fake_levels(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Фейковые grammar.match и router.route; seen — контексты, которые видела грамматика."""
    seen: list[Any] = []

    def match(text: str, ctx: Any) -> Hit | None:
        seen.append(ctx)
        return GRAMMAR.get(text)

    def route(text: str, ctx: Any, mode: str) -> Route:
        assert mode == "normal"
        hit = match(text, ctx)
        if hit:
            return Route("grammar", f"grammar:{hit.action}", text, hit=hit)
        if text in HEURISTIC:
            return Route("brain", HEURISTIC[text], text)
        return Route("hands", "hands", text)

    install(monkeypatch, "jarvis.grammar", match=match)
    install(monkeypatch, "jarvis.router", route=route)
    return seen


class FakeHands:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def decide(self, text: str, ctx: Any) -> Decision:
        self.calls.append(text)
        return HANDS.get(text, Decision(kind="error", reason="обрезано по max_tokens"))


def by_text(rep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["text"]: e for e in rep["errors"]}


def test_router_only_without_live(fake_levels: list[Any]) -> None:
    hands = FakeHands()
    rep = bench.run(CORPUS, "all", live=False, hands=hands)
    assert hands.calls == []  # без --live руки не вызываются
    assert rep["count"] == 7
    errors = by_text(rep)
    assert set(errors) == {"закрой процесс стима"}
    err = errors["закрой процесс стима"]
    assert err["expected"] == 'hands:kill {"name":"стим"}' and err["got"].startswith("grammar:close")
    assert "ложное срабатывание грамматики" in err["hint"]
    assert rep["accuracy"]["level"] == {"ok": 6, "n": 7}
    assert rep["accuracy"]["tool"] == {"ok": 2, "n": 2}  # только грамматика: у рук инструмент не проверялся
    assert rep["accuracy"]["by_level"]["hands"] == {"n": 4, "ok": 3}
    assert set(rep["latency"]) == {"router", "grammar"}


def test_live_hands(fake_levels: list[Any]) -> None:
    hands = FakeHands()
    rep = bench.run(CORPUS, "all", live=True, hands=hands)
    assert hands.calls == ["открой телегу", "покажи дискорд", "закрой его"]
    errors = by_text(rep)
    assert "покажи дискорд" not in errors  # open↔focus для приложения — совпадение
    assert "открой телегу" not in errors  # «телега» ≈ «телегу», лишний kind не в счёт
    assert rep["latency"]["hands"]["n"] == 3 and rep["latency"]["hands"]["p95"] == 500.0
    cases = {c["text"]: c for c in rep["cases"]}
    assert cases["закрой его"]["ok"] and cases["закрой его"]["tool"] == "close"


def test_live_hands_errors_and_hints(fake_levels: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(HANDS, "открой телегу", Decision(tool="ask_gpt", timings={"total_ms": 200.0}))
    monkeypatch.setitem(HANDS, "покажи дискорд", Decision(tool="media", args={"action": "next"}))
    monkeypatch.delitem(HANDS, "закрой его")
    rep = bench.run(CORPUS, "hands", live=True, hands=FakeHands())
    errors = by_text(rep)
    assert "руки отдали в ask_gpt" in errors["открой телегу"]["hint"]
    assert errors["открой телегу"]["got"] == "brain:ask_gpt"
    assert "TOOLS" in errors["покажи дискорд"]["hint"]
    assert "руки вернули ошибку (обрезано по max_tokens)" in errors["закрой его"]["hint"]


def test_level_hands_selects_only_hands(fake_levels: list[Any]) -> None:
    items = bench.select(CORPUS, "hands", None)
    assert [it["level"] for it in items] == ["hands"] * 4
    assert len(bench.select(CORPUS, "all", 3)) == 3


def test_level_grammar_counts_false_positives(fake_levels: list[Any]) -> None:
    rep = bench.run(CORPUS, "grammar")
    assert set(by_text(rep)) == {"закрой процесс стима"}
    assert set(rep["latency"]) == {"grammar"} and rep["latency"]["grammar"]["n"] == 7


def test_context_from_corpus(fake_levels: list[Any]) -> None:
    bench.run(CORPUS, "router")
    win_ctx = fake_levels[1]
    assert win_ctx.active_window.title == "Telegram" and win_ctx.active_window.hwnd == 0
    last_ctx = fake_levels[-1]
    assert last_ctx.last_object == "Telegram" and last_ctx.cur_target() == "Telegram"
    assert last_ctx.found == [r"C:\Users\me\отчёт.docx"]


def test_same_tool_and_args() -> None:
    assert bench.same_tool("open", "focus", {"target": "дискорд"}, {"target": "дискорд"})
    assert bench.same_tool("focus", "open", {"target": "загрузки"}, {"target": "загрузки", "kind": "folder"})
    assert not bench.same_tool("open", "focus", {"target": "отчёт.docx", "kind": "file"}, {})
    assert not bench.same_tool("open", "focus", {"target": "https://example.com"}, {})
    assert not bench.same_tool("kill", "close", {}, {})
    assert bench.same_arg("delta", -10, -20) and not bench.same_arg("delta", -10, 10)
    assert bench.same_arg("set", 30, 30.0) and not bench.same_arg("set", 30, 31)
    assert bench.same_arg("mute", True, True) and not bench.same_arg("mute", True, 1)
    assert bench.same_arg("query", "Отчёт", "отчет")
    assert not bench.same_arg("target", "@cur", "телега")
    assert not bench.same_arg("kind", "file", "folder")
    assert bench.bad_args({"target": "телегу", "kind": "app"}, {"target": "телега"}, skip_kind=True) == []
    assert bench.bad_args({"target": "телегу", "kind": "app"}, {"target": "телега"}) == ["kind"]


def test_same_arg_via_app_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    from pc import apps

    tg = apps.App("Telegram", "TelegramDesktop.TelegramDesktop")
    monkeypatch.setattr(
        apps, "resolve", lambda name: (tg, 95.0) if "тел" in name or "Tele" in name else (None, 0.0)
    )
    assert bench.same_arg("target", "телегу", "Telegram")


def test_real_hands_with_fake_server(fake_levels: list[Any]) -> None:
    """Настоящий Hands.decide поверх httpx.MockTransport: тело запроса уходит, ответ разбирается."""
    from jarvis.config import HandsConfig
    from jarvis.hands import Hands

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        call = {"function": {"name": "kill", "arguments": json.dumps({"name": "стим"}, ensure_ascii=False)}}
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}],
                "timings": {"cache_n": 1183, "prompt_n": 12, "predicted_per_second": 74.0},
            },
        )

    hands = Hands(HandsConfig(), transport=httpx.MockTransport(handler))
    hands.status["server"] = "ready"  # сервер «уже запущен»: ensure_server не нужен
    item = {"text": "убей процесс стима", "level": "hands", "tool": "kill", "args": {"name": "стим"}}
    r = bench.run_case(item, "hands", live=True, hands=hands)
    assert r.ok and r.tool == "kill" and r.args == {"name": "стим"} and "hands" in r.ms
    assert seen and seen[0]["tool_choice"] == "required"
    hands.close()


def test_format_and_save(fake_levels: list[Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bench, "results_dir", lambda: tmp_path / "results")
    rep = bench.run(CORPUS, "all", live=True, hands=FakeHands())
    text = bench.format_report(rep)
    assert "фраза → ожидалось → получено" in text and "что править:" in text
    assert "«закрой процесс стима» → hands:kill" in text
    path = bench.save_report(rep)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["count"] == 7 and saved["live"] is True and saved["errors"]


def test_main_end_to_end(
    fake_levels: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    apps_fixture: list[dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = tmp_path / "phrases.ru.jsonl"
    lines = [json.dumps(it, ensure_ascii=False) for it in CORPUS[:2]] + ["", "{битая строка"]
    corpus.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(bench, "corpus_path", lambda: corpus)
    monkeypatch.setattr(bench, "results_dir", lambda: tmp_path / "results")
    inventories: list[Any] = []
    from pc import apps

    monkeypatch.setattr(apps, "set_inventory", lambda items: inventories.append(items))
    assert bench.main(level="all") == 0
    out = capsys.readouterr().out
    assert "bench: 2 фраз" in out and "ошибок нет" in out
    assert len(inventories[0]) == len(apps_fixture)  # инвентарь — из tests/fixtures/apps.json
    assert list((tmp_path / "results").glob("*.json"))


def test_main_no_corpus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bench, "corpus_path", lambda: tmp_path / "нет.jsonl")
    assert bench.main() == 2


def test_real_corpus_parses() -> None:
    """Корпус S4 читается целиком: уровни и инструменты — из словаря."""
    path = bench.corpus_path()
    if not path.is_file():
        pytest.skip("корпус bench/phrases.ru.jsonl ещё не создан")
    items = bench.load_phrases(path)
    assert len(items) == sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    tools = {"open", "close", "focus", "win", "find", "vol", "media", "kill", "open_found", "lock", "clock"}
    tools |= {"reply", "clarify", "ask_gpt"}
    for it in items:
        assert it["level"] in ("grammar", "hands", "brain"), it
        assert it.get("tool") is None or it["tool"] in tools, it
        bench.make_context(it.get("ctx"))
