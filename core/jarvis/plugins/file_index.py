"""Поиск файлов без ОС: разбор фразы, типы файлов, ранжирование и свой индекс папок (плагин «Поиск по ПК»).

Фраза → запрос (parse_query): «найди договор с Ивановым», «где презентация про бюджет», «покажи фото
за вчера», «найди pdf в загрузках», «открой папку проекты», «найди файлы со словом отчёт», «открой последний
скачанный файл» → слова имени (основы: «договора» → «договор», «презентацию» → «презентац»), типы из белого
списка (KINDS), период, известная папка или часть пути, слова текста, порядок и сколько нужно.

Найденное (индексом Windows или своим индексом) отбирается и ранжируется (rank_files): имя целиком и с начала,
доля слов запроса в имени и пути, свежесть. Свой индекс (scan_folders) — обход папок вширь: до SCAN_DEPTH
уровней, без служебных (node_modules, .git, AppData), скрытых и системных папок, не больше SCAN_MAX_ENTRIES
записей и SCAN_BUDGET_S секунд.
"""

import os
import re
import stat as statmod
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jarvis.commands import numbers
from jarvis.platform.base import IndexedFile
from jarvis.plugins.base import unique

SCAN_DEPTH = 6
SCAN_MAX_ENTRIES = 100_000
SCAN_BUDGET_S = 45.0
CACHE_VERSION = 1

KNOWN_FOLDERS = ("downloads", "documents", "desktop", "pictures", "music", "videos")
FOLDER_TITLES = {
    "downloads": "Загрузки",
    "documents": "Документы",
    "desktop": "Рабочий стол",
    "pictures": "Изображения",
    "music": "Музыка",
    "videos": "Видео",
}
# пропускаем при обходе (и в ответах индекса): служебные папки программ, кэши, корзина
SKIP_DIRS = frozenset(
    {
        *("node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv", "appdata", "programdata"),
        *("$recycle.bin", "system volume information", ".idea", ".vscode", ".cache", ".gradle", ".m2"),
        *(".npm", ".nuget", "site-packages", "dist-packages", "bower_components", ".tox", ".mypy_cache"),
        *(".pytest_cache", ".ruff_cache", "windowsapps"),
    }
)
SKIP_FILES = frozenset({"desktop.ini", "thumbs.db", ".ds_store"})
PARTIAL_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download")
HIDDEN_ATTRS = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


# --- типы файлов ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    key: str
    title: str  # для чипов и подписей: «Документы»
    exts: frozenset[str]
    stems: frozenset[str]  # основы слов запроса (search_stem): «презентац» — «презентацию», «презентации»
    words: frozenset[str] = frozenset()  # слова целиком: «фото», «pdf»


def _set(text: str) -> frozenset[str]:
    return frozenset(text.split())


KINDS: tuple[Kind, ...] = (
    Kind(
        "document",
        "Документы",
        _set("doc docx docm dot dotx odt rtf txt md pages wps pdf"),
        _set("документ текстов вордовск"),
        _set("док доки доку ворд word doc docx txt rtf odt"),
    ),
    Kind(
        "spreadsheet",
        "Таблицы",
        _set("xls xlsx xlsm xlsb ods csv numbers"),
        _set("таблиц табличк эксел экзел"),
        _set("excel xls xlsx csv ods"),
    ),
    Kind(
        "presentation",
        "Презентации",
        _set("ppt pptx pptm pps ppsx odp key"),
        _set("презентац слайд пауэрпоинт поверпоинт паверпоинт"),
        _set("powerpoint ppt pptx odp през презу преза"),
    ),
    Kind(
        "image",
        "Фото",
        _set("jpg jpeg png gif bmp webp heic heif tif tiff svg raw cr2 nef arw dng"),
        _set("фотограф фотк фоток фоточк картинк картинок изображен скриншот скрин снимк снимок"),
        _set("фото photo photos image images picture pictures jpg jpeg png gif heic webp bmp"),
    ),
    Kind(
        "video",
        "Видео",
        _set("mp4 mkv avi mov wmv webm m4v flv mpg mpeg 3gp"),
        _set("видос ролик фильм клип видеоролик видеозапис"),
        _set("видео video videos movie mp4 mkv avi mov wmv"),
    ),
    Kind(
        "audio",
        "Музыка",
        _set("mp3 wav flac m4a ogg aac wma opus aiff mid midi"),
        _set("музык песн песен трек аудиозапис"),
        _set("аудио audio music song songs mp3 wav flac m4a ogg"),
    ),
    Kind(
        "archive",
        "Архивы",
        _set("zip rar 7z tar gz tgz bz2 xz cab"),
        _set("архив"),
        _set("archive zip rar 7z зип рар tar gz"),
    ),
    Kind("pdf", "PDF", _set("pdf"), _set("пдфк"), _set("pdf пдф")),
    Kind("folder", "Папки", frozenset(), _set("папк папок папочк каталог"), _set("folder folders")),
)
KIND_BY_KEY = {k.key: k for k in KINDS}
EXT_KIND: dict[str, str] = {}
for _kind in KINDS:
    for _ext in _kind.exts:
        EXT_KIND.setdefault(_ext, _kind.key)
