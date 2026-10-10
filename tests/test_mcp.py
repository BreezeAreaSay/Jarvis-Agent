"""MCP-сервер pc: инструменты под фейковыми действиями, отображение на политику, лёгкий импорт, stdio."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult

from pc import apps, audio, files, media, policy, privacy, procs, system, windows
from pc import mcp as pc_mcp
from pc.result import Result, fail, ok

ROOT = Path(__file__).resolve().parents[1]
DOC = r"C:\Users\me\Documents\отчёт.docx"

# инструмент, аргументы MCP, функция pc, ожидаемые args и kwargs её вызова
CASES: list[tuple[str, dict[str, Any], tuple[Any, str], tuple[Any, ...], dict[str, Any]]] = [
    ("list_windows", {}, (windows, "list_windows_result"), ("brain",), {}),
    ("list_apps", {"query": "теле"}, (apps, "list_apps_result"), ("теле", "brain"), {}),
    (
        "find_files",
        {"query": "отчёт", "kind": "file"},
        (files, "find"),
        ("отчёт",),
        {"kind": "file", "caller": "brain"},
    ),
    ("open", {"target": "Telegram", "kind": "app"}, (files, "open_target"), ("Telegram", "app", "brain"), {}),
    ("focus", {"target": 1234}, (windows, "focus_target"), (1234, "brain"), {}),
    ("close", {"target": "Блокнот"}, (windows, "close_target"), ("Блокнот", "brain"), {}),
    (
        "window",
        {"action": "maximize", "target": "1234"},
        (windows, "window_action"),
        ("maximize", "1234", "brain"),
        {},
    ),
    (
        "volume",
        {"delta": -10},
        (audio, "volume"),
        (),
        {"set": None, "delta": -10, "mute": None, "caller": "brain"},
    ),
    ("media", {"action": "next"}, (media, "media"), ("next", "brain"), {}),
    ("processes", {"name": "notepad"}, (procs, "processes"), ("notepad", "brain"), {}),
    ("kill_process", {"name": "notepad.exe"}, (procs, "kill"), ("notepad.exe", "brain"), {}),
    ("clipboard_get", {}, (system, "clipboard_get"), ("brain",), {}),
    ("clipboard_set", {"text": "привет"}, (system, "clipboard_set"), ("привет", "brain"), {}),
    ("read_text_file", {"path": DOC}, (files, "read_text"), (DOC, "brain"), {}),
    ("move_to_trash", {"path": DOC}, (files, "trash"), (DOC, "brain"), {}),
    (
        "type_text",
        {"text": "привет\nмир", "target": 1234},
        (system, "type_text"),
        ("привет\nмир", 1234, "brain"),
        {},
    ),
    ("lock", {}, (system, "lock"), ("brain",), {}),
    ("power", {"action": "sleep"}, (system, "power"), ("sleep", "brain"), {}),
]

Calls = list[tuple[str, tuple[Any, ...], dict[str, Any]]]


@pytest.fixture(scope="module")
def server() -> Any:
    return pc_mcp.build_server()


@pytest.fixture
def fake_pc(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Все функции pc, которые зовёт MCP, — фейки: записывают вызов и отвечают ok("<функция>: готово")."""
    calls: Calls = []

    def recorder(fname: str) -> Any:
        def fake(*args: Any, **kwargs: Any) -> Result:
            calls.append((fname, args, kwargs))
            return ok(f"{fname}: готово")

        return fake

    for _, _, (module, fname), _, _ in CASES:
        monkeypatch.setattr(module, fname, recorder(fname))
    monkeypatch.setattr(privacy, "redact_text", lambda text: text)
    return calls


def call(server: Any, name: str, args: dict[str, Any]) -> CallToolResult:
    async def go() -> CallToolResult:
        with anyio.fail_after(20):
            async with create_connected_server_and_client_session(server) as client:
                return await client.call_tool(name, args)

    return anyio.run(go)


def text_of(result: CallToolResult) -> str:
    assert len(result.content) == 1
    return result.content[0].text  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("tool", "args", "target", "exp_args", "exp_kwargs"), CASES, ids=[c[0] for c in CASES]
)
def test_tool_calls_pc_as_brain(server, fake_pc: Calls, tool, args, target, exp_args, exp_kwargs) -> None:
    result = call(server, tool, args)
    assert result.isError is False
    assert text_of(result) == f"{target[1]}: готово"
    assert fake_pc == [(target[1], exp_args, exp_kwargs)]


