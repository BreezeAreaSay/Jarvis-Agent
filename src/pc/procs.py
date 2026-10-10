"""Процессы: поиск по имени, список и завершение.

Имя ищется так: точное имя exe (с .exe и без) → транслит и разговорные имена (apps.name_variants: «телега»,
«хром», «стима») → приложение из инвентаря (apps.resolve → exe) → нечёткое сравнение с именем exe.
Командные строки не собираются вообще: только pid и имя exe (путь — лишь чтобы взять имя файла).
Процессы самого Jarvis (own_pids) не завершаются никогда.
"""

import logging
import ntpath
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import psutil
from rapidfuzz import fuzz

from pc import apps, policy, privacy
from pc.result import Caller, Result, fail, ok

log = logging.getLogger("jarvis")

THRESHOLD = 85.0
OWN_CACHE_S = 2.0
TERMINATE_WAIT_S = 3.0
KILL_WAIT_S = 1.0
ROOT_ENV = "JARVIS_ROOT_PID"
SELF_TEXT = "Это сам Jarvis — выйти можно из трея"
ATTRS = ["pid", "name", "exe"]


@dataclass(frozen=True)
class ProcGroup:
    exe: str
    count: int
    pids: list[int] = field(default_factory=list)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """1 процесс, 2 процесса, 5 процессов."""
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _count_text(n: int) -> str:
    return f"{n} {plural(n, ('процесс', 'процесса', 'процессов'))}"


def _exe_name(info: dict[str, Any]) -> str:
    name = str(info.get("name") or "").strip()
    if not name:
        exe = str(info.get("exe") or "").strip()
        name = ntpath.basename(exe) if exe else ""
    return name


def _stem(exe: str) -> str:
    return exe[:-4] if exe.casefold().endswith(".exe") else exe


def _snapshot() -> list[ProcGroup]:
    """Все процессы, сгруппированные по имени exe (без учёта регистра)."""
    groups: dict[str, tuple[str, list[int]]] = {}
    for p in psutil.process_iter(ATTRS):
        info = getattr(p, "info", None) or {}
        pid = info.get("pid") or getattr(p, "pid", 0)
        name = _exe_name(info)
        if not pid or not name:
            continue
        groups.setdefault(name.casefold(), (name, []))[1].append(int(pid))
    return [ProcGroup(name, len(pids), sorted(pids)) for name, pids in groups.values()]


def _match(groups: list[ProcGroup], name: str) -> list[ProcGroup]:
    q = name.strip()
    stem = _stem(q).casefold()
    exact = [g for g in groups if _stem(g.exe).casefold() == stem]
    if exact:
        return exact
    folded = {id(g): apps.latin(_stem(g.exe)) for g in groups}
    variants = apps.name_variants(q)
    found = [g for g in groups if folded[id(g)] in variants]
    if found:
        return found
    app, _score = apps.lookup(q)
    if app is not None:
        exe = apps.app_exe(app)
        found = [g for g in groups if exe and g.exe.casefold() == exe]
        if not found:
            app_variants = apps.name_variants(app.name)
            found = [g for g in groups if folded[id(g)] in app_variants]
        if found:
            return found
    scored: list[tuple[float, ProcGroup]] = []
    for g in groups:
        score = max((fuzz.ratio(v, folded[id(g)]) for v in variants), default=0.0)
        if score >= THRESHOLD:
            scored.append((score, g))
    scored.sort(key=lambda sg: -sg[0])
    return [g for _s, g in scored]


def find_processes(name: str | None) -> list[ProcGroup]:
    """Группы процессов по имени exe; без имени — все (больше процессов — выше)."""
    groups = _snapshot()
    if not name or not name.strip():
        return sorted(groups, key=lambda g: (-g.count, g.exe.casefold()))
    return _match(groups, name)


# --- свои процессы --------------------------------------------------------------------------------

_own_lock = threading.Lock()
_own_cache: dict[str, Any] = {}


def own_pids() -> set[int]:
    """Сам Jarvis и его дерево: корень (JARVIS_ROOT_PID, если жив, иначе текущий процесс), все потомки корня,
    текущий процесс и его предки до корня. Кэш на OWN_CACHE_S секунд."""
    env = os.environ.get(ROOT_ENV, "").strip()
    key = (env, os.getpid())
    now = time.monotonic()
    with _own_lock:
        hit = _own_cache.get("value")
        if hit is not None and hit[0] == key and now - hit[1] < OWN_CACHE_S:
            return set(hit[2])
    pids = _collect_own(env)
    with _own_lock:
        _own_cache["value"] = (key, now, frozenset(pids))
    return pids


