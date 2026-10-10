"""pc.system: блокировка, питание, буфер обмена, ввод текста; и соответствие действий таблице политики."""

import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from pc import _input, confirm_client, policy, privacy, procs, subproc, system, windows
from pc.result import Caller, Result, fail, ok

SRC = Path(__file__).resolve().parents[1] / "src" / "pc"
OWN_PID = 4242

# --- каждое действие модулей S2a (файлы/система/звук/медиа) — строка таблицы политики ------------

EXPECTED_ACTIONS = {
    "files.py": {
        "find_files",
        "open_app",
        "open_folder",
        "open_file",
        "open_executable",
        "open_url",
        "trash",
        "read_text",
    },
    "system.py": {"lock", "power", "clipboard_get", "clipboard_set", "type_text"},
    "audio.py": {"volume_get", "volume"},
    "media.py": {"media"},
}


@pytest.mark.parametrize("module", sorted(EXPECTED_ACTIONS))
def test_actions_map_to_policy_table(module: str) -> None:
    text = (SRC / module).read_text(encoding="utf-8")
    used = set(re.findall(r"policy\.(?:require|check|decide)\(\s*\"([a-z_]+)\"", text))
    assert used == EXPECTED_ACTIONS[module]
    assert used <= set(policy.TABLE)


# --- фейки ---------------------------------------------------------------------------------------


class FakeSystemApi:
    def __init__(self) -> None:
        self.locked = 0
        self.suspended = 0
        self.clipboard: str | None = "текст"
        self.busy = 0
        self.clip_calls = 0
        self.fg = 0
        self.classes: dict[int, str] = {}
        self.elevated: set[int] = set()
        self.mods = 0
        self.ok = True

    def lock_workstation(self) -> bool:
        self.locked += 1
        return self.ok

    def suspend(self) -> bool:
        self.suspended += 1
        return self.ok

    def clipboard_get(self) -> str | None:
        self.clip_calls += 1
        if self.busy:
            self.busy -= 1
            raise system.ClipboardBusy()
        return self.clipboard

    def clipboard_set(self, text: str) -> None:
        self.clip_calls += 1
        if self.busy:
            self.busy -= 1
            raise system.ClipboardBusy()
        self.clipboard = text

    def foreground(self) -> int:
        return self.fg

    def class_name(self, hwnd: int) -> str:
        return self.classes.get(hwnd, "Notepad")

    def is_elevated(self, pid: int) -> bool:
        return pid in self.elevated

    def modifiers_down(self) -> bool:
        if self.mods:
            self.mods -= 1
            return True
        return False


class Confirms:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, summary: str, details: str, caller: Caller) -> bool:
        self.asked.append((summary, details, caller))
        return self.answer


WINDOWS = {
    100: windows.WindowInfo(100, "Безымянный — Блокнот", 1000, "Notepad.exe"),
    200: windows.WindowInfo(200, "Командная строка", 2000, "cmd.exe"),
    201: windows.WindowInfo(201, "Windows PowerShell", 2001, "powershell.exe"),
    202: windows.WindowInfo(202, "PowerShell 7", 2002, "pwsh.exe"),
    203: windows.WindowInfo(203, "Терминал", 2003, "WindowsTerminal.exe"),
    204: windows.WindowInfo(204, "Ubuntu", 2004, "wsl.exe"),
    205: windows.WindowInfo(205, "Консоль", 2005, "conhost.exe"),
    206: windows.WindowInfo(206, "Консоль", 2006, "OpenConsole.exe"),
    207: windows.WindowInfo(207, "bash", 2007, "bash.exe"),
    208: windows.WindowInfo(208, "Терминал", 2008, "wt.exe"),
    300: windows.WindowInfo(300, "Выполнить", 3000, "explorer.exe"),
    301: windows.WindowInfo(301, "Run", 3001, "explorer.exe"),
    302: windows.WindowInfo(302, "Сохранить как", 1000, "Notepad.exe"),
    400: windows.WindowInfo(400, "Jarvis", OWN_PID, "Jarvis.exe"),
    500: windows.WindowInfo(500, "Редактор реестра", 5000, "regedit.exe"),
    600: windows.WindowInfo(600, "Чужой терминал", 6000, "someapp.exe"),
}


