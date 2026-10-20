"""Программы: инвентарь меню «Пуск», кэш <data>/cache/apps.json, поиск по имени, разбор цели запуска.

Инвентарь = Get-StartApps (имя и AppID: путь, AUMID или путь от известной папки «{GUID}\\…») + ярлыки .lnk из
%ProgramData% и %AppData%\\Microsoft\\Windows\\Start Menu\\Programs (цель ярлыка — путь к exe для иконки
и имени процесса). Get-StartApps недоступна — вместо неё папка shell:AppsFolder через COM.

Кэш читается мгновенно; строится заново при первом обращении без кэша, по list_apps(refresh=True), при промахе
поиска (не чаще раза в 10 минут) и в фоне, если кэш старше суток. Разбор и поиск — чистые функции (их
проверяют тесты на Linux); обращения к Windows — в функциях scan_* (COM — только в потоке с CoInitialize).
"""

import contextlib
import json
import logging
import ntpath
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rapidfuzz import fuzz

from jarvis.platform.base import AppInfo, PlatformError

log = logging.getLogger(__name__)

CACHE_VERSION = 1
STALE_S = 24 * 3600.0  # кэш старше — обновить в фоне
MISS_REFRESH_S = 600.0  # промах поиска обновляет инвентарь не чаще
MATCH_THRESHOLD = 80.0
PS_TIMEOUT_S = 40.0
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# без смены кодировки PowerShell 5.1 пишет в pipe в OEM 866; @() — чтобы одно приложение тоже дало массив
PS_START_APPS = (
    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
    "ConvertTo-Json -Compress -InputObject @(Get-StartApps | Select-Object Name,AppID)"
)

# Известные программы: (имена, exe). Нужны, когда имя в меню «Пуск» не похоже на имя процесса
# («Visual Studio Code» → code.exe) и для поиска процесса или окна по имени («spotify» → Spotify.exe).
# Разговорные русские формы («хром», «телега») — забота слоя команд.
KNOWN_APPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("google chrome", "chrome"), ("chrome.exe",)),
    (("microsoft edge", "edge", "msedge"), ("msedge.exe",)),
    (("mozilla firefox", "firefox"), ("firefox.exe",)),
    (("opera", "opera browser", "opera gx", "opera gx browser"), ("opera.exe",)),
    (("brave", "brave browser"), ("brave.exe",)),
    (("vivaldi",), ("vivaldi.exe",)),
    (("яндекс браузер", "yandex browser", "yandex"), ("browser.exe",)),
    (("яндекс музыка", "yandex music"), ("яндекс музыка.exe", "yandex music.exe", "yandexmusic.exe")),
    (("spotify", "spotify music"), ("spotify.exe",)),
    (("telegram", "telegram desktop"), ("telegram.exe",)),
    (("discord",), ("discord.exe",)),
    (("whatsapp",), ("whatsapp.exe", "whatsapp.root.exe")),
    (("viber",), ("viber.exe",)),
    (("skype",), ("skype.exe",)),
    (("zoom", "zoom workplace"), ("zoom.exe",)),
    (("slack",), ("slack.exe",)),
    (("microsoft teams", "teams"), ("ms-teams.exe", "teams.exe")),
    (("steam",), ("steam.exe", "steamwebhelper.exe")),
    (("epic games launcher", "epic games"), ("epicgameslauncher.exe",)),
    (("battle net", "battlenet"), ("battle.net.exe",)),
    (("obs studio", "obs"), ("obs64.exe", "obs32.exe", "obs.exe")),
    (("visual studio code", "vs code", "vscode", "code"), ("code.exe",)),
    (("visual studio",), ("devenv.exe",)),
    (("pycharm",), ("pycharm64.exe",)),
    (("intellij idea", "idea"), ("idea64.exe",)),
    (("notepad++",), ("notepad++.exe",)),
    (("блокнот", "notepad"), ("notepad.exe",)),
    (("калькулятор", "calculator"), ("calculatorapp.exe", "calculator.exe", "calc.exe")),
    (("paint",), ("mspaint.exe",)),
    (("проводник", "file explorer", "explorer"), ("explorer.exe",)),
    (("диспетчер задач", "task manager"), ("taskmgr.exe",)),
    (("терминал", "windows terminal", "terminal"), ("windowsterminal.exe",)),
    (("командная строка", "command prompt", "cmd"), ("cmd.exe",)),
    (("windows powershell", "powershell"), ("powershell.exe", "pwsh.exe")),
    (("параметры", "settings", "настройки"), ("systemsettings.exe",)),
    (("word", "microsoft word"), ("winword.exe",)),
    (("excel", "microsoft excel"), ("excel.exe",)),
    (("powerpoint", "microsoft powerpoint"), ("powerpnt.exe",)),
    (("outlook", "microsoft outlook"), ("outlook.exe", "olk.exe")),
    (("onenote",), ("onenote.exe",)),
    (("adobe photoshop", "photoshop"), ("photoshop.exe",)),
    (("adobe premiere pro", "premiere pro"), ("adobe premiere pro.exe",)),
    (("vlc media player", "vlc"), ("vlc.exe",)),
    (("aimp",), ("aimp.exe",)),
    (("foobar2000",), ("foobar2000.exe",)),
    (("itunes",), ("itunes.exe",)),
    (("apple music",), ("applemusic.exe",)),
    (("медиаплеер", "media player", "windows media player"), ("microsoft.media.player.exe", "wmplayer.exe")),
    (("obsidian",), ("obsidian.exe",)),
    (("notion",), ("notion.exe",)),
    (("figma",), ("figma.exe",)),
    (("blender",), ("blender.exe",)),
    (("7 zip", "7zip", "7 zip file manager"), ("7zfm.exe",)),
    (("winrar",), ("winrar.exe",)),
    (("qbittorrent",), ("qbittorrent.exe",)),
    (("anydesk",), ("anydesk.exe",)),
    (("teamviewer",), ("teamviewer.exe",)),
    (("minecraft launcher", "minecraft"), ("minecraftlauncher.exe", "minecraft.exe")),
    (("everything",), ("everything.exe",)),
    (("audacity",), ("audacity.exe",)),
    (("snipping tool", "ножницы"), ("snippingtool.exe",)),
)

