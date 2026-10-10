"""Инвентарь приложений: Get-StartApps + отображаемые имена Shell, кэш <data>\\apps.json, поиск по имени.

Кэш обновляют только `jarvis run` (в фоне, раз в сутки и при промахе resolve — enable_auto_refresh)
и `jarvis apps --refresh`; pc.mcp и прочие команды CLI только читают его.

Поиск (resolve/top): алиас из [aliases] → сравнение нормализованных имён в одном «латинском» пространстве:
casefold, ё→е, без пунктуации, русские падежные окончания срезаны, кириллица транслитерирована, похожие
звуки сведены (c/k, w/v, y/i, двойные буквы). Разговорные формы («телега», «хром», «вскод», «настройки») —
таблица GROUPS. Счёт 0–100; ниже THRESHOLD — приложения нет.
"""

import json
import logging
import ntpath
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from pc import policy, settings, subproc
from pc.result import Caller, Result, fail, ok

log = logging.getLogger("jarvis")

THRESHOLD = 85.0
AUTO_REFRESH_MIN_S = 600.0
STA_TIMEOUT_S = 30.0
CACHE_VERSION = 1

POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
# без смены кодировки PowerShell 5.1 пишет в pipe в OEM 866; @() — чтобы одно приложение тоже дало массив
PS_COMMAND = (
    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
    "ConvertTo-Json -Compress -InputObject @(Get-StartApps | Select-Object Name,AppID)"
)
REFRESH_ARGV = [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", PS_COMMAND]
REFRESH_TIMEOUT_S = 30


@dataclass(frozen=True)
class App:
    name: str
    app_id: str


class RefreshError(RuntimeError):
    """Список приложений не получен; старый кэш не тронут."""


# --- нормализация ---------------------------------------------------------------------------------

# Разговорные и альтернативные имена: одна строка — одно приложение. Строки на «.exe» — имя процесса
# (для procs и поиска окна), не ключ поиска. Группа подключается к приложению, если его имя (или имя без
# служебных слов) совпадает с одной из строк группы — так «настройки» находят и «Параметры», и «Settings».
GROUPS: tuple[tuple[str, ...], ...] = (
    ("telegram.exe", "telegram", "telegram desktop", "телеграм", "телеграмм", "телега", "тг", "телеграф"),
    ("chrome.exe", "google chrome", "chrome", "хром", "гугл хром", "гугл", "гугл хроме"),
    (
        "code.exe",
        "visual studio code",
        "vs code",
        "vscode",
        "code",
        "вскод",
        "вс код",
        "вэс код",
        "визуал студио код",
    ),
    ("steam.exe", "steam", "стим"),
    ("discord.exe", "discord", "дискорд"),
    (
        "explorer.exe",
        "проводник",
        "file explorer",
        "explorer",
        "эксплорер",
        "мой компьютер",
        "этот компьютер",
    ),
    ("notepad.exe", "блокнот", "notepad", "нотпад", "ноутпад"),
    ("systemsettings.exe", "параметры", "settings", "настройки", "параметры windows", "настройки windows"),
    ("winword.exe", "word", "microsoft word", "ворд", "майкрософт ворд"),
    ("excel.exe", "excel", "microsoft excel", "эксель", "ексель", "иксель"),
    (
        "powerpnt.exe",
        "powerpoint",
        "microsoft powerpoint",
        "пауэрпоинт",
        "павер поинт",
        "поверпоинт",
        "пауэр поинт",
    ),
    ("outlook.exe", "outlook", "outlook classic", "аутлук"),
    ("obs64.exe", "obs studio", "obs", "обс", "обс студио"),
    ("calculatorapp.exe", "калькулятор", "calculator"),
    ("snippingtool.exe", "ножницы", "snipping tool"),
    ("mspaint.exe", "paint", "пейнт", "пэйнт", "паинт"),
    ("taskmgr.exe", "диспетчер задач", "task manager", "диспетчер"),
    ("windowsterminal.exe", "терминал", "terminal", "windows terminal"),
    ("msedge.exe", "microsoft edge", "edge", "эдж", "едж"),
    ("firefox.exe", "mozilla firefox", "firefox", "файрфокс", "фаерфокс", "огнелис"),
    ("spotify.exe", "spotify", "спотифай", "спотик"),
    ("vlc.exe", "vlc media player", "vlc", "влц", "влс"),
    ("7zfm.exe", "7 zip file manager", "7 zip", "7zip", "7зип", "севен зип", "архиватор"),
    ("notepad++.exe", "notepad++", "нотпад плюс плюс", "ноутпад плюс плюс", "нотепад плюс плюс"),
    ("qbittorrent.exe", "qbittorrent", "торрент", "кубиторрент", "кьюбиторрент"),
    ("zoom.exe", "zoom workplace", "zoom", "зум"),
    ("slack.exe", "slack", "слак"),
    ("whatsapp", "ватсап", "вацап", "вотсап", "вотсапп"),
    ("яндекс музыка", "yandex music"),
    ("browser.exe", "яндекс браузер", "yandex browser", "яндекс"),
    ("gimp", "гимп"),
    ("blender.exe", "blender", "блендер"),
    ("epicgameslauncher.exe", "epic games launcher", "epic games", "эпик", "эпик геймс", "эпик гейм"),
    ("radeonsoftware.exe", "amd software adrenalin edition", "amd software", "адреналин", "amd adrenalin"),
    ("everything.exe", "everything", "эверитинг", "эврисинг"),
    ("почта", "mail", "outlook mail"),
    ("фотографии", "photos", "фото", "фотки"),
    ("камера", "camera"),
    ("часы", "clock", "будильник"),
    ("панель управления", "control panel"),
    ("cmd.exe", "командная строка", "command prompt", "cmd", "консоль"),
    ("powershell.exe", "windows powershell", "powershell", "пауэршелл", "павершелл", "повершелл"),
    ("microsoft store", "store", "магазин", "магазин приложений", "майкрософт стор"),
    ("obsidian.exe", "obsidian", "обсидиан"),
)

# служебные слова: «Google Chrome» → «chrome», «приложение телеграм» → «телеграм»
STOP_WORDS = (
    "microsoft майкрософт windows виндовс google гугл mozilla app приложение программа программу прога прогу "
    "desktop десктоп classic edition software launcher workplace media player плеер file manager games "
    "for and the x64 x86 bit beta preview"
)

_NON_WORD = re.compile(r"[\W_]+")
_CYRILLIC = re.compile(r"[а-я]")
# падежные окончания — длинные первыми
_ENDINGS_TEXT = (
    "ами ями ого его ому ему ыми ими ой ей ом ем ам ям ах ях ую юю ая яя ое ее ые ие ый ий ов ев "
    "а я у ю е и ы о ь"
)
_ENDINGS = sorted(_ENDINGS_TEXT.split(), key=len, reverse=True)
_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
        "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "i", "ь": "", "э": "e",
        "ю": "yu", "я": "ya",
    }
)  # fmt: skip
_C_SOFT = re.compile(r"c(?=[eiy])")
_DOUBLE = re.compile(r"(.)\1+")