class Env:
    def __init__(self, api: FakeSystemApi, confirms: Confirms) -> None:
        self.api = api
        self.confirms = confirms
        self.found: list[Any] = []
        self.focused: list[int] = []
        self.focus_result: Result | None = None
        self.typed: list[str] = []
        self.switch_after: int | None = None
        self.accept = True


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    api = FakeSystemApi()
    api.classes.update({300: "#32770", 301: "#32770", 302: "#32770", 600: "CASCADIA_HOSTING_WINDOW_CLASS"})
    api.elevated.add(5000)
    e = Env(api, Confirms(True))
    monkeypatch.setattr(system, "_api", api)
    monkeypatch.setattr(system, "CHUNK_PAUSE_S", 0)
    confirm_client.set_confirm_handler(e.confirms)
    monkeypatch.setattr(procs, "own_pids", lambda: {OWN_PID, OWN_PID + 1})
    monkeypatch.setattr(privacy, "redact_text", lambda t: t.replace("секрет", "(скрыто)"))
    monkeypatch.setattr(privacy, "redact_title", lambda t: t[:80])

    def find_window(target: str | int) -> windows.WindowInfo | None:
        e.found.append(target)
        if isinstance(target, int):
            return WINDOWS.get(target)
        return next((w for w in WINDOWS.values() if target.casefold() in w.title.casefold()), None)

    def focus(hwnd: int) -> Result:
        e.focused.append(hwnd)
        if e.focus_result is not None:
            return e.focus_result
        api.fg = hwnd
        return ok("Переключился")

    def send_text(text: str) -> int:
        e.typed.append(text)
        if e.switch_after is not None and len(e.typed) >= e.switch_after:
            api.fg = 999
        return len(_input.text_inputs(text)) if e.accept else 0

    monkeypatch.setattr(windows, "find_window", find_window)
    monkeypatch.setattr(windows, "focus", focus)
    monkeypatch.setattr(_input, "send_text", send_text)
    return e


# --- блокировка и питание ------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["user", "brain"])
def test_lock_without_confirm(env: Env, caller: Caller) -> None:
    r = system.lock(caller)
    assert r.ok
    assert r.text == "Заблокировал компьютер"
    assert env.api.locked == 1
    assert env.confirms.asked == []


def test_lock_failed(env: Env) -> None:
    env.api.ok = False
    assert not system.lock("user").ok


class FakeRun:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], timeout: float, cwd: Any = None, env: Any = None
    ) -> subproc.Completed:
        self.calls.append([str(a) for a in argv])
        return subproc.Completed(self.code, b"", b"")


@pytest.mark.parametrize(
    ("action", "question", "flag"),
    [("shutdown", "Выключить компьютер?", "/s"), ("restart", "Перезагрузить компьютер?", "/r")],
)
@pytest.mark.parametrize("caller", ["user", "brain"])
def test_power_shutdown_restart(
    env: Env, monkeypatch: pytest.MonkeyPatch, action: str, question: str, flag: str, caller: Caller
) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    run = FakeRun()
    monkeypatch.setattr(subproc, "run", run)
    r = system.power(action, caller)  # type: ignore[arg-type]
    assert r.ok
    assert env.confirms.asked == [(question, "", caller)]
    assert run.calls == [[r"C:\Windows\System32\shutdown.exe", flag, "/t", "0"]]


