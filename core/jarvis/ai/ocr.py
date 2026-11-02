"""Распознавание текста на экране — запасной путь агента, когда UI Automation почти ничего не видит в окне.

Telegram, программы на Qt, Electron без доступности, игры: в снимке окна — пара полей и кнопки заголовка, а
список чатов, подписи и кнопки только нарисованы. Тогда агент распознаёт текст в окне (на Windows —
Windows.Media.Ocr, jarvis.platform.windows.ocr) и добавляет строки в снимок окна элементами t1, t2… с
прямоугольниками: модель читает их и без картинок, а ui_click по t… — настоящий щелчок мышью в середину
строки.

Когда распознавать (wants_ocr): в снимке окна мало элементов с именем (кнопки заголовка окна не в счёт) или
программа из списка «без доступности» (NO_UIA_EXES). Строки внутри элементов UI Automation, у которых есть имя
или значение (текст поля, подпись кнопки), — повтор: отбрасываются (to_elements); внутри безымянных — нет,
распознанная подпись и есть их имя.

Распознавание путает похожие буквы латиницы и кириллицы («Oтправить», «0тправить»): проверка, рискованно ли
нажатие, смотрит и варианты написания строки (spellings).

Бэкенд синхронный, работает в пуле ввода-вывода (ctx.run_io): на вход — пиксели куска экрана (BGRA) и где этот
кусок на экране; строки — в точках экрана. Не Windows — NoOcr (строк нет); тесты и проверочные задачи
подставляют свой (Agent.ocr).
"""

import sys
from dataclasses import dataclass
from typing import Protocol

from jarvis.ai.format import one_line
from jarvis.ai.uia import UiElement, UiSnapshot

Rect = tuple[int, int, int, int]

# программы, которые UI Automation почти не видит (Qt, свой рендер): распознавать всегда
NO_UIA_EXES = frozenset(
    {
        "telegram.exe",
        "ayugram.exe",
        "kotatogram.exe",
        "64gram.exe",
        "materialgram.exe",
        "viber.exe",
        "obs64.exe",
        "vlc.exe",
        "zoom.exe",
        "anydesk.exe",
        "qbittorrent.exe",
    }
)
# кнопки заголовка окна есть у каждого окна — элементами окна их не считаем
WINDOW_CHROME = frozenset(
    {
        "свернуть",
        "развернуть",
        "восстановить",
        "закрыть",
        "системное меню",
        "minimize",
        "maximize",
        "restore",
        "close",
        "system",
        "system menu",
    }
)
MIN_NAMED = 6  # элементов с именем меньше — окно UI Automation почти не видит: распознать текст
MAX_LINES = 80  # строк в снимке окна
MIN_SIDE = 40  # окно уже стольких точек — распознавать нечего
DUP_AREA = (
    0.25  # строка внутри элемента UI Automation мельче четверти окна (с именем) — его же подпись или текст
)
TEXT_LIMIT = 200


@dataclass
class OcrLine:
    text: str
    rect: Rect  # left, top, right, bottom — точки экрана


class OcrBackend(Protocol):
    def recognize(self, bgra: bytes, size: tuple[int, int], area: Rect) -> list[OcrLine]:
        """Строки текста на пикселях bgra (size — ширина и высота) куска экрана area; прямоугольники строк —
        в точках экрана. Не вышло — []."""
        ...


class NoOcr:
    """Не Windows: распознавать нечем — строк нет."""

    def recognize(self, bgra: bytes, size: tuple[int, int], area: Rect) -> list[OcrLine]:
        return []


def create_ocr(platform: object) -> OcrBackend:
    if sys.platform == "win32" and getattr(platform, "name", "") == "windows":
        from jarvis.platform.windows.ocr import WindowsOcr

        return WindowsOcr()
    return NoOcr()


def _area(rect: Rect) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _inside(point: tuple[int, int], rect: Rect) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def _center(rect: Rect) -> tuple[int, int]:
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def named_elements(snapshot: UiSnapshot | None) -> int:
    """Сколько в снимке элементов с именем, кроме кнопок заголовка окна и самих строк распознавания."""
    if snapshot is None:
        return 0
    return sum(
        1
        for e in snapshot.elements
        if not e.ocr and e.name.strip() and e.name.strip().casefold() not in WINDOW_CHROME
    )


def wants_ocr(snapshot: UiSnapshot | None, exe: str) -> bool:
    """Распознать ли текст окна: программа «без доступности» или UI Automation видит в нём мало."""
    if exe.casefold() in NO_UIA_EXES:
        return True
    return named_elements(snapshot) < MIN_NAMED