EXT_KIND["pdf"] = "pdf"


def kind_of(name: str, is_dir: bool = False) -> str:
    """Тип файла для значка: document, spreadsheet, … folder; незнакомое — other."""
    if is_dir:
        return "folder"
    ext = name.rpartition(".")[2].casefold() if "." in name else ""
    return EXT_KIND.get(ext, "other")


# --- текст ------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"[0-9a-zа-я]+")
_CYRILLIC = re.compile(r"[а-я]")
# окончания существительных и прилагательных (без глагольных — «бюджет» не «бюдж»)
_ENDINGS = tuple(
    sorted(
        _set(
            "иями ями ами ого его ому ему ыми ими ией иям ием иях ая яя ое ее ые ие ой ей ий ый ую юю ом ем "
            "ам ям ах ях ов ев ым им ию ия ии ью а я о е ы и у ю ь й"
        ),
        key=len,
        reverse=True,
    )
)
STOP_WORDS = _set(
    "найди найти найдите поищи поискать поищем ищи искать ищу отыщи где покажи показать покажите открой "
    "открыть откройте запусти запустить дай дайте достань выведи нужен нужна нужно нужны мне мой моя мое "
    "мои моего моей моих мою моем свой свою свои своих своем своей у меня лежит лежат лежал лежала "
    "находится находятся был была были этот эта это эти тот та те то который которая которое которые "
    "которых называется называются названием названия название под с со в во на из для про о об обо по "
    "за и или а также еще файл файлы файла файлов файлик файлики файле файлу компьютере компе компьютер "
    "пк ноуте ноутбуке диске пожалуйста плиз какой какая какое какие какую любой любую все всех год года "
    "году что как чтобы the a an of in on"
)
_FILE_WORDS = frozenset({"файл", "файлы", "файла", "файлов", "файлик", "файлики", "файле", "файлу"})


def norm(text: str) -> str:
    """Для сравнения: без регистра, «ё» → «е»."""
    return text.casefold().replace("ё", "е")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(norm(text))


def search_stem(word: str) -> str:
    """Основа слова для поиска в именах: «договора» → «договор», «ивановым» → «иванов», «фото» → «фот».

    Только кириллица и только окончания имён (существительных и прилагательных); основа — от трёх букв."""
    w = norm(word)
    if len(w) < 4 or not _CYRILLIC.search(w) or not w.isalpha():
        return w
    for ending in _ENDINGS:
        if w.endswith(ending) and len(w) - len(ending) >= 3:
            return w[: -len(ending)]
    return w


def _matches(word: str, pattern: str) -> bool:
    return word.startswith(pattern[:-1]) if pattern.endswith("*") else word == pattern


def spoken_name(name: str, is_dir: bool = False) -> str:
    """Имя для голоса: без расширения, «_» и «.» — пробелы (точка между цифрами остаётся: «15.10»)."""
    base = name if is_dir or "." not in name else name.rpartition(".")[0]
    text = " ".join(re.sub(r"_+|(?<!\d)\.+|\.+(?!\d)", " ", base).split())
    return text if len(text) <= 60 else text[:59].rsplit(" ", 1)[0] + "…"


