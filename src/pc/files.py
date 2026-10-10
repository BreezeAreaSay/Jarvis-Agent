"""Файлы и открытие целей: поиск через es.exe, известные папки, открытие, корзина, чтение текста.

open_target — ЕДИНСТВЕННОЕ место во всём src, где вызывается os.startfile (внутри `_Api.startfile`), и он
никогда не получает сырую строку от модели: приложение — только `shell:AppsFolder\\<AppID>` из инвентаря;
папка или файл — канонический путь после paths.check; URL — после разбора urllib.parse, схема только
http/https.
Всё прочее (cmd, powershell, shell:, file:, search-ms:, ms-msdt:, ms-settings:, …) — отказ.
"""

import codecs
import contextlib
import csv
import io
import logging
import os
import re
import subprocess
import sys
import unicodedata
import urllib.parse
from pathlib import PureWindowsPath
from typing import Literal

from pc import apps, paths, policy, privacy, settings, subproc
from pc.result import Caller, Result, fail, ok

log = logging.getLogger("jarvis")

Kind = Literal["file", "folder", "any"]
OpenKind = Literal["app", "folder", "file", "url"]

ES_TIMEOUT_S = 5.0
MAX_LIMIT = 100
MAX_READ = 65536
ASFW_ANY = 0xFFFFFFFF

# известная папка → FOLDERID (KnownFolders.h)
FOLDER_IDS = {
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}
# как человек называет папку: все формы — после casefold и ё → е
FOLDER_FORMS: dict[str, tuple[str, ...]] = {
    "downloads": (
        *("загрузки", "загрузок", "загрузкам", "загрузками", "загрузках", "закачки", "закачек"),
        *("скачанное", "скачанного", "скачанные", "скачанных", "скачанным", "скачанными"),
        *("downloads", "download"),
    ),
    "documents": ("документы", "документов", "документам", "документами", "документах", "documents"),
    "desktop": (
        *("рабочий стол", "рабочего стола", "рабочему столу", "рабочим столом", "рабочем столе"),
        "desktop",
    ),
    "pictures": (
        *("изображения", "изображений", "изображениям", "изображениями", "изображениях"),
        *("картинки", "картинок", "картинкам", "картинками", "картинках", "pictures"),
    ),
    "videos": ("видео", "видеозаписи", "видеозаписей", "videos", "video"),
    "music": ("музыка", "музыки", "музыке", "музыку", "музыкой", "music"),
}
_FOLDER_LOOKUP = {form: key for key, forms in FOLDER_FORMS.items() for form in forms}
_FOLDER_NOISE = {"папка", "папку", "папке", "папки", "папкой", "мои", "моя", "мою", "мой"}

_DRIVE_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_HTTP = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN = re.compile(r"^(?:[\w-]+\.)+[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#]\S*)?$")
_ES_FUNCTIONS = re.compile(r"(?:content|regex)\s*:", re.IGNORECASE)
# похоже на командную строку: операторы оболочки, переменные, ключи вида /c и -Command
_SHELL_CHARS = re.compile(r"&&|\|\||[|<>^`;$]|%[^%\s]+%|(?:^|\s)[/-][A-Za-z]")
# оболочки, хосты скриптов и системные утилиты, которыми запускают команды: как цель «приложения» — отказ
COMMAND_WORDS = frozenset(
    {
        *("cmd", "powershell", "powershell_ise", "pwsh", "wscript", "cscript", "mshta", "rundll32"),
        *("regsvr32", "msiexec", "wsl", "bash", "conhost", "certutil", "bitsadmin", "schtasks", "wmic"),
        *("msbuild", "forfiles", "installutil", "regasm", "regsvcs", "cmstp", "curl", "wget", "python"),
        *("pythonw", "node", "taskkill", "shutdown", "reg"),
    }
)
_COMMAND_EXT = re.compile(r"\.(?:exe|com|bat|cmd|ps1|vbs|vbe|js|jse|wsf|wsh|hta|msi|scr|pif|cpl|reg)$", re.I)


