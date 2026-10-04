"""Router — детерминированное решение о способе исполнения задачи (ADR 0026).

Router читает текст запроса и выбирает стратегию: DIRECT (распознанная команда исполняется
инструментом без модели), CLARIFY (несколько равных кандидатов — вопрос человеку) или AGENT (задачу
ведёт модель). Модель он не вызывает, инструменты не исполняет и о побочных эффектах не решает — это
делает политика Tool Runtime. Решение — значение с ID сработавших правил.

Главное правило — точность важнее полноты: лишняя команда, отданная агенту, стоит секунды; ложная
DIRECT исполнила бы не то. Поэтому шаблоны якорятся на весь текст, сущность должна совпасть целиком с
доверенным источником (инвентарь, известная папка, разбор URL, путь из самой команды), а вопрос
(«…?») не запускает ничего.
"""

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pydantic import JsonValue

from jarvis.core.budget import BudgetMeter
from jarvis.core.routing import lexicon as lx
from jarvis.core.trace import Tracer, shorten
from jarvis.domain.intents import EntityKind, IntentId, ResolvedEntity
from jarvis.domain.inventory import AppEntry, name_key
from jarvis.domain.routing import Route, RouteDecision, RoutingLevel
from jarvis.domain.states import TaskStatus
from jarvis.domain.task import StageOutcome, Task, TaskChanges
from jarvis.domain.trace import EventKind
from jarvis.domain.urls import normalize_web_url
from jarvis.ports.inventory import Inventory
from jarvis.ports.storage import UnitOfWorkFactory

_FLAGS = re.IGNORECASE | re.UNICODE


def _alt(words: Iterable[str]) -> str:
    """Альтернатива слов для шаблона: длинные формы первыми («перейди на» раньше «перейди»)."""
    return "(?:" + "|".join(re.escape(word) for word in sorted(set(words), key=len, reverse=True)) + ")"


_LAUNCH = _alt(lx.LAUNCH_VERBS)
_OPEN = _alt(lx.OPEN_VERBS)
_URL_VERB = _alt(lx.URL_VERBS)
_SHOW = _alt(lx.SHOW_VERBS)
_FIND = _alt(lx.FIND_VERBS)
_URL_WORD = _alt(lx.URL_WORDS)
_FILES = _alt(("файлы", "файлы и папки", "содержимое", "список файлов", "files", "the files", "all files",
               "contents", "the contents", "files and folders"))  # fmt: skip
_IN = _alt(("в", "из", "на", "in", "of", "inside"))
_NAME = r"(?P<name>[\w.+-]{1,40})"


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, _FLAGS) for pattern in patterns)


_CURRENT = _compile(
    r"где я|где мы|в какой (?:я )?папке(?: я| мы)?|какая (?:сейчас )?(?:текущая|рабочая) папка|"
    r"(?:текущая|рабочая) папка|pwd|where am i|(?:what is |what's )?(?:the )?(?:current|working) "
    r"(?:directory|folder)"
)
_PROCESSES = _compile(
    rf"(?:{_SHOW} )?(?:мне )?(?:все )?(?:запущенные |активные |работающие )?процессы(?: {_NAME})?",
    rf"(?:какие|что за) (?:{_NAME} )?процессы(?: сейчас)?(?: (?:запущены|работают|активны|есть))?",
    rf"(?:show|list)(?: me)?(?: all)?(?: running| active)?(?: {_NAME})? processes",
    rf"(?:show|list)(?: me)?(?: all)?(?: running| active)? processes"
    rf"(?: (?:named |called |matching )?{_NAME})?",
    rf"(?:what|which)(?: {_NAME})? processes are (?:running|active)(?: now)?",
    r"running processes|ps|список процессов",
)
_LISTING = _compile(
    rf"{_SHOW}(?: мне)?(?: все)? {_FILES}(?: {_IN} (?P<loc>.+))?",
    r"что (?:лежит|есть|находится) (?:в|на) (?P<loc>.+)",
    r"что (?:здесь|тут) (?:лежит|есть)(?P<here>)",
    r"что (?:в|на) (?P<loc>.+)",
    r"what(?:'s| is) in (?P<loc>.+)",
    r"(?:ls|dir|ls -l|ls -la)(?P<here>)",
)
_SHOW_FOLDER = _compile(rf"{_SHOW} (?P<loc>.+)")
_SEARCH_NAME = _compile(rf"{_FIND}(?: (?:файл|file|the file))? (?P<name>\S+)")
_SEARCH_EXTENSION = _compile(
    rf"{_FIND}(?: все| all)? (?P<ext>\w+)(?:[ -](?:файлы|файл|документы|files|documents))",
    rf"{_FIND}(?: все| all)? (?:файлы|files) (?P<ext>\w+)",
    rf"{_FIND} (?:все|all) (?P<ext>\w+)",
)
_OPEN_TARGET = _compile(rf"{_OPEN} (?P<target>.+)")
_URL_TARGET = _compile(rf"{_URL_VERB}(?: {_URL_WORD})? (?P<url>\S+)")
_LAUNCH_TARGET = _compile(rf"{_LAUNCH} (?P<target>.+)")

