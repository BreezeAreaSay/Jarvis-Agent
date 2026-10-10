"""pc.procs на фейковом psutil: поиск по имени, свои процессы, завершение и AccessDenied."""

import os
from typing import Any

import psutil
import pytest

from pc import apps, confirm_client, privacy, procs

ME = os.getpid()
ROOT = 900_001  # «Jarvis» — корень дерева
CODEX = 900_002  # codex.exe — потомок корня и наш родитель (мы — pc.mcp)
HANDS = 900_003  # llama-server.exe — потомок корня
SHELL = 900_000  # explorer.exe — родитель корня, не наш


class FakeProc:
    def __init__(self, ps: "FakePsutil", pid: int, name: str, exe: str, ppid: int) -> None:
        self.ps = ps
        self.pid = pid
        self._name = name
        self.exe = exe
        self.ppid = ppid
        self.alive = True
        self.calls: list[str] = []
        self.deny = False  # AccessDenied на terminate/kill
        self.ignore_terminate = False  # не умирает от terminate

    @property
    def info(self) -> dict[str, Any]:
        return {"pid": self.pid, "name": self._name, "exe": self.exe}

    def name(self) -> str:
        return self._name

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.deny:
            raise psutil.AccessDenied(self.pid)
        if not self.ignore_terminate:
            self.alive = False

    def kill(self) -> None:
        self.calls.append("kill")
        if self.deny:
            raise psutil.AccessDenied(self.pid)
        self.alive = False

    def children(self, recursive: bool = False) -> list["FakeProc"]:
        direct = [p for p in self.ps.procs.values() if p.ppid == self.pid]
        if not recursive:
            return direct
        out = list(direct)
        for c in direct:
            out.extend(c.children(recursive=True))
        return out

    def parents(self) -> list["FakeProc"]:
        out = []
        p = self.ps.procs.get(self.ppid)
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
        self.iter_attrs: list[list[str]] = []
        self.waits: list[float] = []

    def add(self, pid: int, name: str, ppid: int = 0, exe: str | None = None) -> FakeProc:
        p = FakeProc(self, pid, name, exe if exe is not None else f"C:\\Program Files\\X\\{name}", ppid)
        self.procs[pid] = p
        return p

    def process_iter(self, attrs: list[str]) -> list[FakeProc]:
        self.iter_attrs.append(list(attrs))
        return [p for p in self.procs.values() if p.alive]

    def Process(self, pid: int) -> FakeProc:  # как в psutil
        p = self.procs.get(pid)
        if p is None or not p.alive:
            raise psutil.NoSuchProcess(pid)
        return p

    def pid_exists(self, pid: int) -> bool:
        return pid in self.procs and self.procs[pid].alive

    def wait_procs(self, plist: list[FakeProc], timeout: float) -> tuple[list[FakeProc], list[FakeProc]]:
        self.waits.append(timeout)
        return [p for p in plist if not p.alive], [p for p in plist if p.alive]


@pytest.fixture
def ps(monkeypatch: pytest.MonkeyPatch, apps_fixture) -> FakePsutil:
    fake = FakePsutil()
    fake.add(SHELL, "explorer.exe", 0, "C:\\Windows\\explorer.exe")
    fake.add(ROOT, "Jarvis.exe", SHELL)
    fake.add(CODEX, "codex.exe", ROOT)
    fake.add(HANDS, "llama-server.exe", ROOT)
    fake.add(ME, "python.exe", CODEX)
    fake.add(100, "chrome.exe", SHELL)
    fake.add(101, "chrome.exe", 100)
    fake.add(102, "Chrome.exe", 100)
    fake.add(110, "Telegram.exe", SHELL)
    fake.add(120, "steam.exe", SHELL)
    fake.add(121, "steamwebhelper.exe", 120)
    fake.add(130, "Discord.exe", SHELL)
    fake.add(140, "WINWORD.EXE", SHELL)
    fake.add(150, "Obsidian.exe", SHELL)
    fake.add(160, "python.exe", SHELL)  # чужой python
    fake.add(170, "", SHELL, "C:\\Tools\\noname.exe")  # имя недоступно — берём из пути
    monkeypatch.setattr(procs, "psutil", fake)
    monkeypatch.setattr(procs, "_own_cache", {})
    monkeypatch.setenv(procs.ROOT_ENV, str(ROOT))
    monkeypatch.setattr(apps, "_inv", None)
    apps.set_inventory(apps_fixture)
    return fake


def names(groups: list[procs.ProcGroup]) -> list[str]:
    return [g.exe for g in groups]


def test_snapshot_groups_without_cmdline(ps: FakePsutil) -> None:
    groups = procs.find_processes(None)
    assert ps.iter_attrs and all(a == ["pid", "name", "exe"] for a in ps.iter_attrs)
    chrome = next(g for g in groups if g.exe.casefold() == "chrome.exe")
    assert chrome.count == 3 and chrome.pids == [100, 101, 102]
    assert groups[0] is chrome  # больше процессов — выше
    assert "noname.exe" in names(groups)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("chrome", "chrome.exe"),
        ("CHROME.EXE", "chrome.exe"),
        ("хром", "chrome.exe"),
        ("хрома", "chrome.exe"),
        ("телега", "Telegram.exe"),
        ("телеграм", "Telegram.exe"),
        ("стима", "steam.exe"),
        ("стим", "steam.exe"),
        ("дискорд", "Discord.exe"),
        ("ворд", "WINWORD.EXE"),
        ("обсидиан", "Obsidian.exe"),
        ("obsidain", "Obsidian.exe"),
        ("steamwebhelper", "steamwebhelper.exe"),
    ],
)
def test_find_processes_by_name(ps: FakePsutil, query: str, expected: str) -> None:
    assert names(procs.find_processes(query)) == [expected]