class _Api:
    """Тонкий слой над ОС: открытие, проверки файловой системы, известные папки, корзина."""

    def startfile(self, target: str) -> None:
        os.startfile(target)  # type: ignore[attr-defined]  # единственный вызов во всём src

    def allow_set_foreground(self) -> None:
        """Разрешить запущенной программе вывести своё окно на передний план (ASFW_ANY)."""
        if sys.platform != "win32":
            return
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        fn = user32.AllowSetForegroundWindow
        fn.argtypes = (ctypes.c_uint32,)
        fn.restype = ctypes.c_int
        fn(ASFW_ANY)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def known_folder(self, folder_id: str) -> str:
        """SHGetKnownFolderPath; ошибка — OSError."""
        if sys.platform != "win32":
            raise OSError("известные папки есть только в Windows")
        import ctypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = (
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            )

        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        get = shell32.SHGetKnownFolderPath
        pptr = ctypes.POINTER(ctypes.c_void_p)
        get.argtypes = (ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, pptr)
        get.restype = ctypes.c_long
        free = ole32.CoTaskMemFree
        free.argtypes = (ctypes.c_void_p,)
        free.restype = None
        guid = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        out = ctypes.c_void_p()
        hr = get(ctypes.byref(guid), 0, None, ctypes.byref(out))
        try:
            if hr < 0 or not out.value:
                raise OSError(f"SHGetKnownFolderPath: 0x{hr & 0xFFFFFFFF:08X}")
            return ctypes.wstring_at(out.value)
        finally:
            if out.value:
                free(out)

    def send_to_trash(self, path: str) -> None:
        from send2trash import send2trash

        send2trash(path)


_api: _Api | None = None


def api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


# --- общее ---------------------------------------------------------------------------------------


def _name(path: str) -> str:
    return PureWindowsPath(path).name or path


def _has_control(text: str) -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text)


def _for(caller: Caller, result: Result) -> Result:
    """Всё, что уходит мозгу, — через privacy."""
    if caller != "brain":
        return result
    data = privacy.redact(result.data) if result.data is not None else None
    return Result(result.ok, privacy.redact_text(result.text), data)


def _unquote(text: str) -> str:
    s = text.strip()
    for a, b in (('"', '"'), ("'", "'"), ("«", "»"), ("“", "”")):
        if len(s) >= 2 and s[0] == a and s[-1] == b:
            return s[1:-1].strip()
    return s


# --- поиск ---------------------------------------------------------------------------------------


def es_argv(es: str, query: str, kind: Kind, limit: int) -> list[str]:
    """Командная строка es.exe; текст пользователя — только после -search (иначе «-отчёт» станет ключом)."""
    attr = {"file": ["/a-d"], "folder": ["/ad"]}.get(kind, [])
    return [
        es,
        "-argv",
        "-cp",
        "65001",
        "-timeout",
        "3000",
        "-n",
        str(limit),
        "-sort",
        "date-modified-descending",
        *attr,
        "-dm",
        "-date-format",
        "1",
        "-csv",
        "-no-header",
        "-no-folder-append-path-separator",
        "-search",
        query,
    ]


def parse_es_csv(stdout: bytes) -> list[str]:
    """Пути из CSV es.exe (колонки «Filename» и «Date Modified» в любом порядке): поле с полным путём."""
    text = stdout.decode("utf-8-sig", errors="replace")
    result: list[str] = []
    for row in csv.reader(io.StringIO(text)):
        fields = [f.strip() for f in row if f.strip()]
        if not fields:
            continue
        path = next((f for f in fields if _DRIVE_ABS.match(f) or f.startswith("\\\\")), fields[0])
        result.append(path)
    return result


def _run_es(argv: list[str]) -> subproc.Completed:
    r = subproc.run(argv, timeout=ES_TIMEOUT_S)
    if r.returncode == 7:  # сбой связи с Everything — один повтор
        r = subproc.run(argv, timeout=ES_TIMEOUT_S)
    return r