def normalize(text: str) -> str:
    """casefold, ё→е, «+» → «plus», пунктуация → пробел, пробелы схлопнуты."""
    t = text.casefold().replace("ё", "е").replace("+", " plus ")
    return " ".join(_NON_WORD.sub(" ", t).split())


def _stem_word(word: str) -> str:
    """Срезать русское падежное окончание: «телегу» → «телег», «хроме» → «хром». Латиница — как есть."""
    if len(word) <= 3 or not _CYRILLIC.search(word):
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def _stem(text: str) -> str:
    return " ".join(_stem_word(w) for w in text.split())


def _fold(text: str) -> str:
    """Нормализованный текст → «латинское» написание со сведёнными похожими звуками."""
    t = text.translate(_TRANSLIT)
    t = t.replace("ph", "f").replace("ck", "k").replace("kh", "h").replace("w", "v").replace("q", "k")
    t = t.replace("x", "ks").replace("j", "dzh")
    t = _C_SOFT.sub("s", t).replace("ch", "\x00").replace("c", "k").replace("\x00", "ch")
    t = t.replace("y", "i")
    return _DOUBLE.sub(r"\1", t)


def latin(text: str) -> str:
    """Сырой текст → сравнимая форма: normalize + транслитерация и сведение звуков."""
    return _fold(normalize(text))