# --- пути -------------------------------------------------------------------------------------------------


def path_key(path: str) -> str:
    """Путь для сравнения: «/» вместо «\\», без регистра и «/» в конце."""
    return norm(path.replace("\\", "/")).rstrip("/")


def under(key: str, roots: Iterable[str]) -> bool:
    """Путь (path_key) лежит в одной из папок (path_key) или совпадает с ней."""
    return any(key == r or key.startswith(r + "/") for r in roots if r)


def path_parts(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", path) if p]


def parent_of(path: str) -> str:
    cut = max(path.rstrip("\\/").rfind("/"), path.rstrip("\\/").rfind("\\"))
    return path[:cut] if cut > 0 else ""


def junk_path(path: str) -> bool:
    """В пути есть служебная или скрытая папка (AppData, node_modules, .git, $Recycle.Bin)."""
    parts = path_parts(norm(path))[:-1]
    return any(p in SKIP_DIRS or (p[:1] in ".$" and len(p) > 1) for p in parts)


# --- запрос -----------------------------------------------------------------------------------------------


@dataclass
class SearchQuery:
    """Что ищем — из фразы человека, аргументов интерфейса или ИИ."""

    text: str = ""  # как сказано (для «Не нашёл «…»»)
    words: list[str] = field(default_factory=list)  # слова имени, как сказаны
    stems: list[str] = field(default_factory=list)  # их основы
    kinds: list[str] = field(default_factory=list)
    date_from: float | None = None
    date_to: float | None = None
    date_label: str = ""
    folder: str = ""  # известная папка: downloads, documents …
    path_words: list[str] = field(default_factory=list)  # основы из «в папке проекты»
    content: list[str] = field(default_factory=list)  # основы слов текста
    sort: str = ""  # recent | relevance; "" — по смыслу (есть слова — relevance)
    limit: int = 0  # 0 — по настройке; 1 — «последний»
    files_only: bool = False

    @property
    def folders_only(self) -> bool:
        return self.kinds == ["folder"]

    @property
    def empty(self) -> bool:
        return not (
            self.stems
            or self.kinds
            or self.date_from is not None
            or self.date_to is not None
            or self.folder
            or self.path_words
            or self.content
            or self.sort == "recent"
        )

    @property
    def order(self) -> str:
        return self.sort or ("relevance" if self.stems else "recent")

    def extensions(self) -> list[str]:
        out: set[str] = set()
        for key in self.kinds:
            kind = KIND_BY_KEY.get(key)
            if kind is not None:
                out |= kind.exts
        return sorted(out)

    def to_json(self) -> dict[str, Any]:
        return {
            "words": list(self.words),
            "kinds": list(self.kinds),
            "date_from": self.date_from,
            "date_to": self.date_to,
            "date_label": self.date_label,
            "folder": self.folder,
            "path_words": list(self.path_words),
            "content": list(self.content),
            "sort": self.order,
            "limit": self.limit,
        }


# (фраза, ключ периода); «*» в конце слова — любое окончание. Длинные проверяются первыми.
_DATE_PHRASES: tuple[tuple[str, str], ...] = (
    ("за сегодня", "today"),
    ("сегодняшн*", "today"),
    ("сегодня", "today"),
    ("за вчера", "yesterday"),
    ("вчерашн*", "yesterday"),
    ("вчера", "yesterday"),
    ("за позавчера", "before_yesterday"),
    ("позавчерашн*", "before_yesterday"),
    ("позавчера", "before_yesterday"),
    ("на этой неделе", "week"),
    ("за эту неделю", "week"),
    ("этой недели", "week"),
    ("на неделе", "week"),
    ("на прошлой неделе", "last_week"),
    ("за прошлую неделю", "last_week"),
    ("прошлой недели", "last_week"),
    ("на той неделе", "last_week"),
    ("за последнюю неделю", "7d"),
    ("за неделю", "7d"),
    ("в этом месяце", "month"),
    ("за этот месяц", "month"),
    ("этого месяца", "month"),
    ("в прошлом месяце", "last_month"),
    ("за прошлый месяц", "last_month"),
    ("прошлого месяца", "last_month"),
    ("за последний месяц", "30d"),
    ("за месяц", "30d"),
    ("в этом году", "year"),
    ("за этот год", "year"),
    ("этого года", "year"),
    ("в прошлом году", "last_year"),
    ("за прошлый год", "last_year"),
    ("прошлого года", "last_year"),
    ("за последний год", "365d"),
    ("за год", "365d"),
)
_DATE_PATTERNS = sorted(((p.split(), key) for p, key in _DATE_PHRASES), key=lambda x: -len(x[0]))
DATE_LABELS = {
    "today": "за сегодня",
    "yesterday": "за вчера",
    "before_yesterday": "за позавчера",
    "week": "на этой неделе",
    "last_week": "на прошлой неделе",
    "7d": "за неделю",
    "month": "в этом месяце",
    "last_month": "в прошлом месяце",
    "30d": "за месяц",
    "year": "в этом году",
    "last_year": "в прошлом году",
    "365d": "за год",
}
_UNIT_SECONDS: tuple[tuple[str, int], ...] = (
    ("сут", 86400),
    ("дн", 86400),
    ("день", 86400),
    ("час", 3600),
    ("недел", 7 * 86400),
    ("месяц", 30 * 86400),
    ("минут", 60),
    ("год", 365 * 86400),
    ("лет", 365 * 86400),
)


def date_range(key: str, now: datetime | None = None) -> tuple[float | None, float | None]:
    """Период по ключу (today, yesterday, week, last_month, 7d …) → (с, до) в unix-времени;
    «до» None — по сейчас."""
    moment = now or datetime.now()
    today = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    jan = today.replace(month=1, day=1)
    ranges: dict[str, tuple[datetime, datetime | None]] = {
        "today": (today, None),
        "yesterday": (today - timedelta(days=1), today),
        "before_yesterday": (today - timedelta(days=2), today - timedelta(days=1)),
        "week": (monday, None),
        "last_week": (monday - timedelta(days=7), monday),
        "month": (first, None),
        "last_month": ((first - timedelta(days=1)).replace(day=1), first),
        "year": (jan, None),
        "last_year": (jan.replace(year=jan.year - 1), jan),
        "7d": (moment - timedelta(days=7), None),
        "30d": (moment - timedelta(days=30), None),
        "365d": (moment - timedelta(days=365), None),
    }
    if key not in ranges:
        return None, None
    start, end = ranges[key]
    return start.timestamp(), end.timestamp() if end is not None else None


def _unit_at(toks: list[str], i: int) -> int:
    """Секунд в единице времени со слова i («дня» → 86400); не единица — 0."""
    if i >= len(toks):
        return 0
    return next((sec for stem, sec in _UNIT_SECONDS if toks[i].startswith(stem)), 0)


def _take_relative(toks: list[str], i: int) -> tuple[int, float] | None:
    """«за последние три дня», «за 2 недели», «за последний час», «в течение суток» со слова i →
    (где кончилось, сколько секунд назад)."""
    if toks[i] == "за":
        j = i + 1
    elif toks[i] == "в" and toks[i + 1 : i + 2] == ["течение"]:
        j = i + 2
    else:
        return None
    recent = j < len(toks) and toks[j].startswith("последн")
    if recent:
        j += 1
    for amount, end in numbers.parse_at(toks, j):
        unit = _unit_at(toks, end)
        if unit and 0 < amount <= 10_000:
            return end + 1, amount * unit
    if recent or (j < len(toks) and toks[j] in ("сутки", "суток", "час")):
        unit = _unit_at(toks, j)
        if unit:
            return j + 1, float(unit)
    return None


def _take_date(toks: list[str], now: datetime) -> tuple[list[str], float | None, float | None, str]:
    """Найти и вырезать период: «за вчера», «на прошлой неделе», «за последние три дня»."""
    for i in range(len(toks)):
        for pattern, key in _DATE_PATTERNS:
            n = len(pattern)
            if i + n <= len(toks) and all(_matches(toks[i + k], pattern[k]) for k in range(n)):
                start, end = date_range(key, now)
                return toks[:i] + toks[i + n :], start, end, DATE_LABELS[key]
        relative = _take_relative(toks, i)
        if relative is not None:
            end_at, seconds = relative
            label = " ".join(toks[i:end_at])
            return toks[:i] + toks[end_at:], now.timestamp() - seconds, None, label
    return toks, None, None, ""


_CONTENT_MARKERS = sorted(
    (
        m.split()
        for m in (
            "со словом",
            "со словами",
            "с словом",
            "с текстом",
            "с фразой",
            "с упоминанием",
            "содержащие",
            "содержащий",
            "содержащую",
            "содержащих",
            "содержащее",
            "содержащая",
            "в которых есть",
            "в котором есть",
            "в которых написано",
            "в котором написано",
            "в которых упоминается",
            "в котором упоминается",
            "где написано",
            "где упоминается",
            "внутри которых",
            "с текстом внутри",
        )
    ),
    key=len,
    reverse=True,
)
_CONTENT_NOUNS = frozenset({"слово", "слова", "текст", "фраза", "фразу", "фразой"})

# основы (search_stem) названий известных папок: «в загрузках», «из документов», «в картинках»
_FOLDER_STEMS: tuple[tuple[str, frozenset[str]], ...] = (
    ("downloads", _set("загрузк загрузок загруженн закачк закачек скачанн скачк скачек download downloads")),
    ("documents", _set("документ documents")),
    ("pictures", _set("изображен картинк картинок pictures")),
    ("music", _set("музык music")),
    ("videos", _set("видео виде videos")),
    ("desktop", _set("desktop")),
)
_PREPOSITIONS = frozenset({"в", "во", "из", "на", "с", "со"})
_FOLDER_LEADS = frozenset(
    {"папке", "папки", "папку", "папка", "мои", "моих", "моей", "моем", "свои", "своих"}
)
_FOLDER_NOUNS = frozenset({"папке", "папки", "папку"})
_DOWNLOADED = ("скачанн", "скачал", "загруженн", "закачанн", "скачен")
_RECENT_ONE = frozenset({"последний", "последнюю", "последнее", "последняя", "последним", "последней"})
_RECENT_MANY = frozenset({"последние", "последних", "свежие", "свежий", "свежую", "недавние", "недавно"})


def _known_folder_at(toks: list[str], i: int) -> tuple[str, int] | None:
    """Известная папка со слова i: (ключ, сколько слов заняла) — «рабочем столе» занимает два."""
    if i >= len(toks):
        return None
    word = toks[i]
    if word.startswith("рабоч") and i + 1 < len(toks) and toks[i + 1].startswith("стол"):
        return "desktop", 2
    stem = search_stem(word)
    for key, stems in _FOLDER_STEMS:
        if word in stems or stem in stems:
            return key, 1
    return None


def _take_folder(toks: list[str]) -> tuple[list[str], str, list[str]]:
    """Вырезать папку: «в загрузках», «на рабочем столе», «из папки проекты», «скачанный» → (слова, ключ,
    слова пути)."""
    out: list[str] = []
    folder = ""
    path_words: list[str] = []
    i = 0
    while i < len(toks):
        word = toks[i]
        if not folder and not path_words and word in _PREPOSITIONS:
            j = i + 1
            while j < len(toks) and toks[j] in _FOLDER_LEADS:
                j += 1
            found = _known_folder_at(toks, j)
            if found is not None:
                folder = found[0]
                i = j + found[1]
                continue
            if (
                any(t in _FOLDER_NOUNS for t in toks[i + 1 : j])
                and j < len(toks)
                and toks[j] not in STOP_WORDS
            ):
                # «в папке проекты»: часть пути — одно слово (дальше — снова имя; слова имени ищутся
                # и в пути, так что «в папке сайт кафе» тоже найдёт файлы из «Сайт кафе»)
                path_words = [search_stem(toks[j])]
                i = j + 1
                continue
        if not folder and word.startswith(_DOWNLOADED):
            folder = "downloads"
            i += 1
            continue
        out.append(word)
        i += 1
    return out, folder, path_words


def _take_content(toks: list[str]) -> tuple[list[str], list[str]]:
    """Отделить слова текста: «… со словом отчёт» → (слова до метки, слова после)."""
    for i in range(len(toks)):
        for marker in _CONTENT_MARKERS:
            n = len(marker)
            if toks[i : i + n] == marker:
                rest = toks[i + n :]
                while rest and rest[0] in _CONTENT_NOUNS:
                    rest = rest[1:]
                return toks[:i], rest
    return toks, []


def _kind_of_word(word: str) -> str | None:
    """«презентацию» → presentation, «фотки» → image; «документация» — не тип (другая основа)."""
    stem = search_stem(word)
    for kind in KINDS:
        if word in kind.words or stem in kind.stems:
            return kind.key
    return None


def parse_query(text: str, now: datetime | None = None) -> SearchQuery:
    """Фраза → SearchQuery: «найди pdf в загрузках за вчера» → kinds [pdf], folder downloads, вчера."""
    moment = now or datetime.now()
    q = SearchQuery(text=" ".join(str(text or "").split()))
    toks = tokens(q.text)
    toks, q.date_from, q.date_to, q.date_label = _take_date(toks, moment)
    toks, q.folder, q.path_words = _take_folder(toks)
    toks, content = _take_content(toks)
    words: list[str] = []
    for word in toks:
        if word in _RECENT_ONE:
            q.sort, q.limit = "recent", 1
        elif word in _RECENT_MANY:
            q.sort = "recent"
        elif word in _FILE_WORDS:
            q.files_only = True
        elif (kind := _kind_of_word(word)) is not None:
            if kind not in q.kinds:
                q.kinds.append(kind)
        elif word not in STOP_WORDS and (len(word) > 1 or word.isdigit()):
            words.append(word)
    q.words = words
    q.stems = unique(search_stem(w) for w in words)
    q.content = unique(search_stem(w) for w in content if w not in STOP_WORDS and len(w) > 1)
    if q.folders_only:
        q.files_only = False
    elif "folder" in q.kinds and len(q.kinds) > 1:
        q.kinds.remove("folder")
    return q


def known_folder_key(text: str) -> str:
    """«downloads», «Загрузки», «загрузках» → ключ известной папки ("" — не она)."""
    value = norm(text).strip()
    if value in KNOWN_FOLDERS:
        return value
    found = _known_folder_at(tokens(value), 0)
    return found[0] if found else ""


# --- ранжирование -----------------------------------------------------------------------------------------


@dataclass
class Hit:
    path: str
    name: str
    kind: str
    size: int
    modified: float
    folder: str  # папка, где лежит
    where: str  # она же для человека: «Документы › Проекты»
    score: float = 0.0
    coverage: float = 1.0  # доля слов запроса в имени (в пути — за половину)
    exact: bool = False  # имя — ровно слова запроса

    @property
    def is_dir(self) -> bool:
        return self.kind == "folder"

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "modified": self.modified,
            "folder": self.folder,
            "where": self.where,
        }