# exe-«посредники»: по ним не узнать программу (Squirrel Update.exe у Discord, лаунчеры, оболочки)
GENERIC_EXES = frozenset(
    {"update.exe", "launcher.exe", "setup.exe", "uninstall.exe", "electron.exe", "java.exe", "javaw.exe"}
    | {"python.exe", "pythonw.exe", "cmd.exe", "rundll32.exe", "msiexec.exe", "explorer.exe", "wscript.exe"}
    | {"powershell.exe", "pwsh.exe", "conhost.exe", "applicationframehost.exe"}
)

_JUNK_NAME = re.compile(
    r"\b(uninstall\w*|deinstall\w*|uninst|удалить|удаление|удалени\w*|деинсталл\w*|readme|read me|help|"
    r"справка|справочник|документаци\w*|documentation|manual|руководство|website|web site|веб сайт|сайт|"
    r"homepage|release notes|changelog|change log|what s new|whats new|license|лицензи\w*|eula)\b"
)
_JUNK_TARGET = re.compile(
    r"(^https?:|unins\d*\.exe$|uninst\w*\.exe$|uninstall\w*\.exe$|\\msiexec\.exe$|^msiexec\.exe$|"
    r"\.(txt|rtf|pdf|chm|hlp|htm|html|url|md|log|ini|xml|doc|docx)$)",
    re.IGNORECASE,
)
_GUID_PATH = re.compile(
    r"^\{([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}\\(.+)$"
)
_OFFICE_EXE = re.compile(r"\.([a-z0-9_]+\.exe)\.\d+$", re.IGNORECASE)  # Microsoft.Office.WINWORD.EXE.15
_SQUIRREL = re.compile(r"^com\.squirrel\.[^.]+\.([^.]+)$", re.IGNORECASE)  # com.squirrel.Discord.Discord
_PROCESS_START = re.compile(r"--processStart\s+\"?([^\"\s]+\.exe)\"?", re.IGNORECASE)
_NON_WORD = re.compile(r"[^\w]+")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+):")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_EXE_FILE = re.compile(r"^[\w.\-]+\.(exe|com|bat|cmd|msc|cpl)$", re.IGNORECASE)

SOURCE_RANK = {"start": 0, "uwp": 0, "path": 1, "custom": 1}


