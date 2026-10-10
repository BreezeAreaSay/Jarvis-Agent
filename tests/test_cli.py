"""CLI: UTF-8 в pipe, route, ask (--dry, уровни, контекст между вызовами), apps, скрытая mcp, подкоманды."""

import importlib
import io
import json
import os
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jarvis import cli, context
from jarvis.core import NEEDS_GPT
from jarvis.events import Done, Status, TextChunk

ROOT = Path(__file__).resolve().parents[1]


def install(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    """Подменить модуль соседа фейком (и в sys.modules, и атрибутом пакета)."""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    package, _, attr = name.rpartition(".")
    monkeypatch.setattr(importlib.import_module(package), attr, mod, raising=False)
    return mod


@dataclass
class Route:
    level: str
    reason: str
    text: str
    deep: bool = False
    hit: Any = None
    local_only: bool = False


@dataclass
class Outcome:
    kind: str = "done"
    ok: bool = True
    text: str = "Сделал потише"
    items: list[str] = field(default_factory=list)
    autohide: bool = True
    action: str = ""


@dataclass
class Decision:
    kind: str = "tool"
    tool: str = "vol"
    args: dict[str, Any] = field(default_factory=lambda: {"delta": -10})
    reason: str = ""
    timings: dict[str, Any] = field(
        default_factory=lambda: {"prompt_n": 14, "cache_n": 1183, "total_ms": 352.0}
    )


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Фейки router, execute, hands и brain; Brain считает создания (шпион Codex)."""
    st = types.SimpleNamespace(
        route=None, decision=Decision(), outcome=Outcome(), exec_calls=[], hands_ctx=[], brains=0, asks=[]
    )

    def route(text: str, ctx: Any, mode: str) -> Route:
        if st.route is not None:
            return st.route
        local = mode == "local" or text.startswith("локально:")
        return Route("hands", "hands", text.removeprefix("локально:").strip(), local_only=local)

    def run(tool: str, args: dict, text: str, ctx: Any, source: str, dry: bool = False) -> Outcome:
        st.exec_calls.append((tool, args, source, dry))
        if tool == "open":
            ctx.remember("Telegram", "app")
        return st.outcome

    class Hands:
        def __init__(self, cfg: Any, job_hook: Any = None) -> None:
            self.status = {"server": "ready"}

        def decide(self, text: str, ctx: Any) -> Decision:
            st.hands_ctx.append(ctx)
            return st.decision

    class Brain:
        def __init__(self, cfg: Any, confirm_address: str, process_hook: Any = None) -> None:
            st.brains += 1
            self.ready = True

        def start(self) -> None:
            pass

        def ask(self, text: str, ctx: Any, deep: bool = False):
            st.asks.append(text)
            yield Status("⚙ list_windows", kind="tool")
            yield TextChunk("Потому что ")
            yield TextChunk("ёлка")
            yield Done(ok=True, text="", timings={"t_first_token": 1.5, "new_thread": True})

        def cancel(self) -> None:
            pass

        def close(self) -> None:
            pass

    class ConfirmServer:
        def __init__(self, callback: Any) -> None:
            self.address = "fake-pipe"

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    install(monkeypatch, "jarvis.router", route=route)
    install(monkeypatch, "jarvis.execute", run=run)
    install(monkeypatch, "jarvis.hands", Hands=Hands)
    install(monkeypatch, "jarvis.brain", Brain=Brain)
    from pc import confirm_client

    monkeypatch.setattr(confirm_client, "ConfirmServer", ConfirmServer)
    return st


# --- UTF-8 в pipe ---------------------------------------------------------------------------------


def test_utf8_pipe_subprocess(tmp_path: Path) -> None:
    """`python -m jarvis route …` в pipe: «ё», «⚙», «✓» не роняют вывод и читаются как UTF-8.

    PYTHONIOENCODING=cp1251 — как pipe на русской Windows: без reconfigure «⚙» дал бы UnicodeEncodeError.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONIOENCODING"] = "cp1251"
    phrase = "проверка ё ⚙ ✓"
    r = subprocess.run(
        [sys.executable, "-m", "jarvis", "route", phrase],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    out, err = r.stdout.decode("utf-8"), r.stderr.decode("utf-8")
    assert "UnicodeEncodeError" not in err, err
    assert r.returncode == 0, err
    assert f"запрос: {phrase}" in out
    assert "уровень:" in out


def test_utf8_reconfigure_in_process(monkeypatch: pytest.MonkeyPatch, fakes: types.SimpleNamespace) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1251", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    fakes.route = Route("grammar", "grammar:open", "ёлка ⚙ ✓")
    assert cli.main(["route", "ёлка ⚙ ✓"]) == 0
    stream.flush()
    assert "ёлка ⚙ ✓" in raw.getvalue().decode("utf-8")


def test_no_streams_no_print(monkeypatch: pytest.MonkeyPatch) -> None:
    """Под pythonw stdout/stderr = None: ничего не печатаем и не падаем."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cli._utf8_streams()
    cli._out("⚙ тест")
    cli._err("✗ тест")


def test_clean_strips_control_chars() -> None:
    assert cli._clean("окно\x1b[2Jзаголовок\x07\x9b ok\nдальше\t!") == "окно[2Jзаголовок ok\nдальше\t!"


# --- route и ask ----------------------------------------------------------------------------------


def test_route_prints_level_and_reason(
    fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.route = Route("brain", "heur:question", "почему тест падает")
    assert cli.main(["route", "почему", "тест", "падает"]) == 0
    out = capsys.readouterr().out
    assert "уровень: brain (GPT)" in out and "причина: heur:question" in out
    assert fakes.brains == 0 and fakes.exec_calls == []


def test_ask_dry_hands(fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ask", "--dry", "сделай", "потише"]) == 0
    out = capsys.readouterr().out
    assert "(dry" in out and "[руки · 4b · hands]" in out
    assert "✓ Сделал потише" in out and "prompt_n 14" in out and "cache_n 1183" in out
    assert fakes.exec_calls == [("vol", {"delta": -10}, "hands", True)]
    assert fakes.hands_ctx[0].active_window is None  # без --window окна нет
    assert fakes.brains == 0
    assert (Path(os.environ["JARVIS_DATA_DIR"]) / "cli_context.json").is_file()


def test_ask_window_option(
    monkeypatch: pytest.MonkeyPatch, fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from pc import windows

    monkeypatch.setattr(windows, "find_window", lambda target: None)
    fakes.decision = Decision(tool="close", args={"target": "@cur"})
    assert cli.main(["ask", "--dry", "--window", "Telegram", "закрой его"]) == 0
    win = fakes.hands_ctx[0].active_window
    assert win is not None and win.title == "Telegram" and win.hwnd == 0


def test_ask_context_between_calls(fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.decision = Decision(tool="open", args={"target": "телегу"})
    assert cli.main(["ask", "открой телегу"]) == 0
    saved = json.loads((Path(os.environ["JARVIS_DATA_DIR"]) / "cli_context.json").read_text(encoding="utf-8"))
    assert saved["last_object"] == "Telegram"
    fakes.decision = Decision(tool="close", args={"target": "@cur"})
    assert cli.main(["ask", "закрой его"]) == 0
    ctx = fakes.hands_ctx[-1]
    assert ctx.last_object == "Telegram" and ctx.cur_target() == "Telegram"
    assert context.load_cli().last_object == "Telegram"


def test_ask_brain_stream_and_cold_start(
    fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.decision = Decision(tool="ask_gpt", args={})
    assert cli.main(["ask", "почему", "так"]) == 0
    out = capsys.readouterr().out
    assert "холодный старт" in out and "⚙ list_windows" in out
    assert "Потому что ёлка\n" in out
    assert "GPT до первых слов 1,5 с (новый тред)" in out
    assert fakes.brains == 1 and fakes.asks == ["почему так"]


def test_ask_local_mode_never_creates_codex(
    fakes: types.SimpleNamespace, config_file: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file('mode = "local"\n')
    fakes.decision = Decision(tool="ask_gpt", args={})
    assert cli.main(["ask", "почему", "небо", "голубое"]) == 1
    assert NEEDS_GPT in capsys.readouterr().out
    assert fakes.brains == 0 and fakes.asks == []
    assert cli.main(["ask", "--level", "brain", "привет"]) == 1
    assert fakes.brains == 0


def test_ask_local_prefix_never_creates_codex(
    fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.decision = Decision(tool="ask_gpt", args={})
    assert cli.main(["ask", "локально: почему небо голубое"]) == 1
    assert NEEDS_GPT in capsys.readouterr().out
    assert fakes.brains == 0 and fakes.asks == []


def test_ask_level_hands(fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ask", "--level", "hands", "--dry", "сделай потише"]) == 0
    out = capsys.readouterr().out
    assert 'решение: vol {"delta": -10}' in out and "руки 352 мс" in out
    assert fakes.exec_calls == [("vol", {"delta": -10}, "hands", True)]


def test_ask_level_hands_reply_and_error(
    fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.decision = Decision(tool="ask_gpt", args={})
    assert cli.main(["ask", "--level", "hands", "почему комп тормозит"]) == 0
    assert "[ask_gpt]" in capsys.readouterr().out and fakes.exec_calls == []
    fakes.decision = Decision(kind="error", tool="", reason="обрезано по max_tokens")
    assert cli.main(["ask", "--level", "hands", "привет как дела"]) == 1
    assert "ошибка рук: обрезано по max_tokens" in capsys.readouterr().out


def test_ask_level_grammar(
    monkeypatch: pytest.MonkeyPatch, fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    hit = types.SimpleNamespace(action="vol", args={"set": 30}, confidence=1.0)
    install(monkeypatch, "jarvis.grammar", match=lambda text, ctx: hit if "громкость" in text else None)
    assert cli.main(["ask", "--level", "grammar", "--dry", "громкость 30"]) == 0
    assert fakes.exec_calls == [("vol", {"set": 30}, "grammar", True)]
    assert cli.main(["ask", "--level", "grammar", "открой то что я вчера качал"]) == 1
    assert "грамматика не узнала" in capsys.readouterr().out


def test_ask_level_brain(fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ask", "--level", "brain", "привет"]) == 0
    out = capsys.readouterr().out
    assert "[GPT · gpt-6-luna" in out and "Потому что ёлка" in out
    assert fakes.brains == 1


def test_ask_items_numbered(fakes: types.SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.decision = Decision(tool="find", args={"query": "отчёт"})
    fakes.outcome = Outcome(text="Нашёл 2", items=[r"C:\Users\me\отчёт.docx", r"C:\Users\me\отчёт (2).docx"])
    assert cli.main(["ask", "найди отчёт"]) == 0
    out = capsys.readouterr().out
    assert "  1. C:\\Users\\me\\отчёт.docx" in out and "  2. C:\\Users\\me\\отчёт (2).docx" in out


def test_console_confirm_not_tty_is_no(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert cli.console_confirm("Завершить chrome.exe (12 процессов)?", "", "brain") is False
    out = capsys.readouterr().out
    assert "просит GPT" in out and "не консоль" in out


def test_console_confirm_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty("д\n"))
    assert cli.console_confirm("Переместить в корзину?", r"C:\Users\me\отчёт.docx", "user") is True
    monkeypatch.setattr(sys, "stdin", Tty("\n"))
    assert cli.console_confirm("Переместить в корзину?", "", "user") is False


# --- apps -----------------------------------------------------------------------------------------


def test_apps_with_fixture(apps_fixture: list[dict[str, str]], capsys: pytest.CaptureFixture[str]) -> None:
    from pc import apps

    apps.set_inventory([apps.App(d["name"], d["app_id"]) for d in apps_fixture])
    try:
        assert cli.main(["apps"]) == 0
        out = capsys.readouterr().out
        assert "Telegram" in out and f"всего: {len(apps_fixture)}" in out
        assert cli.main(["apps", "Telegram"]) == 0
        out = capsys.readouterr().out
        assert "resolve «Telegram» → Telegram" in out and "мс" in out
        assert out.splitlines()[0].startswith("✓")
        assert cli.main(["apps", "несуществующая программа xyz"]) == 1
        assert "не найдено" in capsys.readouterr().out
    finally:
        apps.set_inventory(None)


def test_apps_refresh(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from pc import apps

    inv = [apps.App("Блокнот", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App")]
    monkeypatch.setattr(apps, "refresh", lambda: inv)
    monkeypatch.setattr(apps, "inventory", lambda: inv)
    assert cli.main(["apps", "--refresh"]) == 0
    out = capsys.readouterr().out
    assert "инвентарь обновлён: 1" in out and "Блокнот" in out


# --- прочие подкоманды ----------------------------------------------------------------------------


def test_help_hides_mcp(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for name in ("run", "ask", "route", "apps", "bench", "stats", "doctor", "autostart", "selftest"):
        assert name in out
    assert "mcp" not in out and "SUPPRESS" not in out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    assert "команда" in capsys.readouterr().out


def test_windowed_no_args_is_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    install(monkeypatch, "jarvis.app", main=lambda argv=None: calls.append(argv) or 0)
    monkeypatch.setattr(sys, "stdout", None)
    assert cli.main([]) == 0
    assert calls == [[]]


def test_dispatch_run_mcp_selftest_autostart(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Any] = []
    install(monkeypatch, "jarvis.app", main=lambda argv=None: calls.append(("run", argv)) or 0)
    install(monkeypatch, "pc.mcp", main=lambda: calls.append(("mcp",)))
    install(monkeypatch, "jarvis.selftest", main=lambda: calls.append(("selftest",)) or 1)
    install(
        monkeypatch, "jarvis.winapp", set_autostart=lambda on: calls.append(("autostart", on)) or "включён"
    )
    assert cli.main(["run", "--hidden"]) == 0
    assert cli.main(["mcp"]) == 0
    assert cli.main(["selftest"]) == 1
    assert cli.main(["autostart", "on"]) == 0
    assert calls == [("run", ["--hidden"]), ("mcp",), ("selftest",), ("autostart", True)]
    assert "включён" in capsys.readouterr().out


def test_stats_and_failures(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["stats", "--days", "3"]) == 0
    assert "пуст" in capsys.readouterr().out

    def boom(text: str, ctx: Any, mode: str) -> Any:
        raise RuntimeError("роутер сломан")

    install(monkeypatch, "jarvis.router", route=boom)
    assert cli.main(["route", "что-то"]) == 1
    assert "✗ route: роутер сломан" in capsys.readouterr().err


def test_fmt_timings() -> None:
    line = cli.fmt_timings(
        {
            "route": 0.42,
            "hands": {"prompt_n": 12, "cache_n": 1183, "predicted_per_second": 74.2, "total_ms": 352.0},
            "exec": 20.0,
            "brain": {"first_token": 1500.0, "new_thread": False},
            "total": 1890.0,
        }
    )
    assert line == (
        "роутер 0,42 мс · руки 352 мс (prompt_n 12, cache_n 1183, 74 т/с) · действие 20 мс"
        " · GPT до первых слов 1,5 с · всего 1,9 с"
    )
