"""Процессы: поиск по имени, защищённые системные, дерево самого Jarvis (psutil работает и на Linux)."""

import os

import psutil
import pytest

from jarvis.platform.windows import procs as p

NAMES = ["chrome.exe", "Spotify.exe", "obsidian.exe", "svchost.exe", "steamwebhelper.exe", "steam.exe"]


def test_match_exact_candidates() -> None:
    assert p.match_process_names({"spotify.exe"}, "spotify", NAMES) == {"spotify.exe"}
    assert p.match_process_names({"steam.exe", "steamwebhelper.exe"}, "steam", NAMES) == {
        "steam.exe",
        "steamwebhelper.exe",
    }


def test_match_fuzzy_fallback() -> None:
    assert p.match_process_names(set(), "obsidan", NAMES) == {"obsidian.exe"}  # опечатка
    assert p.match_process_names(set(), "chrom.exe", NAMES) == {"chrome.exe"}
    assert p.match_process_names(set(), "ab", NAMES) == set()  # слишком коротко
    assert p.match_process_names(set(), "telegram", NAMES) == set()


def test_protected_and_own_names() -> None:
    assert p.is_protected("svchost.exe") and p.is_protected("CSRSS.EXE") and p.is_protected("explorer.exe")
    assert p.is_protected("anything.exe", pid=4)
    assert not p.is_protected("chrome.exe", pid=1234)
    assert p.is_own_exe("python.exe") and p.is_own_exe("Jarvis.exe") and p.is_own_exe("electron.exe")
    assert not p.is_own_exe("chrome.exe") and not p.is_own_exe("explorer.exe")


def test_own_processes_include_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(p.ROOT_ENV, raising=False)
    pids = p.OwnProcesses().pids()
    assert os.getpid() in pids


def test_own_processes_with_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = os.getppid()
    monkeypatch.setenv(p.ROOT_ENV, str(parent))
    pids = p.OwnProcesses().pids()
    assert os.getpid() in pids and parent in pids


def test_own_processes_skip_foreign_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """Программа, которую запустил Jarvis (ребёнок ядра), — не часть Jarvis."""

    class Proc:
        def __init__(self, pid: int, name: str, kids: list["Proc"] | None = None) -> None:
            self.pid = pid
            self._name = name
            self.kids = kids or []

        def name(self) -> str:
            return self._name

        def children(self) -> list["Proc"]:
            return self.kids

        def parents(self) -> list["Proc"]:
            return [electron, explorer]

    me = os.getpid()
    notepad = Proc(500, "notepad.exe", [Proc(501, "python.exe")])
    helper = Proc(600, "python.exe")
    core = Proc(me, "python.exe", [notepad, helper])
    renderer = Proc(300, "electron.exe")
    electron = Proc(200, "electron.exe", [core, renderer])
    explorer = Proc(100, "explorer.exe", [electron])
    table = {x.pid: x for x in (core, notepad, helper, renderer, electron, explorer)}

    monkeypatch.delenv(p.ROOT_ENV, raising=False)
    monkeypatch.setattr(p.psutil, "Process", lambda pid: table[pid])
    pids = p.OwnProcesses().pids()
    assert pids == frozenset({me, 200, 300, 600})  # без explorer.exe, notepad.exe и его потомков


def test_process_table_lists_this_process() -> None:
    table = p.process_table()
    me = psutil.Process().name()
    assert (os.getpid(), me) in table
