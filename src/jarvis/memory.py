"""Память о пользователе: короткие заметки, которые человек сам попросил запомнить («запомни, что …»).

<data>\\memory.json — список {id, text, ts}. Пишет в память ТОЛЬКО команда человека через грамматику: у мозга
такого инструмента нет — иначе текст из окна или файла (инъекция) закрепился бы навсегда. Мозгу память уходит
в начале каждого запроса (`[о пользователе: …]` в brain.build_prompt, через pc.privacy); рукам — никогда:
байты их префикса и сообщение пользователя от памяти не зависят. В локальном режиме мозг не вызывается —
память никуда не уходит.

Здесь же JsonList — файл-список JSON с кэшем по mtime и атомарной записью (его использует и jarvis.reminders)
и matches() — поиск записи по части текста с учётом падежей.
"""

import contextlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pc import settings

log = logging.getLogger("jarvis")

FILE_NAME = "memory.json"
LIMIT = 50  # записей
TEXT_MAX = 200  # символов в одной записи
CONTEXT_CHARS = 600  # сколько памяти уходит мозгу в начале запроса
REPLACE_TRIES = 5  # os.replace на Windows падает, пока файл открыт на чтение другим процессом
REPLACE_PAUSE_S = 0.02

ALL_WORDS = frozenset({"все", "всё", "all"})
LAST_WORDS = frozenset({"последнее", "последний", "последняя", "последнюю", "last"})