def where_label(folder: str, known: list[tuple[str, str]]) -> str:
    """Папка для человека: «Документы › Проекты»; вне известных папок — последние две части: «… › Работа ›
    Клиенты»."""
    key = path_key(folder)
    best: tuple[str, str] | None = None
    for root, name in known:
        rk = path_key(root)
        if rk and under(key, [rk]) and (best is None or len(rk) > len(path_key(best[0]))):
            best = (root, name)
    parts = path_parts(folder)
    if best is not None:
        rest = parts[len(path_parts(best[0])) :]
        title = FOLDER_TITLES.get(best[1], best[1])
        if len(rest) > 2:
            return " › ".join([title, "…", *rest[-2:]])
        return " › ".join([title, *rest])
    if len(parts) > 2:
        return " › ".join(["…", *parts[-2:]])
    return " › ".join(parts) or folder


def _score(item: IndexedFile, q: SearchQuery, now: float) -> tuple[float, float, bool] | None:
    name = norm(item.name)
    base = name if item.is_dir or "." not in name else name.rpartition(".")[0]
    words = _TOKEN.findall(base)
    stems = q.stems
    if stems:
        folder = norm(parent_of(item.path))
        in_name = [s for s in stems if s in base]
        in_path = [s for s in stems if s not in base and s in folder]
        if not in_name and not in_path:
            return None
        coverage = (len(in_name) + 0.5 * len(in_path)) / len(stems)
        score = 100.0 * coverage
        score += 10.0 * sum(1 for s in in_name if any(w.startswith(s) for w in words)) / len(stems)
        score += 5.0 * sum(1 for w in q.words if norm(w) in words) / len(stems)  # та же форма слова
        name_stems = {search_stem(w) for w in words if w not in STOP_WORDS}
        exact = bool(name_stems) and name_stems == set(stems)
        if exact:
            score += 40.0
        elif base.startswith(stems[0]):
            score += 12.0
        score -= min(10.0, 1.5 * max(0, len(name_stems) - len(stems)))
    else:
        coverage, exact, score = 1.0, False, 60.0
    age_days = max(0.0, (now - item.modified) / 86400) if item.modified else 365.0
    score += 15.0 * 0.5 ** (age_days / 14.0)
    return score, coverage, exact