_STOP: frozenset[str] = frozenset()


def _stop_words() -> frozenset[str]:
    global _STOP
    if not _STOP:
        words = normalize(STOP_WORDS).split()
        _STOP = frozenset(_fold(w) for w in words) | frozenset(_fold(_stem_word(w)) for w in words)
    return _STOP


def core(folded: str) -> str:
    """Свернутая форма без служебных слов и чисел; если ничего не осталось — пустая строка."""
    stop = _stop_words()
    return " ".join(w for w in folded.split() if w not in stop and not w.isdigit())


def _forms(text: str) -> list[str]:
    """Свернутые формы текста: как есть и с срезанными окончаниями (без повторов)."""
    n = normalize(text)
    out: list[str] = []
    for f in (_fold(n), _fold(_stem(n))):
        if f and f not in out:
            out.append(f)
    return out


@dataclass(frozen=True)
class _Group:
    exe: str | None
    keys: frozenset[str]  # свернутые формы имён группы (с окончаниями и без, без служебных слов)
    variants: frozenset[str]  # keys + имя процесса без .exe


_GROUP_TABLE: list[_Group] = []


def _groups() -> list[_Group]:
    if not _GROUP_TABLE:
        table = []
        for group in GROUPS:
            exe = next((s for s in group if s.endswith(".exe")), None)
            keys: set[str] = set()
            for s in group:
                if s.endswith(".exe"):
                    continue
                for f in _forms(s):
                    keys.add(f)
                    if c := core(f):
                        keys.add(c)
            variants = set(keys)
            if exe:
                variants.add(latin(exe.removesuffix(".exe")))
            table.append(_Group(exe, frozenset(keys), frozenset(variants)))
        _GROUP_TABLE.extend(table)
    return _GROUP_TABLE


def _identity(name: str) -> set[str]:
    """Свернутые формы имени приложения (с окончаниями и без, без служебных слов)."""
    out: set[str] = set()
    for f in _forms(name):
        out.add(f)
        if c := core(f):
            out.add(c)
    return out


def _app_groups(app: App) -> list[_Group]:
    ident = _identity(app.name)
    return [g for g in _groups() if ident & g.keys]


def name_variants(text: str) -> set[str]:
    """Свернутые формы текста и, если это разговорное имя из GROUPS, — все имена группы и имя её процесса.

    Нужна procs/windows: «телега» → {…, "telegram"}; сравнивать с latin(имя exe без .exe).
    """
    ident = _identity(text)
    out = set(ident)
    for g in _groups():
        if ident & g.keys:
            out |= g.variants
    return out


_OFFICE_EXE = re.compile(r"\.([a-z0-9_]+\.exe)\.\d+$", re.IGNORECASE)  # Microsoft.Office.WINWORD.EXE.15
_SQUIRREL = re.compile(r"^com\.squirrel\.[^.]+\.([^.]+)$", re.IGNORECASE)  # com.squirrel.Discord.Discord


def app_exe(app: App) -> str | None:
    """Имя процесса приложения (casefold) по AppID или таблице GROUPS; неизвестно — None."""
    app_id = app.app_id.strip()
    if app_id.casefold().endswith(".exe"):
        return ntpath.basename(app_id).casefold()
    m = _OFFICE_EXE.search(app_id) or _SQUIRREL.match(app_id)
    if m:
        exe = m.group(1).casefold()
        return exe if exe.endswith(".exe") else f"{exe}.exe"
    for g in _app_groups(app):
        if g.exe:
            return g.exe
    return None


# --- индекс поиска ----------------------------------------------------------------------------------

SPAN_WEIGHT = 0.95  # часть многословного имени: «chrome» в «google chrome»
CORE_QUERY_WEIGHT = 0.97  # запрос без служебных слов: «приложение телеграм»
STOP_ONLY_WEIGHT = 0.9  # запрос из одних служебных слов («media player») — только точное совпадение уверенно
MIN_FUZZY_LEN = 3
MIN_PREFIX_LEN = 4
MIN_PREFIX_COVER = 0.6  # «телег» → «telegram» да, «диск» → «discord» нет


