"""Канонизация и проверка путей Windows (AGENTS.md, «Пути, цели и недоверенные данные»).

Вся логика сравнения — на ntpath/PureWindowsPath и не зависит от ОС: тесты идут на Linux.
ОС-зависимое — только _resolve() (ссылки, junction, 8.3 → длинные имена, снятие \\\\?\\) и _known_folder()
(известные папки Windows, если переменных окружения нет — MCP-процесс мозга стартует с очищенным окружением).
Тесты подменяют обе функции.
"""

import functools
import logging
import ntpath
import os
import re
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Literal

from pc import settings
from pc.result import Caller

log = logging.getLogger("jarvis")

Op = Literal["open", "read", "trash", "list"]
OPS: frozenset[str] = frozenset({"open", "read", "trash", "list"})

# AGENTS.md, «Политика риска»: открыть такой файл — «спросить» (файл без расширения — тоже)
EXECUTABLE_SUFFIXES: frozenset[str] = frozenset(
    {
        *(".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta"),
        *(".msi", ".msp", ".scr", ".pif", ".cpl", ".jar", ".reg", ".lnk", ".url", ".appref-ms"),
        *(".settingcontent-ms", ".library-ms", ".search-ms"),
    }
)
# сверх списка AGENTS.md: тоже запускают код или команды оболочки — спросить безопаснее, чем открыть молча
EXTRA_ACTIVE_SUFFIXES: frozenset[str] = frozenset(
    {
        *(".msc", ".chm", ".scf", ".psm1", ".psd1", ".vb", ".ws", ".wsc", ".application", ".appx"),
        *(".appxbundle", ".msix", ".msixbundle", ".appinstaller", ".diagcab", ".xbap", ".gadget", ".inf"),
        *(".ins", ".mst"),
    }
)

# секретные файлы — закрыты для мозга (по имени, без учёта регистра)
SECRET_NAMES: frozenset[str] = frozenset({".env", "auth.json", ".git-credentials"})
SECRET_SUFFIXES: frozenset[str] = frozenset({".kdbx", ".pem", ".pfx", ".p12", ".key"})

# имена устройств Windows: CON, NUL, COM1… — в любом компоненте и с любым расширением
RESERVED_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{dev}{n}" for dev in ("com", "lpt") for n in "0123456789¹²³"}
)

_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')
_DRIVE = re.compile(r"[A-Za-z]:")
_SHORT_BASE = re.compile(r"[^~]{1,6}~[0-9]{1,6}")

_KF_FLAG_DONT_VERIFY = 0x00004000
_FOLDER_IDS = {
    "Profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    "RoamingAppData": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    "LocalAppData": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "Windows": "{F38BF404-1D43-42F2-9305-67DE0B28FC23}",
    "ProgramFiles": "{905E63B6-C1BF-494E-B29C-65B732D3D21A}",
    "ProgramFilesX86": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}",
    "ProgramData": "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
}


class PathDenied(ValueError):
    """Путь нельзя использовать; текст — причина по-русски."""


@dataclass(frozen=True)
class PathCheck:
    """ok — можно; path — канонический путь ("" — канонизация не удалась); confirm — сначала спросить."""

    ok: bool
    path: str
    reason: str = ""
    confirm: bool = False


# --- канонизация --------------------------------------------------------------------------------


def _normalize(raw: object) -> str:
    """Чистая часть канонизации, без обращения к диску. Недопустимая форма — PathDenied."""
    if isinstance(raw, os.PathLike):
        raw = os.fspath(raw)
    if not isinstance(raw, str):
        raise PathDenied("Путь должен быть строкой.")
    p = raw.strip()
    if len(p) >= 2 and p[0] == p[-1] == '"':  # «Копировать как путь» в Проводнике добавляет кавычки
        p = p[1:-1].strip()
    if not p:
        raise PathDenied("Пустой путь.")
    p = p.replace("/", "\\")
    if p.startswith("\\\\"):
        if p[2:3] in ("?", "."):
            raise PathDenied("Пути вида \\\\?\\ и \\\\.\\ не поддерживаются.")
        raise PathDenied("Сетевые пути (UNC) не поддерживаются.")
    if not _DRIVE.match(p):
        raise PathDenied("Нужен полный путь с буквой диска, например C:\\Users\\me\\Documents.")
    if p[2:3] != "\\":
        raise PathDenied("Путь вида C:файл (относительно текущей папки диска) не поддерживается.")
    if ":" in p[2:]:
        raise PathDenied("Двоеточие внутри пути (потоки NTFS) не поддерживается.")
    if _BAD_CHARS.search(p):
        raise PathDenied("Недопустимые символы в пути.")
    parts: list[str] = []
    for part in p[3:].split("\\"):
        if part in ("", ".", ".."):
            parts.append(part)
            continue
        # Windows отбрасывает хвостовые точки и пробелы: C:\Windows.\System32 — это C:\Windows\System32
        name = part.rstrip(". ")
        if not name:
            raise PathDenied("Недопустимое имя в пути: только точки или пробелы.")
        parts.append(name)
    norm = ntpath.normpath(p[0].upper() + ":\\" + "\\".join(parts))
    for part in norm[3:].split("\\") if len(norm) > 3 else ():
        if part.partition(".")[0].rstrip(" ").casefold() in RESERVED_NAMES:
            raise PathDenied("Имена устройств (CON, NUL, COM1…) в пути не поддерживаются.")
    return norm