def test_find_processes_unknown(ps: FakePsutil) -> None:
    assert procs.find_processes("фотошоп") == []
    assert procs.find_processes("автокад") == []


def test_own_pids_tree(ps: FakePsutil) -> None:
    own = procs.own_pids()
    assert own == {ROOT, CODEX, HANDS, ME}
    assert SHELL not in own and 160 not in own


def test_own_pids_cached(ps: FakePsutil) -> None:
    procs.own_pids()
    ps.add(900_010, "conhost.exe", ROOT)
    assert 900_010 not in procs.own_pids()  # кэш 2 с
    procs._own_cache.clear()
    assert 900_010 in procs.own_pids()


def test_own_pids_dead_or_missing_root(ps: FakePsutil, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(procs.ROOT_ENV, "999999")
    assert procs.own_pids() == {ME}
    procs._own_cache.clear()
    monkeypatch.delenv(procs.ROOT_ENV)
    assert procs.own_pids() == {ME}
    procs._own_cache.clear()
    monkeypatch.setenv(procs.ROOT_ENV, "не число")
    assert procs.own_pids() == {ME}


def test_own_pids_root_not_ancestor(ps: FakePsutil, monkeypatch: pytest.MonkeyPatch) -> None:
    # корень жив, но мы не его потомок: предков (explorer и т.п.) не добавляем
    ps.procs[ME].ppid = SHELL
    own = procs.own_pids()
    assert ME in own and ROOT in own and SHELL not in own


def test_processes_result(ps: FakePsutil, monkeypatch: pytest.MonkeyPatch) -> None:
    r = procs.processes("хром", "user")
    assert r.ok and r.data == [{"exe": "chrome.exe", "count": 3}]
    assert "3 процесса" in r.text
    seen = []
    monkeypatch.setattr(privacy, "redact", lambda v: seen.append(v) or v)
    monkeypatch.setattr(privacy, "redact_text", lambda t: t)
    r = procs.processes(None, "brain")
    assert r.ok and seen and all(set(d) == {"exe", "count"} for d in r.data)
    assert all("\\" not in d["exe"] for d in r.data)
    assert not procs.processes("фотошоп", "user").ok


def test_kill_not_found_and_ambiguous(ps: FakePsutil) -> None:
    r = procs.kill("фотошоп", "user")
    assert not r.ok and "Не нашёл" in r.text
    ps.add(180, "backupagent1.exe", SHELL)
    ps.add(181, "backupagent2.exe", SHELL)
    r = procs.kill("backupagent", "user")
    assert not r.ok and "несколько" in r.text and "backupagent1.exe, backupagent2.exe" in r.text
    assert all(not p.calls for p in ps.procs.values())


def test_kill_own_process_denied(ps: FakePsutil) -> None:
    asked = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append(s) or True)
    for target in ("codex", "llama-server.exe", "python"):  # python: среди них есть и мы
        r = procs.kill(target, "user")
        assert not r.ok and r.text == procs.SELF_TEXT
    assert asked == []
    assert all(not p.calls for p in ps.procs.values())


def test_kill_asks_and_respects_no(ps: FakePsutil) -> None:
    asked = []
    confirm_client.set_confirm_handler(lambda s, d, c: asked.append((s, c)) or False)
    r = procs.kill("хром", "user")
    assert not r.ok
    assert asked == [("Завершить chrome.exe (3 процесса)?", "user")]
    assert all(not p.calls for p in ps.procs.values())


def test_kill_terminate_then_kill(ps: FakePsutil) -> None:
    confirm_client.set_confirm_handler(lambda s, d, c: True)
    ps.procs[101].ignore_terminate = True
    r = procs.kill("chrome", "brain")
    assert r.ok and "chrome.exe" in r.text
    assert ps.procs[100].calls == ["terminate"]
    assert ps.procs[101].calls == ["terminate", "kill"]
    assert ps.waits[0] == procs.TERMINATE_WAIT_S
    assert not any(ps.procs[pid].alive for pid in (100, 101, 102))


def test_kill_access_denied(ps: FakePsutil) -> None:
    confirm_client.set_confirm_handler(lambda s, d, c: True)
    ps.procs[140].deny = True
    r = procs.kill("ворд", "user")
    assert not r.ok and r.text == "Нет прав завершить WINWORD.EXE (процесс администратора?)"
    ps.procs[101].deny = True
    r = procs.kill("chrome", "user")
    assert not r.ok and "2 из 3" in r.text and "нет прав" in r.text


def test_kill_brain_without_confirmation_is_refused(ps: FakePsutil) -> None:
    r = procs.kill("телега", "brain")  # нет обработчика и pipe — отказ
    assert not r.ok
    assert ps.procs[110].alive and not ps.procs[110].calls


def test_plural() -> None:
    forms = ("процесс", "процесса", "процессов")
    assert [procs.plural(n, forms) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)] == [
        "процесс",
        "процесса",
        "процессов",
        "процессов",
        "процессов",
        "процесс",
        "процесса",
        "процессов",
        "процессов",
    ]
