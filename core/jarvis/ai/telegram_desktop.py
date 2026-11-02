"""Окно Telegram Desktop для действий агента telegram_send и telegram_read: где в снимке окна поле поиска,
найденные чаты, шапка открытого чата, поле сообщения и сами сообщения.

Снимок — элементы UI Automation (e…) и строки, распознанные на картинке (t…). Смотря по версии Telegram,
UI Automation видит больше или меньше, поэтому всё ищется и там, и там — по месту в окне: слева колонка с
поиском и списком чатов, справа открытый чат — шапка с именем вверху, поле сообщения внизу.

Имена сравниваются без регистра, «ё», знаков и похожих латинских букв. «Ане», «Маме», «Ивану» — то же, что
«Аня», «Мама», «Иван»: у слов сравниваются основы без падежного окончания. Подходят несколько чатов —
выбирает человек (pick вернёт их все).
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from jarvis.ai.ocr import spellings
from jarvis.ai.playbooks import TELEGRAM_EXES
from jarvis.ai.uia import UiElement, UiSnapshot

SEARCH_NAMES = frozenset({"поиск", "search"})  # поле поиска чатов («Поиск по чату» — другое)
MESSAGE_FIELD = re.compile(
    r"написать сообщение|сообщени|write a message|message|комментар|comment|broadcast|трансляц", re.I
)
HEADER_ZONE = 140  # шапка чата — столько точек от верха окна (с заголовком окна)…
HEADER_SHARE = 0.15  # …или такая доля высоты окна, что больше
FOOTER_SHARE = 0.2  # поле сообщения — в нижней части окна
LEFT_SHARE = 0.38  # поле поиска не найдено — левая колонка не шире этой доли окна
SAME_TEXT = 85  # похожесть строк (rapidfuzz), при которой распознанное считается тем же текстом
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


def pick(query: str, names: list[str]) -> list[str]:
    """Чаты, которые человек назвал: [имя] — он, несколько — выбрать человеку, [] — ни одного.

    Имя целиком («Мама», «Аня» при «Аня Смирнова» рядом) важнее части имени; иначе — лучшие совпадения."""
    found: list[tuple[Match, str]] = []
    for name in dict.fromkeys(n.strip() for n in names if n.strip()):
        m = match(query, name)
        if m is not None:
            found.append((m, name))
    whole = [name for m, name in found if m.whole and m.score >= 2]
    if whole:
        return whole
    best = max((m.score for m, _ in found), default=0)
    return [name for m, name in found if m.score == best]


def _in_name(word: str, name: str) -> bool:
    return any(_word_score(word, w) >= 2 for w in _name_words(name))


def answer_pick(answer: str, options: list[str]) -> str:
    """Ответ человека на «какой из чатов?» → один из options или "": «второй», «2», «Смирновой», «той, что с
    работы» — у какого варианта больше слов из ответа (по основе)."""
    words = norm(answer).split()
    scores = [sum(1 for w in words if _in_name(w, option)) for option in options]
    best = max(scores, default=0)
    if best and scores.count(best) == 1:
        return options[scores.index(best)]
    for i, start in enumerate(ORDINALS[: len(options)]):
        if any(w.startswith(start) or w == str(i + 1) for w in words):
            return options[i]
    return ""


def same_name(want: str, seen: str) -> bool:
    """Строка в шапке чата — это имя want (распознавание могло ошибиться в букве)."""
    a, b = norm(want), norm(seen)
    if not a or not b:
        return False
    for spelled in {b, *(norm(s) for s in spellings(seen))}:
        if spelled == a or spelled.startswith(a + " ") or fuzz.ratio(a, spelled) >= SAME_TEXT:
            return True
    return False


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
    upper = top + max(HEADER_ZONE, int((bottom - top) * 0.25))
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
    """Строки левой колонки под полем поиска (найденные чаты и их последние сообщения), сверху вниз."""
    edge = split_x(snapshot, search)
    _left, top, _right, _bottom = frame(snapshot)
    below = search.rect[3] if search is not None else top + 60
    rows = [
        e
        for e in snapshot.elements
        if e.name.strip() and e.kind != "Edit" and e.center[0] < edge and e.rect[1] >= below - 2
    ]
    return sorted(rows, key=lambda e: (e.rect[1], e.rect[0]))


def _header_bottom(snapshot: UiSnapshot) -> int:
    _left, top, _right, bottom = frame(snapshot)
    return top + max(HEADER_ZONE, int((bottom - top) * HEADER_SHARE))


def header(snapshot: UiSnapshot, edge: int) -> list[UiElement]:
    """Строки шапки открытого чата: справа от левой колонки, у верха окна (имя, «был(а) недавно»)."""
    limit = _header_bottom(snapshot)
    return [
        e
        for e in snapshot.elements
        if e.name.strip() and e.kind != "Edit" and e.center[0] > edge and e.center[1] < limit
    ]


def chat_open(snapshot: UiSnapshot, edge: int, name: str) -> UiElement | None:
    """Строка шапки с именем чата name — значит, открыт он."""
    return next((e for e in header(snapshot, edge) if same_name(name, e.name)), None)


def chat_title(snapshot: UiSnapshot, edge: int) -> str:
    """Имя открытого чата — первая строка шапки (для чата по @username, имени которого не знаем)."""
    rows = sorted(header(snapshot, edge), key=lambda e: (e.rect[1], e.rect[0]))
    return rows[0].name.strip() if rows else ""


def message_field(snapshot: UiSnapshot, edge: int) -> UiElement | None:
    """Поле сообщения: самое нижнее поле UI Automation справа от левой колонки (не поиск), а нет —
    распознанная подсказка «Написать сообщение…» внизу окна."""
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
        if e.ocr and e.center[0] > edge and e.center[1] > lower and MESSAGE_FIELD.search(e.name)
    ]
    return max(hints, key=lambda e: e.rect[3]) if hints else None


def typed_shown(snapshot: UiSnapshot, edge: int, field: UiElement | None, text: str) -> bool | None:
    """Набранное видно в поле сообщения: True — да, False — в поле другое (черновик, чужой текст), None —
    не разобрать (поле не показывает текст, и распознанного внизу окна нет)."""
    want = norm(text)
    if field is not None and not field.ocr and field.value.strip():
        return norm(field.value) == want
    _left, top, _right, bottom = frame(snapshot)
    lower = bottom - int((bottom - top) * FOOTER_SHARE)
    head = want[:40]
    for e in snapshot.elements:
        near = e.ocr and e.center[0] > edge and e.center[1] > lower
        if near and fuzz.partial_ratio(head, norm(e.name)) >= SAME_TEXT:
            return True
    return None


def draft(field: UiElement | None) -> str:
    """Текст, который уже стоит в поле сообщения (черновик человека); подсказка поля — не текст."""
    if field is None or field.ocr:
        return ""
    value = field.value.strip()
    if not value or norm(value) == norm(field.name) or MESSAGE_FIELD.fullmatch(value.strip(" .…")):
        return ""
    return value


@dataclass(frozen=True)
class Line:
    text: str
    mine: bool  # справа — своё сообщение, слева — собеседника


def messages(snapshot: UiSnapshot, edge: int, field: UiElement | None) -> list[Line]:
    """Сообщения открытого чата, сверху вниз: строки между шапкой и полем сообщения справа от левой
    колонки. Своё — в правой половине чата (так их рисует Telegram)."""
    _left, top, right, bottom = frame(snapshot)
    upper = _header_bottom(snapshot)
    lower = field.rect[1] if field is not None else bottom - int((bottom - top) * FOOTER_SHARE)
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
    """Сколько своих сообщений с этим текстом видно в чате: стало больше после Enter — сообщение ушло."""
    want = norm(text)
    head = want[:60]
    return sum(
        1
        for line in lines
        if line.mine
        and (
            norm(line.text) == want
            or (len(head) >= 8 and fuzz.partial_ratio(head, norm(line.text)) >= SAME_TEXT)
        )
    )
