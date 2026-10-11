"""S8, ревью безопасности (снимок 5ffbbd2): находки F01–F12 и вторая линия защиты от запросов codex.

Каждый тест падал на коде до исправления: локальный режим (гонка), под-агенты codex, «последнее» и память при
битом конфиге, строка «⚙ буфер», Tab в подтверждении ввода, активные типы файлов, data ответов MCP, IDN-хост,
оболочки под другими именами, журнал без текста, терминалы, трамплин dev-запуска. Данные — выдуманные.
"""

import asyncio
import json
import threading
import time
import types
from typing import Any

import pytest
from openai_codex import client as sdk_client

from jarvis import brain, journal, memory
from jarvis.config import BrainConfig
from jarvis.context import Context
from jarvis.ui import logic
from pc import _input, apps, confirm_client, files, mcp, paths, privacy, procs, system, windows
from pc.windows import WindowInfo
from test_brain import World, n_completed, n_item, run, statuses, wait_until

WIN_ENV = {
    "SystemRoot": r"C:\Windows",
    "windir": r"C:\Windows",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "ProgramW6432": r"C:\Program Files",
    "ProgramData": r"C:\ProgramData",
    "USERPROFILE": r"C:\Users\me",
    "APPDATA": r"C:\Users\me\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
}


@pytest.fixture
def win_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Зоны Windows на Linux; каталог данных остаётся во tmp (его ставит conftest)."""
    for name in paths._ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in WIN_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(paths, "_resolve", lambda p: p)
    monkeypatch.setattr(paths, "_known_folder", lambda name: None)


@pytest.fixture
def private(config_file: Any) -> Any:
    def write(*items: str) -> None:
        body = ", ".join(json.dumps(p, ensure_ascii=False) for p in items)
        config_file(f"[pc]\nprivate_paths = [{body}]\n")

    return write


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(brain, "_new_codex", w.make)
    monkeypatch.setattr(brain, "_start_timer", w.timer)
    monkeypatch.setattr(privacy, "redact_title", lambda title: title)
    monkeypatch.setattr(privacy, "redact_text", lambda text: text)
    monkeypatch.setattr(sdk_client, "subprocess", sdk_client.subprocess)
    return w


# --- F01: «Локально» включили, пока вопрос ждал замок — Codex не поднимается заново -----------------------


def test_f01_local_toggle_while_ask_waits_for_lock(world: World) -> None:
    b = brain.Brain(BrainConfig(), "jarvis-confirm-test")
    try:
        b.start()
        wait_until(lambda: b.ready)
        b._ask_lock.acquire()  # предыдущий ход ещё держит замок
        gen = b.ask("секретный вопрос", Context())  # режим ещё normal — проверка local пройдена
        events: list[object] = []
        t = threading.Thread(target=lambda: events.extend(gen), daemon=True)
        t.start()
        time.sleep(0.2)
        b.close()  # человек включил «Локально» в трее
        b._ask_lock.release()
        t.join(10)
        prompts = [turn.prompt for c in world.codexes for th in c.threads for turn in th.turns]
        assert not any("секретный" in p for p in prompts), prompts
        assert len(world.codexes) == 1 and b._local
    finally:
        b.close()


# --- F09: под-агент codex — ход прерывается, человек видит строку -----------------------------------------


def _spawn(status: str) -> dict[str, Any]:
    return {
        "type": "collabAgentToolCall",
        "id": "collab-1",
        "receiverThreadIds": ["thread-sub"],
        "senderThreadId": "thread-main",
        "status": status,
        "tool": "spawnAgent",
        "agentsStates": {},
        "prompt": "сделай в фоне",
    }


def test_f09_spawn_agent_aborts_turn(world: World) -> None:
    world.script = lambda turn: [
        n_item(turn, "item/started", _spawn("inProgress")),
        n_item(turn, "item/completed", _spawn("completed")),
        n_completed(turn, "completed"),
    ]
    b = brain.Brain(BrainConfig(), "jarvis-confirm-test")
    try:
        events = run(b, "вопрос", Context())
    finally:
        b.close()
    shown = [s.text for s in statuses(events) if s.key != "brain-start"]
    done = events[-1]
    assert any("под-агента" in t for t in shown) and not done.ok and "под-агента" in done.text


def test_instructions_forbid_subagents() -> None:
    assert "Под-агентов не запускай" in brain.DEVELOPER_INSTRUCTIONS


# --- вторая линия: запросы codex к клиенту — отказ (у SDK по умолчанию «accept») --------------------------


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        ("execCommandApproval", {"decision": "denied"}),
        ("applyPatchApproval", {"decision": "denied"}),
        ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
        ("mcpServer/elicitation/request", {"action": "decline"}),
        ("item/tool/requestUserInput", {"answers": {}}),
        ("item/tool/call", {"contentItems": [], "success": False}),
        ("что-то/новое", {}),
    ],
)
def test_server_requests_denied(method: str, expected: dict[str, Any]) -> None:
    assert brain.deny_server_request(method, {"threadId": "t"}) == expected


def test_server_decisions_are_copies() -> None:
    brain.deny_server_request("item/tool/requestUserInput", None)["answers"]["x"] = 1
    assert brain.SERVER_DECISIONS["item/tool/requestUserInput"] == {"answers": {}}


def test_current_time_request_answered() -> None:
    out = brain.deny_server_request("currentTime/read", None)
    assert abs(out["currentTimeAt"] - time.time()) < 5


def test_new_codex_installs_deny_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai_codex

    class FakeCodex:
        def __init__(self, config: Any) -> None:
            self._client = types.SimpleNamespace(_approval_handler=lambda m, p: {"decision": "accept"})

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    codex = brain._new_codex(object())
    assert codex._client._approval_handler is brain.deny_server_request


def test_sdk_client_still_has_approval_handler_slot() -> None:
    """Защита опирается на внутренность SDK 0.160.1: атрибут есть и вызывается на каждый запрос сервера."""
    client = sdk_client.CodexClient()
    assert client._approval_handler == client._default_approval_handler
    client._approval_handler = brain.deny_server_request
    msg = {"id": 1, "method": "item/fileChange/requestApproval", "params": {}}
    assert client._handle_server_request(msg) == {"decision": "decline"}


# --- F03: «последнее» и память при битом конфиге ---------------------------------------------------------

TITLE = "Acme — условия сделки.docx - Word"


def _ctx() -> Context:
    ctx = Context(active_window=WindowInfo(101, TITLE, 7, "WINWORD.EXE"))
    ctx.remember(TITLE, "window", 101)  # «переключись на ворд»
    return ctx


def test_f03_last_window_title_hidden_like_active_window(win_env: None, private: Any) -> None:
    private(r"D:\Clients\Acme")
    prompt = brain.build_prompt("что тут происходит?", _ctx())
    assert f"[окно: {privacy.HIDDEN} — WINWORD.EXE, hwnd 101]" in prompt
    assert "условия сделки" not in prompt, prompt


def test_f03_last_window_title_hidden_when_config_broken(win_env: None, config_file: Any) -> None:
    config_file("[pc\nprivate_paths = [")
    prompt = brain.build_prompt("что тут происходит?", _ctx())
    assert "условия сделки" not in prompt, prompt


def test_f03_memory_private_name_when_config_broken(win_env: None, private: Any, config_file: Any) -> None:
    private(r"D:\Clients\Acme")
    memory.remember("проект Acme сдать в пятницу")
    config_file("[pc\nprivate_paths = [")  # конфиг сломался после записи заметки
    prompt = brain.build_prompt("что по проекту?", None)
    assert "Acme" not in prompt, prompt


def test_f03_memory_still_sent_with_good_config(win_env: None, private: Any) -> None:
    private(r"D:\Clients\Acme")
    memory.remember("люблю короткие ответы")
    assert "[о пользователе: люблю короткие ответы]" in brain.build_prompt("привет", None)


# --- F04: строка «⚙ буфер» показывает хвост и переводы строк ----------------------------------------------


def _shown(payload: str) -> str:
    return logic.display_text(brain.tool_label("clipboard_set", {"text": payload}))


def test_f04_clipboard_label_marks_hidden_tail() -> None:
    payload = "Вот ссылка на документ, которую ты просил: " + "x" * 100 + "\nХВОСТ, которого не видно"
    shown = _shown(payload)
    assert "…" in shown and "ещё" in shown, shown


def test_f04_clipboard_label_shows_line_breaks() -> None:
    assert "⏎" in _shown("первая строка\nвторая строка\n")


# --- F05: Tab в подтверждении ввода — видимый знак ---------------------------------------------------------


class FakeSystemApi:
    def __init__(self, cls: str = "Notepad") -> None:
        self.cls = cls

    def class_name(self, hwnd: int) -> str:
        return self.cls

    def is_elevated(self, pid: int) -> bool:
        return False


def _type_details(monkeypatch: pytest.MonkeyPatch, text: str) -> str:
    monkeypatch.setattr(system, "_api", FakeSystemApi())
    monkeypatch.setattr(procs, "own_pids", lambda: {1})
    win = WindowInfo(500, "Без имени — Блокнот", 42, "notepad.exe")
    monkeypatch.setattr(windows, "find_window", lambda key: win)
    seen: list[str] = []
    confirm_client.set_confirm_handler(lambda s, d, c: seen.append(d) or False)
    system.type_text(text, 500, "brain")
    return logic.display_text(seen[0])


def test_f05_tab_is_a_key_press() -> None:
    assert _input.text_inputs("\t")[0].ki.wVk == _input.VK_TAB


def test_f05_tab_visible_in_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    tabs = _type_details(monkeypatch, "ок\t\t\n")
    spaces = _type_details(monkeypatch, "ок        \n")
    assert tabs != spaces and "⇥" in tabs


# --- F11: эмуляторы терминала — отказ до подтверждения ---------------------------------------------------


@pytest.mark.parametrize(
    ("exe", "cls"),
    [
        ("alacritty.exe", "Window Class"),
        ("wezterm-gui.exe", "org.wezfurlong.wezterm"),
        ("ConEmu64.exe", "VirtualConsoleClass"),
        ("putty.exe", "PuTTY"),
        ("Tabby.exe", "Chrome_WidgetWin_1"),
    ],
)
def test_f11_terminal_refused_before_confirmation(
    monkeypatch: pytest.MonkeyPatch, exe: str, cls: str
) -> None:
    monkeypatch.setattr(system, "_api", FakeSystemApi(cls))
    monkeypatch.setattr(procs, "own_pids", lambda: {1})
    monkeypatch.setattr(windows, "find_window", lambda key: WindowInfo(500, "терминал", 42, exe))
    asked: list[str] = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append(s) or False)
    r = system.type_text("dir", 500, "brain")
    assert not asked and not r.ok


# --- F08: «активные» типы файлов — спросить -------------------------------------------------------------


class FakeFilesApi:
    def __init__(self, existing: set[str] = frozenset()) -> None:
        self.started: list[str] = []
        self.existing = {p.casefold() for p in existing}

    def startfile(self, target: str) -> None:
        self.started.append(target)

    def allow_set_foreground(self) -> None:
        pass

    def is_dir(self, path: str) -> bool:
        return False

    def exists(self, path: str) -> bool:
        return path.casefold() in self.existing


@pytest.mark.parametrize(
    "name",
    ["setup.py", "run.pyw", "app.pyz", "install.sh", "dark.theme", "pack.themepack", "office.rdp",
     "shared.searchConnector-ms", "box.wsb", "addin.xll", "disk.iso"],
)  # fmt: skip
def test_f08_brain_open_active_type_asks(win_env: None, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    path = rf"C:\Users\me\Downloads\{name}"
    monkeypatch.setattr(files, "_api", FakeFilesApi({path}))
    monkeypatch.setattr(paths, "_is_dir", lambda canon, is_dir: False)
    monkeypatch.setattr(files.apps, "run_sta", lambda fn, *a: fn(*a))
    asked: list[str] = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append(s) or False)
    files.open_target(path, None, "brain")
    assert asked, f"{path}: открыт без подтверждения"


# --- F02: data ответов MCP — через privacy ---------------------------------------------------------------


def test_f02_list_apps_data_hides_private_name(win_env: None, private: Any) -> None:
    private(r"D:\Clients\Acme")
    apps.set_inventory(
        [{"name": "Acme Портал", "app_id": "acme.portal"}, {"name": "Telegram", "app_id": "tg"}]
    )
    try:
        out = asyncio.run(mcp.list_apps())
    finally:
        apps.set_inventory(None)
    assert "Acme" not in out and "Telegram" in out, out


def test_f02_list_windows_exe_hides_private_name(
    win_env: None, private: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    private(r"D:\Tools\AcmeVault")
    monkeypatch.setattr(windows, "list_windows", lambda: [WindowInfo(5, "Главное окно", 77, "AcmeVault.exe")])
    out = asyncio.run(mcp.list_windows())
    assert "AcmeVault" not in out, out


def test_f02_kill_many_hides_private_name(
    win_env: None, private: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    private(r"D:\Tools\AcmeVault")
    groups = [procs.ProcGroup("AcmeVault.exe", 1, [77]), procs.ProcGroup("AcmeVaultSync.exe", 1, [78])]
    monkeypatch.setattr(procs, "find_processes", lambda name: groups)
    out = asyncio.run(mcp.kill_process("acme"))
    assert "AcmeVault" not in out, out


# --- F06: IDN-хост в подтверждении ссылки — punycode ----------------------------------------------------


def test_f06_idn_host_shown_as_punycode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "_api", FakeFilesApi())
    seen: list[tuple[str, str]] = []
    confirm_client.set_confirm_handler(lambda s, d, c: seen.append((s, d)) or False)
    files.open_target("https://аpple.com/login", "url", "brain")  # «а» — кириллица
    summary, details = seen[0]
    assert "xn--" in summary + details


def test_f06_ascii_url_details_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "_api", FakeFilesApi())
    seen: list[str] = []
    confirm_client.set_confirm_handler(lambda s, d, c: seen.append(d) or False)
    files.open_target("https://example.com/a?b=1", "url", "brain")
    assert seen == ["https://example.com/a?b=1"]


# --- F07: оболочки под другими именами — отказ -----------------------------------------------------------

SHELLS = [
    {"name": "Командная строка", "app_id": r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\cmd.exe"},
    {
        "name": "Windows PowerShell",
        "app_id": r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe",
    },
    {"name": "Терминал", "app_id": "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"},
    {"name": "Telegram", "app_id": "TelegramDesktop"},
]


@pytest.fixture
def shell_apps(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = FakeFilesApi()
    monkeypatch.setattr(files, "_api", fake)
    monkeypatch.setattr(apps, "run_sta", lambda fn, *a: fn(*a))
    apps.set_inventory(SHELLS)
    confirm_client.set_confirm_handler(lambda s, d, c: True)
    yield fake
    apps.set_inventory(None)


@pytest.mark.parametrize("name", ["cmd", "консоль", "командная строка", "Windows PowerShell", "терминал"])
def test_f07_shell_by_other_name_refused(shell_apps: FakeFilesApi, name: str) -> None:
    r = files.open_target(name, "app", "brain")
    assert not r.ok and not shell_apps.started, f"«{name}» → {shell_apps.started}"


def test_f07_regular_app_still_opens(shell_apps: FakeFilesApi) -> None:
    r = files.open_target("Telegram", "app", "brain")
    assert r.ok and shell_apps.started


# --- F10: журнал без текста при store_text = false -------------------------------------------------------

RECORDS = [
    {"text": "открой C:\\Users\\me\\Documents\\Acme договор.docx", "tool": "open",
     "args": {"target": "C:\\Users\\me\\Documents\\Acme договор.docx", "kind": "file"},
     "ok": True, "error": ""},
    {"text": "открой договор Acme", "tool": "clarify",
     "args": {"question": "Какой договор Acme открыть?"}, "ok": True, "error": ""},
    {"text": "закрой окно Acme переписка", "tool": "close", "args": {"target": "Acme переписка"},
     "ok": False, "error": "Не нашёл окно «Acme переписка»."},
]  # fmt: skip


@pytest.mark.parametrize("rec", RECORDS, ids=["target", "question", "error"])
def test_f10_no_user_text_without_store_text(config_file: Any, rec: dict[str, Any]) -> None:
    config_file("[journal]\nstore_text = false\n")
    journal.Journal._save(dict(rec, ts="2026-10-11T12:00:00.000+00:00"))
    line = (journal.journal_dir() / "2026-10-11.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    saved = json.loads(line)
    assert "text" not in saved and "Acme" not in line, line


# --- F12: трамплин dev-запуска над корнем — «свой» -------------------------------------------------------


class FakeProc:
    def __init__(self, tree: "Tree", pid: int) -> None:
        self.tree, self.pid = tree, pid
        name, _ppid = tree.procs[pid]
        self.info = {"pid": pid, "name": name, "exe": rf"C:\x\{name}"}

    def name(self) -> str:
        return self.info["name"]

    def children(self, recursive: bool = False) -> list["FakeProc"]:
        out, todo = [], [self.pid]
        while todo:
            p = todo.pop()
            for pid, (_n, ppid) in self.tree.procs.items():
                if ppid == p:
                    out.append(FakeProc(self.tree, pid))
                    if recursive:
                        todo.append(pid)
        return out

    def parents(self) -> list["FakeProc"]:
        out, p = [], self.tree.procs[self.pid][1]
        while p in self.tree.procs:
            out.append(FakeProc(self.tree, p))
            p = self.tree.procs[p][1]
        return out

    def terminate(self) -> None:
        self.tree.terminated.append(self.pid)

    def kill(self) -> None:
        self.tree.terminated.append(self.pid)


class Tree:
    def __init__(self) -> None:
        self.procs = {  # pid → (exe, ppid)
            1: ("explorer.exe", 0),
            9: ("uv.exe", 1),
            10: ("jarvis.exe", 9),  # трамплин console_scripts
            11: ("python.exe", 10),  # .venv\Scripts\python.exe
            12: ("python.exe", 11),  # сам Jarvis — JARVIS_ROOT_PID
            20: ("codex.exe", 12),
            30: ("python.exe", 20),  # pc.mcp
        }
        self.terminated: list[int] = []

    def module(self) -> types.SimpleNamespace:
        class Error(Exception):
            pass

        return types.SimpleNamespace(
            Error=Error,
            NoSuchProcess=type("NoSuchProcess", (Error,), {}),
            AccessDenied=type("AccessDenied", (Error,), {}),
            pid_exists=lambda pid: pid in self.procs,
            Process=lambda pid: FakeProc(self, pid),
            process_iter=lambda attrs=None: [FakeProc(self, pid) for pid in self.procs],
            wait_procs=lambda procs_, timeout=None: (procs_, []),
        )


@pytest.fixture
def tree(monkeypatch: pytest.MonkeyPatch) -> Tree:
    t = Tree()
    monkeypatch.setattr(procs, "psutil", t.module())
    monkeypatch.setattr(
        procs, "os", types.SimpleNamespace(getpid=lambda: 30, environ={"JARVIS_ROOT_PID": "12"})
    )
    monkeypatch.setattr(procs.apps, "lookup", lambda name: (None, 0.0))
    procs._own_cache.clear()
    confirm_client.set_confirm_handler(lambda s, d, c: True)  # человек нажал «да»
    yield t
    procs._own_cache.clear()


def test_f12_brain_cannot_kill_jarvis_launcher(tree: Tree) -> None:
    r = procs.kill("jarvis", "brain")
    assert tree.terminated == [] and not r.ok and r.text == procs.SELF_TEXT


def test_f12_explorer_above_launcher_is_not_own(tree: Tree) -> None:
    own = procs.own_pids()
    assert {10, 11, 12, 20, 30} <= own and 1 not in own and 9 not in own
