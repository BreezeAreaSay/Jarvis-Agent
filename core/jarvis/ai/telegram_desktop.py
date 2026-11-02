"""Окно Telegram Desktop для действий агента telegram_send и telegram_read: где в снимке окна поле поиска,
найденные чаты, шапка открытого чата, поле сообщения и сами сообщения.

Снимок — элементы UI Automation (e…) и строки, распознанные на картинке (t…). Смотря по версии Telegram,
UI Automation видит больше или меньше, поэтому всё ищется и там, и там — по месту в окне: слева колонка с
поиском и списком чатов, справа открытый чат — шапка с именем вверху, поле сообщения внизу.

Имена сравниваются без регистра, «ё», знаков и похожих латинских букв. «Ане», «Маме», «Ивану» — то же, что
«Аня», «Мама», «Иван»: у слов сравниваются основы без падежного окончания. Сам выбирается только чат, имя
которого названо целиком; часть имени или несколько подходящих — выбирает человек. Шапка открытого чата —
только полоса на высоте поля поиска (ниже — уже сообщения: имя отправителя в группе — не шапка), и имя в ней
должно совпасть целиком.
"""

import re
from dataclasses import dataclass

from jarvis.ai.ocr import spellings
from jarvis.ai.playbooks import TELEGRAM_EXES
from jarvis.ai.uia import UiElement, UiSnapshot
from jarvis.confirm import parse_yes_no

SEARCH_NAMES = frozenset({"поиск", "search"})  # поле поиска чатов («Поиск по чату» — другое)
# подсказка пустого поля сообщения (вся строка, после norm): «Написать сообщение…», «Write a message…»
PLACEHOLDER = re.compile(
    r"(написать |оставить |)(сообщение|комментарий)|(write|leave) a (message|comment)|message|comment|"
    r"(silent |)broadcast|трансляция|(тихая |)публикация"
)
# заголовки разделов результатов поиска: ниже — не чаты человека, а чужие каналы и найденные сообщения
SECTIONS = frozenset(
    {"глобальный поиск", "global search", "результаты глобального поиска", "global search results",
     "сообщения", "messages", "найденные сообщения", "публикации", "posts"}
)  # fmt: skip
HEADER_SLACK = 16  # шапка чата — до низа поля поиска и ещё столько точек…
HEADER_NO_SEARCH = 100  # …а без поля поиска — столько от верха окна
FOOTER_SHARE = 0.2  # поле сообщения — в нижней части окна
FIELD_SLACK = 8  # строка «в поле» — центр внутри прямоугольника поля с таким запасом
LEFT_SHARE = 0.38  # поле поиска не найдено — левая колонка не шире этой доли окна
SEARCH_ZONE = 140  # распознанная надпись «Поиск» — не ниже стольких точек от верха окна (или четверти)
ORDINALS = ("перв", "втор", "трет", "четв", "пят")  # «второй», «третья» — номер варианта
# окончания имён и прилагательных — сначала длинные: «Избранному», «Смирновой», «Дмитрию», «Марии», «Ане»
_ENDINGS = (
    *("ому", "ему", "ого", "его", "ыми", "ими"),
    *("ой", "ою", "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий", "ую", "юю", "ия", "ии", "ию"),
    *"аеиоуыэюяйь",
)
_WORD = re.compile(r"[^\W_]+(?:\|[^\W_]+)*")  # слово; «Непо|Hello» — два прочтения одного слова


def is_telegram(exe: str) -> bool:
    return exe.casefold() in TELEGRAM_EXES


def norm(text: str) -> str:
    """Для сравнения: без регистра, «ё», знаков и лишних пробелов; «|» прочтений остаётся."""
    return " ".join(_WORD.findall(text.casefold().replace("ё", "е")))


def stem(word: str) -> str:
    """Основа слова без падежного окончания: «ане» и «аня» → «ан», «смирновой» → «смирнов», «избранному» и
    «избранное» → «избранн», «дмитрию» и «дмитрий» → «дмитр». Основа короче 2 букв — слово как есть."""
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 2:
            return word[: -len(ending)]
    return word


def _word_score(query: str, variants: frozenset[str]) -> int:
    """3 — то же слово, 2 — та же основа («Ане» и «Аня»), 1 — начало слова («Ан» и «Анна»), 0 — нет."""
    best = 0
    for variant in variants:
        if variant == query:
            return 3
        if stem(variant) == stem(query) and len(stem(query)) >= 2:
            best = max(best, 2)
        elif len(query) >= 2 and variant.startswith(query):
            best = max(best, 1)
    return best


