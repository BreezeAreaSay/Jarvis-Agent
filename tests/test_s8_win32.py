"""S8, Win32: находки ревью (до исправления падали): job, свои процессы, зависшие окна, звук, буфер."""

import os
import sys
import threading
import time
import types
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, ClassVar

import psutil
import pytest

from jarvis import winapp
from pc import apps, audio, confirm_client, procs, windows

# --- F2: всё, что Jarvis открыл через os.startfile, считается «самим Jarvis» --------------------------

ME = os.getpid()
SHELL = 900_000  # explorer.exe
ROOT = 900_001  # Jarvis.exe
CODEX = 900_002  # codex.exe
WORD = 900_010  # WINWORD.EXE — человек сказал «открой отчёт.docx»; ShellExecute создал его потомком Jarvis
NOTEPAD = 900_020  # notepad++.exe — открыл GPT: потомок pc.mcp


class FakeProc:
    def __init__(self, ps: "FakePsutil", pid: int, name: str, ppid: int) -> None:
        self.ps, self.pid, self._name, self.ppid = ps, pid, name, ppid
        self.alive = True

    @property
    def info(self) -> dict[str, Any]:
        return {"pid": self.pid, "name": self._name, "exe": f"C:\\X\\{self._name}"}

    def name(self) -> str:
        return self._name

    def terminate(self) -> None:
        self.alive = False

    def kill(self) -> None:
        self.alive = False

    def children(self, recursive: bool = False) -> list["FakeProc"]:
        direct = [p for p in self.ps.procs.values() if p.ppid == self.pid and p.alive]
        out = list(direct)
        if recursive:
            for c in direct:
                out.extend(c.children(recursive=True))
        return out

    def parents(self) -> list["FakeProc"]:
        out, p = [], self.ps.procs.get(self.ppid)
        while p is not None:
            out.append(p)
            p = self.ps.procs.get(p.ppid)
        return out


class FakePsutil:
    Error = psutil.Error
    NoSuchProcess = psutil.NoSuchProcess
    AccessDenied = psutil.AccessDenied

    def __init__(self) -> None:
        self.procs: dict[int, FakeProc] = {}

    def add(self, pid: int, name: str, ppid: int) -> FakeProc:
        self.procs[pid] = FakeProc(self, pid, name, ppid)
        return self.procs[pid]

    def process_iter(self, attrs: list[str]) -> list[FakeProc]:
        return [p for p in self.procs.values() if p.alive]

    def Process(self, pid: int) -> FakeProc:
        p = self.procs.get(pid)
        if p is None or not p.alive:
            raise psutil.NoSuchProcess(pid)
        return p

    def pid_exists(self, pid: int) -> bool:
        return pid in self.procs and self.procs[pid].alive

    def wait_procs(self, plist: list[FakeProc], timeout: float) -> tuple[list[FakeProc], list[FakeProc]]:
        return [p for p in plist if not p.alive], [p for p in plist if p.alive]


@pytest.fixture
def tree(monkeypatch: pytest.MonkeyPatch) -> FakePsutil:
    ps = FakePsutil()
    ps.add(SHELL, "explorer.exe", 0)
    ps.add(ME, "Jarvis.exe", SHELL)  # процесс Jarvis (grammar/руки зовут pc прямо в нём)
    ps.add(CODEX, "codex.exe", ME)
    ps.add(900_003, "python.exe", CODEX)  # pc.mcp
    ps.add(WORD, "WINWORD.EXE", ME)  # os.startfile(отчёт.docx) в процессе Jarvis
    ps.add(NOTEPAD, "notepad++.exe", 900_003)  # os.startfile(...) в pc.mcp по просьбе GPT
    monkeypatch.setattr(procs, "psutil", ps)
    monkeypatch.setattr(procs, "_own_cache", {})
    monkeypatch.delenv(procs.ROOT_ENV, raising=False)
    monkeypatch.setattr(apps, "_inv", None)
    apps.set_inventory([])
    return ps


def test_app_opened_by_jarvis_is_not_jarvis(tree: FakePsutil) -> None:
    """«открой отчёт.docx» → Word — потомок Jarvis; «заверши ворд» — спросить и завершить, а не отказ."""
    asked: list[str] = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append(s) or True)
    assert WORD not in procs.own_pids()
    r = procs.kill("winword", "user")
    assert r.ok, r.text  # сейчас: «Это сам Jarvis — выйти можно из трея»
    assert asked and not tree.procs[WORD].alive


class FakeWinApi:
    """Минимальный слой windows._api: одно окно Word, одно окно notepad++."""

    wins: ClassVar[dict[int, tuple[str, int]]] = {
        11: ("отчёт.docx - Word", WORD),
        12: ("new 1 - Notepad++", NOTEPAD),
    }

    def enum_windows(self) -> list[int]:
        return list(self.wins)

    def is_visible(self, hwnd: int) -> bool:
        return True

    def ex_style(self, hwnd: int) -> int:
        return 0

    def owner(self, hwnd: int) -> int:
        return 0

    def is_cloaked(self, hwnd: int) -> bool:
        return False

    def class_name(self, hwnd: int) -> str:
        return "OpusApp"

    def get_title(self, hwnd: int) -> str:
        return self.wins[hwnd][0]

    def pid_of(self, hwnd: int) -> int:
        return self.wins[hwnd][1]

    def exe_of(self, pid: int) -> str:
        return {WORD: "WINWORD.EXE", NOTEPAD: "notepad++.exe"}[pid]