def canonical(path: str) -> str:
    """Абсолютный канонический путь Windows: нормализация, затем _resolve() и повторная проверка формы
    (ссылка или junction может вести на сетевой путь). Ошибка — PathDenied(причина)."""
    norm = _normalize(path)
    resolved = _resolve(norm)
    return norm if resolved == norm else _normalize(resolved)


def _resolve(path: str) -> str:
    """ОС-зависимое: раскрыть ссылки и junction, 8.3 → длинные имена, снять \\\\?\\. Не Windows — как есть."""
    if sys.platform != "win32":
        return path
    try:
        real = os.path.realpath(path)  # Python 3.12 раскрывает и junction; несуществующий хвост — как есть
    except (OSError, ValueError, RuntimeError) as e:
        log.debug("realpath не удался: %s", e)
        real = path
    if real.startswith("\\\\?\\UNC\\"):
        real = "\\\\" + real[8:]  # станет UNC и получит отказ при повторной проверке
    elif real.startswith("\\\\?\\"):
        real = real[4:]
    if "~" in real:
        real = _long_path(real)
    return real


def _long_path(path: str) -> str:  # pragma: no cover - только Windows
    """GetLongPathNameW по самому длинному существующему префиксу; хвост — как есть."""
    import ctypes

    fn = _get_long_path_name()
    head, tail = path, []
    while True:
        size = fn(head, None, 0)
        if size:
            buf = ctypes.create_unicode_buffer(size)
            n = fn(head, buf, size)
            if 0 < n < size:
                return ntpath.join(buf.value, *reversed(tail))
        parent, name = ntpath.split(head)
        if not name or parent == head:
            return path
        tail.append(name)
        head = parent


@functools.cache
def _get_long_path_name() -> Any:  # pragma: no cover - только Windows
    import ctypes
    from ctypes import wintypes

    fn = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    return fn


def _known_folder(name: str) -> str | None:
    """Известная папка Windows (SHGetKnownFolderPath): Profile, RoamingAppData, LocalAppData, Windows,
    ProgramFiles, ProgramFilesX86, ProgramData. Не Windows или ошибка — None."""
    if sys.platform != "win32":
        return None
    try:
        return _shell_folder(_FOLDER_IDS[name])
    except Exception as e:  # pragma: no cover - только Windows
        log.debug("известная папка %s не получена: %s", name, e)
        return None


def _shell_folder(guid: str) -> str | None:  # pragma: no cover - только Windows
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    get = ctypes.WinDLL("shell32", use_last_error=True).SHGetKnownFolderPath
    get.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    get.restype = ctypes.c_long  # HRESULT
    free = ctypes.WinDLL("ole32", use_last_error=True).CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    rfid = GUID.from_buffer_copy(uuid.UUID(guid).bytes_le)
    out = ctypes.c_void_p()
    hr = get(ctypes.byref(rfid), _KF_FLAG_DONT_VERIFY, None, ctypes.byref(out))
    try:
        if hr != 0 or not out.value:
            return None
        return ctypes.wstring_at(out.value)
    finally:
        if out.value:
            free(out)


# --- сравнение ----------------------------------------------------------------------------------


def _fold(part: str) -> str:
    # upper() + casefold(): грубее, чем сравнение NTFS (ı → i, ſ → s), — для зон отказа это в запас
    return part.upper().casefold()


def _key(path: str) -> tuple[str, ...]:
    return tuple(_fold(part) for part in PureWindowsPath(path).parts)


def _within(key: tuple[str, ...], root: tuple[str, ...]) -> bool:
    return bool(root) and key[: len(root)] == root