def _name_words(name: str) -> list[frozenset[str]]:
    """Слова имени, у каждого — варианты прочтения: похожие латинские буквы как кириллица и наоборот
    («Mама»), два прочтения распознавания («Аппа|Anna»). Время и счётчики в строке списка («Аня 12:30»,
    «Работа 3») — не часть имени."""
    readings = [norm(s).split() for s in spellings(name)]
    base = readings[0]
    words: list[frozenset[str]] = []
    for i, word in enumerate(base):
        if word.replace("|", "").isdigit():
            continue
        words.append(frozenset(v for r in readings if len(r) == len(base) for v in r[i].split("|")))
    return words


@dataclass(frozen=True)
class Match:
    score: int  # худшее из слов запроса: 3 — точно, 2 — по основе, 1 — по началу слова
    whole: bool  # запрос называет имя целиком: каждое слово имени совпало со словом запроса


@dataclass(frozen=True)
class Picked:
    whole: list[str]  # имя названо целиком: один — он, несколько — спросить
    partial: list[str]  # названа часть имени («Аня» при «Аня Смирнова») — только спросив человека


def match(query: str, name: str) -> Match | None:
    """Подходит ли имя чата к запросу человека: каждое слово запроса — какое-то слово имени."""
    wanted, words = norm(query).split(), _name_words(name)
    if not wanted or not words:
        return None
    scores = [max(_word_score(q, w) for w in words) for q in wanted]
    if min(scores) == 0:
        return None
    whole = all(any(_word_score(q, w) >= 2 for q in wanted) for w in words)
    return Match(min(scores), whole)


def pick(query: str, names: list[str]) -> Picked:
    """Чаты, которые человек назвал: имя целиком («Мама», «Аня» при «Аня Смирнова» рядом) — whole, иначе
    лучшие совпадения части имени — partial (их выбрать человеку)."""
    found: list[tuple[Match, str]] = []
    for name in dict.fromkeys(n.strip() for n in names if n.strip()):
        m = match(query, name)
        if m is not None:
            found.append((m, name))
    whole = [name for m, name in found if m.whole and m.score >= 2]
    best = max((m.score for m, _ in found), default=0)
    partial = [] if whole else [name for m, name in found if m.score == best]
    return Picked(whole, partial)


def _in_name(word: str, name: str) -> bool:
    return any(_word_score(word, w) >= 2 for w in _name_words(name))


def answer_pick(answer: str, options: list[str]) -> str:
    """Ответ человека на «какой из чатов?» → один из options или "": «второй», «2», «Смирновой», «той, что с
    работы» — у какого варианта больше слов из ответа (по основе); вариант один — и «да»."""
    words = norm(answer).split()
    if len(options) == 1 and parse_yes_no(answer):
        return options[0]
    scores = [sum(1 for w in words if _in_name(w, option)) for option in options]
    best = max(scores, default=0)
    if best and scores.count(best) == 1:
        return options[scores.index(best)]
    for i, start in enumerate(ORDINALS[: len(options)]):
        if any(w.startswith(start) or w == str(i + 1) for w in words):
            return options[i]
    return ""


def same_name(want: str, seen: str) -> bool:
    """Строка — это имя want целиком: слово в слово (без регистра, знаков, времени и чисел; похожие латинские
    буквы и два прочтения распознавания — одно и то же). «Аня» и «Аня Смирнова», «Иванов» и «Иванова» —
    разные."""
    a, b = _name_words(want), _name_words(seen)
    return bool(a) and len(a) == len(b) and all(x & y for x, y in zip(a, b, strict=True))


def placeholder(text: str) -> bool:
    """Подсказка пустого поля сообщения («Написать сообщение…»), а не текст в нём."""
    return bool(PLACEHOLDER.fullmatch(norm(text)))