# --- нормализация и сопоставление (чистые функции) ------------------------------------------------------


def normalize_name(text: str) -> str:
    """Для сравнения имён: casefold, ё→е, без кавычек и «.exe»/«.lnk», «+» → «plus», пунктуация → пробел."""
    t = text.strip().strip("\"'«»“”").strip().casefold().replace("ё", "е")
    t = re.sub(r"\.(exe|lnk)$", "", t)
    t = re.sub(r"[®™©]", "", t).replace("+", " plus ")
    t = _NON_WORD.sub(" ", t).replace("_", " ")
    return " ".join(t.split())


def exe_name(path: str) -> str:
    """«C:\\…\\Spotify.exe» → «spotify.exe»; не exe — ""."""
    base = ntpath.basename(path.strip().strip('"')).casefold()
    return base if base.endswith(".exe") else ""


def exe_stem(name: str) -> str:
    n = name.casefold()
    return n[:-4] if n.endswith(".exe") else n


@dataclass(frozen=True)
class _Group:
    names: frozenset[str]
    exes: tuple[str, ...]


_GROUPS: list[_Group] = []


def known_groups() -> list[_Group]:
    global _GROUPS
    if not _GROUPS:
        _GROUPS = [_Group(frozenset(normalize_name(n) for n in names), exes) for names, exes in KNOWN_APPS]
    return _GROUPS


def known_group(name: str) -> _Group | None:
    """Группа известной программы по имени (уже нормализованному или сырому)."""
    norm = normalize_name(name)
    return next((g for g in known_groups() if norm in g.names), None)


def app_exe(app: AppInfo) -> str:
    """Имя процесса программы (casefold) по пути, AppID или таблице KNOWN_APPS; неизвестно — ""."""
    for candidate in (app.path, app.app_id):
        exe = exe_name(candidate) if candidate else ""
        if exe and exe not in GENERIC_EXES:
            return exe
    if m := _OFFICE_EXE.search(app.app_id):
        return m.group(1).casefold()
    if m := _SQUIRREL.match(app.app_id):
        return f"{m.group(1).casefold()}.exe"
    group = known_group(app.name)
    return group.exes[0] if group else ""


def _app_keys(app: AppInfo) -> list[str]:
    """Нормализованные имена, по которым ищется программа: имя, имя exe, «простой» AUMID («Chrome»)."""
    out = [normalize_name(app.name)]
    exe = app_exe(app)
    if exe:
        out.append(normalize_name(exe_stem(exe)))
    app_id = app.app_id.strip()
    if app_id and not any(ch in app_id for ch in "\\/!{") and "." not in app_id and len(app_id) >= 3:
        out.append(normalize_name(app_id))
    return [k for i, k in enumerate(out) if k and k not in out[:i]]


def _contains_words(words: list[str], part: list[str]) -> bool:
    n = len(part)
    return any(words[i : i + n] == part for i in range(len(words) - n + 1))


def _key_score(query: str, key: str) -> float:
    if not key or not query:
        return 0.0
    if query == key:
        return 100.0
    if _contains_words(key.split(), query.split()):
        return 90.0 + 9.0 * len(query) / len(key)
    if len(query) >= 3 and key.startswith(query) and len(query) / len(key) >= 0.6:
        return 82.0 + 9.0 * len(query) / len(key)
    return float(fuzz.ratio(query, key)) * 0.95


def score_app(query: str, app: AppInfo) -> float:
    """Насколько программа подходит под запрос (0–100); запрос — normalize_name(...)."""
    best = max((_key_score(query, key) for key in _app_keys(app)), default=0.0)
    group = known_group(query)
    if group is not None and best < 98.0:
        exe = app_exe(app)
        if (exe and exe in group.exes) or normalize_name(app.name) in group.names:
            best = 98.0
    return best


def rank_apps(query: str, apps: Sequence[AppInfo]) -> list[tuple[float, AppInfo]]:
    """Программы по убыванию соответствия; при равенстве — меню «Пуск» раньше, короткое имя раньше."""
    q = normalize_name(query)
    if not q:
        return []
    scored = [(score_app(q, app), i, app) for i, app in enumerate(apps)]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda s: (-s[0], SOURCE_RANK.get(s[2].source, 2), len(s[2].name), s[1]))
    return [(score, app) for score, _i, app in scored]