def clip(rect: Rect, bounds: Rect) -> Rect | None:
    """Часть rect внутри bounds; меньше MIN_SIDE по стороне — None."""
    left, top = max(rect[0], bounds[0]), max(rect[1], bounds[1])
    right, bottom = min(rect[2], bounds[2]), min(rect[3], bounds[3])
    if right - left < MIN_SIDE or bottom - top < MIN_SIDE:
        return None
    return left, top, right, bottom


def crop(
    bgra: bytes, size: tuple[int, int], area: Rect, rect: Rect
) -> tuple[bytes, tuple[int, int], Rect] | None:
    """Пиксели куска экрана rect из пикселей area (BGRA, size — их ширина и высота; точек снимка на точку
    экрана может быть не 1): (пиксели, их размер, какой кусок экрана на них на самом деле). Нет пересечения
    или оно мелкое — None."""
    part = clip(rect, area)
    if part is None:
        return None
    width, height = size
    kx = width / max(1, area[2] - area[0])
    ky = height / max(1, area[3] - area[1])
    x0, y0 = round((part[0] - area[0]) * kx), round((part[1] - area[1]) * ky)
    x1, y1 = min(width, round((part[2] - area[0]) * kx)), min(height, round((part[3] - area[1]) * ky))
    if x1 - x0 < 1 or y1 - y0 < 1 or len(bgra) < width * height * 4:
        return None
    stride = width * 4
    rows = [bgra[y * stride + x0 * 4 : y * stride + x1 * 4] for y in range(y0, y1)]
    shown = (
        area[0] + round(x0 / kx),
        area[1] + round(y0 / ky),
        area[0] + round(x1 / kx),
        area[1] + round(y1 / ky),
    )
    return b"".join(rows), (x1 - x0, y1 - y0), shown


INPUT_KINDS = frozenset({"Edit", "ComboBox"})


def _plain(text: str) -> str:
    """Для сравнения подсказки поля с распознанным: без регистра, знаков и «ё»."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.casefold().replace("ё", "е")).split())


def to_elements(
    lines: list[OcrLine], snapshot: UiSnapshot | None, window: Rect
) -> tuple[list[UiElement], bool]:
    """Строки распознавания → элементы снимка окна (id пока пустые, ocr=True): только в окне, без повторов
    элементов UI Automation, сверху вниз, не больше MAX_LINES. Второе — обрезано ли.

    Поле ввода, значение которого UI Automation не отдаёт (пустое), — не повтор: строки в нём (набранный
    текст, черновик) остаются, кроме подсказки, совпадающей с именем поля."""
    window_area = max(1, _area(window))
    known: list[Rect] = []
    blank: list[tuple[Rect, str]] = []  # поля без значения: (где, имя-подсказка)
    for e in snapshot.elements if snapshot is not None else []:
        if e.ocr or e.kind == "Document" or not 0 < _area(e.rect) < DUP_AREA * window_area:
            continue
        if e.kind in INPUT_KINDS and not e.value.strip() and not e.password:
            blank.append((e.rect, _plain(e.name)))
        elif e.name.strip() or (e.value.strip() and not e.password):
            known.append(e.rect)
    out: list[UiElement] = []
    for line in sorted(lines, key=lambda line: (line.rect[1], line.rect[0])):
        text = one_line(line.text, TEXT_LIMIT)
        if not any(ch.isalnum() for ch in text):
            continue
        center = _center(line.rect)
        if not _inside(center, window) or any(_inside(center, rect) for rect in known):
            continue
        if any(_inside(center, rect) and (not hint or _plain(text) == hint) for rect, hint in blank):
            continue
        out.append(UiElement("", text, "Text", line.rect, ocr=True))
    return out[:MAX_LINES], len(out) > MAX_LINES


# похожие буквы: латиница → кириллица (и цифры, которые распознавание ставит вместо букв) и обратно
_TO_CYRILLIC = str.maketrans("AaBCcEeHKkMOoPpTXxYy03", "АаВСсЕеНКкМОоРрТХхУуОЗ")
_TO_LATIN = str.maketrans("АаВСсЕеНКкМОоРрТХхУу", "AaBCcEeHKkMOoPpTXxYy")


def spellings(text: str) -> list[str]:
    """Строка и её варианты, где похожие буквы приведены к кириллице и к латинице: «Oтправить» (латинская O)
    и «0тправить» распознаются как «Отправить» — для проверок, рискованно ли нажатие."""
    return list(dict.fromkeys((text, text.translate(_TO_CYRILLIC), text.translate(_TO_LATIN))))