_FOLDER_PREFIXES = (
    "папке", "папку", "папка", "каталоге", "каталог", "директории", "директорию", "the folder", "folder",
    "the directory", "directory",
)  # fmt: skip
_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|[\\/]|~(?:[\\/]|$)|\.{1,2}[\\/])[^"«»]*')
_FILE_SUFFIX = re.compile(r"[^\\/]+\.[A-Za-z0-9]{1,5}")  # последний сегмент «имя.расширение» — файл
_QUOTED = re.compile(r'"(?P<a>[^"]+)"|«(?P<b>[^»]+)»')
_RELATIVE = re.compile(r"[\w.-]+")
_FILE_NAME = re.compile(r"[A-Za-z0-9_.*?-]+")
_ASCII_PROCESS = re.compile(r"[a-z0-9_.+-]+")


@dataclass(frozen=True)
class Command:
    """Текст команды без вежливости и знаков в конце: регистр сохранён (в нём бывают пути)."""

    text: str
    question: bool  # заканчивался «?»: вопрос ничего не запускает


def prepare(raw: str) -> Command:
    text = raw.strip().replace("ё", "е").replace("Ё", "Е")
    question = text.endswith("?")
    text = re.sub(r"[\s!?.…]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=\w)-ка\b", "", text, flags=_FLAGS)  # «открой-ка»
    changed = True
    while changed:
        changed = False
        for word in lx.POLITE_PREFIXES:
            stripped = re.sub(rf"^{re.escape(word)}[\s,!:]+", "", text, flags=_FLAGS)
            if stripped != text:
                text, changed = stripped, True
        for word in lx.POLITE_SUFFIXES:
            stripped = re.sub(rf"[\s,]+{re.escape(word)}$", "", text, flags=_FLAGS)
            if stripped != text:
                text, changed = stripped, True
    return Command(text=text, question=question)


norm = name_key


@dataclass(frozen=True)
class _Hit:
    intent: IntentId
    entities: list[ResolvedEntity]
    rules: list[str]
    reason: str


@dataclass(frozen=True)
class _Clarify:
    question: str
    candidates: list[ResolvedEntity]
    rules: list[str]


@dataclass(frozen=True)
class _Miss:
    """Похоже на команду, но не DIRECT: почему (правила попадают в решение для объяснения)."""

    rules: list[str] = field(default_factory=list[str])


_Result = _Hit | _Clarify | _Miss | None


def _first(patterns: Sequence[re.Pattern[str]], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = pattern.fullmatch(text)
        if match is not None:
            return match
    return None


class Router:
    """Чистое решение: текст запроса → `RouteDecision`. Ввод-вывод — только через порт инвентаря."""

    def __init__(self, inventory: Inventory, *, direct: bool = True) -> None:
        self._inventory = inventory
        self._direct = direct  # False — только AGENT (бенчмарк модели, где нужна именно модель)

    def decide(self, text: str, working_directory: str | None = None) -> RouteDecision:
        command = prepare(text)
        if not self._direct:
            return _agent(["agent.default", "direct.disabled"])
        if not command.text:
            return _agent(["agent.default"])
        misses: list[str] = []
        for matcher in (
            self._current,
            self._processes,
            self._listing,
            self._search,
            self._open_folder,
            self._open_url,
            self._launch,
        ):
            result = matcher(command, working_directory)
            if isinstance(result, _Hit):
                return RouteDecision(
                    strategy=Route.DIRECT,
                    intent=result.intent,
                    entities=result.entities,
                    rules=result.rules,
                    reason=result.reason,
                )
            if isinstance(result, _Clarify):
                return RouteDecision(
                    strategy=Route.CLARIFY,
                    entities=result.candidates,
                    rules=result.rules,
                    reason="несколько равных кандидатов: нужен выбор человека",
                    question=result.question,
                )
            if isinstance(result, _Miss):
                misses.extend(rule for rule in result.rules if rule not in misses)
        return _agent(["agent.default", *misses])

    # --- чтение -----------------------------------------------------------------------------------

    def _current(self, command: Command, working_directory: str | None) -> _Result:
        if _first(_CURRENT, command.text) is None:
            return None
        return _Hit(IntentId.FS_CURRENT, [], ["direct.current.folder"], "прямая команда: текущая папка")

    def _processes(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_PROCESSES, command.text)
        if match is None:
            return None
        rules = ["direct.process.list"]
        raw = (match.groupdict().get("name") or "").casefold()
        if not raw or raw in lx.PROCESS_STOP_WORDS:
            return _Hit(IntentId.PROCESS_LIST, [], rules, "прямая команда: список процессов")
        name, source = (
            lx.PROCESS_ALIASES.get(raw, raw),
            "alias.process" if raw in lx.PROCESS_ALIASES else "command",
        )
        if _ASCII_PROCESS.fullmatch(name) is None:
            return _Miss([*rules, "direct.reject.process_name"])
        entity = ResolvedEntity(kind=EntityKind.PROCESS, value=name, label=name, source=source)
        return _Hit(
            IntentId.PROCESS_LIST, [entity], [*rules, "filter.process"], "прямая команда: процессы по имени"
        )

    def _listing(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_LISTING, command.text)
        if match is not None:
            groups = match.groupdict()
            loc = groups.get("loc")
            # «что в X» бывает и про файл («что в файле?», «что в отчёте»): относительное имя — папка,
            # только если так и сказано («в папке src») или это путь.
            folder, rule = (
                self._folder(loc, working_directory, explicit=False)
                if loc is not None
                else (_working(working_directory), "folder.current")
            )
            if folder is None:
                return _Miss(["direct.list.files", rule])
            return _Hit(
                IntentId.FS_LIST, [folder], ["direct.list.files", rule], "прямая команда: содержимое папки"
            )
        match = _first(_SHOW_FOLDER, command.text)
        if match is None:
            return None
        folder, rule = self._folder(match.group("loc"), working_directory, explicit=False)
        if folder is None:
            return _Miss()  # «покажи …» — не обязательно папка: решит агент
        return _Hit(
            IntentId.FS_LIST, [folder], ["direct.list.folder", rule], "прямая команда: содержимое папки"
        )

    def _search(self, command: Command, working_directory: str | None) -> _Result:
        root = _working(working_directory)
        match = _first(_SEARCH_EXTENSION, command.text)
        if match is not None:
            extension = match.group("ext").casefold()
            if extension in lx.SEARCH_EXTENSIONS:
                pattern = ResolvedEntity(
                    kind=EntityKind.PATTERN, value=f"*.{extension}", label=f"*.{extension}", source="command"
                )
                return _Hit(
                    IntentId.FS_SEARCH,
                    [pattern, root],
                    ["direct.search.file", "pattern.extension"],
                    "прямая команда: поиск файлов по расширению",
                )
        match = _first(_SEARCH_NAME, command.text)
        if match is None:
            return None
        raw = _unquote(match.group("name"))
        found = _file_pattern(raw)
        if found is None:
            return _Miss(["direct.search.file", "direct.reject.not_a_file_name"])
        pattern, rule = found
        entity = ResolvedEntity(kind=EntityKind.PATTERN, value=pattern, label=raw, source="command")
        return _Hit(
            IntentId.FS_SEARCH, [entity, root], ["direct.search.file", rule], "прямая команда: поиск файла"
        )

    # --- открыть и запустить (побочный эффект: вопрос ничего не запускает) ------------------------

    def _open_folder(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_OPEN_TARGET, command.text)
        if match is None:
            return None
        target = match.group("target")
        explicit = _folder_word(target) is not None
        folder, rule = self._folder(target, working_directory, explicit=explicit)
        if folder is None:
            return _Miss(["direct.open.folder", rule]) if explicit else None
        if command.question:
            return _Miss(["direct.open.folder", "direct.reject.question"])
        return _Hit(
            IntentId.FOLDER_OPEN, [folder], ["direct.open.folder", rule], "прямая команда: открыть папку"
        )

    def _open_url(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_URL_TARGET, command.text)
        if match is None:
            return None
        raw = match.group("url")
        url = normalize_web_url(raw)
        if url is None:
            return None
        if command.question:
            return _Miss(["direct.open.url", "direct.reject.question"])
        rule = "url.http" if raw.casefold().startswith(("http://", "https://")) else "url.domain"
        entity = ResolvedEntity(kind=EntityKind.URL, value=url, label=url, source="command")
        return _Hit(IntentId.URL_OPEN, [entity], ["direct.open.url", rule], "прямая команда: открыть адрес")

    def _launch(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_LAUNCH_TARGET, command.text)
        if match is None:
            return None
        rules = ["direct.launch.verb"]
        key = norm(_strip_words(match.group("target"), lx.APP_WORDS))
        if not key:
            return _Miss([*rules, "direct.reject.no_target"])
        if command.question:
            return _Miss([*rules, "direct.reject.question"])
        if key in lx.BROWSER_WORDS:
            browser = self._inventory.default_browser()
            if browser is None:
                return _Miss([*rules, "direct.reject.no_default_browser"])
            return _Hit(
                IntentId.APP_LAUNCH,
                [_app_entity(browser, "inventory.default_browser")],
                [*rules, "inventory.default_browser"],
                "прямая команда: запуск браузера по умолчанию",
            )
        found = _apps_named(key, self._inventory.apps())
        if not found:
            return _Miss([*rules, "direct.reject.unknown_app"])
        if len(found) > 1:
            names = ", ".join(f"«{entry.name}»" for entry, _ in found[:5])
            return _Clarify(
                question=f"Какое приложение открыть: {names}?",
                candidates=[_app_entity(entry, source) for entry, source in found[:5]],
                rules=[*rules, "inventory.app.ambiguous"],
            )
        entry, source = found[0]
        return _Hit(
            IntentId.APP_LAUNCH,
            [_app_entity(entry, source)],
            [*rules, source],
            "прямая команда: запуск приложения из инвентаря",
        )

    # --- сущности ---------------------------------------------------------------------------------

    def _folder(
        self, raw: str, working_directory: str | None, *, explicit: bool
    ) -> tuple[ResolvedEntity | None, str]:
        """Папка из текста команды: рабочая, известная (из инвентаря) или путь из самой команды."""
        text = raw.strip()
        word = _folder_word(text)
        if word is not None:
            text, explicit = text[len(word) :].strip(), True
        key = norm(text)
        if key in lx.CURRENT_FOLDER:
            return _working(working_directory), "folder.current"
        known = lx.FOLDER_ALIASES.get(key)
        if known is not None:
            if not explicit and known not in lx.UNAMBIGUOUS_FOLDERS:
                return None, "direct.reject.ambiguous_folder"
            path = self._inventory.known_folder(known)
            if path is None:
                return None, "direct.reject.unknown_folder"
            return ResolvedEntity(
                kind=EntityKind.FOLDER, value=path, label=text, source="folder.known"
            ), f"folder.known.{known.value}"
        quoted = _QUOTED.fullmatch(text)
        if quoted is not None:
            path = quoted.group("a") or quoted.group("b")
            return ResolvedEntity(
                kind=EntityKind.FOLDER, value=path, label=path, source="command"
            ), "folder.path"
        if _PATH.fullmatch(text) is not None and " " not in text.strip():
            if _FILE_SUFFIX.fullmatch(re.split(r"[\\/]", text.rstrip("\\/"))[-1]) is not None:
                return None, "direct.reject.file_not_folder"
            return ResolvedEntity(
                kind=EntityKind.FOLDER, value=text, label=text, source="command"
            ), "folder.path"
        if explicit and _RELATIVE.fullmatch(text) is not None:
            return ResolvedEntity(
                kind=EntityKind.FOLDER, value=text, label=text, source="command"
            ), "folder.relative"
        return None, "direct.reject.unknown_folder"


class RoutingStage:
    """Стадия ROUTING: решение Router, событие `route.decided` и переход по стратегии."""

    def __init__(self, *, router: Router, uow: UnitOfWorkFactory, tracer: Tracer) -> None:
        self._router = router
        self._uow = uow
        self._tracer = tracer

    async def handle(self, task: Task, budget: BudgetMeter) -> StageOutcome:
        started = time.perf_counter()
        decision = self._router.decide(task.request.text, task.request.working_directory)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        event = self._tracer.event(task.id, EventKind.ROUTE_DECIDED, decision_payload(decision, elapsed_ms))
        with self._uow() as uow:
            uow.trace.append([event])
            uow.commit()
        match decision.strategy:
            case Route.DIRECT:
                return StageOutcome(
                    next_status=TaskStatus.EXECUTING,
                    reason=decision.reason,
                    changes=TaskChanges(route=Route.DIRECT, routing=decision),
                )
            case Route.CLARIFY:
                return StageOutcome(
                    next_status=TaskStatus.COMPLETED,
                    reason=decision.reason,
                    changes=TaskChanges(route=Route.CLARIFY, routing=decision, answer=decision.question),
                )
            case _:
                return StageOutcome(
                    next_status=TaskStatus.PLANNING,
                    reason=decision.reason,
                    changes=TaskChanges(route=Route.AGENT, routing=decision),
                )


def decision_payload(decision: RouteDecision, elapsed_ms: float | None = None) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "strategy": decision.strategy.value,
        "level": decision.level.value if decision.level else None,
        "intent": decision.intent.value if decision.intent else None,
        "entities": [
            {
                "kind": entity.kind.value,
                "value": shorten(entity.value, 200),
                "label": shorten(entity.label, 100),
                "source": entity.source,
            }
            for entity in decision.entities[:5]
        ],
        "rules": list[JsonValue](decision.rules[:20]),
        "reason": shorten(decision.reason),
    }
    if decision.question is not None:
        payload["question"] = shorten(decision.question, 300)
    if elapsed_ms is not None:
        payload["duration_ms"] = elapsed_ms
    return payload


def _agent(rules: list[str]) -> RouteDecision:
    return RouteDecision(
        strategy=Route.AGENT,
        level=RoutingLevel.LOCAL,
        rules=rules,
        reason="прямой команды нет: задачу ведёт агент на локальной модели",
    )


def _working(working_directory: str | None) -> ResolvedEntity:
    path = working_directory or "."
    return ResolvedEntity(kind=EntityKind.FOLDER, value=path, label=path, source="working_directory")


def _folder_word(text: str) -> str | None:
    lowered = text.casefold()
    for word in _FOLDER_PREFIXES:
        if lowered == word or lowered.startswith(f"{word} "):
            return text[: len(word)]
    return None


def _strip_words(text: str, words: Sequence[str]) -> str:
    lowered = text.casefold()
    for word in sorted(words, key=len, reverse=True):
        if lowered.startswith(f"{word} "):
            return text[len(word) + 1 :]
    return text


def _unquote(text: str) -> str:
    quoted = _QUOTED.fullmatch(text)
    if quoted is not None:
        return quoted.group("a") or quoted.group("b") or text
    return text.strip("'`")


def _file_pattern(raw: str) -> tuple[str, str] | None:
    """Шаблон поиска по имени файла из команды. Обычное слово («ошибку») именем файла не считается."""
    if _FILE_NAME.fullmatch(raw) is None or not any(ch.isalnum() for ch in raw):
        return None
    if "*" in raw or "?" in raw:
        return raw, "pattern.wildcard"
    stem, dot, extension = raw.rpartition(".")
    if dot and stem and 1 <= len(extension) <= 10 and extension.isalnum():
        return raw, "pattern.file_name"
    if raw.casefold() in lx.KNOWN_FILE_NAMES:
        return f"{raw}*", "pattern.known_name"
    return None


def _apps_named(key: str, apps: Sequence[AppEntry]) -> list[tuple[AppEntry, str]]:
    """Приложения, чьё имя, алиас или ID совпадает с ключом целиком (без нечёткого поиска)."""
    found: dict[str, tuple[AppEntry, str]] = {}
    for entry in apps:
        if key == norm(entry.name):
            found.setdefault(entry.id, (entry, "inventory.app.name"))
        elif key in {norm(alias) for alias in entry.aliases} or key == entry.id:
            found.setdefault(entry.id, (entry, "inventory.app.alias"))
    return list(found.values())


def _app_entity(entry: AppEntry, source: str) -> ResolvedEntity:
    return ResolvedEntity(kind=EntityKind.APP, value=entry.id, label=entry.name, source=source)