SECRET_TEXT = "Пароли и ключи не запоминаю: память уходит в GPT вместе с вопросами"
_SECRET = re.compile(
    r"парол|password|passwd|пин-?код|pin-?код|(?<![a-zа-я])pin(?![a-zа-я])|(?<![a-zа-я])cvv|(?<![a-zа-я])cvc"
    r"|токен|token|api[ _-]?key|апи[ -]?ключ|ключ (?:api|апи)|секретн\w* ключ|seed|сид-?фраз|мнемоническ",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
_WORD = re.compile(r"\w+")
# служебные слова запроса «забудь про мой кофе» — не часть искомого
_QUERY_STOP = frozenset(
    {
        "про",
        "о",
        "об",
        "что",
        "то",
        "чтобы",
        "мне",
        "меня",
        "мой",
        "моя",
        "мое",
        "мои",
        "мою",
        "это",
        "насчет",
    }
)


class StoreError(Exception):
    """Запись не сохранена или не принята; текст — для человека."""


# --- файл-список JSON ------------------------------------------------------------------------------


class JsonList[T]:
    """Список записей в <data>\\<name>: чтение с кэшем по (mtime, размер), атомарная запись (tmp +
    os.replace).

    Битый файл (не JSON или не список) — пустой список, предупреждение в лог и копия <name>.bad рядом.
    Ошибка чтения — последнее прочитанное (или пусто) и предупреждение; ошибка записи — StoreError.
    """

    def __init__(
        self, name: str, parse: Callable[[dict[str, Any]], T | None], dump: Callable[[T], dict[str, Any]]
    ) -> None:
        self.name = name
        self.lock = threading.RLock()  # чтение-изменение-запись в процессе — под ним
        self._parse = parse
        self._dump = dump
        self._key: tuple[str, int, int] | None = None  # (путь, mtime_ns, размер) прочитанного файла
        self._items: list[T] = []

    def path(self) -> Path:
        return settings.data_dir() / self.name

    def version(self) -> int:
        """mtime файла в нс; 0 — файла нет. Изменился — значит, записи поменял этот или другой процесс."""
        try:
            return self.path().stat().st_mtime_ns
        except OSError:
            return 0

    def load(self) -> list[T]:
        path = self.path()
        with self.lock:
            try:
                st = path.stat()
            except FileNotFoundError:
                self._key, self._items = None, []
                return []
            except OSError as e:
                log.warning("%s: нет доступа: %s", self.name, e)
                return list(self._items)
            key = (str(path), st.st_mtime_ns, st.st_size)
            if key == self._key:
                return list(self._items)
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except OSError as e:
                log.warning("%s не читается: %s", self.name, e)
                return list(self._items)
            except ValueError:
                data = None
            if not isinstance(data, list):
                self._quarantine(path)
                self._key, self._items = None, []
                return []
            items = [item for raw in data if isinstance(raw, dict) for item in [self._parse(raw)] if item]
            self._key, self._items = key, items
            return list(items)

    def save(self, items: list[T]) -> None:
        """Записать атомарно; ошибка ввода-вывода — StoreError (кэш и файл — прежние)."""
        with self.lock:
            tmp: Path | None = None
            try:
                path = settings.data_file(self.name)
                tmp = path.with_name(f"{self.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                text = json.dumps([self._dump(i) for i in items], ensure_ascii=False, indent=1)
                tmp.write_text(text + "\n", encoding="utf-8")
                _replace(tmp, path)
                st = path.stat()
            except OSError as e:
                log.warning("%s не сохранён: %s", self.name, e)
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
                raise StoreError(f"Не удалось сохранить {self.name}: {e.strerror or e}") from e
            self._key, self._items = (str(path), st.st_mtime_ns, st.st_size), list(items)

    def _quarantine(self, path: Path) -> None:
        """Битый файл — в <name>.bad (человек сможет поправить руками), дальше — с пустого списка."""
        bad = path.with_name(f"{self.name}.bad")
        try:
            _replace(path, bad)
            log.warning("%s битый — начинаю с пустого, копия: %s", self.name, bad.name)
        except OSError as e:
            log.warning("%s битый и не переименован: %s", self.name, e)


def _replace(src: Path, dst: Path) -> None:
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 == REPLACE_TRIES:
                raise
            time.sleep(REPLACE_PAUSE_S)


def new_id(taken: Iterable[str]) -> str:
    used = set(taken)
    while (value := secrets.token_hex(4)) in used:
        pass
    return value


# --- поиск по части текста --------------------------------------------------------------------------


def _fold(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _stem(word: str) -> str:
    """Грубая основа слова без окончания: «машину» → «маши», «пиццу» → «пицц», «ёлку» → «елк»; до 3 букв —
    целиком."""
    n = len(word)
    return word if n <= 3 else word[: n - 1] if n <= 5 else word[: n - 2]


def matches(query: str, text: str) -> bool:
    """Каждое значимое слово запроса — начало какого-то слова текста (с точностью до окончания)."""
    words = [w for w in _WORD.findall(_fold(query)) if w not in _QUERY_STOP]
    if not words:
        return False
    have = _WORD.findall(_fold(text))
    return all(any(h.startswith(_stem(w)) for h in have) for w in words)


def clean_text(text: str, limit: int = TEXT_MAX) -> str:
    """Одна строка без управляющих символов; длиннее limit — StoreError (обрезать молча — потерять смысл)."""
    out = " ".join(_CONTROL.sub(" ", str(text)).split())
    if len(out) > limit:
        raise StoreError(f"Слишком длинно — скажи короче (до {limit} символов)")
    return out


def is_secret(text: str) -> bool:
    """Похоже на пароль, ключ или код — такое в память (и в облако) не кладём."""
    return _SECRET.search(text) is not None


# --- память ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryItem:
    id: str
    text: str
    ts: float


def _parse(raw: dict[str, Any]) -> MemoryItem | None:
    text, ident, ts = raw.get("text"), raw.get("id"), raw.get("ts")
    if not isinstance(text, str) or not text.strip() or not isinstance(ident, str) or not ident:
        return None
    if not isinstance(ts, int | float) or isinstance(ts, bool):
        ts = 0.0
    return MemoryItem(ident, " ".join(_CONTROL.sub(" ", text).split())[:TEXT_MAX], float(ts))


_store: JsonList[MemoryItem] = JsonList(FILE_NAME, _parse, asdict)


def path() -> Path:
    return _store.path()


def items() -> list[MemoryItem]:
    """Записи в порядке добавления (номер в списке = индекс + 1)."""
    return _store.load()


def remember(text: str, now: float | None = None) -> tuple[MemoryItem, bool]:
    """Запомнить заметку → (запись, добавлена ли). Такая уже есть — (она же, False), без дубля.

    StoreError: пусто, слишком длинно, похоже на пароль, лимит LIMIT, ошибка записи.
    """
    clean = clean_text(text)
    if len(clean) < 2:
        raise StoreError("Не понял, что запомнить")
    if is_secret(clean):
        raise StoreError(SECRET_TEXT)
    with _store.lock:
        current = _store.load()
        for item in current:
            if _fold(item.text) == _fold(clean):
                return item, False
        if len(current) >= LIMIT:
            raise StoreError(f"Память полна ({LIMIT} записей) — забудь лишнее: «забудь 1»")
        item = MemoryItem(new_id(i.id for i in current), clean, time.time() if now is None else now)
        _store.save([*current, item])
    return item, True


def select(selector: int | str, current: list[MemoryItem] | None = None) -> list[MemoryItem]:
    """Что выбрано: номер в списке (с 1), «все», «последнее» (добавленная последней) или часть текста."""
    current = items() if current is None else current
    if isinstance(selector, str) and selector.strip().isdigit():
        selector = int(selector.strip())
    if isinstance(selector, bool):
        return []
    if isinstance(selector, int):
        return [current[selector - 1]] if 1 <= selector <= len(current) else []
    word = _fold(str(selector)).strip()
    if word in ALL_WORDS:
        return list(current)
    if word in LAST_WORDS:
        return [max(current, key=lambda i: i.ts)] if current else []
    return [i for i in current if matches(word, i.text)]


def forget(selector: int | str) -> list[MemoryItem]:
    """Забыть выбранное (см. select): по части текста — все подходящие. → забытые записи."""
    with _store.lock:
        current = _store.load()
        chosen = select(selector, current)
        if chosen:
            ids = {i.id for i in chosen}
            _store.save([i for i in current if i.id not in ids])
    return chosen


def forget_ids(ids: Iterable[str]) -> list[MemoryItem]:
    """Забыть записи по id (отмена «запомни»). → забытые."""
    wanted = set(ids)
    with _store.lock:
        current = _store.load()
        gone = [i for i in current if i.id in wanted]
        if gone:
            _store.save([i for i in current if i.id not in wanted])
    return gone


def restore(records: Iterable[dict[str, Any]]) -> list[MemoryItem]:
    """Вернуть забытые записи (отмена «забудь»); уже существующие id пропускаются. → возвращённые."""
    with _store.lock:
        current = _store.load()
        have = {i.id for i in current}
        back = [i for r in records if isinstance(r, dict) for i in [_parse(r)] if i and i.id not in have]
        if not back:
            return []
        if len(current) + len(back) > LIMIT:
            raise StoreError(f"Память полна ({LIMIT} записей)")
        _store.save(sorted([*current, *back], key=lambda i: i.ts))
    return back


def as_context(limit_chars: int = CONTEXT_CHARS) -> str:
    """Память одной строкой для мозга: «a; b; c» не длиннее limit_chars.

    Не влезает всё — берутся самые новые записи (человек мог уточнить старое), порядок — хронологический.
    privacy и чистку от [ ] делает вызывающий (brain.build_prompt).
    """
    chosen: list[MemoryItem] = []
    size = 0
    for item in reversed(items()):
        extra = len(item.text) + (2 if chosen else 0)
        if size + extra > limit_chars:
            break
        chosen.append(item)
        size += extra
    return "; ".join(i.text for i in reversed(chosen))