class _Index:
    """Предвычисленные ключи поиска одного инвентаря."""

    def __init__(self, apps: tuple[App, ...]) -> None:
        self.apps = apps
        self.exact: dict[str, list[int]] = {}
        self.keys: list[str] = []
        self.owners: list[int] = []
        self.weights: list[float] = []
        self.prefixes: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for i, app in enumerate(apps):
            full: set[str] = _identity(app.name)
            for g in _app_groups(app):
                full |= g.keys
            for key in full:
                self._add(key, i, 1.0, seen)
                self.exact.setdefault(key, []).append(i)
                words = key.split()
                for start in range(len(words)):
                    self.prefixes.append((" ".join(words[start:]), i))
                for span in _spans(words):
                    self._add(span, i, SPAN_WEIGHT, seen)

    def _add(self, key: str, owner: int, weight: float, seen: set[tuple[str, int]]) -> None:
        if (key, owner) in seen:
            return
        seen.add((key, owner))
        self.keys.append(key)
        self.owners.append(owner)
        self.weights.append(weight)

    def scores(self, text: str) -> list[float]:
        """Лучший счёт каждого приложения (индекс как в apps)."""
        best = [0.0] * len(self.apps)
        n = normalize(text)
        if not n:
            return best
        queries: list[tuple[str, float, bool]] = []  # форма, вес, можно ли префикс
        for f in _forms(n):
            c = core(f)
            queries.append((f, 1.0 if c else STOP_ONLY_WEIGHT, True))
            if c and c != f:
                queries.append((c, CORE_QUERY_WEIGHT, False))
        for q, weight, allow_prefix in queries:
            for i in self.exact.get(q, ()):
                best[i] = 100.0
            if len(q.replace(" ", "")) < MIN_FUZZY_LEN:
                continue
            for _key, score, k in process.extract(
                q, self.keys, scorer=fuzz.ratio, limit=None, score_cutoff=60
            ):
                s = score * self.weights[k] * weight
                owner = self.owners[k]
                if s > best[owner]:
                    best[owner] = s
            if allow_prefix and len(q) >= MIN_PREFIX_LEN:
                for suffix, owner in self.prefixes:
                    if len(suffix) > len(q) and suffix.startswith(q):
                        cover = len(q) / len(suffix)
                        s = 80.0 + 20.0 * cover
                        if cover >= MIN_PREFIX_COVER and s > best[owner]:
                            best[owner] = s
        return best


def _spans(words: list[str]) -> Iterable[str]:
    """Непрерывные части многословного имени, кроме него самого, коротких и из одних служебных слов."""
    stop = _stop_words()
    n = len(words)
    for size in range(1, n):
        for start in range(n - size + 1):
            part = words[start : start + size]
            if all(w in stop or w.isdigit() for w in part):
                continue
            span = " ".join(part)
            if len(span.replace(" ", "")) >= MIN_FUZZY_LEN:
                yield span


# --- инвентарь ----------------------------------------------------------------------------------


class _Inventory:
    """Снимок инвентаря. key — (путь кэша, mtime, размер); None — задан через set_inventory()."""

    def __init__(self, key: tuple[Any, ...] | None, apps: Iterable[App]) -> None:
        self.key = key
        self.apps = tuple(apps)
        self._index: _Index | None = None

    def index(self) -> _Index:
        if self._index is None:
            self._index = _Index(self.apps)
        return self._index


_lock = threading.Lock()
_inv: _Inventory | None = None
_auto_refresh = False
_last_auto_refresh = 0.0


def cache_path() -> Path:
    return settings.data_dir() / "apps.json"


def _file_key(path: Path) -> tuple[Any, ...]:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _read_cache(path: Path) -> list[App]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("кэш приложений не прочитан (%s): %s", path, e)
        return []
    if (
        not isinstance(raw, dict)
        or raw.get("version") != CACHE_VERSION
        or not isinstance(raw.get("apps"), list)
    ):
        log.warning("кэш приложений неизвестного формата: %s", path)
        return []
    return _apps_from(raw["apps"])