def is_within(path: str, root: str) -> bool:
    """path совпадает с root или лежит внутри: по частям и без учёта регистра.

    C:\\Users\\me ≠ C:\\Users\\meow. Без обращения к диску; для решений сравнивайте пути после canonical().
    """
    try:
        return _within(_key(_normalize(path)), _key(_normalize(root)))
    except PathDenied:
        return False


def _last_name(path: str) -> str:
    return PureWindowsPath(str(path).replace("/", "\\")).name.rstrip(". ")


def _is_short_name(part: str) -> bool:
    """Компонент похож на короткое имя 8.3 (PROGRA~1, RUNNER~1.TXT)."""
    if "~" not in part or part.count(".") > 1:
        return False
    base, _, ext = part.partition(".")
    return len(base) <= 8 and len(ext) <= 3 and _SHORT_BASE.fullmatch(base) is not None


def is_executable_type(path: str) -> bool:
    """Исполняемый или «активный» тип (список AGENTS.md и EXTRA_ACTIVE_SUFFIXES) или файл без расширения."""
    name = _last_name(path)
    if not name:
        return False
    suffix = PureWindowsPath(name).suffix.casefold()
    return not suffix or suffix in EXECUTABLE_SUFFIXES or suffix in EXTRA_ACTIVE_SUFFIXES


def is_secret_file(path: str) -> bool:
    """Файл с секретами по имени: .env (и .env.*), auth.json, .git-credentials, *.kdbx/pem/pfx/p12/key."""
    name = _last_name(path).casefold()
    if not name:
        return False
    if name in SECRET_NAMES or name.startswith(".env."):
        return True
    return PureWindowsPath(name).suffix in SECRET_SUFFIXES


# --- зоны ---------------------------------------------------------------------------------------

_ENV_NAMES = (
    "SystemRoot",
    "windir",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "ProgramData",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
)


@dataclass(frozen=True)
class _Zone:
    key: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class _Zones:
    common: tuple[_Zone, ...]  # отказ всем
    brain: tuple[_Zone, ...]  # отказ мозгу
    profiles: tuple[tuple[str, ...], ...]  # ~ — для правила скрытых папок ~\.*
    config_broken: bool  # конфиг не прочитан: private_paths неизвестны


_zone_cache: tuple[tuple[object, ...], _Zones] | None = None


def _getenv(name: str) -> str:
    # на Windows os.environ без учёта регистра; на Linux (тесты) — точное имя или ВЕРХНИЙ регистр
    return (os.environ.get(name) or os.environ.get(name.upper()) or "").strip()


def _zone_root(raw: str) -> tuple[str, ...] | None:
    try:
        return _key(canonical(raw))
    except PathDenied as e:
        log.debug("зона пропущена: %s", e)
        return None


def _values(*candidates: str | None, default: str = "") -> list[str]:
    found = [c for c in candidates if c]
    return found or ([default] if default else [])


def _build_zones(
    env: dict[str, str], private: tuple[str, ...], data: str, config: str, broken: bool
) -> _Zones:
    common: list[_Zone] = []
    brain: list[_Zone] = []

    def add(target: list[_Zone], raw: str, label: str) -> bool:
        key = _zone_root(raw)
        if key and all(z.key != key for z in target):
            target.append(_Zone(key, label))
        return key is not None

    windows = _values(env["SystemRoot"], env["windir"], _known_folder("Windows"), default="C:\\Windows")
    programs = [
        *_values(env["ProgramFiles"], _known_folder("ProgramFiles"), default="C:\\Program Files"),
        *_values(
            env["ProgramFiles(x86)"], _known_folder("ProgramFilesX86"), default="C:\\Program Files (x86)"
        ),
        *_values(env["ProgramW6432"]),
    ]
    program_data = _values(env["ProgramData"], _known_folder("ProgramData"), default="C:\\ProgramData")
    # профиль и AppData: нет ни переменной, ни ответа ОС — зону не добавляем, ничего не выдумываем
    profiles = _values(env["USERPROFILE"], _known_folder("Profile"))
    roaming = _values(env["APPDATA"], _known_folder("RoamingAppData")) or [
        p + "\\AppData\\Roaming" for p in profiles
    ]
    local = _values(env["LOCALAPPDATA"], _known_folder("LocalAppData")) or [
        p + "\\AppData\\Local" for p in profiles
    ]

    for raw in windows:
        add(common, raw, "системная папка Windows")
    for raw in programs:
        add(common, raw, "папка программ")
    for raw in program_data:
        add(common, raw, "общие данные программ")
    for raw in roaming:
        add(common, raw + "\\Microsoft", "данные Microsoft в профиле")
    for raw in profiles:
        add(common, raw + "\\.ssh", "ключи SSH")
        add(common, raw + "\\.codex", "настройки Codex")
    for i, raw in enumerate(private):
        if not add(common, raw, "закрытая папка из настроек"):
            log.warning("private_paths[%d] пропущен: нужен полный локальный путь с буквой диска", i)

    for raw in (*roaming, *local):
        add(brain, raw, "данные программ в профиле")
    add(brain, data, "данные Jarvis")
    add(brain, config, "конфиг Jarvis")

    profile_keys: list[tuple[str, ...]] = []
    for raw in profiles:
        key = _zone_root(raw)
        if key and key not in profile_keys:
            profile_keys.append(key)
    return _Zones(tuple(common), tuple(brain), tuple(profile_keys), broken)


