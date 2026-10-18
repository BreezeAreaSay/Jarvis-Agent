"""Файлы: поиск по индексу Windows (Windows Search через OLE DB) и «Показать в папке».

Запрос к индексу — SQL Windows Search (SELECT … FROM SYSTEMINDEX WHERE …) через провайдер Search.CollatorDSO:
ADODB.Connection и Recordset из pywin32, в потоке платформы (COM STA). Сборка запроса — чистая функция
build_sql() (проверяется тестами на Linux): слова человека попадают в SQL только экранированными — кавычка
удваивается, подстановочные знаки LIKE берутся в скобки, в CONTAINS остаются только буквы и цифры, расширения
проходят белый список символов. Регистр и «ё» не важны: буквы шаблона LIKE — наборы «[оО]», «[еёЕЁ]».
Даты в индексе — UTC: границы переводятся в UTC.
"""

import calendar
import contextlib
import logging
import ntpath
import os
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

from jarvis.platform.base import FileQuery, IndexedFile, PlatformError

log = logging.getLogger(__name__)

CONNECTION = "Provider=Search.CollatorDSO;Extended Properties='Application=Windows';"
COLUMNS = (
    "System.ItemPathDisplay",
    "System.FileName",
    "System.Size",
    "System.DateModified",
    "System.ItemType",
)
MAX_ROWS = 300
QUERY_TIMEOUT_S = 10
UNAVAILABLE = "Индекс Windows недоступен: служба «Windows Search» выключена или не отвечает"
BAD_QUERY = "Индекс Windows не принял запрос"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EXT = re.compile(r"^[a-z0-9]{1,10}$")
_TERM_JUNK = re.compile(r"[^\w-]+")
_YO_VARIANTS = 4  # «отчет» → «отчёт»: сколько вариантов с «ё» добавлять в CONTAINS


# --- сборка запроса --------------------------------------------------------------------------------------


def sql_string(text: str) -> str:
    """Строковый литерал SQL: в одинарных кавычках, кавычка внутри удвоена, без управляющих символов."""
    return "'" + _CONTROL.sub("", text).replace("'", "''") + "'"


def _letter(ch: str) -> str:
    if ch in "%_[":
        return f"[{ch}]"  # подстановочный знак — буквально
    if ch in "еёЕЁ":
        return "[еёЕЁ]"
    upper, lower = ch.upper(), ch.lower()
    if upper != lower and len(upper) == 1 and len(lower) == 1:
        return f"[{lower}{upper}]"
    return ch


def like_body(text: str) -> str:
    """Текст для шаблона LIKE: без учёта регистра и «ё», подстановочные знаки — буквально."""
    return "".join(_letter(ch) for ch in _CONTROL.sub("", text))


def like_contains(text: str) -> str:
    """LIKE-шаблон «где-то внутри»: '%…%'."""
    return f"%{like_body(text)}%"


def contains_terms(word: str) -> str | None:
    """Условие CONTAINS для слова: '"отчет*" OR "отчёт*"' (по началу слова); пустое слово — None."""
    clean = _TERM_JUNK.sub("", word.casefold()).strip("-_")
    if not clean:
        return None
    variants = [clean]
    if "ё" not in clean:
        for i, ch in enumerate(clean):
            if ch == "е" and len(variants) <= _YO_VARIANTS:
                variants.append(clean[:i] + "ё" + clean[i + 1 :])
    return " OR ".join(f'"{v}*"' for v in variants)


def scope_url(path: str) -> str:
    """Папка → «file:C:/Users/me/Downloads» для SCOPE."""
    return "file:" + path.replace("\\", "/").rstrip("/")


