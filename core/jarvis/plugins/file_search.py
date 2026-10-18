"""«Поиск по ПК»: файлы и папки — голосом, в быстром поиске поверх окон и для Jarvis AI.

Где ищет. Индекс Windows (Windows Search, ctx.platform.search_index) — по имени, типу, дате, папке и тексту
внутри файлов. Если индекс выключен в настройках или служба не отвечает — свой лёгкий индекс: «Загрузки»,
«Документы», «Рабочий стол», «Изображения», «Музыка», «Видео» и папки из настроек (вглубь до SCAN_DEPTH,
без node_modules, .git, AppData, скрытых и системных папок, не больше SCAN_MAX_ENTRIES записей). Он живёт
в памяти (копия — в кэше, чтобы после перезапуска искать сразу) и обновляется в фоне.

Фраза → запрос (parse_query): «найди договор с Ивановым», «где презентация про бюджет», «покажи фото
за вчера», «найди pdf в загрузках», «открой папку проекты», «найди файлы со словом отчёт», «открой последний
скачанный файл» → слова имени (основы: «договора» → «договор», «презентацию» → «презентац»), типы из белого
списка, даты, известная папка или часть пути, слова текста, порядок и сколько нужно. Найденное ранжируется
(rank_files): имя целиком и с начала, доля слов запроса в имени и пути, свежесть.

Голос: одно явное совпадение — Jarvis открывает его («Открываю …»); несколько — карточка с номерами
в оверлее (plugin.file_search.results) и «Нашёл N, сэр — какой открыть?»: CHOICE_TTL_S секунд работают
команды «открой второй», «второй», «номер три» (динамические, только пока висит выбор), а голос сразу слушает
ответ без «Джарвис» (окно продолжения). Ничего — «Не нашёл …». Горячая клавиша (по умолчанию Ctrl+Alt+F)
открывает быстрый поиск в оверлее (plugin.file_search.box {open}). Jarvis AI получает инструменты find_files
и open_file (ToolRegistry.register_provider).
"""

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from jarvis import paths
from jarvis.ai.format import iso_local, parse_when
from jarvis.ai.tools import Tool, ToolEnv, ToolOutput, untrusted
from jarvis.platform.base import FileQuery, IndexedFile, PlatformError
from jarvis.plugins.base import ConfigField, Plugin, PluginAction, error_text, unique
from jarvis.plugins.file_index import (
    DATE_LABELS,
    KIND_BY_KEY,
    KINDS,
    KNOWN_FOLDERS,
    STOP_WORDS,
    Hit,
    OwnIndex,
    SearchQuery,
    clear_winner,
    date_range,
    known_folder_key,
    parse_query,
    path_key,
    path_parts,
    rank_files,
    scan_folders,
    search_stem,
    spoken_name,
    tokens,
)
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

TITLE = "Поиск по ПК"
ICON = "search"  # значок тостов и истории
CARD_LIMIT = 8  # строк с номерами в карточке оверлея
CHOICE_TTL_S = 30.0  # сколько ждём «открой второй» после карточки
FOLLOW_UP_S = 8.0  # окно ответа без «Джарвис» — не меньше этого
DEFAULT_LIMIT = 20
AI_LIMIT = 10
AI_PATHS_TTL_S = 900.0  # пути из find_files ИИ открывает без подтверждения столько секунд
INDEX_FETCH_MIN = 60  # строк у индекса Windows берём с запасом: порядок по смыслу наводим сами
INDEX_RECHECK_S = 600.0  # индекс Windows не отвечал — снова пробуем через столько
OWN_REFRESH_S = 900.0  # свой индекс обновляется в фоне так часто
OWN_WAIT_S = 20.0  # поиск ждёт первого обхода папок не дольше
CACHE_FILE = "file_search_index.json"

