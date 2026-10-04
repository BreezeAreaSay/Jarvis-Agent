"""Что исполняет прямая команда и как об этом сказать (ADR 0026, ADR 0030).

Аргументы вызова строятся только из сущностей решения Router (инвентарь, известные папки, разбор URL,
путь из команды, рабочая папка), ответ — шаблоном по результату инструмента. Модели здесь нет.
"""

from dataclasses import dataclass

from pydantic import JsonValue

from jarvis.domain.intents import EntityKind, IntentId
from jarvis.domain.routing import RouteDecision

LIST_SHOWN = 30
SEARCH_SHOWN = 20
PROCESSES_SHOWN = 30
LIST_MAX_ENTRIES = 200
SEARCH_MAX_RESULTS = 100
PROCESS_MAX_RESULTS = 200


@dataclass(frozen=True)
class DirectCall:
    tool: str
    arguments: dict[str, JsonValue]


def direct_call(decision: RouteDecision) -> DirectCall:
    assert decision.intent is not None
    entity = decision.entity
    match decision.intent:
        case IntentId.APP_LAUNCH:
            return DirectCall("app.launch", {"app": _value(entity(EntityKind.APP))})
        case IntentId.URL_OPEN:
            return DirectCall("url.open", {"url": _value(entity(EntityKind.URL))})
        case IntentId.FOLDER_OPEN:
            return DirectCall("folder.open", {"path": _value(entity(EntityKind.FOLDER))})
        case IntentId.FS_CURRENT:
            return DirectCall("system.cwd", {})
        case IntentId.FS_LIST:
            return DirectCall(
                "filesystem.list",
                {"path": _value(entity(EntityKind.FOLDER)), "max_entries": LIST_MAX_ENTRIES},
            )
        case IntentId.FS_SEARCH:
            return DirectCall(
                "filesystem.search",
                {
                    "root": _value(entity(EntityKind.FOLDER)),
                    "pattern": _value(entity(EntityKind.PATTERN)),
                    "max_results": SEARCH_MAX_RESULTS,
                },
            )
        case IntentId.PROCESS_LIST:
            process = entity(EntityKind.PROCESS)
            arguments: dict[str, JsonValue] = {"max_results": PROCESS_MAX_RESULTS}
            if process is not None:
                arguments["name"] = process.value
            return DirectCall("process.list", arguments)


def answer(decision: RouteDecision, output: dict[str, JsonValue]) -> str:
    """Ответ пользователю по результату инструмента. Имена файлов и процессов — данные: они только
    перечисляются (CLI выводит ответ без управляющих символов)."""
    assert decision.intent is not None
    match decision.intent:
        case IntentId.APP_LAUNCH:
            return f"Запускаю {_text(output.get('name'))}."
        case IntentId.URL_OPEN:
            return f"Открываю {_text(output.get('url'))} в браузере."
        case IntentId.FOLDER_OPEN:
            return f"Открываю папку {_text(output.get('path'))}."
        case IntentId.FS_CURRENT:
            return f"Рабочая папка: {_text(output.get('path'))}"
        case IntentId.FS_LIST:
            return _listing(output)
        case IntentId.FS_SEARCH:
            return _search(output)
        case IntentId.PROCESS_LIST:
            process = decision.entity(EntityKind.PROCESS)
            return _processes(output, process.value if process else None)


def _listing(output: dict[str, JsonValue]) -> str:
    path = _text(output.get("path"))
    entries = _items(output.get("entries"))
    total = _count(output.get("total"), len(entries))
    if total == 0:
        return f"Папка {path} пуста."
    lines = [f"{path} — {total} {_plural(total, 'элемент', 'элемента', 'элементов')}:"]
    for entry in entries[:LIST_SHOWN]:
        name = _text(entry.get("name"))
        if entry.get("kind") == "dir":
            lines.append(f"  {name}/")
        else:
            size = entry.get("size")
            lines.append(f"  {name}" + (f" ({_size(size)})" if isinstance(size, int) else ""))
    if total > min(len(entries), LIST_SHOWN):
        lines.append(f"  … и ещё {total - min(len(entries), LIST_SHOWN)}")
    return "\n".join(lines)


def _search(output: dict[str, JsonValue]) -> str:
    root = _text(output.get("root"))
    pattern = _text(output.get("pattern"))
    matches = _items(output.get("matches"))
    limits = output.get("limits_hit")
    stopped = " Поиск остановлен на пределе — результат может быть неполным." if limits else ""
    if not matches:
        return f"По шаблону «{pattern}» в {root} ничего не найдено.{stopped}"
    lines = [f"Найдено {len(matches)} по шаблону «{pattern}» в {root}:"]
    for match in matches[:SEARCH_SHOWN]:
        path = _text(match.get("path"))
        suffix = "/" if match.get("kind") == "dir" else ""
        lines.append(f"  {_relative(path, root)}{suffix}")
    if len(matches) > SEARCH_SHOWN:
        lines.append(f"  … и ещё {len(matches) - SEARCH_SHOWN}")
    if stopped:
        lines.append(stopped.strip())
    return "\n".join(lines)


def _processes(output: dict[str, JsonValue], name: str | None) -> str:
    processes = _items(output.get("processes"))
    total = _count(output.get("total"), len(processes))
    which = f" «{name}»" if name else ""
    if total == 0:
        return f"Процессов{which} нет."
    lines = [f"Процессы{which}: {total}"]
    for process in processes[:PROCESSES_SHOWN]:
        lines.append(f"  {_text(process.get('name'))} (pid {process.get('pid')})")
    if total > min(len(processes), PROCESSES_SHOWN):
        lines.append(f"  … и ещё {total - min(len(processes), PROCESSES_SHOWN)}")
    return "\n".join(lines)


def _value(entity: object) -> str:
    value = getattr(entity, "value", None)
    if not isinstance(value, str):
        raise ValueError("в решении Router нет сущности, нужной прямой команде")
    return value


def _text(value: JsonValue) -> str:
    return "" if value is None else str(value)


def _items(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _count(value: JsonValue, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _relative(path: str, root: str) -> str:
    for separator in ("/", "\\"):
        prefix = root.rstrip("/\\") + separator
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} Б"


def _plural(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)