def resolve_app(query: str, apps: Sequence[AppInfo], threshold: float = MATCH_THRESHOLD) -> AppInfo | None:
    """Лучшая программа по имени («chrome» → «Google Chrome»); ниже порога — None."""
    ranked = rank_apps(query, apps)
    if ranked and ranked[0][0] >= threshold:
        return ranked[0][1]
    return None


def find_by_app_id(app_id: str, apps: Iterable[AppInfo]) -> AppInfo | None:
    key = app_id.strip().casefold()
    return next((a for a in apps if a.app_id.casefold() == key), None)


def exe_candidates(query: str, apps: Sequence[AppInfo] = ()) -> set[str]:
    """Имена процессов (casefold), которыми может быть программа из запроса: «spotify» → {"spotify.exe"},
    «Google Chrome» → {"chrome.exe", …}, «C:\\…\\obs64.exe» → {"obs64.exe"}."""
    raw = query.strip().strip('"')
    out: set[str] = set()
    if not raw:
        return out
    if exe := exe_name(raw):
        out.add(exe)
    norm = normalize_name(raw)
    if norm:
        out.add(f"{norm}.exe")
        if " " in norm:
            out.add(f"{norm.replace(' ', '')}.exe")
    group = known_group(norm)
    if group is not None:
        out.update(group.exes)
    app = resolve_app(raw, apps) if apps else None
    if app is not None:
        if exe := app_exe(app):
            out.add(exe)
        group = known_group(app.name)
        if group is not None:
            out.update(group.exes)
    return out


# --- разбор источников (чистые функции) ---------------------------------------------------------------


def is_junk(name: str, target: str = "") -> bool:
    """Деинсталлятор, справка, readme, ссылка на сайт — не программа."""
    return bool(_JUNK_NAME.search(normalize_name(name)) or (target and _JUNK_TARGET.search(target.strip())))


