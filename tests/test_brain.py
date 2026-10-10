"""Мозг: overrides, прокладка subprocess, треды, поток событий и ошибки — на фейковом Codex.

Уведомления синтетические, но собраны из настоящих generated-моделей SDK 0.160.1 (те же поля и алиасы).
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex import client as sdk_client
from openai_codex.errors import InvalidRequestError, TransportClosedError
from openai_codex.generated import v2_all as g
from openai_codex.models import Notification
from openai_codex.types import ReasoningEffort

from jarvis import brain
from jarvis.config import BrainConfig
from jarvis.context import Context
from jarvis.events import Done, Event, Status, TextChunk
from pc import privacy, settings
from pc.windows import WindowInfo

ROOT = Path(__file__).resolve().parents[1]

SAFE_THREAD_ARGS = {
    "approval_mode": ApprovalMode.deny_all,
    "sandbox": Sandbox.read_only,
    "ephemeral": True,
    "developer_instructions": brain.DEVELOPER_INSTRUCTIONS,
    "model": BrainConfig().model_quick,
}

# --- синтетические уведомления ---------------------------------------------------------------------


def n_delta(turn: "FakeTurn", text: str, item: str = "msg-1") -> Notification:
    payload = g.AgentMessageDeltaNotification.model_validate(
        {"delta": text, "itemId": item, "threadId": turn.thread_id, "turnId": turn.id}
    )
    return Notification("item/agentMessage/delta", payload)


def n_item(turn: "FakeTurn", method: str, item: dict[str, Any]) -> Notification:
    model, stamp = (
        (g.ItemStartedNotification, "startedAtMs")
        if method == "item/started"
        else (g.ItemCompletedNotification, "completedAtMs")
    )
    payload = model.model_validate({"item": item, stamp: 0, "threadId": turn.thread_id, "turnId": turn.id})
    return Notification(method, payload)


def mcp_call(status: str, call_id: str = "call-1", tool: str = "list_windows", args: Any = None) -> dict:
    item = {"type": "mcpToolCall", "id": call_id, "server": "pc", "tool": tool, "arguments": args or {}}
    item["status"] = status
    if status == "failed":
        item["error"] = {"message": "отказ"}
    return item


def agent_msg(text: str, phase: str | None = "final_answer", item: str = "msg-1") -> dict:
    return {"type": "agentMessage", "id": item, "text": text, "phase": phase}


def n_completed(turn: "FakeTurn", status: str, error: dict | None = None) -> Notification:
    payload = g.TurnCompletedNotification.model_validate(
        {"threadId": turn.thread_id, "turn": {"id": turn.id, "items": [], "status": status, "error": error}}
    )
    return Notification("turn/completed", payload)


def n_error(turn: "FakeTurn", message: str, info: Any = None, will_retry: bool = False) -> Notification:
    payload = g.ErrorNotification.model_validate(
        {
            "error": {"message": message, "codexErrorInfo": info},
            "threadId": turn.thread_id,
            "turnId": turn.id,
            "willRetry": will_retry,
        }
    )
    return Notification("error", payload)


def n_usage(turn: "FakeTurn") -> Notification:
    part = {
        "cachedInputTokens": 900,
        "inputTokens": 1000,
        "outputTokens": 12,
        "reasoningOutputTokens": 0,
        "totalTokens": 1012,
    }
    payload = g.ThreadTokenUsageUpdatedNotification.model_validate(
        {"threadId": turn.thread_id, "turnId": turn.id, "tokenUsage": {"last": part, "total": part}}
    )
    return Notification("thread/tokenUsage/updated", payload)


def n_mcp_status(thread_id: str | None, status: str, error: str | None = None) -> Notification:
    payload = g.McpServerStatusUpdatedNotification.model_validate(
        {"name": "pc", "status": status, "error": error, "threadId": thread_id}
    )
    return Notification("mcpServer/startupStatus/updated", payload)


def reply(turn: "FakeTurn") -> list[Any]:
    return [
        n_delta(turn, "Привет"),
        n_delta(turn, ", мир"),
        n_item(turn, "item/completed", agent_msg("Привет, мир")),
        n_usage(turn),
        n_completed(turn, "completed"),
    ]


# --- фейковый Codex --------------------------------------------------------------------------------


class FakeTurn:
    def __init__(self, thread: "FakeThread", turn_id: str, prompt: str, kwargs: dict[str, Any]) -> None:
        self.thread_id, self.id, self.prompt, self.kwargs = thread.id, turn_id, prompt, kwargs
        self.q: queue.Queue[Any] = queue.Queue()
        self.interrupts = 0

    def stream(self):
        while True:
            item = self.q.get(timeout=5)
            if isinstance(item, BaseException):
                raise item
            yield item
            if item.method == "turn/completed":
                return

    def interrupt(self) -> None:
        self.interrupts += 1
        self.q.put(n_completed(self, "interrupted"))


class FakeThread:
    def __init__(self, codex: "FakeCodex", thread_id: str) -> None:
        self.codex, self.id = codex, thread_id
        self.turns: list[FakeTurn] = []

    def turn(self, prompt: str, **kwargs: Any) -> FakeTurn:
        turn = FakeTurn(self, f"{self.id}/turn-{len(self.turns) + 1}", prompt, kwargs)
        self.turns.append(turn)
        for ev in self.codex.world.script(turn):
            turn.q.put(ev)
        return turn


class FakeClient:
    def __init__(self) -> None:
        self.notes: queue.Queue[Any] = queue.Queue()

    def next_notification(self) -> Notification:
        item = self.notes.get()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeCodex:
    def __init__(self, world: "World", config: Any) -> None:
        self.world, self.config = world, config
        self._client = FakeClient()
        self.thread_starts: list[dict[str, Any]] = []
        self.threads: list[FakeThread] = []
        self.closed = False
        self._lock = threading.Lock()

    def thread_start(self, **kwargs: Any) -> FakeThread:
        with self._lock:
            self.thread_starts.append(kwargs)
            if self.world.thread_start_error is not None:
                raise self.world.thread_start_error
            thread = FakeThread(self, f"c{len(self.world.codexes)}-t{len(self.threads) + 1}")
            self.threads.append(thread)
            return thread

    def close(self) -> None:
        self.closed = True
        self._client.notes.put(TransportClosedError("Codex process is not running"))


class FakeTimer:
    def __init__(self, seconds: float, fn: Callable[[], None]) -> None:
        self.seconds, self.fn, self.cancelled = seconds, fn, False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.fn()


class World:
    def __init__(self) -> None:
        self.codexes: list[FakeCodex] = []
        self.timers: list[FakeTimer] = []
        self.script: Callable[[FakeTurn], list[Any]] = reply
        self.thread_start_error: BaseException | None = None
        self.gate: threading.Event | None = None

    def make(self, config: Any) -> FakeCodex:
        if self.gate is not None:
            assert self.gate.wait(5)
        codex = FakeCodex(self, config)
        self.codexes.append(codex)
        return codex

    def timer(self, seconds: float, fn: Callable[[], None]) -> FakeTimer:
        t = FakeTimer(seconds, fn)
        self.timers.append(t)
        return t

    @property
    def codex(self) -> FakeCodex:
        return self.codexes[-1]

    def thread_starts(self) -> list[dict[str, Any]]:
        return [kw for c in self.codexes for kw in c.thread_starts]


def wait_until(cond: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "не дождались условия"
        time.sleep(0.01)


def run(b: brain.Brain, text: str = "вопрос", ctx: Context | None = None, deep: bool = False) -> list[Event]:
    events = list(b.ask(text, ctx if ctx is not None else Context(), deep=deep))
    assert isinstance(events[-1], Done)
    assert sum(isinstance(e, Done) for e in events) == 1
    assert all(isinstance(e, Status | TextChunk) for e in events[:-1])
    assert events[-1].level == "brain"
    return events


def statuses(events: list[Event]) -> list[Status]:
    return [e for e in events if isinstance(e, Status)]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(brain, "_new_codex", w.make)
    monkeypatch.setattr(brain, "_start_timer", w.timer)
    monkeypatch.setattr(privacy, "redact_title", lambda title: title)
    monkeypatch.setattr(privacy, "redact_text", lambda text: text)
    # прокладка ставится в настоящий openai_codex.client — после теста вернуть как было
    monkeypatch.setattr(sdk_client, "subprocess", sdk_client.subprocess)
    return w


@pytest.fixture
def make_brain(world: World):
    made: list[brain.Brain] = []

    def make(
        cfg: BrainConfig | None = None, start: bool = True, clock: list[float] | None = None
    ) -> brain.Brain:
        b = brain.Brain(cfg or BrainConfig(), "jarvis-confirm-test")
        if clock is not None:
            b._clock = lambda: clock[0]
        made.append(b)
        if start:
            b.start()
            assert b._boot_done.wait(5)
            assert b.ready, b.status
        return b

    yield make
    for b in made:
        b.close()


# --- импорт и overrides ----------------------------------------------------------------------------


def test_import_does_not_pull_openai_codex() -> None:
    code = "import sys, jarvis.brain; assert 'openai_codex' not in sys.modules, sorted(sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


ALLOWED_KEYS = {
    "web_search": str,
    "model_reasoning_summary": str,
    "model_verbosity": str,
    "project_doc_max_bytes": int,
    "history.persistence": str,
    "features.shell_tool": bool,
    "features.view_image": bool,
    "features.apps": bool,
    "features.plugins": bool,
    "features.tool_suggest": bool,
    "features.image_generation": bool,
    "features.multi_agent": bool,
    "features.goals": bool,
    "tools.experimental_request_user_input.enabled": bool,
    "thread_unload_delay_secs": int,
    "mcp_servers.pc.command": str,
    "mcp_servers.pc.args": list,
    "mcp_servers.pc.cwd": str,
    "mcp_servers.pc.env": dict,
    "mcp_servers.pc.default_tools_approval_mode": str,
    "mcp_servers.pc.startup_timeout_sec": int,
    "mcp_servers.pc.tool_timeout_sec": int,
}


def parsed_overrides(cfg: BrainConfig | None = None, address: str = r"\\.\pipe\jarvis-confirm-me-1") -> dict:
    out = {}
    for item in brain.build_overrides(cfg or BrainConfig(), address):
        key, _, raw = item.partition("=")  # codex делит по первому «=»
        out[key] = (raw, tomllib.loads(f"x = {raw}")["x"])
    return out


def test_overrides_only_allowed_keys_and_toml_types() -> None:
    values = parsed_overrides()
    assert set(values) == set(ALLOWED_KEYS)
    for key, (raw, value) in values.items():
        assert type(value) is ALLOWED_KEYS[key], (key, raw)


def test_override_values() -> None:
    v = {k: val for k, (_, val) in parsed_overrides().items()}
    assert v["web_search"] == "disabled"
    assert v["model_reasoning_summary"] == "none"
    assert v["model_verbosity"] == "low"
    assert v["project_doc_max_bytes"] == 0
    assert v["history.persistence"] == "none"
    assert v["features.shell_tool"] is False
    assert all(v[k] is False for k in ALLOWED_KEYS if k.startswith("features."))
    assert v["tools.experimental_request_user_input.enabled"] is False
    assert v["thread_unload_delay_secs"] == 20 * 60 + 300
    assert v["mcp_servers.pc.default_tools_approval_mode"] == "approve"
    assert v["mcp_servers.pc.startup_timeout_sec"] == 15
    assert v["mcp_servers.pc.tool_timeout_sec"] == 120
    env = v["mcp_servers.pc.env"]
    assert env == {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "JARVIS_DATA_DIR": str(settings.data_dir()),
        "JARVIS_CONFIG": str(settings.config_path()),
        "JARVIS_CONFIRM_PIPE": r"\\.\pipe\jarvis-confirm-me-1",
        "JARVIS_ROOT_PID": str(os.getpid()),
    }
    assert all(isinstance(x, str) for x in env.values())
    assert parsed_overrides(BrainConfig(idle_new_thread_min=5))["thread_unload_delay_secs"][1] == 600


def test_paths_are_literal_single_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    data = r"C:\JarvisData\new\tmp"  # в двойных кавычках \n и \t стали бы управляющими символами
    monkeypatch.setattr(settings, "data_dir", lambda: Path(data))
    monkeypatch.setattr(settings, "app_root", lambda: Path(r"C:\Jarvis"))
    values = parsed_overrides()
    assert values["mcp_servers.pc.cwd"][0] == r"'C:\Jarvis'"
    assert values["mcp_servers.pc.command"][0].startswith("'")
    raw_env, env = values["mcp_servers.pc.env"]
    assert f"JARVIS_DATA_DIR = '{data}'" in raw_env
    assert "JARVIS_CONFIRM_PIPE = '\\\\.\\pipe\\jarvis-confirm-me-1'" in raw_env
    assert env["JARVIS_DATA_DIR"] == data


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\me\it's",
        r"C:\Jarvis\a'b\t\n",
        "C:\\Users\\me\\x\ty",
        r"C:\Users\me\отчёт \"x\"",
        r"C:\Jarvis",
    ],
)
def test_toml_path_roundtrip(path: str) -> None:
    raw = brain.toml_path(path)
    assert tomllib.loads(f"x = {raw}")["x"] == path
    if "'" in path or "\t" in path:
        assert raw.startswith('"')  # литеральная строка не вмещает кавычку — экранирование в двойных
    else:
        assert raw == f"'{path}'"


def test_quote_in_data_dir_survives_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    data = r"C:\Users\me\O'Brien Data"
    monkeypatch.setattr(settings, "data_dir", lambda: Path(data))
    assert parsed_overrides()["mcp_servers.pc.env"][1]["JARVIS_DATA_DIR"] == data


def test_mcp_command_dev_uses_python_not_pythonw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "pythonw.exe"))
    monkeypatch.setattr(settings, "is_frozen", lambda: False)
    command, args, cwd = brain.mcp_command()
    assert command == str(scripts / "python.exe")
    assert args == ["-m", "pc.mcp"]
    assert cwd == str(settings.app_root())


def test_mcp_command_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(settings, "install_dir", lambda: tmp_path / "Jarvis")
    command, args, cwd = brain.mcp_command()
    assert command == str(tmp_path / "Jarvis" / "jarvis-cli.exe")
    assert args == ["mcp"]
    assert cwd == str(tmp_path / "Jarvis")
    v = parsed_overrides()
    assert v["mcp_servers.pc.args"][1] == ["mcp"]
    assert v["mcp_servers.pc.command"][1] == command


def test_codex_bin_dev_and_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_cli_bin import bundled_codex_path

    assert brain.codex_bin() == bundled_codex_path()
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    exe = "codex.exe" if os.name == "nt" else "codex"
    assert brain.codex_bin() == tmp_path / "codex_cli_bin" / "bin" / exe


def test_codex_env_and_home() -> None:
    home = settings.data_dir() / "codex-home"
    assert brain.codex_home() == home
    assert brain.codex_env(BrainConfig()) == {"CODEX_HOME": str(home)}
    env = brain.codex_env(BrainConfig(proxy=" http://127.0.0.1:7890 "))
    assert env == {
        "CODEX_HOME": str(home),
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "127.0.0.1,localhost",
    }


# --- прокладка subprocess --------------------------------------------------------------------------


def test_shim_adds_no_window_flag_and_calls_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk_client, "subprocess", sdk_client.subprocess)
    monkeypatch.setattr(brain, "_WINDOWS", True)
    monkeypatch.setattr(brain, "_CREATE_NO_WINDOW", 0x08000000)
    seen: list[dict[str, Any]] = []

    def fake_init(self: subprocess.Popen, args: Any, **kwargs: Any) -> None:
        self._child_created = False
        self.pid = 4242
        seen.append(kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    hooked: list[Any] = []
    brain.install_subprocess_shim(hooked.append)
    shim = sdk_client.subprocess
    assert shim is not subprocess and shim._jarvis_shim
    assert shim.PIPE is subprocess.PIPE and shim.TimeoutExpired is subprocess.TimeoutExpired
    proc = shim.Popen(["codex", "app-server"], stdin=shim.PIPE, creationflags=0x10)
    assert seen[-1]["creationflags"] == 0x08000010
    assert hooked == [proc] and proc.pid == 4242
    shim.Popen(["codex"])
    assert seen[-1]["creationflags"] == 0x08000000


def test_shim_runs_real_process_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk_client, "subprocess", sdk_client.subprocess)
    first: list[Any] = []
    second: list[Any] = []
    brain.install_subprocess_shim(first.append)
    shim = sdk_client.subprocess
    brain.install_subprocess_shim(second.append)
    assert sdk_client.subprocess is shim  # повторная установка не оборачивает прокладку ещё раз
    assert sdk_client.subprocess.Popen[str] is not None  # аннотация subprocess.Popen[str] в SDK работает
    proc = shim.Popen([sys.executable, "-c", "print('ok')"], stdout=shim.PIPE, stdin=shim.DEVNULL, text=True)
    out, _ = proc.communicate(timeout=30)
    assert out.strip() == "ok" and proc.returncode == 0
    assert first == [] and second == [proc]


# --- старт, треды, поток событий -----------------------------------------------------------------


def test_start_in_background_and_codex_config(world: World, make_brain) -> None:
    world.gate = threading.Event()
    b = make_brain(start=False)
    t0 = time.perf_counter()
    b.start()
    assert time.perf_counter() - t0 < 0.5  # UI не ждёт
    assert b.status["state"] == "starting" and not b.ready
    world.gate.set()
    assert b._boot_done.wait(5) and b.ready
    config = world.codex.config
    data = settings.data_dir()
    assert config.cwd == str(data / "brain") and (data / "brain").is_dir()
    assert config.env == {"CODEX_HOME": str(data / "codex-home")} and (data / "codex-home").is_dir()
    assert config.codex_bin is None
    assert config.config_overrides == brain.build_overrides(BrainConfig(), "jarvis-confirm-test")
    assert world.codex.thread_starts == [SAFE_THREAD_ARGS]  # запасной тред создан при старте
    assert b.status["threads"]["spare"] is not None


def test_frozen_codex_bin_and_path(world: World, make_brain, tmp_path: Path, monkeypatch) -> None:
    exe = "codex.exe" if os.name == "nt" else "codex"
    (tmp_path / "codex_cli_bin" / "bin").mkdir(parents=True)
    (tmp_path / "codex_cli_bin" / "codex-path").mkdir()
    monkeypatch.setattr(settings, "is_frozen", lambda: True)
    monkeypatch.setattr(settings, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    make_brain()
    config = world.codex.config
    assert config.codex_bin == str(tmp_path / "codex_cli_bin" / "bin" / exe)
    assert config.env["PATH"].split(os.pathsep)[0] == str(tmp_path / "codex_cli_bin" / "codex-path")


def test_text_stream_and_timings(world: World, make_brain) -> None:
    def script(turn: FakeTurn) -> list[Any]:
        return [
            n_delta(turn, "Сейчас.", item="m1"),
            n_delta(turn, "Готово", item="m2"),
            n_delta(turn, "."),
            n_item(turn, "item/completed", agent_msg("Сейчас.", phase="commentary", item="m1")),
            n_item(turn, "item/completed", agent_msg("Готово.", item="m2")),
            n_usage(turn),
            n_completed(turn, "completed"),
        ]

    world.script = script
    b = make_brain()
    events = run(b, "привет")
    chunks = [e.text for e in events if isinstance(e, TextChunk)]
    assert chunks == ["Сейчас.", "\n", "Готово", "\n", "."]  # новое сообщение агента — с новой строки
    done = events[-1]
    assert done.ok and done.text == "Готово." and not done.cancelled
    assert set(done.timings) == {"t_first_token", "t_total", "new_thread", "model", "usage"}
    assert done.timings["new_thread"] is True
    assert done.timings["model"] == "gpt-6-luna"
    assert 0 <= done.timings["t_first_token"] <= done.timings["t_total"]
    assert done.timings["usage"] == {
        "input": 1000,
        "cached": 900,
        "output": 12,
        "reasoning": 0,
        "total": 1012,
    }
    turn = world.codex.threads[0].turns[0]
    assert turn.kwargs == {"model": "gpt-6-luna", "effort": ReasoningEffort.low}  # service_tier пуст — нет
    assert turn.prompt == "привет"
    # второй вопрос — в том же треде
    world.script = reply
    second = run(b)[-1]
    assert second.ok and second.text == "Привет, мир" and second.timings["new_thread"] is False
    assert len(world.codex.threads[0].turns) == 2


def test_deep_model_effort_and_service_tier(world: World, make_brain) -> None:
    b = make_brain(BrainConfig(service_tier="fast"))
    done = run(b, deep=True)[-1]
    assert done.ok and done.timings["model"] == "gpt-6.1-sol"
    turn = world.codex.threads[0].turns[0]
    assert turn.kwargs == {"model": "gpt-6.1-sol", "effort": ReasoningEffort.high, "service_tier": "fast"}


def test_mcp_tool_call_status(world: World, make_brain) -> None:
    def script(turn: FakeTurn) -> list[Any]:
        return [
            n_item(turn, "item/started", mcp_call("inProgress", "call-7", "open", {"target": "блокнот"})),
            n_item(turn, "item/completed", mcp_call("completed", "call-7", "open", {"target": "блокнот"})),
            n_item(
                turn,
                "item/started",
                mcp_call("inProgress", "call-8", "clipboard_set", {"text": "строка\nдва"}),
            ),
            n_item(
                turn, "item/completed", mcp_call("failed", "call-8", "clipboard_set", {"text": "строка\nдва"})
            ),
            n_delta(turn, "Открыл блокнот."),
            n_completed(turn, "completed"),
        ]

    world.script = script
    events = run(make_brain(), "открой блокнот")
    got = [(s.text, s.kind, s.done, s.ok, s.key) for s in statuses(events)]
    assert got == [
        ("⚙ open …", "tool", False, True, "call-7"),
        ("⚙ open …", "tool", True, True, "call-7"),
        ("⚙ буфер: строка два", "tool", False, True, "call-8"),
        ("⚙ буфер: строка два", "tool", True, False, "call-8"),
    ]
    assert events[-1].ok and events[-1].text == "Открыл блокнот."


def test_cancel_interrupts_turn(world: World, make_brain) -> None:
    world.script = lambda turn: [n_delta(turn, "Начинаю")]  # дальше ход «думает», пока не прервут
    b = make_brain()
    events: list[Event] = []
    for ev in b.ask("длинный вопрос", Context()):
        events.append(ev)
        if isinstance(ev, TextChunk):
            b.cancel()
    done = events[-1]
    assert isinstance(done, Done) and done.cancelled and not done.ok and done.text == brain.CANCELLED_TEXT
    assert world.codex.threads[0].turns[0].interrupts == 1
    assert b.ready  # после отмены мозг работает дальше
    world.script = reply
    assert run(b)[-1].ok


def test_abandoned_generator_interrupts_turn(world: World, make_brain) -> None:
    world.script = lambda turn: [n_delta(turn, "Начинаю")]
    b = make_brain()
    gen = b.ask("вопрос", Context())
    assert isinstance(next(gen), TextChunk)
    gen.close()
    turn = world.codex.threads[0].turns[0]
    wait_until(lambda: turn.interrupts == 1)
    world.script = reply
    assert run(b)[-1].ok  # замок ask освобождён


def test_ask_waits_for_start(world: World, make_brain) -> None:
    world.gate = threading.Event()
    b = make_brain(start=False)
    b.start()
    gen = b.ask("вопрос", Context())
    first = next(gen)
    assert first == Status(brain.START_TEXT, key="brain-start")
    world.gate.set()
    rest = list(gen)
    assert rest[0] == Status(brain.START_TEXT, done=True, ok=True, key="brain-start")
    assert isinstance(rest[-1], Done) and rest[-1].ok


def test_ask_starts_brain_if_not_started(world: World, make_brain) -> None:
    b = make_brain(start=False)
    events = run(b)
    assert events[0].text == brain.START_TEXT and events[-1].ok
    assert len(world.codexes) == 1


# --- ошибки ---------------------------------------------------------------------------------------


def test_config_error_on_first_thread_start(world: World, make_brain) -> None:
    world.thread_start_error = InvalidRequestError(-32600, "failed to load configuration: invalid override")
    b = make_brain(start=False)
    b.start()
    assert b._boot_done.wait(5)
    st = b.status
    assert st["state"] == "error" and st["reason"] == "config"
    assert "конфигурации Codex" in st["error"] and "failed to load configuration" in st["error"]
    assert world.codex.closed  # бесполезный процесс закрыт
    done = run(b)[-1]  # ask пробует перезапуск — ошибка та же, понятным текстом
    assert not done.ok and done.reason == "config" and "конфигурации Codex" in done.text
    assert len(world.codexes) == 2 and all(c.closed for c in world.codexes)


def test_start_error_from_codex_stderr() -> None:
    exc = TransportClosedError(
        "Codex process closed stdout. stderr_tail=WARNING: x\n"
        "Error: unknown variant `bogus`, expected one of `disabled`, `cached`\n"
        "in `web_search`\n\nStack backtrace:\n"
    )
    reason, text = brain.start_error(exc)
    assert reason == "config"
    assert "unknown variant `bogus`" in text and "in `web_search`" in text
    assert brain.start_error(FileNotFoundError("codex.exe"))[0] == "start"
    assert brain.start_error(TransportClosedError("closed"))[0] == "start"


def test_region_403_on_failed_turn(world: World, make_brain) -> None:
    info = {"responseStreamConnectionFailed": {"httpStatusCode": 403}}
    world.script = lambda turn: [
        n_completed(turn, "failed", {"message": "unexpected status", "codexErrorInfo": info})
    ]
    done = run(make_brain())[-1]
    assert not done.ok and done.reason == "region" and done.text == brain.REGION_TEXT


def test_region_retry_limited_to_20_seconds(world: World, make_brain) -> None:
    info = {"httpConnectionFailed": {"httpStatusCode": 403}}
    world.script = lambda turn: [n_error(turn, "Forbidden", info, will_retry=True)]  # сервер повторяет
    b = make_brain()
    events: list[Event] = []
    for ev in b.ask("вопрос", Context()):
        events.append(ev)
        if isinstance(ev, Status) and ev.text == brain.RETRY_TEXT:
            limits = [t for t in world.timers if t.seconds == brain.RETRY_LIMIT_S]
            assert len(limits) == 1 and brain.RETRY_LIMIT_S == 20.0
            limits[0].fire()  # «прошло 20 с»
    done = events[-1]
    assert (
        isinstance(done, Done) and not done.ok and done.reason == "region" and done.text == brain.REGION_TEXT
    )
    assert not done.cancelled
    assert world.codex.threads[0].turns[0].interrupts == 1


def test_endless_network_retry_is_cut(world: World, make_brain) -> None:
    info = {"responseStreamDisconnected": {"httpStatusCode": None}}

    def script(turn: FakeTurn) -> list[Any]:
        return [n_error(turn, "Reconnecting... waiting for network", info, will_retry=True)]

    world.script = script
    b = make_brain()
    events: list[Event] = []
    for ev in b.ask("вопрос", Context()):
        events.append(ev)
        if isinstance(ev, Status) and ev.text == brain.RETRY_TEXT:
            assert not [t for t in world.timers if t.seconds == brain.RETRY_LIMIT_S]  # это не 403
            limits = [t for t in world.timers if t.seconds == brain.NETWORK_RETRY_LIMIT_S]
            assert len(limits) == 1
            limits[0].fire()
    done = events[-1]
    assert isinstance(done, Done) and not done.ok and done.reason == "network" and not done.cancelled
    assert "Нет связи с GPT" in done.text and "Reconnecting" in done.text
    assert b.ready


@pytest.mark.parametrize(
    "message",
    [
        "unsupported_country_region_territory",
        "Country, region, or territory not supported",
        "Request blocked: RESTRICTED REGION",
        "<html>Attention Required! | Cloudflare</html>",
    ],
)
def test_region_markers(message: str) -> None:
    assert brain.classify_error(message) == ("region", brain.REGION_TEXT)


def test_quota_message_suggests_local_mode(world: World, make_brain) -> None:
    error = {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}
    world.script = lambda turn: [n_completed(turn, "failed", error)]
    done = run(make_brain())[-1]
    assert not done.ok and done.reason == "quota"
    assert "usage limit" in done.text and "локально" in done.text
    assert (
        brain.classify_error("Too Many Requests", {"httpConnectionFailed": {"httpStatusCode": 429}})[0]
        == "quota"
    )


def test_other_turn_error_is_shown(world: World, make_brain) -> None:
    world.script = lambda turn: [n_completed(turn, "failed", {"message": "model overloaded"})]
    done = run(make_brain())[-1]
    assert not done.ok and done.reason == "error" and "model overloaded" in done.text


def test_mcp_startup_failed_message(world: World, make_brain) -> None:
    b = make_brain()
    spare_id = world.codex.threads[0].id
    world.codex._client.notes.put(n_mcp_status(spare_id, "failed", "python.exe не найден"))
    wait_until(lambda: b.status["threads"]["spare"]["mcp"].startswith("failed"))
    events = run(b)
    warns = [s for s in statuses(events) if s.kind == "warn"]
    assert len(warns) == 1 and "python.exe не найден" in warns[0].text and "не запустились" in warns[0].text
    assert events[-1].ok  # GPT отвечает и без инструментов
    assert not [s for s in statuses(run(b)) if s.kind == "warn"]  # второй раз не повторяем


def test_dead_process_in_turn_recreated_on_next_ask(world: World, make_brain) -> None:
    world.script = lambda turn: [n_delta(turn, "При"), TransportClosedError("Codex process closed stdout")]
    b = make_brain()
    done = run(b)[-1]
    assert not done.ok and done.reason == "transport" and done.text == brain.DEAD_TEXT
    assert b.status["state"] == "error"
    world.script = reply
    events = run(b)
    assert events[0].text == brain.START_TEXT and events[-1].ok
    assert len(world.codexes) == 2
    wait_until(lambda: world.codexes[0].closed)  # умерший процесс закрыт (в фоне)


def test_dead_process_noticed_by_event_pump(world: World, make_brain) -> None:
    b = make_brain()
    world.codex._client.notes.put(TransportClosedError("Codex process closed stdout"))
    wait_until(lambda: b.status["state"] == "error")
    assert b.status["reason"] == "transport"
    assert run(b)[-1].ok
    assert len(world.codexes) == 2


def test_rejected_thread_retries_in_new_thread(world: World, make_brain, monkeypatch) -> None:
    b = make_brain()
    calls = {"n": 0}
    original = FakeThread.turn

    def flaky(self: FakeThread, prompt: str, **kwargs: Any) -> FakeTurn:
        calls["n"] += 1
        if calls["n"] == 1:
            raise InvalidRequestError(-32600, "thread not found")
        return original(self, prompt, **kwargs)

    monkeypatch.setattr(FakeThread, "turn", flaky)
    done = run(b)[-1]
    assert done.ok and done.timings["new_thread"] is True and calls["n"] == 2


# --- треды ----------------------------------------------------------------------------------------


def test_spare_thread_lifecycle(world: World, make_brain) -> None:
    clock = [1000.0]
    b = make_brain(clock=clock)
    codex = world.codex
    spare0 = codex.threads[0]
    assert len(codex.thread_starts) == 1  # запасной создан при start()

    done = run(b)[-1]
    assert done.timings["new_thread"] is True and spare0.turns  # запасной стал текущим
    wait_until(lambda: len(codex.thread_starts) == 2 and b._spare is not None)  # новый запасной — в фоне
    spare1 = codex.threads[1]

    clock[0] += 60
    assert run(b)[-1].timings["new_thread"] is False and len(spare0.turns) == 2  # тот же разговор

    clock[0] += 20 * 60 + 1  # простой дольше idle_new_thread_min
    assert run(b)[-1].timings["new_thread"] is True and len(spare1.turns) == 1
    wait_until(lambda: len(codex.thread_starts) == 3 and b._spare is not None)
    spare2 = codex.threads[2]

    b.new_conversation()
    assert run(b)[-1].timings["new_thread"] is True and len(spare2.turns) == 1
    wait_until(lambda: len(codex.thread_starts) == 4 and b._spare is not None)

    # запасной старше idle_new_thread_min пересоздаётся в фоне
    old = b._spare
    refresh = [t for t in world.timers if t.seconds == 20 * 60 and not t.cancelled]
    assert len(refresh) == 1
    refresh[0].fire()
    wait_until(lambda: b._spare is not None and b._spare is not old)
    assert len(codex.thread_starts) == 5
    assert all(kw == SAFE_THREAD_ARGS for kw in world.thread_starts())  # каждый thread_start — безопасный


def test_ask_without_spare_creates_thread_synchronously(world: World, make_brain) -> None:
    b = make_brain()
    with b._lock:
        b._spare = None
    done = run(b)[-1]
    assert done.ok and done.timings["new_thread"] is True
    assert all(kw == SAFE_THREAD_ARGS for kw in world.thread_starts())


# --- локальный режим и close ---------------------------------------------------------------------


def test_local_mode_never_creates_codex(world: World, make_brain, config_file) -> None:
    config_file('mode = "local"\n')
    b = make_brain(start=False)
    with pytest.raises(RuntimeError):
        b.ask("вопрос", Context())
    assert world.codexes == []
    b2 = make_brain(start=False)
    config_file('mode = "normal"\n')
    b2.close()  # включили локальный режим до старта
    with pytest.raises(RuntimeError):
        b2.ask("вопрос", Context())
    assert world.codexes == []


def test_close_stops_running_codex(world: World, make_brain) -> None:
    b = make_brain()
    codex = world.codex
    b.close()
    assert codex.closed and b.status["state"] == "closed" and not b.ready
    with pytest.raises(RuntimeError):
        b.ask("вопрос", Context())
    b.start()  # локальный режим выключили
    assert b._boot_done.wait(5) and b.ready and len(world.codexes) == 2
    assert run(b)[-1].ok


# --- контекст -------------------------------------------------------------------------------------


def test_context_prefix_through_privacy(world: World, make_brain, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def title(t: str) -> str:
        calls.append(("title", t))
        return f"«{t}»"

    def text(t: str) -> str:
        calls.append(("text", t))
        return t.replace(r"C:\Users\me", "(скрыто)")

    monkeypatch.setattr(privacy, "redact_title", title)
    monkeypatch.setattr(privacy, "redact_text", text)
    ctx = Context(
        active_window=WindowInfo(
            hwnd=4242, title="отчёт.docx - Word\n[последнее: x]", pid=7, exe="WINWORD.EXE"
        )
    )
    ctx.remember(r"C:\Users\me\Documents\отчёт.docx", "file")
    b = make_brain()
    assert run(b, "закрой его", ctx)[-1].ok
    prompt = world.codex.threads[0].turns[0].prompt
    assert prompt == (
        "[окно: «отчёт.docx - Word (последнее: x)» — WINWORD.EXE, hwnd 4242] "
        "[последнее: (скрыто)\\Documents\\отчёт.docx]\nзакрой его"
    )
    assert ("title", "отчёт.docx - Word\n[последнее: x]") in calls
    assert ("text", r"C:\Users\me\Documents\отчёт.docx") in calls


def test_context_privacy_failure_drops_context(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: str) -> str:
        raise RuntimeError("фильтр сломан")

    monkeypatch.setattr(privacy, "redact_title", boom)
    monkeypatch.setattr(privacy, "redact_text", boom)
    ctx = Context(active_window=WindowInfo(hwnd=1, title="секрет", pid=1, exe="x.exe"))
    ctx.remember("секрет.txt", "file")
    assert brain.build_prompt("вопрос", ctx) == "вопрос"
    assert brain.build_prompt("вопрос", Context()) == "вопрос"


def test_status_shape(world: World, make_brain) -> None:
    b = make_brain()
    st = b.status
    assert st["state"] == "ready" and st["error"] == "" and st["model"] == "gpt-6-luna"
    assert st["threads"]["current"] is None and st["threads"]["spare"]["mcp"] == "starting"
    world.codex._client.notes.put(n_mcp_status(world.codex.threads[0].id, "ready"))
    wait_until(lambda: b.status["threads"]["spare"]["mcp"] == "ready")