def _collect_own(env: str) -> set[int]:
    me = os.getpid()
    root = me
    if env:
        try:
            pid = int(env)
            if pid > 0 and psutil.pid_exists(pid):
                root = pid
        except ValueError:
            log.warning("%s не число: %r", ROOT_ENV, env)
    pids = {root, me}
    try:
        pids.update(int(c.pid) for c in psutil.Process(root).children(recursive=True))
    except psutil.Error as e:
        log.debug("потомки Jarvis не получены: %s", e)
    if root != me:
        chain: list[int] = []
        try:
            for parent in psutil.Process(me).parents():
                chain.append(int(parent.pid))
                if parent.pid == root:
                    pids.update(chain)  # только если корень — наш предок
                    break
        except psutil.Error as e:
            log.debug("предки процесса не получены: %s", e)
    return pids


# --- действия -----------------------------------------------------------------------------------


def processes(name: str | None, caller: Caller) -> Result:
    """Список процессов (по имени или все): data — [{exe, count}]."""
    denied = policy.require("list_processes", caller, "Показать список процессов")
    if denied is not None:
        return denied
    query = (name or "").strip()
    groups = find_processes(query or None)
    if query and not groups:
        return fail(f"Не нашёл процесс «{query}».")
    data: Any = [{"exe": g.exe, "count": g.count} for g in groups]
    if query:
        text = "; ".join(f"{g.exe} — {_count_text(g.count)}" for g in groups[:5])
    else:
        text = f"Процессов: {sum(g.count for g in groups)}, программ: {len(groups)}"
    if caller == "brain":
        # имена exe и числа — без путей и командных строк; общий фильтр мозга всё равно применяется
        data = privacy.redact(data)
        text = privacy.redact_text(text)
    return ok(text, data)


def kill(name: str, caller: Caller) -> Result:
    """Завершить все процессы одной программы: policy kill_process (спросить), свои — никогда."""
    query = (name or "").strip()
    if not query:
        return fail("Не указано, какой процесс завершить.")
    groups = find_processes(query)
    if not groups:
        return fail(f"Не нашёл процесс «{query}».")
    if len(groups) > 1:
        names = ", ".join(g.exe for g in groups[:8])
        return fail(
            f"Под «{query}» подходят несколько программ: {names}. Уточните, какую завершить.",
            [{"exe": g.exe, "count": g.count} for g in groups],
        )
    group = groups[0]
    own = own_pids()
    is_self = bool(own.intersection(group.pids))
    if is_self and policy.require("jarvis_self", caller, f"Завершить {group.exe} — это Jarvis") is not None:
        return fail(SELF_TEXT)
    summary = f"Завершить {group.exe} ({_count_text(group.count)})?"
    details = "Несохранённые данные программы пропадут."
    denied = policy.require("kill_process", caller, summary, details)
    if denied is not None:
        return denied
    return _terminate(group.exe, own)


def _terminate(exe: str, own: set[int]) -> Result:
    """terminate → ждать TERMINATE_WAIT_S → kill оставшихся. Процессы берутся заново (после подтверждения)."""
    key = exe.casefold()
    targets = []
    for p in psutil.process_iter(ATTRS):
        info = getattr(p, "info", None) or {}
        pid = info.get("pid") or getattr(p, "pid", 0)
        if pid and pid not in own and _exe_name(info).casefold() == key:
            targets.append(p)
    if not targets:
        return ok(f"{exe} уже не запущен")
    denied: dict[int, Any] = {}
    sent = []
    for p in targets:
        try:
            p.terminate()
            sent.append(p)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied[p.pid] = p
    _gone, alive = psutil.wait_procs(sent, timeout=TERMINATE_WAIT_S)
    killed = []
    for p in alive:
        try:
            p.kill()
            killed.append(p)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied[p.pid] = p
    still: list[Any] = []
    if killed:
        _gone, still = psutil.wait_procs(killed, timeout=KILL_WAIT_S)
    total = len(targets)
    if len(denied) == total:
        return fail(f"Нет прав завершить {exe} (процесс администратора?)")
    if denied:
        done = total - len(denied) - len(still)
        return fail(
            f"Завершил {done} из {total} процессов {exe}; на остальные нет прав (процесс администратора?)"
        )
    if still:
        return fail(f"{exe} не завершился: осталось {_count_text(len(still))}")
    return ok(f"Завершил {exe}")