# программы и сценарии: по голосу и по просьбе ИИ запускаются только с разрешения человека
RISKY_EXTENSIONS = frozenset(
    {
        *("exe", "msi", "bat", "cmd", "com", "ps1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "scr"),
        *("lnk", "url", "reg", "hta", "cpl", "msc", "jar", "appref-ms", "pif", "inf"),
    }
)


@dataclass
class SearchOutcome:
    hits: list[Hit]
    mode: str  # index | own
    clear: bool = False  # лучшее явно лучше остальных — голосом открываем сразу
    content_skipped: bool = False  # слова текста искались только в именах (свой индекс текст не читает)


@dataclass
class Choice:
    """Карточка с номерами после голосового поиска: ждём «открой второй»."""

    label: str
    items: list[Hit]
    expires: float  # по self.clock()


# --- голосовые фразы ---------------------------------------------------------------------------------------

_FIND_VERBS = ("найди", "поищи", "где", "покажи")
# (что ищем, как говорят) — «что» уходит в разбор вместе с хвостом фразы
_KIND_NOUNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("файл", ("файл", "файлы", "файлик")),
    ("документ", ("документ", "документы")),
    ("таблица", ("таблицу", "таблица", "таблицы")),
    ("презентация", ("презентацию", "презентация", "презентации")),
    ("pdf", ("pdf", "пдф")),
    ("архив", ("архив", "архивы")),
    ("папка", ("папку", "папка", "папки")),
)
_DOC_NOUNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("договор", ("договор", "договоры", "договора")),
    ("отчет", ("отчет", "отчеты", "отчета")),
    ("резюме", ("резюме",)),
    ("счет", ("счет", "счета")),
    ("акт", ("акт", "акты")),
    ("смета", ("смету", "смета", "сметы")),
    ("справка", ("справку", "справка", "справки")),
    ("скан", ("скан", "сканы", "скан копию")),
    ("инструкция", ("инструкцию", "инструкция")),
    ("заявление", ("заявление",)),
)
_MEDIA_NOUNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("фото", ("фото", "фотки", "фотографии", "картинки", "скриншоты", "скриншот")),
    ("видео", ("видео", "ролики", "видосы")),
    ("музыка", ("музыку", "песни", "песню", "треки")),
)
_MEDIA_VERBS = ("найди", "покажи", "открой", "где")
_LOCATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("на компьютере", ("на компьютере", "на пк", "у меня на компьютере", "на диске")),
    ("в загрузках", ("в загрузках", "в папке загрузки", "среди скачанных")),
    ("в документах", ("в документах", "в моих документах")),
    ("на рабочем столе", ("на рабочем столе",)),
    ("в изображениях", ("в изображениях", "в картинках")),
)
_RECENT_DOWNLOAD = (
    "открой последний скачанный файл",
    "открой последний загруженный файл",
    "открой последнюю загрузку",
    "открой последнее скачанное",
    "открой последний скачанный",
    "открой то что я скачал",
    "открой то что я скачала",
    "покажи последний скачанный файл",
    "последний скачанный файл",
    "что я скачал последним",
    "что я скачала последним",
)
_ORDINALS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("первый", "первую", "первое", "первая"), "один", "1"),
    (("второй", "вторую", "второе", "вторая"), "два", "2"),
    (("третий", "третью", "третье", "третья"), "три", "3"),
    (("четвертый", "четвертую", "четвертое", "четвертая"), "четыре", "4"),
    (("пятый", "пятую", "пятое", "пятая"), "пять", "5"),
    (("шестой", "шестую", "шестое", "шестая"), "шесть", "6"),
    (("седьмой", "седьмую", "седьмое", "седьмая"), "семь", "7"),
    (("восьмой", "восьмую", "восьмое", "восьмая"), "восемь", "8"),
)


def pick_phrases(index: int, last: bool = False) -> list[str]:
    """Фразы выбора строки карточки: «второй», «открой вторую», «номер два»."""
    forms, cardinal, digit = _ORDINALS[index]
    out: list[str] = []
    for form in forms:
        out += [form, f"открой {form}", f"{form} файл", f"открой {form} файл", f"{form} вариант"]
    out += [f"номер {cardinal}", f"открой номер {cardinal}", f"номер {digit}", f"открой номер {digit}"]
    if last:
        out += ["последний", "открой последний", "последнюю", "открой последнюю", "последнее"]
    return out


def reveal_phrases(index: int) -> list[str]:
    forms = _ORDINALS[index][0]
    return [p for f in forms[:2] for p in (f"покажи {f} в папке", f"покажи в папке {f}")]


# --- плагин -----------------------------------------------------------------------------------------------


def _str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return "" if value is None else " ".join(str(value).split())