def rank_files(
    items: Iterable[IndexedFile],
    q: SearchQuery,
    *,
    now: float | None = None,
    known: list[tuple[str, str]] | None = None,
    exclude: Iterable[str] = (),
) -> list[Hit]:
    """Отобрать (тип, даты, папки, исключения) и упорядочить найденное: лучшие — первыми."""
    moment = time.time() if now is None else now
    exts = set(q.extensions())
    excluded = [path_key(e) for e in exclude if e]
    known_list = known or []
    scope = [path_key(root) for root, key in known_list if key == q.folder] if q.folder else []
    seen: set[str] = set()
    hits: list[Hit] = []
    for item in items:
        key = path_key(item.path)
        if not key or key in seen:
            continue
        seen.add(key)
        if junk_path(item.path) or under(key, excluded):
            continue
        if (q.folders_only and not item.is_dir) or (q.files_only and item.is_dir):
            continue
        if exts and (item.is_dir or item.name.rpartition(".")[2].casefold() not in exts):
            continue
        if q.date_from is not None and item.modified < q.date_from:
            continue
        if q.date_to is not None and item.modified >= q.date_to:
            continue
        if q.folder and not under(key, scope):
            continue
        folder = parent_of(item.path)
        if q.path_words and not all(w in norm(folder) for w in q.path_words):
            continue
        scored = _score(item, q, moment)
        if scored is None:
            continue
        score, coverage, exact = scored
        hits.append(
            Hit(
                path=item.path,
                name=item.name,
                kind=kind_of(item.name, item.is_dir),
                size=item.size,
                modified=item.modified,
                folder=folder,
                where=where_label(folder, known_list),
                score=round(score, 2),
                coverage=coverage,
                exact=exact,
            )
        )
    if q.order == "recent":
        hits.sort(key=lambda h: (h.modified, h.score), reverse=True)
    else:
        hits.sort(key=lambda h: (h.score, h.modified), reverse=True)
    return hits


