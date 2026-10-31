"""Словарь ударений и «ё» Jarvis для голосов piper (как собран и на каких условиях — scripts/ru_dict).

Голоса piper читают текст через espeak-ng, а его русский словарь ставит ударение иначе, чем словари, примерно
в 6 % форм слов, не знает «ё» в словах через «е» («еще», «черный») и пропускает знак ударения U+0301. Словарь
Jarvis это исправляет: согласие ударений со словарём OpenRussian — 94,2 → 99,6 %, «ё» — 106 тыс. слов, «за́мок»
и «замо́к» читаются по знаку (знаки ставит модель в ответах голосом — prompts.SOURCE_VOICE).

Сборка установщика кладёт ru_dict и ru_dict.json в jarvis/resources/voice. Перед загрузкой голоса install()
ставит словарь в espeak-ng-data голоса (исходный остаётся рядом — ru_dict.orig), только если фонемные таблицы
голоса те же, с которыми словарь собран: с чужими таблицами espeak-ng может завершить процесс ядра. Выключили
(settings.tts.stress_dict), таблицы другие или голос с этим словарём уже ронял ядро (этап crashguard STAGE) —
возвращается исходный. espeak-ng читает словарь один раз на процесс: смена действует со следующего запуска.
"""

import contextlib
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Literal

from jarvis import crashguard, paths

log = logging.getLogger(__name__)

ACCENT = "́"  # знак ударения: «за́мок»
STAGE = "ru-dict"  # загрузка голоса со словарём Jarvis: упало ядро — дальше голос со словарём espeak-ng
FILE, INFO, ORIG = "ru_dict", "ru_dict.json", "ru_dict.orig"
Dictionary = Literal["jarvis", "espeak"]


def strip_accents(text: str) -> str:
    """Без знаков ударения: для экрана и для голосов, которые знак не понимают."""
    return text.replace(ACCENT, "") if ACCENT in text else text


def folder() -> Path:
    return paths.resource_dir() / "voice"


def bundled(where: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    """Словарь в составе Jarvis и его описание (SHA-256, размер, фонемные таблицы); нет или битый — None."""
    base = where or folder()
    path = base / FILE
    try:
        info = json.loads((base / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not path.is_file() or not isinstance(info, dict) or not isinstance(info.get("phoneme_tables"), dict):
        return None
    if not info["phoneme_tables"] or not isinstance(info.get("sha256"), str):
        return None
    return path, info


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _is(path: Path, info: dict[str, Any]) -> bool:
    """В path — этот словарь (размер, затем SHA-256)."""
    try:
        return path.stat().st_size == int(info.get("size") or -1) and _digest(path) == info["sha256"]
    except (OSError, ValueError, TypeError):
        return False


def _same(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and _digest(a) == _digest(b)
    except OSError:
        return False


def compatible(espeak: Path, info: dict[str, Any]) -> bool:
    """Фонемные таблицы голоса — те, с которыми собран словарь."""
    tables = info.get("phoneme_tables") or {}
    try:
        return bool(tables) and all(_digest(espeak / name) == digest for name, digest in tables.items())
    except (OSError, TypeError):
        return False


def _put(src: Path, dst: Path) -> None:
    """Копия через временный файл и замену: прерванная запись не оставит полсловаря."""
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def install(espeak: Path, on: bool = True, *, source: Path | None = None) -> Dictionary:
    """Какой русский словарь будет у голоса: «jarvis» — словарь Jarvis, «espeak» — исходный. Зовётся до
    загрузки голоса. Ошибка записи — словарь как был (в журнал)."""
    target, orig = espeak / FILE, espeak / ORIG
    found = bundled(source)
    reason = ""
    if not on:
        reason = "выключен в настройках"
    elif found is None:
        reason = "его нет в этой сборке"
    elif crashguard.crashed().get(STAGE):
        reason = "голос с ним аварийно останавливал ядро"
    elif not compatible(espeak, found[1]):
        reason = "фонемные таблицы голоса другие"
    try:
        if found is not None and not reason:
            ours, info = found
            if not _is(target, info):
                if target.is_file() and not orig.exists():
                    shutil.copyfile(target, orig)  # исходный — чтобы вернуть, если словарь выключат
                _put(ours, target)
                log.info("голос: словарь ударений Jarvis → %s", target)
            return "jarvis"
        if orig.is_file() and not _same(target, orig):
            _put(orig, target)
            log.info("голос: исходный словарь espeak-ng → %s (%s)", target, reason)
    except OSError as e:
        log.warning("голос: словарь ударений не заменён (%s) — остался прежний", e)
        return "jarvis" if found is not None and _is(target, found[1]) else "espeak"
    return "espeak"