class FileSearch(Plugin):
    id = "file_search"
    name = "Поиск по ПК"
    description = (
        "Находит файлы и папки по имени, типу, дате и тексту — голосом, в быстром поиске и для Jarvis AI."
    )
    icon = "file-search"
    color = ("#93C5FD", "#2563EB")
    fields = (
        ConfigField(
            "use_index",
            "bool",
            "Искать через индекс Windows",
            default=True,
            hint="Быстро и по тексту внутри файлов. Если индекс выключен — Jarvis ведёт свой индекс папок",
        ),
        ConfigField(
            "include",
            "list",
            "Дополнительные папки",
            default=[],
            hint="Свой индекс просматривает их вместе с «Загрузками», «Документами», «Рабочим столом», "
            "«Изображениями», «Музыкой» и «Видео»",
            fields=(ConfigField("path", "text", "Папка", placeholder="D:\\Проекты"),),
        ),
        ConfigField(
            "exclude",
            "list",
            "Не искать в папках",
            default=[],
            fields=(ConfigField("path", "text", "Папка", placeholder="D:\\Архив"),),
        ),
        ConfigField(
            "max_results",
            "number",
            "Сколько результатов показывать",
            default=DEFAULT_LIMIT,
            min=5,
            max=50,
            step=5,
            integer=True,
        ),
        ConfigField(
            "hotkey", "hotkey", "Быстрый поиск", default="Ctrl+Alt+F", hint="Строка поиска поверх всех окон"
        ),
    )
    actions = (PluginAction("reindex", "Обновить индекс"),)
    examples = (
        "найди договор с Ивановым",
        "покажи фото за вчера",
        "найди pdf в загрузках",
        "открой последний скачанный файл",
    )

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        self.clock: Callable[[], float] = time.monotonic  # тесты подставляют свои часы
        self._known: list[tuple[str, str]] = []  # (путь, ключ) известных папок
        self._index_ok: bool | None = None  # индекс Windows: None — не проверяли
        self._index_error = ""
        self._index_checked = 0.0
        self._own = OwnIndex()
        self._scan_task: asyncio.Task[Any] | None = None
        self._scan_stop: threading.Event | None = None
        self._scan_for: tuple[list[str], list[str]] = ([], [])  # папки и исключения текущего обхода
        self._wake: asyncio.Event | None = None
        self._choice: Choice | None = None
        self._choice_timer: asyncio.TimerHandle | None = None
        self._last: list[Hit] = []
        self._ai_paths: dict[str, float] = {}
        self._box = False
        self._static: list[DynamicCommand] | None = None
        self._unregister_tools: Callable[[], None] | None = None

    # --- жизненный цикл ----------------------------------------------------------------------------------
    async def start(self) -> None:
        self._known = []
        for key in KNOWN_FOLDERS:
            try:
                path = await self.ctx.run_platform(self.ctx.platform.known_folder, key)
            except Exception as e:
                log.info("поиск по ПК: папка %s не найдена (%s)", key, error_text(e))
                continue
            if path:
                self._known.append((str(path), key))
        registry = getattr(self.ctx.ai, "tools", None)
        register = getattr(registry, "register_provider", None)
        if callable(register):
            self._unregister_tools = register(self.id, self.ai_tools)
        self._wake = asyncio.Event()
        self.spawn(self._loop(), "index")

    async def stop(self) -> None:
        if self._unregister_tools is not None:
            self._unregister_tools()
            self._unregister_tools = None
        if self._scan_stop is not None:
            self._scan_stop.set()
        self._drop_choice(notify=False)
        self._index_ok, self._index_error, self._index_checked = None, "", 0.0
        self._own = OwnIndex()
        self._last = []
        self._ai_paths = {}
        self._box = False
        self._wake = None

    async def on_config(self, config: dict[str, Any], old: dict[str, Any]) -> None:
        if config.get("include") != old.get("include") or config.get("exclude") != old.get("exclude"):
            self._own = OwnIndex()
        if config.get("use_index") != old.get("use_index"):
            self._index_ok, self._index_checked = None, 0.0
        self.wake()
        self.changed()

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        while self._wake is not None:
            self._wake.clear()
            try:
                await self._refresh()
            except Exception:
                log.exception("поиск по ПК: обновление индекса упало")
            delay = OWN_REFRESH_S if self._uses_own() else INDEX_RECHECK_S
            if self._wake is None:
                return
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await self._wake.wait()

    async def _refresh(self) -> None:
        if self._use_index() and (
            self._index_ok is not True or self.clock() - self._index_checked >= INDEX_RECHECK_S
        ):
            await self._probe_index()
        if self._uses_own():
            if not self._own.items:
                await self._load_cache()
            stale = time.time() - self._own.built >= OWN_REFRESH_S
            if not self._own.items or stale or self._own.roots != self._roots():
                await self.reindex()

    # --- индекс Windows ----------------------------------------------------------------------------------
    def _use_index(self) -> bool:
        return bool(self.config.get("use_index", True))

    def _uses_own(self) -> bool:
        return not self._use_index() or self._index_ok is False

    def _set_index(self, ok: bool, error: str = "") -> None:
        self._index_checked = self.clock()
        if ok != self._index_ok or error != self._index_error:
            self._index_ok, self._index_error = ok, error
            self.changed()
            if not ok:
                self.wake()  # пора строить свой индекс

    async def _probe_index(self) -> bool:
        try:
            await self.ctx.run_platform(self.ctx.platform.search_index, FileQuery(limit=1))
        except PlatformError as e:
            log.info("поиск по ПК: индекс Windows недоступен — %s", e)
            self._set_index(False, str(e))
            return False
        self._set_index(True)
        return True

    async def _index_search(self, q: SearchQuery, limit: int) -> list[IndexedFile]:
        scopes = [path for path, key in self._known if key == q.folder] if q.folder else []
        if q.folder and not scopes:
            return []
        base = FileQuery(
            words=list(q.stems),
            extensions=q.extensions(),
            folders_only=q.folders_only,
            files_only=q.files_only,
            scopes=scopes,
            path_words=list(q.path_words),
            content=list(q.content),
            date_from=q.date_from,
            date_to=q.date_to,
            limit=max(INDEX_FETCH_MIN, limit * 4),
        )
        found = await self.ctx.run_platform(self.ctx.platform.search_index, base)
        if len(q.stems) > 1 and len(found) < limit:
            more = await self.ctx.run_platform(self.ctx.platform.search_index, replace(base, any_word=True))
            found = [*found, *more]
        return found

    # --- свой индекс -------------------------------------------------------------------------------------
    def _folders(self, key: str) -> list[str]:
        out: list[str] = []
        for item in self.config.get(key) or []:
            path = str(item.get("path") or "").strip() if isinstance(item, dict) else ""
            if path:
                out.append(os.path.expandvars(os.path.expanduser(path)))
        return out

    def _roots(self) -> list[str]:
        return unique([*(path for path, _ in self._known), *self._folders("include")])

    def _cache_path(self) -> str:
        return str(paths.cache_dir() / CACHE_FILE)

    async def _load_cache(self) -> None:
        def read() -> OwnIndex | None:
            try:
                with open(self._cache_path(), encoding="utf-8") as f:
                    return OwnIndex.from_json(json.load(f))
            except (OSError, ValueError):
                return None

        cached = await self.ctx.run_io(read)
        if cached and cached.roots == self._roots() and cached.exclude == self._folders("exclude"):
            self._own = cached
            self.changed()

    def _save_cache(self, index: OwnIndex) -> None:
        target = self._cache_path()
        tmp = target + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index.to_json(), f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, target)
        except OSError as e:
            log.info("поиск по ПК: кэш индекса не сохранён (%s)", e)

    @property
    def scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    async def reindex(self) -> OwnIndex:
        """Обойти папки заново. Обход один: второй вызов ждёт идущий, а если папки в настройках сменились —
        старый обход останавливается и начинается новый."""
        want = (self._roots(), self._folders("exclude"))
        task = self._scan_task
        if task is None or task.done() or self._scan_for != want:
            if task is not None and not task.done() and self._scan_stop is not None:
                self._scan_stop.set()
            self._scan_for = want
            stop = self._scan_stop = threading.Event()
            task = self._scan_task = asyncio.ensure_future(self._scan(*want, stop))
            # ошибку обхода забирает ожидающий, а если ждать перестали — она не всплывёт предупреждением
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self.changed()
        return await asyncio.shield(task)

    async def _scan(self, roots: list[str], exclude: list[str], stop: threading.Event) -> OwnIndex:
        try:
            items, truncated = await self.ctx.run_io(scan_folders, roots, exclude=exclude, stop=stop)
        finally:
            self.changed()
        if stop.is_set():  # остановлен: плагин выключили или папки в настройках сменились
            return self._own
        index = OwnIndex.make(items, roots, exclude, time.time(), truncated)
        self._own = index
        self.changed()
        await self.ctx.run_io(self._save_cache, index)
        return index

    async def _own_ready(self) -> OwnIndex:
        if not self._own.items:
            await self._load_cache()
        if not self._own.items or self._own.roots != self._roots():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(OWN_WAIT_S):
                    await self.reindex()
        return self._own

    # --- поиск ---------------------------------------------------------------------------------------------
    def _limit(self, requested: Any = None) -> int:
        try:
            value = int(requested) if requested is not None else int(self.config.get("max_results") or 0)
        except (TypeError, ValueError):
            value = 0
        return max(1, min(50, value or DEFAULT_LIMIT))

    async def search(self, q: SearchQuery, limit: int | None = None) -> SearchOutcome:
        """Найти по запросу: индекс Windows, а если его нет — свой индекс."""
        size = self._limit(limit) if q.limit != 1 else 1
        exclude = self._folders("exclude")
        if self._use_index() and self._index_ok is not False:
            try:
                found = await self._index_search(q, size)
            except PlatformError as e:
                log.info("поиск по ПК: индекс Windows не ответил — свой индекс (%s)", e)
                self._set_index(False, str(e))
            else:
                self._set_index(True)
                hits = rank_files(found, q, known=self._known, exclude=exclude)[:size]
                return SearchOutcome(hits, "index", clear_winner(hits, q))
        own = await self._own_ready()
        effective = q
        if q.content:  # текст файлов свой индекс не читает — слова ищем в именах
            effective = replace(q, stems=unique([*q.stems, *q.content]), content=[])
        hits = await self.ctx.run_io(
            lambda: rank_files(own.candidates(effective), effective, known=self._known, exclude=exclude)
        )
        hits = hits[:size]
        return SearchOutcome(hits, "own", clear_winner(hits, effective), content_skipped=bool(q.content))

    def query_from_args(self, args: dict[str, Any]) -> SearchQuery:
        """Запрос из аргументов интерфейса и ИИ: текст + уточнения (kinds, date, folder, sort, content …)."""
        q = parse_query(_str(args, "text") or _str(args, "query"))
        kinds = args.get("kinds") if isinstance(args.get("kinds"), list) else [args.get("kind")]
        chosen = [str(k) for k in kinds if isinstance(k, str) and k in KIND_BY_KEY]
        if chosen:
            q.kinds = unique(chosen)
            q.files_only = q.files_only and not q.folders_only
        date = _str(args, "date")
        if date:
            start, end = date_range(date)
            if start is None:
                raise UserFacingError(f"Неизвестный период «{date}»")
            q.date_from, q.date_to, q.date_label = start, end, DATE_LABELS[date]
        folder = _str(args, "folder")
        if folder:
            key = known_folder_key(folder)
            if not key:
                raise UserFacingError(f"Не знаю папку «{folder}»")
            q.folder = key
        for name in ("modified_after", "modified_before"):
            if args.get(name):
                try:
                    moment, _ = parse_when(args[name], date_only_hour=0)
                except ValueError as e:
                    raise UserFacingError(str(e)) from None
                if name == "modified_after":
                    q.date_from = moment
                else:
                    q.date_to = moment
        content = _str(args, "content")
        if content:
            q.content = unique(
                [*q.content, *(search_stem(w) for w in tokens(content) if w not in STOP_WORDS)]
            )
        sort = _str(args, "sort")
        if sort in ("recent", "relevance"):
            q.sort = sort
        return q

    def _remember(self, hits: list[Hit]) -> None:
        self._last = list(hits)

    def _hit(self, args: dict[str, Any]) -> tuple[str, int]:
        """Путь из аргументов ({path} или {index} — номер в последних результатах, с 1)."""
        path = str(args.get("path") or "").strip()
        index = -1
        if not path and args.get("index") is not None:
            try:
                index = int(args["index"]) - 1
            except (TypeError, ValueError):
                raise UserFacingError("Неверный номер") from None
            pool = self._choice.items if self._choice is not None else self._last
            if not 0 <= index < len(pool):
                raise UserFacingError("Нет результата с таким номером")
            path = pool[index].path
        if not path:
            raise UserFacingError("Не указан файл")
        return path, index

    # --- голос ---------------------------------------------------------------------------------------------
    def _say(self, text: str) -> str:
        address = self.ctx.settings.value.profile.address or ""
        out = text.format(address=address)
        if not address:
            for old, new in ((", .", "."), (", —", " —"), (", ?", "?"), (" ,", ",")):
                out = out.replace(old, new)
        return out

    async def voice_search(self, text: str, slots: dict[str, Any], label: str = "") -> ExecResult:
        """Голосовой поиск: одно явное — открыть, несколько — карточка и «какой открыть?», ничего —
        «не нашёл»."""
        q = parse_query(text)
        label = " ".join(label.split()) or q.text or text
        if q.empty:
            return ExecResult(
                False,
                TITLE,
                "Скажите, что найти: «найди договор с Ивановым»",
                icon=ICON,
                say=self._say("Что найти, {address}?"),
            )
        outcome = await self.search(q)
        hits = outcome.hits
        self._remember(hits)
        if not hits:
            return ExecResult(
                False,
                f"Поиск: {label}",
                "Ничего не нашлось",
                icon=ICON,
                say=self._say(f"Не нашёл «{label}», {{address}}."),
            )
        if outcome.clear:
            hit = hits[0]
            if not await self.open_hit(hit, 0, str(slots.get("_source") or "")):
                return self._cancelled(hit)
            return self._opened_result(hit)
        items = hits[:CARD_LIMIT]
        self._offer(label, items, len(hits), str(slots.get("_source") or ""))
        return ExecResult(
            True,
            f"Поиск: {label}",
            f"Найдено: {len(hits)}",
            icon=ICON,
            say=self._say(f"Нашёл {len(items)}, {{address}} — какой открыть?"),
            data={"silent": True},
        )

    def _cancelled(self, hit: Hit) -> ExecResult:
        return ExecResult(
            False,
            f"Открыть «{hit.name}»",
            "Отменено",
            icon=ICON,
            say="Хорошо, не запускаю.",
            data={"cancelled": True},
        )

    def _opened_result(self, hit: Hit) -> ExecResult:
        what = f"папку «{spoken_name(hit.name, True)}»" if hit.is_dir else f"«{spoken_name(hit.name)}»"
        return ExecResult(True, f"Открыть «{hit.name}»", hit.where, icon=ICON, say=f"Открываю {what}.")

    async def open_hit(self, hit: Hit, index: int = -1, source: str = "") -> bool:
        """Открыть найденное. Программу или сценарий по голосу — только с разрешения (могли ослышаться)."""
        risky = hit.name.rpartition(".")[2].casefold() in RISKY_EXTENSIONS
        if risky and source in ("voice", "remote", "telegram"):
            allowed = await self.ctx.confirmations.request(
                f"Запустить «{hit.name}»?",
                f"Это программа или сценарий: {hit.where}",
                kind="plugin",
                source=source,
                spoken=f"Запустить {spoken_name(hit.name)}?",
            )
            if not allowed:
                return False
        await self.ctx.run_platform(self.ctx.platform.open_path, hit.path)
        self.publish("opened", {"path": hit.path, "index": index})
        return True

    def _offer(self, label: str, items: list[Hit], total: int, source: str) -> None:
        """Карточка с номерами и ~30 с на «открой второй»."""
        self._drop_choice(notify=False)
        choice = self._choice = Choice(label, items, self.clock() + CHOICE_TTL_S)
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            self._choice_timer = loop.call_later(CHOICE_TTL_S, self._expire_choice, choice)
        try:  # «второй» может прозвучать сразу — команды выбора нужны без задержки менеджера
            self.ctx.commands.refresh_dynamic()
        except Exception:
            log.exception("поиск по ПК: команды выбора не обновились сразу")
            self.commands_changed()
        self.publish(
            "results",
            {"query": label, "items": [h.to_json() for h in items], "total": total, "ttl": CHOICE_TTL_S},
        )
        if source == "voice":
            self.spawn(self._listen_for_answer(), "answer")

    async def _listen_for_answer(self) -> None:
        """Дождаться, когда Jarvis начнёт «какой открыть?», и открыть окно ответа без «Джарвис»."""
        seconds = float(self.ctx.settings.value.voice.follow_up_s or 0)
        if seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while loop.time() < deadline:
            with contextlib.suppress(Exception):
                if self.ctx.tts.speaking:
                    break
            await asyncio.sleep(0.05)
        follow_up = getattr(self.ctx.voice, "follow_up", None)
        if callable(follow_up):
            follow_up(max(FOLLOW_UP_S, seconds))

    def _expire_choice(self, choice: Choice) -> None:
        if self._choice is choice:
            self._drop_choice()

    def _drop_choice(self, notify: bool = True) -> None:
        if self._choice_timer is not None:
            self._choice_timer.cancel()
            self._choice_timer = None
        if self._choice is not None:
            self._choice = None
            if notify:
                self.commands_changed()

    async def _pick(self, index: int, slots: dict[str, Any], reveal: bool = False) -> ExecResult:
        choice = self._choice
        if choice is None or self.clock() > choice.expires or index >= len(choice.items):
            return ExecResult(False, TITLE, "Список уже закрыт — повторите поиск", icon=ICON)
        hit = choice.items[index]
        if reveal:
            await self.ctx.run_platform(self.ctx.platform.reveal_path, hit.path)
            self.publish("opened", {"path": hit.path, "index": index, "reveal": True})
            return ExecResult(True, f"Показать «{hit.name}» в папке", hit.where, icon=ICON, say="Показываю.")
        if not await self.open_hit(hit, index, str(slots.get("_source") or "")):
            return self._cancelled(hit)
        return self._opened_result(hit)

    def commands(self) -> list[DynamicCommand]:
        if self._static is None:
            self._static = self._static_commands()
        out = list(self._static)
        choice = self._choice
        if choice is not None:
            count = min(len(choice.items), len(_ORDINALS))
            for i in range(count):
                out.append(
                    self.command(
                        f"pick:{i}",
                        f"Открыть {i + 1}-й из найденного",
                        pick_phrases(i, last=count > 1 and i == count - 1),
                        lambda slots, n=i: self._pick(n, slots),
                        icon=ICON,
                    )
                )
                out.append(
                    self.command(
                        f"reveal:{i}",
                        f"Показать {i + 1}-й в папке",
                        reveal_phrases(i),
                        lambda slots, n=i: self._pick(n, slots, reveal=True),
                        icon=ICON,
                    )
                )
        return out

    def _static_commands(self) -> list[DynamicCommand]:
        out: list[DynamicCommand] = []

        def search(key: str, title: str, phrases: list[str], prefix: str) -> None:
            async def run(slots: dict[str, Any]) -> ExecResult:
                tail = " ".join(str(slots.get("query") or slots.get("text") or "").split())
                text = f"{prefix} {tail}".strip()
                # «найди файл zxqw» → «Не нашёл «zxqw»», но «файлы со словом отчёт» — целиком
                plain = prefix == "файлы" and tail and tail.split()[0].casefold() not in STOP_WORDS
                return await self.voice_search(text, slots, tail if plain else text)

            out.append(self.command(key, title, phrases, run, icon=ICON))

        for lemma, forms in (*_KIND_NOUNS, *_DOC_NOUNS):
            phrases = [f"{v} {f} {{query}}" for v in (*_FIND_VERBS, "открой") for f in forms]
            phrases += [f"{v} {f}" for v in _FIND_VERBS for f in forms]
            # «файлы со словом отчёт» звучит естественнее, чем «файл со словом отчёт»
            search(f"find:{lemma}", f"Найти: {lemma}", phrases, "файлы" if lemma == "файл" else lemma)
        for lemma, forms in _MEDIA_NOUNS:
            dated = [f"{v} {f} за {{query}}" for v in _MEDIA_VERBS for f in forms]
            search(f"media_date:{lemma}", f"Найти {lemma} за период", dated, f"{lemma} за")
            placed = [
                f"{v} {f} {p} {{query}}" for v in _MEDIA_VERBS for f in forms for p in ("в", "из", "на")
            ]
            placed += [f"{v} мои {f} {{query}}" for v in _MEDIA_VERBS for f in forms]
            placed += [
                f"{v} {w} {f}" for v in ("найди", "покажи") for f in forms for w in ("мои", "последние")
            ]
            placed += [f"{v} {f} на компьютере" for v in ("найди", "покажи") for f in forms]
            search(f"media:{lemma}", f"Найти {lemma} на компьютере", placed, f"{lemma} в")
        for i, (where, forms) in enumerate(_LOCATIONS):
            phrases = [f"{v} {f} {{query}}" for v in (*_FIND_VERBS, "открой") for f in forms]
            search(f"place:{i}", f"Найти {where}", phrases, where)
        search("in_folder", "Найти в папке", [f"{v} в папке {{query}}" for v in _FIND_VERBS], "в папке")
        search(
            "where",
            "Где лежит файл",
            ["где лежит {query}", "где лежат {query}", "где находится файл {query}"],
            "",
        )
        recent = (("последний", ("последний", "последнюю", "последнее")), ("последние", ("последние",)))
        for word, forms in recent:
            phrases = [f"{v} {f} {{query}}" for v in ("открой", "покажи", "найди") for f in forms]
            search(f"recent:{word}", "Найти последний файл", phrases, word)

        async def last_download(slots: dict[str, Any]) -> ExecResult:
            return await self.voice_search("последний скачанный файл", slots)

        out.append(
            self.command(
                "last_download",
                "Открыть последний скачанный файл",
                _RECENT_DOWNLOAD,
                last_download,
                icon=ICON,
            )
        )
        return out

    # --- действия ------------------------------------------------------------------------------------------
    async def act_search(self, args: dict[str, Any]) -> dict[str, Any]:
        """{text?, kinds?, date?, folder?, sort?, content?, limit?} → {query, items, total, mode, parsed}."""
        q = self.query_from_args(args)
        if q.empty:
            return {"query": q.text, "items": [], "total": 0, "mode": self._mode(), "parsed": q.to_json()}
        outcome = await self.search(q, args.get("limit"))
        self._remember(outcome.hits)
        return {
            "query": q.text,
            "items": [h.to_json() for h in outcome.hits],
            "total": len(outcome.hits),
            "mode": outcome.mode,
            "content_skipped": outcome.content_skipped,
            "parsed": q.to_json(),
        }

    async def act_open(self, args: dict[str, Any]) -> dict[str, Any]:
        path, index = self._hit(args)
        await self.ctx.run_platform(self.ctx.platform.open_path, path)
        self.publish("opened", {"path": path, "index": index})
        return {"path": path}

    async def act_reveal(self, args: dict[str, Any]) -> dict[str, Any]:
        path, index = self._hit(args)
        await self.ctx.run_platform(self.ctx.platform.reveal_path, path)
        self.publish("opened", {"path": path, "index": index, "reveal": True})
        return {"path": path}

    async def act_copy_path(self, args: dict[str, Any]) -> dict[str, Any]:
        path, _ = self._hit(args)
        await self.ctx.run_platform(self.ctx.platform.clipboard_set, path)
        return {"path": path, "message": "Путь скопирован"}

    async def act_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        """Горячая клавиша: открыть или закрыть быстрый поиск в оверлее."""
        return await self.act_box({"open": not self._box})

    async def act_box(self, args: dict[str, Any]) -> dict[str, Any]:
        self._box = bool(args.get("open"))
        self.publish("box", {"open": self._box})
        return {"open": self._box}

    async def act_reindex(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._use_index():
            await self._probe_index()
        if self._uses_own():
            index = await self.reindex()
            return {"mode": "own", "files": len(index.items), "message": f"В индексе: {len(index.items)}"}
        return {"mode": "index", "message": "Индекс Windows на месте"}

    def _mode(self) -> str:
        return "own" if self._uses_own() else "index"

    def status(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self._mode(),
            "index_available": self._index_ok,
            "files": len(self._own.items),
            "scanning": self.scanning,
            "indexed_at": self._own.built,
            "truncated": self._own.truncated,
        }
        if self._index_error and self._use_index():
            data["index_error"] = self._index_error
            data["hint"] = "Индекс Windows недоступен — ищу по своему индексу папок"
        return data

    # --- Jarvis AI -----------------------------------------------------------------------------------------
    def ai_tools(self) -> list[Tool]:
        if not self.running:
            return []
        kinds = [k.key for k in KINDS]
        return [
            Tool(
                "find_files",
                "Найти файлы и папки на компьютере пользователя: по словам из имени, типу, папке, дате "
                "изменения и тексту внутри. Возвращает пути; открыть найденное — open_file.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Что искать, по-русски: «договор с Ивановым», «презентация про "
                            "бюджет», «pdf в загрузках за неделю»",
                        },
                        "kind": {"type": "string", "enum": kinds, "description": "Тип файлов"},
                        "folder": {"type": "string", "enum": [*KNOWN_FOLDERS], "description": "Где искать"},
                        "modified_after": {"type": "string", "description": "Изменён не раньше, ISO 8601"},
                        "modified_before": {"type": "string", "description": "Изменён раньше, ISO 8601"},
                        "content": {"type": "string", "description": "Слова, которые есть внутри файла"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Сколько"},
                    },
                    "required": [],
                },
                self._tool_find,
                lambda a: f"Поиск файлов: {_str(a, 'query') or _str(a, 'content') or 'на компьютере'}",
                status="searching",
                status_text="Ищу файлы…",
                timeout=60.0,
            ),
            Tool(
                "open_file",
                "Открыть файл или папку (путь из find_files) программой по умолчанию; reveal — показать в "
                "Проводнике.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Полный путь"},
                        "reveal": {"type": "boolean", "description": "Показать в папке вместо открытия"},
                    },
                    "required": ["path"],
                },
                self._tool_open,
                lambda a: f"Открыть «{_str(a, 'path').replace(chr(92), '/').rsplit('/', 1)[-1]}»",
                action=True,
                timeout=90.0,
            ),
        ]

    async def _tool_find(self, env: ToolEnv, args: dict[str, Any]) -> ToolOutput:
        q = self.query_from_args({**args, "kinds": None})
        label = q.text or _str(args, "content") or "файлы"
        if q.empty:
            return ToolOutput.fail("укажи, что искать: query, kind, folder, даты или content")
        try:
            limit = max(1, min(20, int(args.get("limit") or AI_LIMIT)))
        except (TypeError, ValueError):
            limit = AI_LIMIT
        outcome = await self.search(q, limit)
        hits = outcome.hits
        self._remember(hits)
        now = self.clock()
        self._ai_paths = {k: v for k, v in self._ai_paths.items() if v > now}
        for hit in hits:
            self._ai_paths[path_key(hit.path)] = now + AI_PATHS_TTL_S
        items = [
            {
                "n": i + 1,
                "name": h.name,
                "path": h.path,
                "kind": h.kind,
                "size": h.size,
                "modified": iso_local(h.modified) if h.modified else None,
                "where": h.where,
            }
            for i, h in enumerate(hits)
        ]
        note = " (текст файлов недоступен — искал по именам)" if outcome.content_skipped else ""
        title = f"Поиск файлов: {label}"
        if not items:
            return ToolOutput(f"Ничего не найдено{note}.", title=title, detail="Не найдено")
        return ToolOutput(
            untrusted("Найденные файлы", items) + note, title=title, detail=f"Найдено: {len(items)}"
        )

    async def _tool_open(self, env: ToolEnv, args: dict[str, Any]) -> ToolOutput:
        path = str(args.get("path") or "").strip().strip('"')
        if not path:
            return ToolOutput.fail("не указан путь")
        name = path_parts(path)[-1] if path_parts(path) else path
        reveal = bool(args.get("reveal"))
        title = f"Показать «{name}» в папке" if reveal else f"Открыть «{name}»"
        known = self._ai_paths.get(path_key(path), 0.0) > self.clock()
        risky = not reveal and name.rpartition(".")[2].casefold() in RISKY_EXTENSIONS
        if not known or risky:
            allowed = await self.ctx.confirmations.request(
                f"Открыть «{name}»?",
                f"Jarvis AI просит {'показать' if reveal else 'открыть'}: {path}",
                kind="plugin",
                source=env.source,
            )
            if not allowed:
                return ToolOutput("Пользователь не разрешил.", ok=False, title=title, detail="Отклонено")
        action = self.ctx.platform.reveal_path if reveal else self.ctx.platform.open_path
        await self.ctx.run_platform(action, path)
        self.publish("opened", {"path": path, "index": -1, "reveal": reveal})
        done = "показан в папке" if reveal else "открыт"
        return ToolOutput(f"Готово: {done} {path}", title=title, detail="Выполнено")