def test_data_as_compact_json_on_next_line(server, fake_pc: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    data = [DOC, {"hwnd": 1, "title": "Блокнот — отчёт.txt"}]
    monkeypatch.setattr(files, "find", lambda *a, **k: ok("Нашёл 2", data))
    text = text_of(call(server, "find_files", {"query": "отчёт"}))
    first, second = text.split("\n")
    assert first == "Нашёл 2"
    assert second == json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    assert "отчёт" in second


def test_dataclass_data_is_serialized(server, fake_pc: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    info = windows.WindowInfo(hwnd=4242, title="Блокнот", pid=7, exe="notepad.exe")
    monkeypatch.setattr(windows, "list_windows_result", lambda caller: ok("Окна", [info]))
    _, second = text_of(call(server, "list_windows", {})).split("\n")
    assert json.loads(second) == [{"hwnd": 4242, "title": "Блокнот", "pid": 7, "exe": "notepad.exe"}]


def test_fail_is_plain_text_not_protocol_error(
    server, fake_pc: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows, "focus_target", lambda target, caller: fail("Не нашёл окно «Блокнот»."))
    result = call(server, "focus", {"target": "Блокнот"})
    assert result.isError is False
    assert text_of(result) == "Не нашёл окно «Блокнот»."


def test_exception_is_text_without_traceback(server, fake_pc: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(target: Any, caller: str) -> Result:
        raise RuntimeError(r"C:\Users\me\секрет.txt")

    monkeypatch.setattr(windows, "close_target", broken)
    result = call(server, "close", {"target": "Блокнот"})
    text = text_of(result)
    assert result.isError is False
    assert "внутренняя ошибка (RuntimeError)" in text
    assert "секрет" not in text and "Traceback" not in text


def test_not_implemented_is_clear(server, fake_pc: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    def stub(caller: str) -> Result:
        raise NotImplementedError

    monkeypatch.setattr(system, "lock", stub)
    assert "пока не реализовано" in text_of(call(server, "lock", {}))


def test_non_result_is_error_text(server, fake_pc: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "lock", lambda caller: "готово")
    assert "внутренняя ошибка (TypeError)" in text_of(call(server, "lock", {}))


def test_text_passes_privacy(server, fake_pc: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privacy, "redact_text", lambda text: text.replace(r"C:\Users\me\.ssh", "(скрыто)"))
    monkeypatch.setattr(files, "trash", lambda path, caller: fail(r"Нет доступа: C:\Users\me\.ssh"))
    assert text_of(call(server, "move_to_trash", {"path": DOC})) == "Нет доступа: (скрыто)"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("focus", {"target": "@cur"}),
        ("close", {"target": " @CUR "}),
        ("window", {"action": "minimize", "target": "@cur"}),
        ("type_text", {"text": "привет", "target": "@cur"}),
    ],
)
def test_cur_target_is_not_for_brain(server, fake_pc: Calls, tool: str, args: dict[str, Any]) -> None:
    assert text_of(call(server, tool, args)) == pc_mcp.NO_CUR
    assert fake_pc == []


def test_window_target_rules(server, fake_pc: Calls) -> None:
    assert text_of(call(server, "window", {"action": "minimize"})) == pc_mcp.NO_TARGET
    assert text_of(call(server, "type_text", {"text": "привет", "target": " "})) == pc_mcp.NO_TARGET
    assert fake_pc == []
    call(server, "window", {"action": "minimize_all", "target": "Блокнот"})
    assert fake_pc == [("window_action", ("minimize_all", None, "brain"), {})]


@pytest.mark.parametrize("args", [{}, {"set": 30, "mute": True}, {"set": 10, "delta": 5, "mute": False}])
def test_volume_needs_exactly_one(server, fake_pc: Calls, args: dict[str, Any]) -> None:
    assert text_of(call(server, "volume", args)) == "Укажи ровно одно: set, delta или mute."
    assert fake_pc == []


def test_volume_mute(server, fake_pc: Calls) -> None:
    call(server, "volume", {"mute": True})
    assert fake_pc == [("volume", (), {"set": None, "delta": None, "mute": True, "caller": "brain"})]


def test_bad_enum_is_error_without_traceback(server, fake_pc: Calls) -> None:
    result = call(server, "media", {"action": "stop"})
    assert result.isError is True
    assert "Traceback" not in text_of(result)
    assert fake_pc == []


def test_tools_map_to_policy(server) -> None:
    listed = {t.name for t in anyio.run(server.list_tools)}
    assert len(listed) == 18
    assert listed == set(pc_mcp.TOOL_ACTIONS) == set(pc_mcp.TOOLS)
    for tool, actions in pc_mcp.TOOL_ACTIONS.items():
        assert actions, tool
        for action in actions:
            assert action in policy.TABLE, f"{tool} → {action}: нет в таблице политики"
            policy.check(action, "brain")


def test_annotations_and_descriptions(server) -> None:
    tools = anyio.run(server.list_tools)
    read_only = {t.name for t in tools if t.annotations.readOnlyHint}
    destructive = {t.name for t in tools if t.annotations.destructiveHint}
    assert read_only == {
        "list_windows",
        "list_apps",
        "find_files",
        "processes",
        "clipboard_get",
        "read_text_file",
    }
    assert destructive == {"kill_process", "move_to_trash", "power"}
    for t in tools:
        assert t.annotations.destructiveHint is not None
        assert t.outputSchema is None
        assert t.description and "\n" not in t.description and len(t.description) < 250
        assert any("а" <= ch <= "я" for ch in t.description.casefold()), t.name


def test_input_schemas_are_exact(server) -> None:
    schemas = {t.name: t.inputSchema for t in anyio.run(server.list_tools)}
    assert schemas["find_files"]["properties"]["kind"]["enum"] == ["file", "folder", "any"]
    assert schemas["window"]["properties"]["action"]["enum"] == [
        "minimize",
        "maximize",
        "restore",
        "minimize_all",
    ]
    assert schemas["media"]["properties"]["action"]["enum"] == ["play_pause", "next", "prev"]
    assert schemas["power"]["properties"]["action"]["enum"] == ["sleep", "shutdown", "restart"]
    assert set(schemas["volume"]["properties"]) == {"set", "delta", "mute"}
    assert schemas["type_text"]["required"] == ["text", "target"]
    assert schemas["list_windows"]["properties"] == {}


def run_python(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ROOT
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


LIMIT = 3.0 if os.environ.get("CI") else 1.0


def test_import_is_light_and_fast() -> None:
    code = (
        "import sys, time\n"
        "t = time.perf_counter()\n"
        "import pc.mcp\n"
        "dt = time.perf_counter() - t\n"
        "heavy = [m for m in ('PySide6', 'openai_codex', 'mcp', 'pc.windows', 'pc.audio')\n"
        "         if m in sys.modules]\n"
        "assert not heavy, heavy\n"
        "print(dt)\n"
    )
    assert float(run_python(code)) < LIMIT


def test_server_start_does_not_import_qt_codex_or_actions() -> None:
    code = (
        "import sys, time\n"
        "t = time.perf_counter()\n"
        "import pc.mcp\n"
        "pc.mcp.build_server()\n"
        "dt = time.perf_counter() - t\n"
        "heavy = [m for m in sys.modules if m.split('.')[0] in ('PySide6', 'openai_codex')]\n"
        "heavy += [m for m in ('pc.windows', 'pc.audio', 'pc.files', 'pc.system', 'pc.apps')\n"
        "          if m in sys.modules]\n"
        "assert not heavy, heavy\n"
        "print(dt)\n"
    )
    # FastMCP сам по себе грузится ≈0,7–0,9 с; запас — от регресса вроде случайного импорта Qt
    assert float(run_python(code)) < 2 * LIMIT


def test_stdio_server_lists_18_tools(tmp_path: Path) -> None:
    env = {
        "JARVIS_DATA_DIR": os.environ["JARVIS_DATA_DIR"],
        "JARVIS_CONFIG": os.environ["JARVIS_CONFIG"],
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "pc.mcp"], cwd=str(ROOT), env=env)
    errlog = tmp_path / "server-stderr.txt"

    async def go() -> tuple[list[str], str]:
        with anyio.fail_after(60), errlog.open("w", encoding="utf-8") as err:
            async with (
                stdio_client(params, errlog=err) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                # вызов без действия ПК: проверка аргументов в самом MCP-слое
                answer = text_of(await session.call_tool("volume", {}))
                return names, answer

    names, answer = anyio.run(go)
    assert sorted(names) == sorted(pc_mcp.TOOLS), errlog.read_text(encoding="utf-8")
    assert answer == "Укажи ровно одно: set, delta или mute."
    log_file = Path(os.environ["JARVIS_DATA_DIR"]) / "logs" / "pc-mcp.log"
    assert "pc.mcp: pid" in log_file.read_text(encoding="utf-8")