def parse_start_apps(raw: bytes | str) -> list[tuple[str, str]]:
    """Вывод Get-StartApps | ConvertTo-Json → [(Name, AppID)]: массив или объект, без мусора и повторов."""
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw.lstrip("\ufeff")
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    items = data if isinstance(data, list) else [data]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        app_id = str(item.get("AppID") or item.get("AppId") or "").strip()
        if not name or not app_id or is_junk(name, app_id):
            continue
        key = (normalize_name(name), app_id.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append((name, app_id))
    return out


@dataclass(frozen=True)
class Shortcut:
    """Ярлык меню «Пуск»: имя (файл без .lnk), путь к ярлыку, цель и аргументы."""

    name: str
    lnk: str
    target: str
    args: str = ""


def shortcut_ok(sc: Shortcut) -> bool:
    """Ярлык программы: цель — exe, не деинсталлятор, не справка и не ссылка."""
    return bool(exe_name(sc.target)) and not is_junk(sc.name, sc.target)


def split_guid_path(app_id: str) -> tuple[str, str] | None:
    """«{6D809377-…}\\Mozilla Firefox\\firefox.exe» → ("6D809377-…", "Mozilla Firefox\\firefox.exe")."""
    m = _GUID_PATH.match(app_id.strip())
    return (m.group(1), m.group(2)) if m else None


def merge_inventory(
    start_apps: Iterable[tuple[str, str]],
    shortcuts: Iterable[Shortcut],
    resolve_path: Callable[[str], str] = lambda _app_id: "",
) -> list[AppInfo]:
    """Get-StartApps + ярлыки → инвентарь. Путь к exe — из AppID (resolve_path) или ярлыка с тем же именем;
    ярлыки без пары в Get-StartApps добавляются сами (app_id — путь к ярлыку)."""
    shortcuts = list(shortcuts)
    by_name: dict[str, Shortcut] = {}
    for sc in shortcuts:
        if shortcut_ok(sc):
            by_name.setdefault(normalize_name(sc.name), sc)
    apps: list[AppInfo] = []
    seen: set[str] = set()
    for name, app_id in start_apps:
        norm = normalize_name(name)
        if not norm or is_junk(name, app_id):
            continue
        path = ""
        with contextlib.suppress(Exception):
            path = resolve_path(app_id) or ""
        sc = by_name.get(norm)
        if not path and sc is not None:
            path = sc.target
        source = "uwp" if "!" in app_id else "start"
        key = f"{norm}|{app_id.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        seen.add(norm)
        apps.append(AppInfo(name, app_id, path, source))
    for sc in shortcuts:
        norm = normalize_name(sc.name)
        if not norm or norm in seen or not shortcut_ok(sc):
            continue
        seen.add(norm)
        apps.append(AppInfo(sc.name, sc.lnk, sc.target, "start"))
    return apps


def squirrel_exe(target: str, args: str, exists: Callable[[str], bool] = os.path.isfile) -> str:
    """Ярлык Squirrel («Update.exe --processStart Discord.exe») → путь к настоящему exe в app-<версия>."""
    m = _PROCESS_START.search(args or "")
    if not m or exe_name(target) != "update.exe":
        return target
    folder = ntpath.dirname(target)
    with contextlib.suppress(OSError):
        versions = sorted(
            (d for d in os.listdir(folder) if d.casefold().startswith("app-")),
            key=lambda d: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", d[4:])],
            reverse=True,
        )
        for version in versions:
            path = ntpath.join(folder, version, m.group(1))
            if exists(path):
                return path
    return ntpath.join(folder, m.group(1))


TargetKind = Literal["url", "shell", "path", "appid", "exe", "name"]


def classify_target(target: str) -> TargetKind:
    """Что передали в launch(): ссылку или URI (https:, ms-settings:, steam:), shell:-путь, путь к файлу или
    папке, AppID (AUMID «…!App» или «{GUID}\\…»), имя exe («notepad.exe») или имя программы."""
    t = target.strip().strip('"').strip()
    if _DRIVE.match(t) or t.startswith(("\\\\", "\\", "/", "%", "~", ".\\", "..\\", "./", "../")):
        return "path"
    m = _SCHEME.match(t)
    if m:
        return "shell" if m.group(1).casefold() == "shell" else "url"
    if split_guid_path(t) is not None:
        return "appid"
    if "!" in t and not any(ch.isspace() for ch in t) and "\\" not in t:
        return "appid"
    if _EXE_FILE.match(t):
        return "exe"
    return "name"


# --- кэш --------------------------------------------------------------------------------------------------


def _apps_from_json(items: Any) -> list[AppInfo]:
    out: list[AppInfo] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        name, app_id = item.get("name"), item.get("app_id")
        if not isinstance(name, str) or not isinstance(app_id, str) or not name.strip() or not app_id.strip():
            continue
        path = item.get("path") if isinstance(item.get("path"), str) else ""
        source = item.get("source") if isinstance(item.get("source"), str) else "start"
        out.append(AppInfo(name.strip(), app_id.strip(), path or "", source or "start"))
    return out


def read_cache(path: Path) -> tuple[list[AppInfo], float] | None:
    """Кэш инвентаря: (программы, время обновления); нет или испорчен — None."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("кэш программ не прочитан (%s): %s", path, e)
        return None
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        log.warning("кэш программ неизвестного формата: %s", path)
        return None
    apps = _apps_from_json(raw.get("apps"))
    if not apps:
        return None
    updated = raw.get("updated")
    return apps, float(updated) if isinstance(updated, int | float) else 0.0


def write_cache(path: Path, apps: Sequence[AppInfo], updated: float) -> None:
    """Атомарно: временный файл рядом + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "updated": updated, "apps": [a.to_json() for a in apps]}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


