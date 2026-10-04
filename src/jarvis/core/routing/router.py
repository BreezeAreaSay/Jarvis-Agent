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

import asyncio
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
from jarvis.domain.paths import unsupported_form
from jarvis.domain.routing import CloudMode, Route, RouteDecision, RoutingLevel
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
    # По-английски слово перед processes бывает и не именем («zombie», «system», «top»): фильтр — только
    # после named / called / matching.
    rf"(?:show|list)(?: me)?(?: all)?(?: running| active)? processes(?: (?:named|called|matching) {_NAME})?",
    r"(?:what|which) processes are (?:running|active)(?: now)?",
    r"running processes|ps|список процессов",
)
_LISTING = _compile(
    rf"{_SHOW}(?: мне)?(?: все)? {_FILES}(?: {_IN} (?P<loc>.+))?",
    rf"{_SHOW}(?: мне)? (?:содержимое|список файлов) (?P<loc>(?:папки|каталога|директории) .+)",
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
    "папке", "папку", "папка", "папки", "каталоге", "каталога", "каталог", "директории", "директорию",
    "the folder", "folder", "the directory", "directory",
)  # fmt: skip
_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|[\\/]|~(?:[\\/]|$)|\.{1,2}[\\/])[^"«»]*')
_ROOT = re.compile(r"~|[A-Za-z]:[\\/]?|[\\/]")
_FILE_SUFFIX = re.compile(r"[^\\/]+\.[A-Za-z0-9]{1,5}")  # последний сегмент «имя.расширение» — файл
_QUOTED = re.compile(r'"(?P<a>[^"]+)"|«(?P<b>[^»]+)»')
_RELATIVE = re.compile(r"[\w.-]+")
_FILE_NAME = re.compile(r"[A-Za-z0-9_.*?-]+")
_ASCII_PROCESS = re.compile(r"[a-z0-9_.+-]*[a-z][a-z0-9_.+-]*")  # латиница, хотя бы одна буква


@dataclass(frozen=True)
class Command:
    """Команда без вежливости и знаков в конце. `text` — символы самой команды (регистр, «ё», пробелы в
    кавычках сохранены: из него берутся пути и адреса), `key` — та же строка с «ё» → «е» для сравнения
    со словарём: длины равны, поэтому позиции совпадений в `key` — позиции в `text`."""

    text: str
    key: str
    question: bool  # в запросе есть «?»: вопрос ничего не запускает
    tail_on_path: bool = False  # знаки в конце сняты с пути или адреса («…/a!»): их сущность неточна


def prepare(raw: str) -> Command:
    question = _is_question(raw)
    text = _collapse_spaces(raw.strip())
    tail_on_path = False
    changed = True
    while changed:
        before = text
        text, on_path = _strip_tail(text)
        tail_on_path = tail_on_path or on_path
        for word in lx.POLITE_PREFIXES:
            text = re.sub(rf"^{re.escape(word)}[\s,!:]+", "", text, flags=_FLAGS)
        text = re.sub(r"^(\w+)-ка\b", r"\1", text, flags=_FLAGS)  # «открой-ка»; «~/дача-ка» не трогаем
        for word in lx.POLITE_SUFFIXES:
            text = re.sub(rf"[\s,]+{re.escape(word)}$", "", text, flags=_FLAGS)
        changed = text != before
    key = text.replace("ё", "е").replace("Ё", "Е")
    return Command(text=text, key=key, question=question, tail_on_path=tail_on_path)


def _is_question(raw: str) -> bool:
    """Вопрос — любой «?» в запросе («открыть браузер?!», «а?»), кроме «?» внутри адреса со схемой
    (`https://ya.ru/search?q=1`); «?» в конце такого адреса — снова вопрос."""
    for token in raw.split():
        if "://" in token:
            if "?" in token[len(token.rstrip("!?.…")) :]:
                return True
        elif "?" in token:
            return True
    return False


def _collapse_spaces(text: str) -> str:
    """Пробелы схлопываются вне кавычек: внутри кавычек — путь как есть («Мои  проекты»)."""
    parts = re.split(r'("[^"]*"|«[^»]*»)', text)
    return "".join(
        part if index % 2 else re.sub(r"\s+", " ", part) for index, part in enumerate(parts)
    ).strip()


def _strip_tail(text: str) -> tuple[str, bool]:
    """Снять знаки в конце предложения («загрузки!», «github.com.»). Точки пути («..», «C:\\Users\\..»)
    остаются: если без знаков от слова ничего не осталось или оно кончается разделителем пути, знаки —
    часть слова. Второй результат — знаки сняты со слова, похожего на путь или адрес."""
    body = text.rstrip()
    head, _, last = body.rpartition(" ")
    bare = last.rstrip("!?.…")
    if bare == last or not bare or bare.endswith((".", "/", "\\")):
        return body, False
    return (f"{head} {bare}" if head else bare), ("/" in bare or "\\" in bare)


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


@dataclass(frozen=True)
class _Match:
    """Совпадение шаблона по `Command.key`; группы — символы из `Command.text`."""

    found: re.Match[str]
    text: str

    def group(self, name: str) -> str | None:
        if name not in self.found.re.groupindex:
            return None
        start, end = self.found.span(name)
        return None if start < 0 else self.text[start:end]

    def required(self, name: str) -> str:
        """Группа, которая в шаблоне обязательна."""
        value = self.group(name)
        if value is None:
            raise LookupError(f"в совпадении нет группы {name}")
        return value


def _first(patterns: Sequence[re.Pattern[str]], command: Command) -> _Match | None:
    for pattern in patterns:
        found = pattern.fullmatch(command.key)
        if found is not None:
            return _Match(found, command.text)
    return None


class Router:
    """Чистое решение: текст запроса → `RouteDecision`. Ввод-вывод — только через порт инвентаря."""

    def __init__(
        self, inventory: Inventory, *, direct: bool = True, mode: CloudMode = CloudMode.AUTO
    ) -> None:
        self._inventory = inventory
        self._direct = direct  # False — только AGENT (бенчмарк модели, где нужна именно модель)
        self._mode = mode  # режим по умолчанию: models.routing.mode

    def warm_up(self) -> None:
        """Прочитать инвентарь заранее: первое решение тогда не включает чтение меню «Пуск» и реестра."""
        if self._direct:
            self._inventory.apps()
            self._inventory.default_browser()

    def decide(
        self, text: str, working_directory: str | None = None, mode: CloudMode | None = None
    ) -> RouteDecision:
        """`mode` — режим запуска задачи; None — режим по умолчанию. Router выбирает стратегию и уровень
        модели, но не провайдера: провайдера внутри уровня выбирает Model Gateway (ADR 0027)."""
        command = prepare(text)
        # Режим запуска может только ужесточить настройку: local_only конфига не отменяется (ADR 0026).
        mode = CloudMode.LOCAL_ONLY if self._mode is CloudMode.LOCAL_ONLY else (mode or self._mode)
        if not self._direct:
            return _agent(command, ["agent.default", "direct.disabled"], mode)
        if not command.text:
            return _agent(command, ["agent.default"], mode)
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
            if isinstance(result, _Hit) and command.tail_on_path and _from_command(result.entities):
                # «открой https://example.com/a!»: «!» мог быть частью адреса — не угадываем.
                misses.extend([*result.rules, "direct.reject.trailing_punctuation"])
                continue
            if isinstance(result, _Hit):
                return RouteDecision(
                    strategy=Route.DIRECT,
                    mode=mode,
                    intent=result.intent,
                    entities=result.entities,
                    rules=result.rules,
                    reason=result.reason,
                )
            if isinstance(result, _Clarify):
                return RouteDecision(
                    strategy=Route.CLARIFY,
                    mode=mode,
                    entities=result.candidates,
                    rules=result.rules,
                    reason="несколько равных кандидатов: нужен выбор человека",
                    question=result.question,
                )
            if isinstance(result, _Miss):
                misses.extend(rule for rule in result.rules if rule not in misses)
        return _agent(command, ["agent.default", *misses], mode)

    # --- чтение -----------------------------------------------------------------------------------

    def _current(self, command: Command, working_directory: str | None) -> _Result:
        if _first(_CURRENT, command) is None:
            return None
        return _Hit(IntentId.FS_CURRENT, [], ["direct.current.folder"], "прямая команда: текущая папка")

    def _processes(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_PROCESSES, command)
        if match is None:
            return None
        rules = ["direct.process.list"]
        raw = (match.group("name") or "").casefold()
        if not raw or raw in lx.PROCESS_STOP_WORDS:
            return _Hit(IntentId.PROCESS_LIST, [], rules, "прямая команда: список процессов")
        # Имя процесса — как его написал пользователь, латиницей: разговорных имён программ («хром»)
        # ядро не знает, такие запросы ведёт агент.
        if _ASCII_PROCESS.fullmatch(raw) is None:
            return _Miss([*rules, "direct.reject.process_name"])
        entity = ResolvedEntity(kind=EntityKind.PROCESS, value=raw, label=raw, source="command")
        return _Hit(
            IntentId.PROCESS_LIST, [entity], [*rules, "filter.process"], "прямая команда: процессы по имени"
        )

    def _listing(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_LISTING, command)
        if match is not None:
            loc = match.group("loc")
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
        match = _first(_SHOW_FOLDER, command)
        if match is None:
            return None
        folder, rule = self._folder(match.required("loc"), working_directory, explicit=False)
        if folder is None:
            return _Miss()  # «покажи …» — не обязательно папка: решит агент
        return _Hit(
            IntentId.FS_LIST, [folder], ["direct.list.folder", rule], "прямая команда: содержимое папки"
        )

    def _search(self, command: Command, working_directory: str | None) -> _Result:
        root = _working(working_directory)
        match = _first(_SEARCH_EXTENSION, command)
        if match is not None:
            extension = match.required("ext").casefold()
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
        match = _first(_SEARCH_NAME, command)
        if match is None:
            return None
        raw = _unquote(match.required("name"))
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
        match = _first(_OPEN_TARGET, command)
        if match is None:
            return None
        target = match.required("target")
        explicit = _folder_word(target) is not None
        folder, rule = self._folder(target, working_directory, explicit=explicit)
        if folder is None:
            # Не папка — возможно, приложение («открой телеграм»): тогда отказ не нужен в объяснении.
            plain = not explicit and rule == "direct.reject.unknown_folder"
            return None if plain else _Miss(["direct.open.folder", rule])
        if command.question:
            return _Miss(["direct.open.folder", "direct.reject.question"])
        return _Hit(
            IntentId.FOLDER_OPEN, [folder], ["direct.open.folder", rule], "прямая команда: открыть папку"
        )

    def _open_url(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_URL_TARGET, command)
        if match is None:
            return None
        raw = match.required("url")
        url = normalize_web_url(raw)
        if url is None:
            return None
        if command.question:
            return _Miss(["direct.open.url", "direct.reject.question"])
        rule = "url.http" if raw.casefold().startswith(("http://", "https://")) else "url.domain"
        entity = ResolvedEntity(kind=EntityKind.URL, value=url, label=raw, source="command")
        apps = _apps_named(norm(raw), self._inventory.apps()) if rule == "url.domain" else []
        if apps:  # «открой Battle.net» — и сайт, и установленное приложение: выбирает человек
            names = ", ".join(f"приложение «{app.name}»" for app, _ in apps[:4])
            return _Clarify(
                question=f"Что открыть: {names} или сайт {url}?",
                candidates=[*(_app_entity(app, source) for app, source in apps[:4]), entity],
                rules=["direct.open.url", rule, "url.or_app.ambiguous"],
            )
        return _Hit(IntentId.URL_OPEN, [entity], ["direct.open.url", rule], "прямая команда: открыть адрес")

    def _launch(self, command: Command, working_directory: str | None) -> _Result:
        match = _first(_LAUNCH_TARGET, command)
        if match is None:
            return None
        rules = ["direct.launch.verb"]
        key = norm(_strip_words(match.required("target"), lx.APP_WORDS))
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
        candidate = text.strip('"«»')
        if re.match(r"[\\/]|[A-Za-z]:", candidate) and unsupported_form(candidate, "windows"):
            # Сетевой путь (\\сервер\папка, /\сервер), \\?\… или поток NTFS: открытие обратилось бы к
            # чужому серверу — только через агента, на любой ОС.
            return None, "direct.reject.unsupported_path"
        key = norm(text)
        if key in lx.CURRENT_FOLDER:
            return _working(working_directory), "folder.current"
        known = lx.FOLDER_ALIASES.get(key)
        if known is not None:
            if not explicit and known not in lx.UNAMBIGUOUS_FOLDERS:
                return None, "direct.reject.ambiguous_folder"
            path = self._inventory.known_folder(known)
            if path is None:  # такой папки на этом компьютере нет
                return None, "direct.reject.folder_not_found"
            return ResolvedEntity(
                kind=EntityKind.FOLDER, value=path, label=text, source="folder.known"
            ), f"folder.known.{known.value}"
        quoted = _QUOTED.fullmatch(text)
        if quoted is not None:
            # «открой "Discord"» — не папка: в кавычках папка, только если так и сказано или это путь.
            path = quoted.group("a") or quoted.group("b")
            if not explicit and _PATH.fullmatch(path) is None:
                return None, "direct.reject.unknown_folder"
            return _path_entity(path, explicit=explicit)
        if _PATH.fullmatch(text) is not None and " " not in text.strip():
            return _path_entity(text, explicit=explicit)
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
        # В потоке: первое решение о запуске читает инвентарь (меню «Пуск»), цикл событий не ждёт.
        decision = await asyncio.to_thread(
            self._router.decide, task.request.text, task.request.working_directory, task.request.mode
        )
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
        "mode": decision.mode.value,
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


_LEVEL_REASONS = {
    RoutingLevel.FAST: "прямой команды нет: короткая задача агента на локальной модели",
    RoutingLevel.LOCAL: "прямой команды нет: задачу ведёт агент на локальной модели",
    RoutingLevel.SMART: "прямой команды нет: задачу ведёт агент уровня smart (облако — по политике)",
    RoutingLevel.CODING: "прямой команды нет: задачу ведёт агент уровня coding (облако — по политике)",
}


def _agent(command: Command, rules: list[str], mode: CloudMode) -> RouteDecision:
    level, rule = _level(command, mode)
    return RouteDecision(
        strategy=Route.AGENT,
        level=level,
        mode=mode,
        rules=[*rules, rule],
        reason=_LEVEL_REASONS[level],
    )


def _level(command: Command, mode: CloudMode) -> tuple[RoutingLevel, str]:
    """Уровень модели: режим запуска, а в режиме auto — сильные признаки запроса; сомнение — local."""
    match mode:
        case CloudMode.LOCAL_ONLY:
            return RoutingLevel.LOCAL, "level.mode.local_only"
        case CloudMode.SMART:
            return RoutingLevel.SMART, "level.mode.smart"
        case CloudMode.CODING:
            return RoutingLevel.CODING, "level.mode.coding"
        case CloudMode.AUTO:
            if _starts_with(command.key, lx.CODING_SIGNALS):
                return RoutingLevel.CODING, "level.signal.coding"
            if _starts_with(command.key, lx.SMART_SIGNALS):
                return RoutingLevel.SMART, "level.signal.smart"
            return RoutingLevel.LOCAL, "level.default"


def _starts_with(text: str, phrases: Sequence[str]) -> bool:
    lowered = text.casefold()
    return any(lowered == phrase or lowered.startswith(f"{phrase} ") for phrase in phrases)


def _path_entity(path: str, *, explicit: bool) -> tuple[ResolvedEntity | None, str]:
    """Путь из команды. Router не видит диска: файл от папки отличает только сама команда. Последняя
    часть «имя.расширение» — файл; без слова «папка» путь — папка, только если кончается разделителем
    («~/projects/») или это корень («~», «C:\\»): «покажи /etc/hosts» ведёт агент."""
    last = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
    if _FILE_SUFFIX.fullmatch(last) is not None:
        return None, "direct.reject.file_not_folder"
    if not explicit and not (path.endswith(("/", "\\")) or _ROOT.fullmatch(path)):
        return None, "direct.reject.path_needs_folder_word"
    return ResolvedEntity(kind=EntityKind.FOLDER, value=path, label=path, source="command"), "folder.path"


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
    if normalize_web_url(raw) is not None:
        return None  # «где находится python.org» — вопрос о сайте, а не поиск файла
    stem, dot, extension = raw.rpartition(".")
    if dot and stem and 1 <= len(extension) <= 10 and extension.isalnum() and not extension.isdigit():
        return raw, "pattern.file_name"
    # «найди README», «найди Makefile»: имя в привычном написании; «find security» — не файл.
    if (raw.isupper() and raw.casefold() in lx.UPPERCASE_FILE_NAMES) or raw.casefold() in lx.TOOL_FILE_NAMES:
        return f"{raw}*", "pattern.known_name"
    return None


def _apps_named(key: str, apps: Sequence[AppEntry]) -> list[tuple[AppEntry, str]]:
    """Приложения, чьё имя, алиас или ID совпадает с ключом целиком (без нечёткого поиска)."""
    found: dict[str, tuple[AppEntry, str]] = {}
    for entry in apps:
        if key == norm(entry.name):
            found.setdefault(entry.id, (entry, "inventory.app.name"))
        elif key in {norm(alias) for alias in entry.aliases} or key == norm(entry.id):
            found.setdefault(entry.id, (entry, "inventory.app.alias"))
    return list(found.values())


def _from_command(entities: Sequence[ResolvedEntity]) -> bool:
    return any(
        entity.source == "command" and entity.kind in (EntityKind.URL, EntityKind.FOLDER)
        for entity in entities
    )


def _app_entity(entry: AppEntry, source: str) -> ResolvedEntity:
    return ResolvedEntity(kind=EntityKind.APP, value=entry.id, label=entry.name, source=source)
