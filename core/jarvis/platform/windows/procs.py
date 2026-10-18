"""Процессы: свои (дерево Jarvis), системные (их не завершаем), поиск запущенных по имени программы или exe.

Свои процессы: ядро (os.getpid()), его предки, пока они — части Jarvis (python.exe venv, Electron/Jarvis.exe,
uv.exe), и потомки корня с такими же именами. Программы, запущенные Jarvis (их родитель — ядро), своими не
считаются: в чужие ветки дерева поиск не спускается. Корень можно задать явно: JARVIS_ROOT_PID.
"""

import contextlib
import logging
import os
import threading
import time
from collections.abc import Iterable

import psutil
from rapidfuzz import fuzz

from jarvis.platform.windows.apps import exe_stem

log = logging.getLogger(__name__)

ROOT_ENV = "JARVIS_ROOT_PID"
OWN_EXES = frozenset({"python.exe", "pythonw.exe", "electron.exe", "py.exe", "uv.exe"})
OWN_CACHE_S = 10.0
FUZZY_THRESHOLD = 85.0

# системные процессы: завершать их нельзя (Windows упадёт или пропадёт рабочий стол)
PROTECTED_EXES = frozenset(
    {"system", "system idle process", "idle", "registry", "memory compression", "secure system"}
    | {"smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe"}
    | {"svchost.exe", "dwm.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "explorer.exe"}
    | {"taskhostw.exe", "spoolsv.exe", "audiodg.exe", "conhost.exe", "runtimebroker.exe", "dllhost.exe"}
    | {"startmenuexperiencehost.exe", "shellexperiencehost.exe", "searchhost.exe", "textinputhost.exe"}
    | {"msmpeng.exe", "securityhealthservice.exe", "wudfhost.exe", "lsm.exe", "logonui.exe"}
)
PROTECTED_PIDS = frozenset({0, 4})


def is_own_exe(name: str) -> bool:
    n = name.casefold()
    return n in OWN_EXES or n.startswith("jarvis")


def is_protected(name: str, pid: int = -1) -> bool:
    return pid in PROTECTED_PIDS or name.casefold() in PROTECTED_EXES


class OwnProcesses:
    """Pid процессов Jarvis; кэш на OWN_CACHE_S секунд."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached: tuple[float, frozenset[int]] | None = None

    def pids(self) -> frozenset[int]:
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._cached[0] < OWN_CACHE_S:
                return self._cached[1]
        pids = frozenset(self._collect())
        with self._lock:
            self._cached = (now, pids)
        return pids

    def _collect(self) -> set[int]:
        me = os.getpid()
        pids = {me}
        try:
            ancestors = psutil.Process(me).parents()  # ближайший первым
        except psutil.Error as e:
            log.debug("предки ядра не получены: %s", e)
            ancestors = []
        env = os.environ.get(ROOT_ENV, "").strip()
        root = me
        if env.isdigit() and int(env) > 0 and psutil.pid_exists(int(env)):
            root = int(env)
            chain: list[int] = []
            for parent in ancestors:  # корень — наш предок: вся цепочка до него тоже своя
                chain.append(parent.pid)
                if parent.pid == root:
                    pids.update(chain)
                    break
        else:
            for parent in ancestors:  # подняться, пока предки — части Jarvis (не explorer.exe, не терминал)
                try:
                    if not is_own_exe(parent.name()):
                        break
                except psutil.Error:
                    break
                pids.add(parent.pid)
                root = parent.pid
        pids.add(root)
        stack = [root]
        while stack:
            try:
                children = psutil.Process(stack.pop()).children()
            except psutil.Error:
                continue
            for child in children:
                with contextlib.suppress(psutil.Error):
                    if is_own_exe(child.name()):
                        pids.add(child.pid)
                        stack.append(child.pid)
        return pids


def match_process_names(candidates: set[str], query: str, names: Iterable[str]) -> set[str]:
    """Имена запущенных процессов (casefold), подходящие под запрос.

    Сначала точные кандидаты (apps.exe_candidates), иначе — самые похожие имена exe (ratio ≥ 85) на запрос
    без «.exe»: «obsidan» → obsidian.exe. Пусто — ничего не подошло.
    """
    pool = {n.casefold() for n in names if n}
    exact = {c.casefold() for c in candidates} & pool
    if exact:
        return exact
    stem = exe_stem(query.strip().strip('"').casefold())
    if len(stem) < 3:
        return set()
    best = 0.0
    found: set[str] = set()
    for name in pool:
        score = float(fuzz.ratio(stem, exe_stem(name)))
        if score < FUZZY_THRESHOLD or score < best:
            continue
        if score > best:
            best, found = score, set()
        found.add(name)
    return found


def process_table() -> list[tuple[int, str]]:
    """(pid, имя exe) всех процессов."""
    out: list[tuple[int, str]] = []
    for p in psutil.process_iter(["pid", "name"]):
        info = getattr(p, "info", None) or {}
        pid, name = info.get("pid"), info.get("name")
        if pid is not None and name:
            out.append((int(pid), str(name)))
    return out