# --- места в окне --------------------------------------------------------------------------------------
def frame(snapshot: UiSnapshot) -> tuple[int, int, int, int]:
    """Окно на экране: из снимка, а нет — по элементам."""
    if snapshot.rect[2] > snapshot.rect[0] and snapshot.rect[3] > snapshot.rect[1]:
        return snapshot.rect
    rects = [e.rect for e in snapshot.elements if e.rect[2] > e.rect[0]]
    if not rects:
        return 0, 0, 0, 0
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def search_field(snapshot: UiSnapshot) -> UiElement | None:
    """Поле поиска чатов: поле UI Automation «Поиск» в левой половине окна, а нет — распознанная надпись
    «Поиск» слева вверху (щелчок по ней ставит курсор в поле)."""
    left, top, right, bottom = frame(snapshot)
    middle = left + (right - left) // 2
    upper = top + max(SEARCH_ZONE, int((bottom - top) * 0.25))
    fields = [
        e
        for e in snapshot.elements
        if not e.ocr and e.kind == "Edit" and norm(e.name) in SEARCH_NAMES and e.center[0] < middle
    ]
    if fields:
        return min(fields, key=lambda e: e.rect[1])
    labels = [
        e
        for e in snapshot.elements
        if e.ocr and norm(e.name) in SEARCH_NAMES and e.center[0] < middle and e.center[1] < upper
    ]
    return min(labels, key=lambda e: e.rect[1]) if labels else None


def split_x(snapshot: UiSnapshot, search: UiElement | None) -> int:
    """Правый край левой колонки (поиск и список чатов)."""
    left, _top, right, _bottom = frame(snapshot)
    if search is not None:
        return search.rect[2] + 12
    return left + int((right - left) * LEFT_SHARE)


def results(snapshot: UiSnapshot, search: UiElement | None) -> list[UiElement]:
    """Строки левой колонки под полем поиска, сверху вниз, — найденные чаты (и их последние сообщения) до
    раздела «Глобальный поиск» или «Сообщения». Левее поля поиска — папки, не чаты."""
    edge = split_x(snapshot, search)
    _left, top, _right, _bottom = frame(snapshot)
    below = search.rect[3] if search is not None else top + 60
    start = search.rect[0] if search is not None else 0
    rows = sorted(
        (
            e
            for e in snapshot.elements
            if e.name.strip() and e.kind != "Edit" and start <= e.center[0] < edge and e.rect[1] >= below - 2
        ),
        key=lambda e: (e.rect[1], e.rect[0]),
    )
    out: list[UiElement] = []
    for e in rows:
        if norm(e.name) in SECTIONS:
            break
        out.append(e)
    return out


def _header_bottom(snapshot: UiSnapshot, search: UiElement | None) -> int:
    if search is not None:
        return search.rect[3] + HEADER_SLACK
    return frame(snapshot)[1] + HEADER_NO_SEARCH


def header(snapshot: UiSnapshot, search: UiElement | None) -> list[UiElement]:
    """Строки шапки открытого чата (имя, «был(а) недавно»): справа от левой колонки, не ниже поля поиска —
    дальше начинаются сообщения. Кнопки шапки («Поиск», «Позвонить») — не строки."""
    edge, limit = split_x(snapshot, search), _header_bottom(snapshot, search)
    return sorted(
        (
            e
            for e in snapshot.elements
            if e.name.strip()
            and e.kind not in ("Edit", "Button", "Document")
            and e.center[0] > edge
            and e.center[1] < limit
        ),
        key=lambda e: (e.rect[1], e.rect[0]),
    )


def chat_open(snapshot: UiSnapshot, search: UiElement | None, name: str) -> UiElement | None:
    """Строка шапки с именем чата name целиком — значит, открыт он."""
    return next((e for e in header(snapshot, search) if same_name(name, e.name)), None)


def chat_title(snapshot: UiSnapshot, search: UiElement | None) -> str:
    """Имя открытого чата — верхняя строка шапки (для чата по @username, имени которого не знаем)."""
    rows = header(snapshot, search)
    return rows[0].name.strip() if rows else ""


def message_field(snapshot: UiSnapshot, search: UiElement | None) -> UiElement | None:
    """Поле сообщения: самое нижнее поле UI Automation справа от левой колонки (не поиск), а нет —
    распознанная подсказка пустого поля («Написать сообщение…») внизу окна — вся строка, не слово в
    сообщении."""
    edge = split_x(snapshot, search)
    _left, top, _right, bottom = frame(snapshot)
    fields = [
        e
        for e in snapshot.elements
        if not e.ocr
        and e.kind in ("Edit", "Document")
        and e.center[0] > edge
        and not norm(e.name).startswith(tuple(SEARCH_NAMES))
    ]
    if fields:
        return max(fields, key=lambda e: e.rect[3])
    lower = bottom - int((bottom - top) * FOOTER_SHARE)
    hints = [
        e
        for e in snapshot.elements
        if e.ocr and e.center[0] > edge and e.center[1] > lower and placeholder(e.name)
    ]
    return max(hints, key=lambda e: e.rect[3]) if hints else None