def _apps_from(items: Iterable[Any]) -> list[App]:
    out: list[App] = []
    for item in items:
        if isinstance(item, App):
            out.append(item)
        elif isinstance(item, dict):
            name, app_id = item.get("name"), item.get("app_id")
            if isinstance(name, str) and isinstance(app_id, str) and name.strip() and app_id.strip():
                out.append(App(name.strip(), app_id.strip()))
    return out


def _write_cache(apps: list[App]) -> Path:
    """Атомарно: tmp рядом + os.replace."""
    path = settings.data_file("apps.json")
    payload = {
        "version": CACHE_VERSION,
        "updated": int(time.time()),
        "apps": [{"name": a.name, "app_id": a.app_id} for a in apps],
    }
    tmp = path.with_name(f"apps.json.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def _current() -> _Inventory:
    """Снимок из памяти; файл кэша изменился (другой процесс обновил) — перечитать."""
    global _inv
    inv = _inv
    if inv is not None and inv.key is None:
        return inv
    path = cache_path()
    key = _file_key(path)
    if inv is not None and inv.key == key:
        return inv
    with _lock:
        if _inv is not None and (_inv.key is None or _inv.key == key):
            return _inv
        _inv = _Inventory(key, _read_cache(path))
        return _inv


def inventory() -> list[App]:
    """Инвентарь из памяти; первый вызов — из кэша <data>\\apps.json; кэша нет — []."""
    return list(_current().apps)


def set_inventory(apps: Iterable[App | dict[str, str]] | None) -> None:
    """Подменить инвентарь (тесты, `jarvis bench`); принимает App или dict name/app_id. None — снова кэш."""
    global _inv
    with _lock:
        _inv = None if apps is None else _Inventory(None, _apps_from(apps))


# --- обновление -----------------------------------------------------------------------------------

_JUNK_NAME = re.compile(
    r"\b(uninstall\w*|deinstall\w*|uninst|удалить|удаление|удалени\w*|деинсталл\w*|readme|read me|help|"
    r"справка|справочник|документация|documentation|manual|руководство|website|web site|веб сайт|сайт|"
    r"homepage|release notes|changelog|change log|what s new|license|лицензия|лицензионное)\b"
)
_JUNK_ID = re.compile(
    r"(^https?:|unins\d*\.exe$|uninst\w*\.exe$|uninstall\w*\.exe$|\.(txt|rtf|pdf|chm|hlp|htm|html|url|md|log)$)",
    re.IGNORECASE,
)


def is_junk(name: str, app_id: str) -> bool:
    """Деинсталлятор, справка, readme, ссылка на сайт — не приложение."""
    return bool(_JUNK_NAME.search(normalize(name)) or _JUNK_ID.search(app_id.strip()))


def parse_start_apps(stdout: bytes) -> list[App]:
    """Разобрать вывод Get-StartApps | ConvertTo-Json: массив или один объект; без мусора и повторов."""
    raw = json.loads(stdout.decode("utf-8-sig").strip() or "[]")
    items = raw if isinstance(raw, list) else [raw]
    out: list[App] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        app_id = str(item.get("AppID") or "").strip()
        if not name or not app_id or is_junk(name, app_id):
            continue
        key = (normalize(name), app_id.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(App(name, app_id))
    return out


def _merge_display_names(apps: list[App], shell: Iterable[tuple[str, str]]) -> list[App]:
    """Добавить отображаемые имена Shell (AppsFolder), если они отличаются: тот же app_id, другое имя."""
    by_id = {a.app_id.casefold(): a.app_id for a in apps}
    seen = {(normalize(a.name), a.app_id.casefold()) for a in apps}
    out = list(apps)
    for name, path in shell:
        name, path = (name or "").strip(), (path or "").strip()
        app_id = by_id.get(path.casefold())
        if not name or app_id is None or is_junk(name, app_id):
            continue
        key = (normalize(name), app_id.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(App(name, app_id))
    return out


def _start_apps() -> list[App]:
    try:
        r = subproc.run(REFRESH_ARGV, timeout=REFRESH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RefreshError("PowerShell не ответил за 30 с — список приложений не обновлён") from None
    except OSError as e:
        raise RefreshError(f"Не удалось запустить PowerShell: {e}") from e
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise RefreshError(f"Get-StartApps завершился с кодом {r.returncode}: {err}")
    try:
        apps = parse_start_apps(r.stdout)
    except (ValueError, UnicodeDecodeError) as e:
        raise RefreshError(f"Не разобрал вывод Get-StartApps: {e}") from e
    if not apps:
        raise RefreshError("Get-StartApps вернул пустой список — кэш не тронут")
    return apps


def refresh() -> list[App]:
    """Get-StartApps + отображаемые имена Shell → атомарная запись кэша и замена инвентаря в памяти.

    Ошибка PowerShell — RefreshError (старый кэш не тронут). Отображаемые имена необязательны:
    их ошибка — только в лог.
    """
    global _inv
    apps = _start_apps()
    try:
        apps = _merge_display_names(apps, api().shell_app_names())
    except Exception as e:
        log.warning("отображаемые имена приложений не получены: %s", e)
    path = _write_cache(apps)
    with _lock:
        _inv = _Inventory(_file_key(path), apps)
    log.info("инвентарь приложений обновлён: %d", len(apps))
    return list(apps)


def enable_auto_refresh(on: bool) -> None:
    """Обновлять инвентарь при промахе resolve (не чаще раза в 10 мин). Только для `jarvis run`."""
    global _auto_refresh
    _auto_refresh = bool(on)


def _maybe_auto_refresh() -> bool:
    global _last_auto_refresh
    if not _auto_refresh:
        return False
    now = time.monotonic()
    with _lock:
        if _last_auto_refresh and now - _last_auto_refresh < AUTO_REFRESH_MIN_S:
            return False
        _last_auto_refresh = now
    try:
        refresh()
    except Exception as e:
        log.warning("обновление инвентаря при промахе: %s", e)
        return False
    return True


# --- поиск --------------------------------------------------------------------------------------


def _alias_target(name: str) -> str | None:
    aliases = settings.load_pc_settings().aliases
    if not aliases:
        return None
    key = name.strip().casefold()
    if key in aliases:
        return aliases[key]
    n = normalize(name)
    for k, v in aliases.items():
        if normalize(k) == n:
            return v
    return None


def _best(inv: _Inventory, name: str) -> tuple[int | None, float]:
    if not inv.apps:
        return None, 0.0
    scores = inv.index().scores(name)
    best_i = max(range(len(scores)), key=lambda i: (scores[i], -i))
    return best_i, scores[best_i]


def _by_alias(inv: _Inventory, name: str) -> tuple[App | None, float]:
    target = _alias_target(name)
    if not target:
        return None, 0.0
    t = target.strip().casefold()
    for app in inv.apps:
        if app.name.casefold() == t:
            return app, 100.0
    i, score = _best(inv, target)
    if i is not None and score >= THRESHOLD:
        return inv.apps[i], score
    return None, 0.0


def _resolve_once(name: str) -> tuple[App | None, float]:
    inv = _current()
    app, score = _by_alias(inv, name)
    if app is not None:
        return app, score
    i, score = _best(inv, name)
    if i is None or score < THRESHOLD:
        return None, round(score, 1)
    return inv.apps[i], round(score, 1)


def lookup(name: str) -> tuple[App | None, float]:
    """Как resolve, но без обновления инвентаря при промахе — для поиска окна или процесса."""
    if not name or not name.strip():
        return None, 0.0
    return _resolve_once(name)


def resolve(name: str) -> tuple[App | None, float]:
    """Приложение по имени: алиас → нечёткое сравнение. Ниже THRESHOLD — (None, лучший счёт).

    При промахе и enable_auto_refresh(True) — refresh() (не чаще раза в 10 мин) и повтор.
    """
    app, score = lookup(name)
    if app is None and name and name.strip() and _maybe_auto_refresh():
        app, score = _resolve_once(name)
    return app, score


def top(name: str, n: int = 5) -> list[tuple[App, float]]:
    """Лучшие n приложений со счётом (по одному на app_id), по убыванию."""
    inv = _current()
    if not inv.apps or not name.strip():
        return []
    scores = inv.index().scores(name)
    alias_app, _ = _by_alias(inv, name)
    if alias_app is not None:
        scores[inv.apps.index(alias_app)] = 100.0
    best: dict[str, tuple[float, int]] = {}
    for i, app in enumerate(inv.apps):
        key = app.app_id.casefold()
        if scores[i] > 0 and (key not in best or scores[i] > best[key][0]):
            best[key] = (scores[i], i)
    ranked = sorted(best.values(), key=lambda si: (-si[0], si[1]))
    return [(inv.apps[i], round(s, 1)) for s, i in ranked[:n]]


def list_apps_result(query: str | None, caller: Caller) -> Result:
    """Без запроса — все имена; с запросом — топ-5 со счётом. data — список dict."""
    denied = policy.require("list_apps", caller, "Показать список приложений")
    if denied is not None:
        return denied
    if not query or not query.strip():
        names = sorted({a.name for a in inventory()}, key=str.casefold)
        if not names:
            return fail("Список приложений пуст — обновите его: jarvis apps --refresh")
        return ok(f"Приложений: {len(names)}", [{"name": n} for n in names])
    found = top(query, 5)
    data = [{"name": a.name, "score": s} for a, s in found]
    if not found or found[0][1] < THRESHOLD:
        return ok(f"Приложение «{query.strip()}» не найдено; похожие: {len(data)}", data)
    return ok(f"Лучшее совпадение: {found[0][0].name}", data)


# --- STA-поток для COM ----------------------------------------------------------------------------

_sta_lock = threading.Lock()
_Job = tuple[Future[Any], Callable[..., Any], tuple[Any, ...]]
_sta_queue: queue.Queue[_Job] | None = None
_sta_thread: threading.Thread | None = None


def _sta_worker(jobs: queue.Queue[_Job]) -> None:
    if sys.platform == "win32":
        try:
            import pythoncom

            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        except Exception:
            log.exception("CoInitializeEx в STA-потоке не удался")
    while True:
        fut, fn, args = jobs.get()
        if not fut.set_running_or_notify_cancel():
            continue
        try:
            fut.set_result(fn(*args))
        except BaseException as e:
            fut.set_exception(e)


def run_sta(fn: Callable[..., Any], *args: Any) -> Any:
    """Выполнить fn(*args) в постоянном потоке с COM (STA); исключение пробрасывается вызывающему.

    Ждём не дольше STA_TIMEOUT_S: зависший вызов — TimeoutError, следующий вызов получит новый поток.
    """
    global _sta_queue, _sta_thread
    if threading.current_thread() is _sta_thread:
        return fn(*args)
    with _sta_lock:
        if _sta_queue is None or _sta_thread is None or not _sta_thread.is_alive():
            _sta_queue = queue.Queue()
            _sta_thread = threading.Thread(
                target=_sta_worker, args=(_sta_queue,), name="jarvis-sta", daemon=True
            )
            _sta_thread.start()
        jobs = _sta_queue
    fut: Future[Any] = Future()
    jobs.put((fut, fn, args))
    try:
        return fut.result(timeout=STA_TIMEOUT_S)
    except TimeoutError:
        if fut.done():  # TimeoutError бросила сама fn
            raise
        fut.cancel()
        with _sta_lock:
            if _sta_queue is jobs:
                _sta_queue = None
                _sta_thread = None
        raise TimeoutError(f"COM-поток не ответил за {STA_TIMEOUT_S:.0f} с") from None


# --- слой ОС ----------------------------------------------------------------------------------------


def _shell_app_names() -> list[tuple[str, str]]:
    """(отображаемое имя, AppID) из shell:AppsFolder. Только в STA-потоке."""
    import win32com.client

    shell = win32com.client.Dispatch("Shell.Application")
    folder = shell.NameSpace("shell:AppsFolder")
    if folder is None:
        return []
    items = folder.Items()
    out: list[tuple[str, str]] = []
    for i in range(int(items.Count)):
        item = items.Item(i)
        if item is not None:
            out.append((str(item.Name or ""), str(item.Path or "")))
    return out


class _Api:
    def shell_app_names(self) -> list[tuple[str, str]]:
        if sys.platform != "win32":
            return []
        return run_sta(_shell_app_names)


_api: Any = None


def api() -> Any:
    global _api
    if _api is None:
        _api = _Api()
    return _api