def find(query: str, kind: Kind = "any", caller: Caller = "user", limit: int = 20) -> Result:
    """Поиск файлов и папок через Everything (es.exe). data — список путей."""
    q = " ".join((query or "").split())
    q = "".join(ch for ch in q if not _has_control(ch))
    if not q:
        return fail("Что искать?")
    if kind not in ("file", "folder", "any"):
        return fail("Искать можно файлы, папки или всё")
    if caller == "brain" and _ES_FUNCTIONS.search(q):
        return fail("Поиск по содержимому и регулярным выражениям GPT недоступен")
    denied = policy.require("find_files", caller, f"Найти «{q}»?")
    if denied:
        return denied
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = 20
    es = str(settings.es_path())
    argv = es_argv(es, q, kind, limit)
    try:
        r = _run_es(argv)
    except FileNotFoundError:
        return _for(caller, fail(f"Не нашёл es.exe: {es} — проверь [pc] es_path в jarvis.toml"))
    except subprocess.TimeoutExpired:
        return fail("Everything не ответил за 5 с")
    except OSError as e:
        log.warning("es.exe не запустился: %s", e)
        return _for(caller, fail(f"Не удалось запустить es.exe: {es}"))
    if r.returncode == 8:
        return fail("Everything не запущен — запусти Everything")
    if r.returncode == 7:
        return fail("Everything не отвечает — попробуй ещё раз")
    if r.returncode in (4, 6):
        log.error("es.exe: ошибка аргументов (код %d) — баг Jarvis; argv: %s", r.returncode, argv[:-1])
        return fail("Поиск не сработал: ошибка в аргументах es.exe")
    if r.returncode != 0:
        log.warning("es.exe вернул код %d: %s", r.returncode, r.stderr[:300].decode("utf-8", "replace"))
        return fail(f"Поиск не сработал: es.exe вернул код {r.returncode}")

    found: list[str] = []
    for path in parse_es_csv(r.stdout):
        try:
            allowed = paths.check(path, caller, "list").ok
        except Exception:
            allowed = False
        if not allowed:
            continue
        if caller == "brain":
            shown = privacy.redact_path(path)
            if shown is None:
                continue
            path = shown
        found.append(path)

    if not found:
        return _for(caller, ok(f"Ничего не нашёл по «{q}»", []))
    names = ", ".join(_name(p) for p in found[:3]) + (", …" if len(found) > 3 else "")
    return _for(caller, ok(f"Нашёл {len(found)}: {names}", found))


# --- известные папки -----------------------------------------------------------------------------


def _folder_key(name: str) -> str | None:
    words = [w for w in _unquote(name).casefold().replace("ё", "е").split() if w not in _FOLDER_NOISE]
    return _FOLDER_LOOKUP.get(" ".join(words)) if words else None


def known_folder(name: str) -> str | None:
    """Путь известной папки по имени в любом падеже («загрузки», «папку загрузок», «рабочем столе»)."""
    key = _folder_key(name)
    if key is None:
        return None
    try:
        return api().known_folder(FOLDER_IDS[key])
    except OSError as e:
        log.warning("известная папка %s: %s", key, e)
        return None


# --- открытие ------------------------------------------------------------------------------------


def _start(target: str) -> str | None:
    """Открыть через os.startfile в STA-потоке. None — успех (PID не возвращается), иначе текст ошибки."""
    a = api()
    with contextlib.suppress(OSError):
        a.allow_set_foreground()
    try:
        apps.run_sta(a.startfile, target)
    except Exception as e:
        log.warning("открыть не удалось: %r", e)
        return str(e) or e.__class__.__name__
    return None


def _scheme(target: str) -> str | None:
    """Схема вида «xxx:»; буква диска (C:) — не схема."""
    m = _SCHEME.match(target)
    if not m or len(m.group(1)) < 2:
        return None
    return m.group(1).casefold()


def _looks_like_command(target: str) -> bool:
    if _SHELL_CHARS.search(target) or _COMMAND_EXT.search(target):
        return True
    first = target.split()[0].casefold() if target.split() else ""
    return first.removesuffix(".exe") in COMMAND_WORDS


def detect_kind(target: str) -> OpenKind | None:
    """Вид цели без подсказки: http(s) → url; другая схема → None (отказ); путь с буквой диска → file/folder;
    известная папка → folder; иначе app."""
    if _HTTP.match(target):
        return "url"
    if _scheme(target):
        return None
    if _DRIVE_ABS.match(target):
        return "folder" if api().is_dir(target) else "file"
    if target.startswith(("\\", "/")):
        return "file"
    if _folder_key(target):
        return "folder"
    return "app"