def _field_box(snapshot: UiSnapshot, field: UiElement) -> tuple[int, int, int, int]:
    """Где в окне текст поля: у поля UI Automation — его прямоугольник; у распознанной подсказки — её строка
    от её левого края до правого края окна (набранное шире подсказки)."""
    left, top, right, bottom = field.rect
    if field.ocr:
        right = frame(snapshot)[2]
    return left - FIELD_SLACK, top - FIELD_SLACK, right + FIELD_SLACK, bottom + FIELD_SLACK


def field_text(snapshot: UiSnapshot, field: UiElement) -> str:
    """Текст, который стоит в поле сообщения ("" — пусто): значение UI Automation, а без него —
    распознанные строки внутри поля сверху вниз. Подсказка пустого поля — не текст."""
    if not field.ocr:
        value = field.value.strip()
        if value and not placeholder(value) and norm(value) != norm(field.name):
            return value
    left, top, right, bottom = _field_box(snapshot, field)
    lines = sorted(
        (
            e
            for e in snapshot.elements
            if e.ocr
            and e is not field
            and left <= e.center[0] <= right
            and top <= e.center[1] <= bottom
            and not placeholder(e.name)
            and sum(ch.isalnum() for ch in e.name) >= 2
        ),
        key=lambda e: (e.rect[1], e.rect[0]),
    )
    return " ".join(e.name.strip() for e in lines)


def draft(snapshot: UiSnapshot, field: UiElement) -> str:
    """Черновик человека в поле сообщения до набора: видна подсказка пустого поля — его нет."""
    return "" if field.ocr else field_text(snapshot, field)


def typed_shown(snapshot: UiSnapshot, field: UiElement, text: str) -> bool | None:
    """В поле сообщения ровно набранное: True — да, False — там другое (черновик вместе с нашим текстом,
    чужой текст), None — текста поля не видно. Длинный текст переносится, а поле растёт вверх: у распознанной
    подсказки видна только его последняя строка — она должна быть концом набранного."""
    seen = norm(field_text(snapshot, field))
    want = norm(text)
    if not seen:
        return None
    if seen == want:
        return True
    return field.ocr and len(seen) >= 8 and want.endswith(seen) and len(want) > len(seen)


@dataclass(frozen=True)
class Line:
    text: str
    mine: bool  # справа — своё сообщение, слева — собеседника


def messages(snapshot: UiSnapshot, search: UiElement | None, field: UiElement | None) -> list[Line]:
    """Сообщения открытого чата, сверху вниз: строки между шапкой и полем сообщения справа от левой
    колонки. Своё — в правой половине чата (так их рисует Telegram)."""
    edge = split_x(snapshot, search)
    _left, top, right, bottom = frame(snapshot)
    upper = _header_bottom(snapshot, search)
    lower = field.rect[1] - FIELD_SLACK if field is not None else bottom - int((bottom - top) * FOOTER_SHARE)
    middle = edge + (right - edge) // 2
    rows = sorted(
        (
            e
            for e in snapshot.elements
            if e.name.strip()
            and e.kind not in ("Edit", "Button", "Document")
            and e.center[0] > edge
            and upper <= e.center[1] < lower
        ),
        key=lambda e: (e.rect[1], e.rect[0]),
    )
    out: list[Line] = []
    for e in rows:
        line = Line(e.name.strip(), e.center[0] > middle)
        if not out or out[-1] != line:
            out.append(line)
    return out


def mine_count(lines: list[Line], text: str) -> int:
    """Сколько своих сообщений с этим текстом видно в чате: стало больше после Enter — сообщение ушло.
    Длинное переносится на несколько строк — тогда своя строка, с которой оно начинается."""
    want = norm(text)
    count = 0
    for line in lines:
        seen = norm(line.text)
        if line.mine and seen and (seen == want or (len(seen) >= 12 and want.startswith(seen))):
            count += 1
    return count