def clear_winner(hits: list[Hit], q: SearchQuery) -> bool:
    """Открыть сразу: совпадение одно, просили «последний» или лучшее явно лучше остальных."""
    if not hits:
        return False
    if len(hits) == 1 or q.limit == 1:
        return True
    top, second = hits[0], hits[1]
    if not q.stems:
        return False
    if top.exact and not second.exact:
        return True
    return top.coverage >= 1.0 and second.coverage < 1.0 and top.score - second.score >= 25.0


# --- свой индекс ------------------------------------------------------------------------------------------


def _hidden(name: str, st: os.stat_result) -> bool:
    if name[:1] in ".$":
        return True
    return bool(getattr(st, "st_file_attributes", 0) & HIDDEN_ATTRS)


def _skip_file(name: str) -> bool:
    lower = name.casefold()
    return lower in SKIP_FILES or lower.startswith("~$") or lower.endswith(PARTIAL_SUFFIXES)


def scan_folders(
    roots: Iterable[str],
    *,
    exclude: Iterable[str] = (),
    max_depth: int = SCAN_DEPTH,
    max_entries: int = SCAN_MAX_ENTRIES,
    budget_s: float = SCAN_BUDGET_S,
    stop: threading.Event | None = None,
) -> tuple[list[IndexedFile], bool]:
    """Обойти папки вширь: (файлы и папки, обрезан ли обход пределами). Глубина 1 — только сама папка.

    Пропускает служебные (SKIP_DIRS), скрытые и системные папки и файлы, ссылки и точки соединения,
    недокачанные файлы; папки из exclude — вместе с содержимым."""
    excluded = [path_key(e) for e in exclude if e]
    queue: deque[tuple[str, int]] = deque()
    taken: list[str] = []
    for root in roots:
        key = path_key(root)
        if not key or under(key, excluded) or under(key, taken) or not os.path.isdir(root):
            continue
        taken.append(key)
        queue.append((root, 1))
    out: list[IndexedFile] = []
    truncated = False
    deadline = time.monotonic() + budget_s
    while queue:
        if stop is not None and stop.is_set():
            break
        if time.monotonic() > deadline or len(out) >= max_entries:
            truncated = True
            break
        folder, depth = queue.popleft()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if len(out) >= max_entries:
                truncated = True
                break
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            name = entry.name
            if _hidden(name, st):
                continue
            if is_dir:
                if name.casefold() in SKIP_DIRS or under(path_key(entry.path), excluded):
                    continue
                out.append(IndexedFile(entry.path, name, 0, st.st_mtime, True))
                if depth < max_depth:
                    queue.append((entry.path, depth + 1))
            elif statmod.S_ISREG(st.st_mode) and not _skip_file(name):
                out.append(IndexedFile(entry.path, name, st.st_size, st.st_mtime, False))
    return out, truncated