def test_windows_of_apps_opened_by_jarvis_are_listed(
    tree: FakePsutil, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Окна приложений, открытых Jarvis или GPT, должны быть в списке окон, а не скрыты как «окна Jarvis»."""
    monkeypatch.setattr(windows, "_api", FakeWinApi())
    titles = [w.title for w in windows.list_windows()]
    assert titles == ["отчёт.docx - Word", "new 1 - Notepad++"]  # сейчас: []


# --- F1: Job Object без breakaway — всё, что открыл GPT, умирает вместе с Jarvis ------------------------


def test_job_lets_apps_opened_by_brain_outlive_jarvis(monkeypatch: pytest.MonkeyPatch) -> None:
    """pc.mcp (потомок codex.exe в job) открывает приложения через os.startfile: без
    JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK они наследуют job и гибнут при выходе Jarvis (KILL_ON_JOB_CLOSE)."""
    seen: dict[str, Any] = {}
    fake = types.ModuleType("win32job")
    fake.JobObjectExtendedLimitInformation = 9
    fake.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    fake.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000
    fake.JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
    fake.CreateJobObject = lambda sa, name: "job"
    fake.QueryInformationJobObject = lambda job, cls: {"BasicLimitInformation": {"LimitFlags": 0}}
    fake.SetInformationJobObject = lambda job, cls, info: seen.update(info=info)
    monkeypatch.setitem(sys.modules, "win32job", fake)
    winapp._WinApi().create_job()
    flags = seen["info"]["BasicLimitInformation"]["LimitFlags"]
    assert flags & 0x2000  # KILL_ON_JOB_CLOSE
    assert flags & 0x1000  # сейчас нет: Word/браузер, открытые GPT, — члены job


# --- F3: ShowWindow/BringWindowToTop на зависшем окне подвешивают поток действия -----------------------


class HungWinApi(FakeWinApi):
    """Окно зависшего приложения: синхронные ShowWindow/SetWindowPos ждут его вечно (тут — 5 с)."""

    wins: ClassVar[dict[int, tuple[str, int]]] = {21: ("Документ - Word (Не отвечает)", 555)}

    def exe_of(self, pid: int) -> str:
        return "WINWORD.EXE"

    def is_elevated(self, pid: int) -> bool:
        return False

    def is_window(self, hwnd: int) -> bool:
        return True

    def is_iconic(self, hwnd: int) -> bool:
        return False

    def is_hung(self, hwnd: int) -> bool:  # IsHungAppWindow
        return True

    def foreground(self) -> int:
        return 1

    def show(self, hwnd: int, cmd: int) -> None:
        time.sleep(5)

    def bring_to_top(self, hwnd: int) -> bool:
        time.sleep(5)
        return True

    def set_foreground(self, hwnd: int) -> bool:
        return False


def test_minimize_hung_window_does_not_hang(tree: FakePsutil, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows, "_api", HungWinApi())
    out: list[Any] = []
    t = threading.Thread(
        target=lambda: out.append(windows.window_action("minimize", "word", "user")), daemon=True
    )
    t.start()
    t.join(1.0)
    assert out, "window_action висит на ShowWindow зависшего окна"
    assert not out[0].ok


# --- F5: зависший вызов COM в потоке звука ломает звук до перезапуска Jarvis --------------------------


class AudioApi:
    def thread_init(self) -> None:
        pass

    def thread_done(self) -> None:
        pass

    def endpoint(self) -> object:
        return object()


def test_audio_recovers_after_hung_com_call(monkeypatch: pytest.MonkeyPatch) -> None:
    audio._stop_thread()
    monkeypatch.setattr(audio, "_api", AudioApi())
    monkeypatch.setattr(audio, "TIMEOUT_S", 0.3)
    release = threading.Event()
    try:
        with pytest.raises(FutureTimeout):
            audio._call(lambda ev: release.wait(10))  # GetDefaultAudioEndpoint завис (служба звука)
        assert audio._call(lambda ev: 42) == 42  # сейчас: тоже таймаут — очередь стоит за зависшим вызовом
    finally:
        release.set()
        audio._stop_thread()


# --- F6: ошибки pywin32 (pywintypes.error — не OSError) проходят мимо обработки в буфере обмена ---------


def test_clipboard_api_error_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    from pc import system

    class PyWinError(Exception):
        """Как pywintypes.error: __bases__ == (Exception,) (pywin32 win32/test/test_exceptions.py)."""

    pywintypes = types.ModuleType("pywintypes")
    pywintypes.error = PyWinError
    clip = types.ModuleType("win32clipboard")
    clip.CF_UNICODETEXT = 13
    clip.OpenClipboard = lambda *a: None
    clip.CloseClipboard = lambda: None
    clip.EmptyClipboard = lambda: None

    def set_text(text: str, fmt: int) -> None:
        raise PyWinError(1418, "SetClipboardData", "Thread does not have a clipboard open.")

    clip.SetClipboardText = set_text
    monkeypatch.setitem(sys.modules, "pywintypes", pywintypes)
    monkeypatch.setitem(sys.modules, "win32clipboard", clip)
    monkeypatch.setattr(system, "_api", system._Api.__new__(system._Api))
    r = system.clipboard_set("привет", "user")  # сейчас: исключение PyWinError наружу
    assert not r.ok and "буфер" in r.text