def _zones() -> _Zones:
    """Зоны, канонизированные через _resolve; кэш — по переменным, private_paths, data_dir() и конфигу."""
    global _zone_cache
    pc = settings.load_pc_settings()
    broken = bool(settings.config_error())
    env = {name: _getenv(name) for name in _ENV_NAMES}
    data, config = str(settings.data_dir()), str(settings.config_path())
    # _resolve и _known_folder в ключе: их подмена в тестах сбрасывает кэш
    key = (tuple(env.values()), pc.private_paths, data, config, broken, _resolve, _known_folder)
    cached = _zone_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    zones = _build_zones(env, pc.private_paths, data, config, broken)
    _zone_cache = (key, zones)
    return zones


# --- проверка -----------------------------------------------------------------------------------


def _zone_reason(canon: str, key: tuple[str, ...], caller: Caller, zones: _Zones) -> str:
    for zone in zones.common:
        if _within(key, zone.key):
            return f"Путь в закрытой зоне ({zone.label})."
    if caller != "brain":
        return ""
    if zones.config_broken:
        return "Конфиг jarvis.toml не читается — пути для GPT закрыты, пока его не исправят (jarvis doctor)."
    if any(_is_short_name(part) for part in key[1:]):
        return "Короткие имена 8.3 не поддерживаются — нужен полный путь."
    for zone in zones.brain:
        if _within(key, zone.key):
            return f"Путь закрыт для GPT ({zone.label})."
    for profile in zones.profiles:
        if len(key) > len(profile) and _within(key, profile) and key[len(profile)].startswith("."):
            return "Путь закрыт для GPT (скрытая папка профиля)."
    if is_secret_file(canon):
        return "Путь закрыт для GPT (файл с секретами)."
    return ""


def _trash_reason(key: tuple[str, ...], caller: Caller, zones: _Zones) -> str:
    if len(key) <= 1:
        return "Корень диска в корзину не отправляется."
    roots = [z.key for z in zones.common] + list(zones.profiles)
    if caller == "brain":
        roots += [z.key for z in zones.brain]
    if any(_within(root, key) for root in roots):
        return "В этой папке есть закрытые зоны — в корзину нельзя."
    return ""


def _is_dir(canon: str, is_dir: bool | None) -> bool:
    if is_dir is not None:
        return is_dir
    if not PureWindowsPath(canon).name:  # корень диска
        return True
    return os.path.isdir(canon)


def check(path: str, caller: Caller, op: Op, is_dir: bool | None = None) -> PathCheck:
    """Можно ли caller выполнить op над path. op ∈ open | read | trash | list.

    is_dir: True/False — папка или файл; None — спросить ОС. confirm=True — op="open" для файла
    исполняемого или «активного» типа (папок это не касается).
    """
    if op not in OPS:
        raise ValueError(f"неизвестная операция с путём: {op!r}")
    if caller not in ("user", "brain"):
        raise ValueError(f"неизвестный вызывающий: {caller!r}")
    try:
        canon = canonical(path)
    except PathDenied as e:
        return PathCheck(False, "", str(e))
    zones = _zones()
    key = _key(canon)
    reason = _zone_reason(canon, key, caller, zones)
    if not reason and op == "trash":
        reason = _trash_reason(key, caller, zones)
    if reason:
        return PathCheck(False, canon, reason)
    confirm = op == "open" and not _is_dir(canon, is_dir) and is_executable_type(canon)
    return PathCheck(True, canon, "", confirm)


def hidden_for_brain(path: str) -> bool:
    """True — путь нельзя показывать мозгу: зоны всем и мозгу, секреты, недопустимая форма пути."""
    return not check(path, "brain", "list", is_dir=False).ok