def test_power_declined(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.confirms.answer = False
    run = FakeRun()
    monkeypatch.setattr(subproc, "run", run)
    assert not system.power("shutdown", "user").ok
    assert not system.power("sleep", "user").ok
    assert run.calls == []
    assert env.api.suspended == 0


def test_power_without_handler(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    confirm_client.set_confirm_handler(None)
    run = FakeRun()
    monkeypatch.setattr(subproc, "run", run)
    assert not system.power("restart", "brain").ok
    assert run.calls == []


def test_power_sleep(env: Env) -> None:
    r = system.power("sleep", "user")
    assert r.ok
    assert env.api.suspended == 1
    assert env.confirms.asked[0][0] == "Перевести компьютер в сон?"


def test_power_errors(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subproc, "run", FakeRun(code=1190))
    assert not system.power("shutdown", "user").ok
    assert not system.power("hibernate", "user").ok  # type: ignore[arg-type]


def test_shutdown_exe_ignores_odd_systemroot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"..\evil")
    assert system.shutdown_exe() == r"C:\Windows\System32\shutdown.exe"
    monkeypatch.delenv("SYSTEMROOT")
    assert system.shutdown_exe() == r"C:\Windows\System32\shutdown.exe"


# --- буфер обмена --------------------------------------------------------------------------------


def test_clipboard_get_user(env: Env) -> None:
    env.api.clipboard = "строка 1\r\nстрока 2"
    r = system.clipboard_get("user")
    assert r.ok
    assert r.data == "строка 1\nстрока 2"
    assert r.text == "В буфере: строка 1⏎строка 2"
    assert env.confirms.asked == []


def test_clipboard_get_brain_asks_and_redacts(env: Env) -> None:
    env.api.clipboard = "пароль секрет"
    r = system.clipboard_get("brain")
    assert r.ok
    assert r.data == "пароль (скрыто)"
    assert env.confirms.asked == [("GPT просит прочитать буфер обмена (уйдёт в облако)", "", "brain")]


def test_clipboard_get_brain_declined(env: Env) -> None:
    env.confirms.answer = False
    assert not system.clipboard_get("brain").ok
    assert env.api.clip_calls == 0


def test_clipboard_busy_retries(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(system.time, "sleep", sleeps.append)
    env.api.busy = 4
    assert system.clipboard_get("user").ok
    assert env.api.clip_calls == 5
    assert sleeps == [0.05] * 4
    env.api.busy = 5
    env.api.clip_calls = 0
    r = system.clipboard_get("user")
    assert not r.ok
    assert env.api.clip_calls == 5


def test_clipboard_empty(env: Env) -> None:
    env.api.clipboard = None
    r = system.clipboard_get("user")
    assert r.ok
    assert r.data == ""


def test_clipboard_set_user(env: Env) -> None:
    r = system.clipboard_set("строка 1\nстрока 2", "user")
    assert r.ok
    assert r.text == "Скопировал в буфер обмена"
    assert env.api.clipboard == "строка 1\r\nстрока 2"


def test_clipboard_set_brain_text_visible(env: Env) -> None:
    text = "а" * 70 + "\nконец"
    r = system.clipboard_set(text, "brain")
    assert r.ok
    assert r.text == "⚙ буфер: " + "а" * 60 + "…"
    assert env.confirms.asked == []
    assert system.clipboard_set("коротко\nи ясно", "brain").text == "⚙ буфер: коротко⏎и ясно"


def test_clipboard_set_busy_and_empty(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system.time, "sleep", lambda s: None)
    env.api.busy = 2
    assert system.clipboard_set("x", "user").ok
    assert not system.clipboard_set("", "user").ok
    assert not system.clipboard_set("x" * 100_001, "user").ok


# --- ввод текста ---------------------------------------------------------------------------------


def test_type_text_user_denied(env: Env) -> None:
    r = system.type_text("привет", 100, "user")
    assert not r.ok
    assert env.found == []
    assert env.typed == []


def test_type_text_cur_target_denied(env: Env) -> None:
    for target in ("@cur", " @CUR ", ""):
        r = system.type_text("привет", target, "brain")
        assert not r.ok
        assert r.text == "Укажи окно: hwnd из контекста или list_windows"
    assert env.found == []


@pytest.mark.parametrize("target", [100, "100", " 100 "])
def test_type_text_hwnd_target(env: Env, target: Any) -> None:
    r = system.type_text("привет", target, "brain")
    assert r.ok, r.text
    assert env.found == [100]
    assert env.focused == [100]
    assert "".join(env.typed) == "привет"


def test_type_text_title_target(env: Env) -> None:
    r = system.type_text("привет", "Блокнот", "brain")
    assert r.ok
    assert env.found == ["Блокнот"]


def test_type_text_window_not_found(env: Env) -> None:
    r = system.type_text("привет", 777, "brain")
    assert not r.ok
    assert env.confirms.asked == []


@pytest.mark.parametrize("hwnd", [200, 201, 202, 203, 204, 205, 206, 207, 208, 600])
def test_type_text_consoles_denied(env: Env, hwnd: int) -> None:
    r = system.type_text("dir", hwnd, "brain")
    assert not r.ok
    assert r.text == "В консоли и терминалы текст не ввожу"
    assert env.confirms.asked == []
    assert env.typed == []


@pytest.mark.parametrize("hwnd", [300, 301])
def test_type_text_run_dialog_denied(env: Env, hwnd: int) -> None:
    r = system.type_text("cmd", hwnd, "brain")
    assert not r.ok
    assert "Выполнить" in r.text
    assert env.confirms.asked == []


def test_type_text_other_dialog_allowed(env: Env) -> None:
    assert system.type_text("отчёт", 302, "brain").ok


def test_type_text_jarvis_window_denied(env: Env) -> None:
    r = system.type_text("привет", 400, "brain")
    assert not r.ok
    assert "Jarvis" in r.text
    assert env.confirms.asked == []


def test_type_text_elevated_denied(env: Env) -> None:
    r = system.type_text("привет", 500, "brain")
    assert not r.ok
    assert "администратора" in r.text
    assert env.confirms.asked == []


def test_type_text_length(env: Env) -> None:
    assert not system.type_text("я" * 501, 100, "brain").ok
    assert env.confirms.asked == []
    assert system.type_text("я" * 500, 100, "brain").ok


@pytest.mark.parametrize("text", ["\x16", "a\x03b", "\x1b[2J", "\x08\x08"])
def test_type_text_control_chars_denied(env: Env, text: str) -> None:
    assert not system.type_text(text, 100, "brain").ok
    assert env.confirms.asked == []


def test_type_text_confirmation(env: Env) -> None:
    r = system.type_text("строка 1\r\nстрока 2\n", 100, "brain")
    assert r.ok
    assert env.confirms.asked == [
        ("Ввести текст в «Безымянный — Блокнот» (Notepad.exe)?", "строка 1⏎\nстрока 2⏎\n", "brain")
    ]
    assert "".join(env.typed) == "строка 1\nстрока 2\n"


def test_type_text_declined(env: Env) -> None:
    env.confirms.answer = False
    assert not system.type_text("привет", 100, "brain").ok
    assert env.focused == []
    assert env.typed == []


def test_type_text_chunks(env: Env) -> None:
    text = "😀ё" * 40  # 80 символов
    r = system.type_text(text, 100, "brain")
    assert r.ok
    assert [len(c) for c in env.typed] == [32, 32, 16]
    assert "".join(env.typed) == text
    assert r.data == {"typed": 80, "hwnd": 100}


def test_type_text_foreground_changed_mid_typing(env: Env) -> None:
    env.switch_after = 2
    r = system.type_text("x" * 100, 100, "brain")
    assert not r.ok
    assert r.text == "Окно сменилось — ввод остановлен"
    assert r.data == {"typed": 64}
    assert len(env.typed) == 2


def test_type_text_focus_failed(env: Env) -> None:
    env.focus_result = fail("Не удалось переключиться на «Безымянный — Блокнот»")
    r = system.type_text("привет", 100, "brain")
    assert not r.ok
    assert env.typed == []


def test_type_text_sendinput_blocked(env: Env) -> None:
    env.accept = False
    r = system.type_text("привет", 100, "brain")
    assert not r.ok
    assert r.data == {"typed": 0}


def test_type_text_waits_for_modifiers(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system.time, "sleep", lambda s: None)
    env.api.mods = 3
    assert system.type_text("привет", 100, "brain").ok
    env.api.mods = 10**9
    monkeypatch.setattr(system, "MODIFIERS_WAIT_S", 0)
    r = system.type_text("привет", 100, "brain")
    assert not r.ok
    assert "Ctrl" in r.text


# --- настоящий Windows (CI) ----------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="нужен Windows")
def test_real_api_read_only_calls() -> None:
    api = system._Api()
    assert isinstance(api.foreground(), int)
    assert isinstance(api.modifiers_down(), bool)
    assert isinstance(api.is_elevated(os.getpid()), bool)
    assert api.is_elevated(0) is True  # не проверить — считаем повышенными
    assert api.class_name(0) == ""
    assert os.path.isfile(system.shutdown_exe())
