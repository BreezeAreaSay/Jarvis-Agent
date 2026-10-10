"""Фильтр всего, что уходит мозгу: заголовки окон, пути, тексты результатов и ошибок.

Пути закрытых зон (pc.paths.hidden_for_brain) и имена из private_paths заменяются на «(скрыто)»;
заголовок окна, где встречается имя из private_paths, скрывается целиком.
"""

import functools
import re
from pathlib import PureWindowsPath
from typing import Any

from pc import paths, settings
from pc.result import Result

HIDDEN = "(скрыто)"
TITLE_MAX = 80

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
# невидимые символы направления текста: ими подделывают вид заголовка («exe.txt» ↔ «txt.exe»)
_BIDI = str.maketrans(dict.fromkeys("‎‏؜‪‫‬‭‮⁦⁧⁨⁩"))
# путь Windows в свободном тексте: от буквы диска до конца строки, кавычки, двоеточия или следующего пути
_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?:(?![A-Za-z]:[\\/])[^\r\n\t\"'<>|*?:`])*")
_TRAIL = " .,;!?»…"
_PAIRS = {")": "(", "]": "[", "}": "{"}
_SPACE = re.compile(r"\s")
_TITLE_KEYS = frozenset({"title"})


@functools.lru_cache(maxsize=8)
def _private_names(private: tuple[str, ...]) -> tuple[tuple[str, ...], re.Pattern[str] | None]:
    """Имена (последний компонент) private_paths и, для файлов, имя без расширения (Проводник его прячет)."""
    names: set[str] = set()
    for raw in private:
        name = PureWindowsPath(raw.strip().replace("/", "\\")).name.rstrip(". ")
        if not name:
            continue
        names.add(name)
        stem = PureWindowsPath(name).stem
        if stem != name and len(stem) >= 3:
            names.add(stem)
    if not names:
        return (), None
    ordered = tuple(sorted(names, key=len, reverse=True))
    return ordered, re.compile("|".join(re.escape(n) for n in ordered), re.IGNORECASE)


def _names() -> tuple[tuple[str, ...], re.Pattern[str] | None]:
    return _private_names(settings.load_pc_settings().private_paths)


def _has_private_name(text: str) -> bool:
    names, pattern = _names()
    if pattern is None:
        return False
    folded = text.casefold()
    return pattern.search(text) is not None or any(n.casefold() in folded for n in names)


def _trim(candidate: str) -> str:
    """Убрать с конца найденного пути знаки препинания и непарные закрывающие скобки."""
    s = candidate
    while s:
        last = s[-1]
        unpaired = last in _PAIRS and s.count(_PAIRS[last]) < s.count(last)
        if not (last in _TRAIL or last.isspace() or unpaired):
            break
        s = s[:-1]
    return s


def _hidden_length(candidate: str) -> int:
    """Сколько символов от начала candidate скрыть (0 — путь можно показать).

    Где кончается путь в свободном тексте, точно не узнать: целиком скрытый кандидат прячется до конца
    (лишнее слово после пути — меньшее зло); иначе проверяются начала последнего компонента до пробела
    («…\\.env не найден» → «(скрыто) не найден»).
    """
    if paths.hidden_for_brain(candidate):
        return len(candidate)
    cut = max(candidate.rfind("\\"), candidate.rfind("/")) + 1
    for m in _SPACE.finditer(candidate, cut):
        prefix = candidate[: m.start()]
        if prefix[cut:].strip() and paths.hidden_for_brain(prefix):
            return len(prefix)
    return 0


def _redact_paths(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _PATH.finditer(text):
        candidate = _trim(m.group(0))
        n = _hidden_length(candidate) if len(candidate) > 3 else 0
        if n:
            out += [text[pos : m.start()], HIDDEN]
            pos = m.start() + n
    out.append(text[pos:])
    return "".join(out)


def redact_text(text: str) -> str:
    """Пути закрытых зон и имена из private_paths в свободном тексте → «(скрыто)»."""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    result = _redact_paths(text)
    _, pattern = _names()
    return pattern.sub(HIDDEN, result) if pattern is not None else result


def redact_title(title: str) -> str:
    """Заголовок окна для мозга: одной строкой, без управляющих символов, не длиннее 80 символов.

    Есть имя из private_paths (или конфиг не читается и они неизвестны) — «(скрыто)».
    """
    text = " ".join(_CONTROL.sub(" ", str(title or "")).translate(_BIDI).split())
    if not text:
        return ""
    if settings.config_error() or _has_private_name(text):
        return HIDDEN
    text = _redact_paths(text)
    if len(text) > TITLE_MAX:
        text = text[: TITLE_MAX - 1].rstrip() + "…"
    return text


def redact_path(path: str) -> str | None:
    """None — путь скрыт от мозга (закрытая зона, секрет, недопустимая форма, имя из private_paths)."""
    if paths.hidden_for_brain(path) or _has_private_name(str(path)):
        return None
    return path


def redact(value: Any) -> Any:
    """Рекурсивно по dict/list/tuple/str и Result; ключи dict не меняются, значения под "title" —
    как заголовки окон. Прочие типы — как есть."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Result):
        return Result(value.ok, redact_text(value.text), redact(value.data))
    if isinstance(value, dict):
        return {
            k: redact_title(v) if isinstance(v, str) and str(k).casefold() in _TITLE_KEYS else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    return value