class AppInventory:
    """Инвентарь программ в памяти + кэш на диске. Потокобезопасен: фоновое обновление меняет список целиком.

    scanner строит инвентарь заново (в Windows — scan_inventory, ему нужен поток с COM); background_init
    готовит фоновый поток (CoInitialize) и возвращает функцию завершения.
    """

    def __init__(
        self,
        cache_path: Callable[[], Path],
        scanner: Callable[[], list[AppInfo]],
        background_init: Callable[[], Callable[[], None]] | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._scanner = scanner
        self._background_init = background_init
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._apps: list[AppInfo] | None = None
        self._updated = 0.0
        self._loaded = False
        self._last_miss_refresh: float | None = None
        self._built_at: float | None = None  # monotonic последнего построения в этом процессе
        self._generation = 0  # растёт с каждым построением: параллельный refresh() не строит дважды
        self._bg: threading.Thread | None = None

    def _load(self) -> list[AppInfo] | None:
        with self._lock:
            if not self._loaded:
                self._loaded = True
                cached = read_cache(self._cache_path())
                if cached is not None:
                    self._apps, self._updated = cached
            return self._apps

    def peek(self) -> list[AppInfo]:
        """Что уже известно (память или кэш) — без построения."""
        return list(self._load() or [])

    def get(self, refresh: bool = False) -> list[AppInfo]:
        """Инвентарь: из кэша мгновенно; refresh — построить заново сейчас; кэш старый — обновить в фоне."""
        if refresh:
            return self.refresh()
        apps = self._load()
        if apps is None:
            return self.refresh()
        if time.time() - self._updated > STALE_S:
            self.refresh_in_background()
        return list(apps)

    def refresh(self) -> list[AppInfo]:
        """Построить инвентарь заново; ошибка — PlatformError, старый кэш не тронут. Если пока ждали, его
        построил другой поток (фоновое обновление), — вернуть готовый."""
        generation = self._generation
        with self._refresh_lock:
            if self._generation != generation and self._apps is not None:
                return list(self._apps)
            started = time.monotonic()
            try:
                apps = self._scanner()
            except PlatformError:
                raise
            except Exception as e:
                log.exception("инвентарь программ не построен")
                raise PlatformError("Не удалось получить список программ") from e
            if not apps:
                raise PlatformError("Список программ пуст — Windows ничего не вернула")
            updated = time.time()
            try:
                write_cache(self._cache_path(), apps, updated)
            except OSError as e:
                log.warning("кэш программ не записан: %s", e)
            with self._lock:
                self._apps, self._updated, self._loaded = list(apps), updated, True
                self._generation += 1
                self._built_at = time.monotonic()
            log.info("инвентарь программ обновлён: %d за %.1f с", len(apps), time.monotonic() - started)
            return list(apps)

    def refresh_in_background(self) -> None:
        with self._lock:
            if self._bg is not None and self._bg.is_alive():
                return
            self._bg = threading.Thread(target=self._background, name="jarvis-apps-refresh", daemon=True)
            self._bg.start()

    def _background(self) -> None:
        done: Callable[[], None] | None = None
        try:
            if self._background_init is not None:
                done = self._background_init()
            self.refresh()
        except Exception as e:
            log.warning("фоновое обновление списка программ: %s", e)
        finally:
            if done is not None:
                with contextlib.suppress(Exception):
                    done()

    def resolve(self, query: str) -> AppInfo | None:
        """Программа по имени; промах — обновить инвентарь (не чаще раза в MISS_REFRESH_S) и искать снова."""
        try:
            apps = self.get()
        except PlatformError as e:  # инвентаря нет и не строится — пусть launch попробует запустить exe
            log.info("инвентарь программ недоступен: %s", e)
            return None
        app = resolve_app(query, apps)
        if app is not None:
            return app
        now = time.monotonic()
        recent = [t for t in (self._last_miss_refresh, self._built_at) if t is not None]
        if recent and now - max(recent) < MISS_REFRESH_S:
            return None
        self._last_miss_refresh = now
        try:
            apps = self.refresh()
        except PlatformError as e:
            log.info("инвентарь не обновлён при промахе «%s»: %s", query, e)
            return None
        return resolve_app(query, apps)


# --- Windows: сбор инвентаря ------------------------------------------------------------------------------


def powershell_exe() -> str:
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def run_get_start_apps() -> list[tuple[str, str]]:
    """Get-StartApps через PowerShell (без окна); ошибка — исключение."""
    argv = [powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", PS_START_APPS]
    r = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=PS_TIMEOUT_S,
        creationflags=NO_WINDOW,
        check=False,
    )
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RuntimeError(f"Get-StartApps: код {r.returncode}: {err}")
    return parse_start_apps(r.stdout)


def start_menu_dirs() -> list[str]:
    out = []
    for var in ("PROGRAMDATA", "APPDATA"):
        base = os.environ.get(var)
        if base:
            out.append(ntpath.join(base, "Microsoft", "Windows", "Start Menu", "Programs"))
    return out


class ShortcutReader:
    """Цели ярлыков через WScript.Shell (COM; создаётся в потоке, где читаются ярлыки)."""

    def __init__(self) -> None:
        import win32com.client  # type: ignore[import-not-found]

        self._shell = win32com.client.Dispatch("WScript.Shell")

    def read(self, lnk: str) -> tuple[str, str]:
        """(цель, аргументы); не прочитался — ("", "")."""
        try:
            sc = self._shell.CreateShortcut(lnk)
            return str(sc.TargetPath or "").strip(), str(sc.Arguments or "").strip()
        except Exception as e:
            log.debug("ярлык %s не прочитан: %s", lnk, e)
            return "", ""


def scan_shortcuts(reader: ShortcutReader | None = None) -> list[Shortcut]:
    """Ярлыки .lnk меню «Пуск» (общие и пользователя), без «Автозагрузки»."""
    reader = reader or ShortcutReader()
    out: list[Shortcut] = []
    for root in start_menu_dirs():
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.casefold() != "startup"]
            for file in files:
                if not file.casefold().endswith(".lnk"):
                    continue
                lnk = ntpath.join(folder, file)
                target, args = reader.read(lnk)
                if target:
                    target = squirrel_exe(target, args)
                out.append(Shortcut(file[:-4], lnk, target, args))
    return out