@dataclass
class OwnIndex:
    items: list[IndexedFile] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # path_key — для быстрого отбора по словам
    roots: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    built: float = 0.0
    truncated: bool = False

    @classmethod
    def make(
        cls, items: list[IndexedFile], roots: list[str], exclude: list[str], built: float, truncated: bool
    ) -> "OwnIndex":
        return cls(items, [path_key(i.path) for i in items], roots, exclude, built, truncated)

    def candidates(self, q: SearchQuery) -> list[IndexedFile]:
        """Грубый отбор по словам (хоть одно слово в пути); точный — в rank_files."""
        words = q.stems or q.content
        if not words:
            return list(self.items)
        return [item for item, key in zip(self.items, self.keys, strict=True) if any(w in key for w in words)]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "roots": self.roots,
            "exclude": self.exclude,
            "built": self.built,
            "truncated": self.truncated,
            "items": [[i.path, i.size, round(i.modified, 1), int(i.is_dir)] for i in self.items],
        }

    @classmethod
    def from_json(cls, data: Any) -> "OwnIndex | None":
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        items: list[IndexedFile] = []
        for row in data.get("items") or []:
            if not (isinstance(row, list) and len(row) == 4 and isinstance(row[0], str) and row[0]):
                continue
            try:
                size, modified = int(row[1] or 0), float(row[2] or 0)
            except (TypeError, ValueError):
                continue  # испорченная строка кэша — пропустить, а не терять весь кэш
            items.append(IndexedFile(row[0], path_parts(row[0])[-1], size, modified, bool(row[3])))
        return cls.make(
            items,
            [str(r) for r in data.get("roots") or []],
            [str(e) for e in data.get("exclude") or []],
            float(data.get("built") or 0),
            bool(data.get("truncated")),
        )