def normalize_url(target: str) -> str | None:
    """Нормализованный URL http/https или None. Без пробелов и управляющих символов, без user:pass@."""
    s = target.strip()
    if not s or len(s) > 2048 or _has_control(s) or any(ch.isspace() for ch in s) or "\\" in s:
        return None
    if not _HTTP.match(s):
        if _scheme(s) or not _DOMAIN.match(s):
            return None
        s = "https://" + s
    try:
        parts = urllib.parse.urlsplit(s)
        host = parts.hostname
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme.casefold() not in ("http", "https") or not parts.netloc or not host:
        return None
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return None
    return parts.geturl()


def _open_url(target: str, caller: Caller) -> Result:
    url = normalize_url(target)
    if url is None:
        return fail("Открываю только обычные ссылки http/https")
    host = urllib.parse.urlsplit(url).hostname or url
    denied = policy.require("open_url", caller, f"Открыть ссылку {url}?", url)
    if denied:
        return denied
    err = _start(url)
    if err is not None:
        return fail(f"Не удалось открыть {host}")
    return ok(f"Открыл {host}", url)


def _open_folder(target: str, caller: Caller) -> Result:
    folder = known_folder(target) if not _DRIVE_ABS.match(target) else target
    if folder is None:
        if _folder_key(target):
            return fail(f"Не нашёл папку «{target}»")
        return fail("Укажи полный путь к папке или известную папку: загрузки, документы, рабочий стол…")
    try:
        chk = paths.check(folder, caller, "open", is_dir=True)
    except paths.PathDenied as e:
        return fail(str(e) or "Эту папку открыть нельзя")
    if not chk.ok:
        return fail(chk.reason or "Эту папку открыть нельзя")
    name = _name(chk.path)
    if not api().is_dir(chk.path):
        return fail(f"Не нашёл папку «{name}»")
    # папка не исполняемая, даже если в имени «.exe»: confirm здесь не применяется
    denied = policy.require("open_folder", caller, f"Открыть папку «{name}»?", chk.path)
    if denied:
        return denied
    if _start(chk.path) is not None:
        return fail(f"Не удалось открыть папку «{name}»")
    return ok(f"Открыл папку «{name}»", chk.path)


def _open_file(target: str, caller: Caller) -> Result:
    if not _DRIVE_ABS.match(target):
        return fail("Укажи полный путь к файлу")
    try:
        chk = paths.check(target, caller, "open")
    except paths.PathDenied as e:
        return fail(str(e) or "Этот файл открыть нельзя")
    if not chk.ok:
        return fail(chk.reason or "Этот файл открыть нельзя")
    name = _name(chk.path)
    if api().is_dir(chk.path):
        return _open_folder(chk.path, caller)
    if not api().exists(chk.path):
        return fail(f"Не нашёл файл «{name}»")
    if chk.confirm or paths.is_executable_type(chk.path):
        denied = policy.require("open_executable", caller, f"Открыть исполняемый файл «{name}»?", chk.path)
    else:
        denied = policy.require("open_file", caller, f"Открыть файл «{name}»?", chk.path)
    if denied:
        return denied
    if _start(chk.path) is not None:
        return fail(f"Не удалось открыть «{name}»")
    return ok(f"Открыл «{name}»", chk.path)


def _open_app(target: str, caller: Caller) -> Result:
    if _looks_like_command(target):
        return fail("Это похоже на команду — Jarvis открывает только приложения из меню «Пуск»")
    app, _score = apps.resolve(target)
    if app is None:
        return fail(f"Не нашёл приложение «{target}»")
    if not app.app_id or _has_control(app.app_id) or len(app.app_id) > 1024:
        return fail(f"Не удалось открыть {app.name}")
    denied = policy.require("open_app", caller, f"Открыть {app.name}?", app.app_id)
    if denied:
        return denied
    if _start("shell:AppsFolder\\" + app.app_id) is not None:
        return fail(f"Не удалось открыть {app.name}")
    return ok(f"Открыл {app.name}", app.name)