def utc_literal(ts: float) -> str:
    """unix-время → «2026/10/17 21:00:00» (UTC — так даты хранит индекс)."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y/%m/%d %H:%M:%S")


def build_sql(query: FileQuery) -> str:
    """FileQuery → SQL Windows Search. Новые файлы — первыми (порядок по смыслу наводит плагин)."""
    limit = max(1, min(MAX_ROWS, int(query.limit or 50)))
    where: list[str] = []
    scopes = [s for s in query.scopes if s.strip()]
    if scopes:
        where.append("(" + " OR ".join(f"SCOPE={sql_string(scope_url(s))}" for s in scopes) + ")")
    else:
        where.append("SCOPE='file:'")
    words = [w for w in query.words if w.strip()]
    if words and query.any_word:
        parts = [f"System.ItemPathDisplay LIKE {sql_string(like_contains(w))}" for w in words]
        where.append("(" + " OR ".join(parts) + ")")
    else:
        where.extend(f"System.FileName LIKE {sql_string(like_contains(w))}" for w in words)
    extensions = [e for e in (x.casefold().lstrip(".") for x in query.extensions) if _EXT.match(e)]
    if extensions:
        parts = [f"System.FileName LIKE {sql_string('%.' + like_body(e))}" for e in extensions]
        where.append("(" + " OR ".join(parts) + ")")
    if query.folders_only:
        where.append("(System.ItemType = 'Directory' OR System.Kind = 'folder')")
    elif query.files_only or extensions:
        where.append("System.ItemType <> 'Directory'")
    for word in query.path_words:
        if word.strip():
            where.append(f"System.ItemFolderPathDisplay LIKE {sql_string(like_contains(word))}")
    for word in query.content:
        terms = contains_terms(word)
        if terms:
            where.append(f"CONTAINS(*, {sql_string(terms)})")
    if query.date_from is not None:
        where.append(f"System.DateModified >= {sql_string(utc_literal(query.date_from))}")
    if query.date_to is not None:
        where.append(f"System.DateModified < {sql_string(utc_literal(query.date_to))}")
    return (
        f"SELECT TOP {limit} {', '.join(COLUMNS)} FROM SYSTEMINDEX WHERE {' AND '.join(where)} "
        "ORDER BY System.DateModified DESC"
    )


# --- разбор ответа ---------------------------------------------------------------------------------------


def to_epoch(value: Any) -> float:
    """Дата из ADO → unix-время. Без часового пояса — UTC (так её отдаёт индекс)."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return calendar.timegm(value.timetuple()) + value.microsecond / 1_000_000
        return value.timestamp()
    return 0.0


def parse_row(values: list[Any] | tuple[Any, ...]) -> IndexedFile | None:
    """Строка ответа (порядок COLUMNS) → IndexedFile; без пути — None."""
    path, name, size, modified, item_type = (list(values) + [None] * len(COLUMNS))[: len(COLUMNS)]
    path = str(path or "").strip()
    if not path:
        return None
    is_dir = str(item_type or "").casefold() in ("directory", "folder")
    try:
        size_value = int(size) if size is not None and not is_dir else 0
    except (TypeError, ValueError):
        size_value = 0
    return IndexedFile(
        path=path,
        name=str(name or "").strip() or ntpath.basename(path.rstrip("\\/")),
        size=max(0, size_value),
        modified=to_epoch(modified),
        is_dir=is_dir,
    )


# --- Windows ---------------------------------------------------------------------------------------------


def run_query(sql: str, limit: int = MAX_ROWS) -> list[IndexedFile]:
    """Выполнить запрос к индексу (поток платформы, COM уже инициализирован). Индекса нет — PlatformError."""
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]
    except ImportError as e:
        raise PlatformError(UNAVAILABLE) from e
    conn: Any = None
    rs: Any = None
    try:
        try:
            conn = win32com.client.Dispatch("ADODB.Connection")
            conn.CommandTimeout = QUERY_TIMEOUT_S
            conn.Open(CONNECTION)
        except pythoncom.com_error as e:
            log.warning("индекс Windows: подключение не открылось: %r", e)
            raise PlatformError(UNAVAILABLE) from e
        try:
            rs = win32com.client.Dispatch("ADODB.Recordset")
            rs.Open(sql, conn)
        except pythoncom.com_error as e:
            log.warning("индекс Windows не принял запрос: %r\n%s", e, sql)
            raise PlatformError(BAD_QUERY) from e
        out: list[IndexedFile] = []
        while not rs.EOF and len(out) < limit:
            item = parse_row([rs.Fields.Item(i).Value for i in range(len(COLUMNS))])
            if item is not None:
                out.append(item)
            rs.MoveNext()
        return out
    finally:
        for obj in (rs, conn):
            if obj is not None:
                with contextlib.suppress(Exception):
                    obj.Close()


def explorer_select_command(path: str) -> str:
    """Командная строка «explorer.exe /select,"<путь>"» (кавычек в путях Windows не бывает)."""
    if '"' in path:
        raise PlatformError("Неверный путь")
    explorer = ntpath.join(os.environ.get("SYSTEMROOT") or r"C:\Windows", "explorer.exe")
    return f'"{explorer}" /select,"{path}"'


def reveal(path: str) -> None:
    """Открыть папку в Проводнике и выделить в ней файл (или папку)."""
    target = ntpath.normpath(path)
    try:
        from win32com.shell import shell  # type: ignore[import-not-found]

        folder = ntpath.dirname(target) or target
        folder_pidl = shell.SHILCreateFromPath(folder, 0)[0]
        item_pidl = shell.SHILCreateFromPath(target, 0)[0]
        shell.SHOpenFolderAndSelectItems(folder_pidl, (item_pidl,), 0)
        return
    except Exception as e:
        log.info("SHOpenFolderAndSelectItems не сработал (%r) — запускаю explorer /select", e)
    try:
        subprocess.Popen(
            explorer_select_command(target),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log.warning("explorer /select: %r", e)
        raise PlatformError("Не удалось открыть Проводник") from e