def scan_apps_folder() -> list[tuple[str, str]]:
    """(имя, AppID) из shell:AppsFolder — запасной путь, если Get-StartApps не сработала."""
    import win32com.client  # type: ignore[import-not-found]

    shell = win32com.client.Dispatch("Shell.Application")
    folder = shell.NameSpace("shell:AppsFolder")
    if folder is None:
        return []
    out: list[tuple[str, str]] = []
    items = folder.Items()
    for i in range(int(items.Count)):
        item = items.Item(i)
        if item is None:
            continue
        name, app_id = str(item.Name or "").strip(), str(item.Path or "").strip()
        if name and app_id and not is_junk(name, app_id):
            out.append((name, app_id))
    return out


def resolve_app_path(app_id: str) -> str:
    """AppID → путь к exe, если он есть на диске: «{GUID}\\…\\x.exe» через известную папку, «C:\\…\\x.exe»."""
    if not exe_name(app_id):
        return ""
    split = split_guid_path(app_id)
    if split is not None:
        from jarvis.platform.windows.winapi import known_folder_path

        base = known_folder_path(split[0])
        if not base:
            return ""
        path = ntpath.join(base, split[1])
    elif _DRIVE.match(app_id):
        path = app_id
    else:
        return ""
    return path if os.path.isfile(path) else ""


def scan_inventory() -> list[AppInfo]:
    """Полный сбор инвентаря (Windows, поток с COM). PowerShell идёт параллельно чтению ярлыков."""
    result: dict[str, Any] = {}

    def run_ps() -> None:
        try:
            result["apps"] = run_get_start_apps()
        except Exception as e:
            result["error"] = e

    ps = threading.Thread(target=run_ps, name="jarvis-get-startapps", daemon=True)
    ps.start()
    try:
        shortcuts = scan_shortcuts()
    except Exception as e:
        log.warning("ярлыки меню «Пуск» не прочитаны: %s", e)
        shortcuts = []
    ps.join(PS_TIMEOUT_S + 5)
    start_apps: list[tuple[str, str]] = result.get("apps") or []
    if not start_apps:
        log.warning("Get-StartApps не сработала (%s) — читаю shell:AppsFolder", result.get("error"))
        try:
            start_apps = scan_apps_folder()
        except Exception as e:
            log.warning("shell:AppsFolder не прочитана: %s", e)
    if not start_apps and not shortcuts:
        raise PlatformError("Не удалось получить список программ")
    apps = merge_inventory(start_apps, shortcuts, resolve_app_path)
    # игры Steam, Epic, GOG — тоже программы: «запусти CS2» — ссылкой лаунчера, без кликов по его окну
    from jarvis.platform.windows import games

    return games.merge(apps, games.scan())


def com_thread_init() -> Callable[[], None]:
    """CoInitialize для фонового потока; возвращает CoUninitialize."""
    import pythoncom  # type: ignore[import-not-found]

    pythoncom.CoInitialize()
    return pythoncom.CoUninitialize