def open_target(target: str, kind: OpenKind | None, caller: Caller) -> Result:
    """Открыть приложение, папку, файл или ссылку. kind=None — определить по виду цели."""
    raw = _unquote(target or "")
    if not raw:
        return fail("Что открыть?")
    if _has_control(raw) or len(raw) > 2048:
        return _for(caller, fail("В цели недопустимые символы"))
    if re.fullmatch(r"[A-Za-z]:", raw):  # «D:» — корень диска
        raw += "\\"
    scheme = _scheme(raw)
    if scheme and scheme not in ("http", "https"):
        return _for(caller, fail("Такие ссылки Jarvis не открывает — только http/https"))
    if kind == "app" and _DRIVE_ABS.match(raw):  # путь вместо имени приложения — как файл (exe — спросить)
        kind = None
    if scheme or kind is None:
        kind = "url" if scheme else detect_kind(raw)
    handlers = {"url": _open_url, "folder": _open_folder, "file": _open_file, "app": _open_app}
    handler = handlers.get(kind) if kind else None
    if handler is None:
        return fail("Не понял, что открыть: приложение, папку, файл или ссылку?")
    return _for(caller, handler(raw, caller))


# --- корзина и чтение ----------------------------------------------------------------------------


def trash(path: str, caller: Caller) -> Result:
    """Переместить в корзину (только корзина, только с подтверждением)."""
    raw = _unquote(path or "")
    if not _DRIVE_ABS.match(raw) or _has_control(raw):
        return _for(caller, fail("Укажи полный путь к файлу или папке"))
    try:
        chk = paths.check(raw, caller, "trash")
    except paths.PathDenied as e:
        return _for(caller, fail(str(e) or "Это удалить нельзя"))
    if not chk.ok:
        return _for(caller, fail(chk.reason or "Это удалить нельзя"))
    name = _name(chk.path)
    if not api().exists(chk.path):
        return _for(caller, fail(f"Не нашёл «{name}»"))
    denied = policy.require("trash", caller, f"Переместить в корзину «{name}»?", chk.path)
    if denied:
        return denied
    try:
        api().send_to_trash(chk.path)
    except Exception as e:
        log.warning("корзина: %r", e)
        return _for(caller, fail(f"Не удалось переместить в корзину «{name}»"))
    return _for(caller, ok(f"Переместил в корзину «{name}»", chk.path))


def decode_text(data: bytes, truncated: bool) -> str | None:
    """utf-8-sig → utf-16 по BOM → cp1251. None — похоже на двоичный файл."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        if len(data) % 2:
            data = data[:-1]
        return data.decode("utf-16", errors="replace")
    if b"\0" in data:
        return None
    try:
        # при обрезке последний символ UTF-8 мог разрезаться — неполный хвост отбрасывается
        return codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=not truncated)
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def read_text(path: str, caller: Caller, max_bytes: int = MAX_READ) -> Result:
    """Прочитать начало текстового файла (≤64 КБ). data: {"path", "text", "truncated"}."""
    raw = _unquote(path or "")
    if not _DRIVE_ABS.match(raw) or _has_control(raw):
        return _for(caller, fail("Укажи полный путь к файлу"))
    try:
        chk = paths.check(raw, caller, "read")
    except paths.PathDenied as e:
        return _for(caller, fail(str(e) or "Этот файл читать нельзя"))
    if not chk.ok:
        return _for(caller, fail(chk.reason or "Этот файл читать нельзя"))
    name = _name(chk.path)
    summary = f"GPT просит прочитать файл «{name}» (уйдёт в облако)"
    denied = policy.require("read_text", caller, summary, chk.path)
    if denied:
        return denied
    if api().is_dir(chk.path):
        return _for(caller, fail(f"«{name}» — это папка"))
    limit = max(1, min(MAX_READ, int(max_bytes)))
    try:
        with open(chk.path, "rb") as f:
            data = f.read(limit + 1)
    except IsADirectoryError:
        return _for(caller, fail(f"«{name}» — это папка"))
    except FileNotFoundError:
        return _for(caller, fail(f"Не нашёл файл «{name}»"))
    except OSError as e:
        log.warning("чтение файла: %r", e)
        return _for(caller, fail(f"Не удалось прочитать «{name}»"))
    truncated = len(data) > limit
    text = decode_text(data[:limit], truncated)
    if text is None:
        return _for(caller, fail(f"«{name}» — не текстовый файл"))
    if truncated:
        text += "\n(обрезано)"
    phrase = f"Прочитал «{name}»" + (" (обрезано)" if truncated else "")
    if caller == "brain":
        text = privacy.redact_text(text)
        phrase = privacy.redact_text(phrase)
    return ok(phrase, {"path": chk.path, "text": text, "truncated": truncated})
