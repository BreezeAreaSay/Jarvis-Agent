"""Зрение агента: снимок экрана или окна, с которым он работает, для модели, понимающей картинки.

После каждого действия агент видит весь экран (render_screen): что открылось, всплыло, где курсор, — окно
впереди на нём обведено. Мониторов несколько — монитор окна впереди крупно (с метками и сеткой), остальные
следом мельче, для обзора (render_other): что открыто и всплыло там.
Инструмент screenshot — крупно одно окно (PrintWindow — даже перекрытое; свёрнутое не снять), zoom — лупа:
кусок экрана в полном разрешении, мелкий — с увеличением (render_zoom). Поверх — метки
элементов UI Automation окна впереди (e12 у кнопки — «Set-of-Marks»: модель нажимает ui_click по id, а не
гадает координаты) и бледная сетка через каждые 100 точек картинки с подписями по краям — по ней модель
называет точку для click_point. Что изменилось с прошлой картинки того же монитора — обведено зелёным
(changes: сравнение уменьшенных серых копий клетками; мелочь вроде часов и мигающего курсора не в счёт).
Shot хранит, какой прямоугольник экрана (физические точки) лежит на картинке, и переводит точку картинки
в точку экрана.

Работает в потоке ввода-вывода (ctx.run_io): Pillow и сжатие JPEG не держат цикл событий.
"""

import base64
import io
import logging
import re
from dataclasses import dataclass
from typing import Any

from jarvis.ai.uia import UiElement

log = logging.getLogger(__name__)

MAX_SIDE = 1280  # больше по стороне не отдаём: дорого и не нужно
OTHER_SIDE = 960  # другие мониторы — только обзор: мельче
ZOOM_SIDE = 1024  # лупа: мелкий кусок экрана увеличиваем до этой стороны…
ZOOM_MAX = 3.0  # …но не больше чем втрое
ZOOM_CAPTION = "Лупа"  # подпись картинки лупы начинается так (_prune агента её узнаёт)
SIG_COLS = 64  # что изменилось: экран — клетками 64 в ширину (на 1920 точек — по 30)
SIG_DIFF = 16  # клетка изменилась, если её средняя яркость сдвинулась больше чем на столько (из 255)
SIG_MOST = 0.5  # изменилось больше половины клеток — «почти весь экран», без рамок
MAX_CHANGES = 6  # рамок изменений на картинке
GRID = 100  # шаг сетки в точках картинки
JPEG_QUALITY = 78

Rect = tuple[int, int, int, int]


@dataclass
class Shot:
    """Снимок окна для модели: картинка width×height ↔ прямоугольник экрана rect."""

    url: str  # data:image/jpeg;base64,…
    width: int
    height: int
    rect: Rect  # left, top, right, bottom — физические точки экрана
    hwnd: int = 0  # снимок окна; 0 — снимок экрана
    title: str = ""  # заголовок окна (у снимка экрана — окна впереди)
    where: str = ""  # другой монитор, только обзор: где он («справа от основного»)
    zoom: float = 0.0  # лупа: во сколько раз крупнее экрана
    sig: bytes = b""  # уменьшенная серая копия (клетки SIG_COLS в ширину) — сравнить со следующей картинкой
    changes: int = -1  # сколько областей изменилось с прошлой картинки; -1 — не сравнивали, -2 — почти всё

    def _changes_text(self) -> str:
        if self.changes == -2:
            return " С прошлого снимка экран изменился почти весь."
        if self.changes == 0:
            return " С прошлого снимка на экране ничего не изменилось."
        if self.changes > 0:
            return f" Зелёным обведено, что изменилось с прошлого снимка ({self.changes})."
        return ""

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        sx = left + round(x * (right - left) / max(1, self.width))
        sy = top + round(y * (bottom - top) / max(1, self.height))
        return sx, sy

    def to_image(self, sx: float, sy: float) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        x = round((sx - left) * self.width / max(1, right - left))
        y = round((sy - top) * self.height / max(1, bottom - top))
        return x, y

    def contains(self, x: float, y: float) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def caption(self) -> str:
        if self.zoom:
            left, top, right, bottom = self.rect
            return (
                f"{ZOOM_CAPTION}: кусок экрана {right - left}×{bottom - top} точек, ×{self.zoom:.3g} — "
                f"картинка {self.width}×{self.height}. Метки e… — элементы окна впереди. Сетка — через "
                f"каждые {GRID} точек; click_point принимает координаты этой картинки, пока не придёт новый "
                "снимок экрана."
            )
        if self.where:
            return (
                f"Другой монитор ({self.where}) — {self.width}×{self.height} точек, только для обзора: "
                "click_point и метки — по картинке монитора с окном впереди. Работать здесь — переключись "
                "на окно этого монитора (focus_window), и он станет главной картинкой."
            )
        if not self.hwnd:
            front = f"; окно впереди «{self.title}» обведено жёлтым" if self.title else ""
            return (
                f"Снимок экрана — {self.width}×{self.height} точек{front}. Метки e… — id элементов окна "
                f"впереди (жми ui_click по id). Сетка — через каждые {GRID} точек; click_point принимает "
                "координаты этой картинки. Если на снимке видны панель и курсор агента Jarvis — это ты, их "
                f"не нажимай.{self._changes_text()}"
            )
        return (
            f"Снимок окна «{self.title}» — {self.width}×{self.height} точек. Метки e… — id элементов "
            f"из снимка окна (жми ui_click по id). Сетка — через каждые {GRID} точек; click_point принимает "
            "координаты этой картинки."
        )


# модели, которые точно не понимают картинки (для настройки «Авто»)
TEXT_ONLY = re.compile(
    r"deepseek|yandexgpt|gpt-oss|coder|codestral|devstral|ministral|mixtral|llama-?3(?:\.[01])?(?:[:\-]|$)|"
    r"qwen\d?(?:\.\d)?:\d|qwen3-\d+b|phi-?[34](?!.*vision)|minimax-m1|minimax-text|abab|ernie|yi-",
    re.I,
)
VISION_HINT = re.compile(
    r"vision|-vl|\bvl\b|vl-|qvq|pixtral|llava|minicpm-v|gemma-?3|moondream|glm-\d(?:\.\d)?v", re.I
)
CLI_VISION = {"chatgpt": True, "claude_code": True, "gemini_cli": True, "qwen_code": False}


def sees_images(provider: str, model: str, setting: str | bool) -> bool:
    """Понимает ли модель картинки: «on»/«off» — как сказал человек, «auto» — по провайдеру и модели."""
    if setting is True or setting == "on":
        return True
    if setting is False or setting == "off":
        return False
    model = (model or "").strip()
    if VISION_HINT.search(model):
        return True
    if provider in CLI_VISION:
        return CLI_VISION[provider]
    return not TEXT_ONLY.search(model)


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow без FreeType: только мелкий растровый шрифт
        return ImageFont.load_default()


def render(
    png: bytes,
    rect: Rect,
    *,
    elements: list[UiElement] | None = None,
    hwnd: int = 0,
    title: str = "",
    max_side: int = MAX_SIDE,
    grid: bool = True,
) -> Shot:
    """PNG окна → Shot: уменьшить, подписать элементы (их rect — в точках экрана) и наложить сетку."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    return _draw(image, rect, elements=elements, hwnd=hwnd, title=title, max_side=max_side, grid=grid)


def screen_part(screen: tuple[int, int, int, int], monitor: Rect | None) -> Rect:
    """Какую часть всех мониторов (screen — left, top, width, height) показать, в точках экрана: монитор окна
    впереди (monitor — left, top, right, bottom), если он внутри экрана, иначе весь экран."""
    left, top, width, height = screen
    whole = (left, top, left + width, top + height)
    if monitor is None:
        return whole
    rect = (max(left, monitor[0]), max(top, monitor[1]), min(whole[2], monitor[2]), min(whole[3], monitor[3]))
    return rect if rect[2] - rect[0] >= 100 and rect[3] - rect[1] >= 100 else whole


def render_screen(
    png: bytes,
    screen: tuple[int, int, int, int],
    *,
    monitor: Rect | None = None,
    focus: Rect | None = None,
    elements: list[UiElement] | None = None,
    title: str = "",
    max_side: int = MAX_SIDE,
    previous: bytes | None = None,
) -> Shot:
    """PNG всего экрана (виртуального: screen — left, top, width, height) → Shot экрана: монитор окна впереди
    (monitor), окно обведено (focus), его элементы подписаны, сетка."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    rect = screen_part(screen, monitor)
    left, top, width, height = screen
    kx, ky = image.width / max(1, width), image.height / max(1, height)  # точек картинки на точку экрана
    box = (
        round((rect[0] - left) * kx),
        round((rect[1] - top) * ky),
        round((rect[2] - left) * kx),
        round((rect[3] - top) * ky),
    )
    if box != (0, 0, image.width, image.height):
        image = image.crop(box)
    return _draw(
        image, rect, elements=elements, title=title, max_side=max_side, focus=focus, previous=previous
    )


def render_pixels(
    bgra: bytes,
    size: tuple[int, int],
    area: Rect,
    *,
    focus: Rect | None = None,
    elements: list[UiElement] | None = None,
    title: str = "",
    max_side: int = MAX_SIDE,
    previous: bytes | None = None,
) -> Shot:
    """Пиксели экрана (BGRA, без PNG — быстрее) части экрана area (left, top, right, bottom: монитор окна
    впереди) → Shot экрана: окно обведено, его элементы подписаны, сетка; previous — sig прошлой картинки
    того же монитора: что изменилось — обвести."""
    from PIL import Image

    image = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    return _draw(
        image, area, elements=elements, title=title, max_side=max_side, focus=focus, previous=previous
    )


def render_other(bgra: bytes, size: tuple[int, int], area: Rect, main: Rect) -> Shot:
    """Пиксели другого монитора (area) → Shot для обзора: мельче, без сетки и меток; main — монитор окна
    впереди (главная картинка), от него — «справа», «слева», «над», «под»."""
    from PIL import Image

    image = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    shot = _draw(image, area, max_side=OTHER_SIDE, grid=False)
    shot.where = side_of(area, main)
    return shot


def render_zoom(
    bgra: bytes, size: tuple[int, int], area: Rect, *, elements: list[UiElement] | None = None
) -> Shot:
    """Пиксели куска экрана area → Shot лупы: в полном разрешении, мелкий кусок — крупнее (до ZOOM_SIDE,
    не больше ZOOM_MAX раз), метки элементов и сетка."""
    from PIL import Image

    image = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    scale = min(ZOOM_MAX, ZOOM_SIDE / max(1, image.width, image.height))
    if scale > 1.05:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS
        )
    shot = _draw(image, area, elements=elements)
    shot.zoom = round(shot.width / max(1, area[2] - area[0]), 2)
    return shot


def side_of(area: Rect, main: Rect) -> str:
    """Где монитор area от главного main — словами."""
    dx = (area[0] + area[2]) - (main[0] + main[2])
    dy = (area[1] + area[3]) - (main[1] + main[3])
    if abs(dx) >= abs(dy):
        return "справа от основного" if dx > 0 else "слева от основного"
    return "под основным" if dy > 0 else "над основным"


def _draw(
    image: Any,
    rect: Rect,
    *,
    elements: list[UiElement] | None = None,
    hwnd: int = 0,
    title: str = "",
    max_side: int = MAX_SIDE,
    grid: bool = True,
    focus: Rect | None = None,
    previous: bytes | None = None,
) -> Shot:
    from PIL import ImageDraw

    image.thumbnail((max_side, max_side))
    width, height = image.size
    shot = Shot("", width, height, rect, hwnd, title)
    boxes: list[tuple[int, int, int, int]] = []
    if previous is not None:  # до рамок и меток: сравнивается сам экран (b"" — сравнивать пока не с чем)
        shot.sig = signature(image)
        if previous and len(previous) == len(shot.sig):
            found = changes(previous, shot.sig, SIG_COLS)
            shot.changes, boxes = (-2, []) if found is None else (len(found), found)
    draw = ImageDraw.Draw(image, "RGBA")
    if boxes:
        cell_w, cell_h = width / SIG_COLS, height / max(1, len(shot.sig) // SIG_COLS)
        for c0, r0, c1, r1 in boxes:
            box = [round(c0 * cell_w), round(r0 * cell_h), round(c1 * cell_w) - 1, round(r1 * cell_h) - 1]
            draw.rectangle(box, outline=(0, 220, 90, 235), width=2)
    if focus is not None:  # окно впереди на снимке экрана
        left, top = shot.to_image(focus[0], focus[1])
        right, bottom = shot.to_image(focus[2], focus[3])
        if right > left and bottom > top:
            draw.rectangle([left, top, right - 1, bottom - 1], outline=(255, 214, 0, 230), width=3)
    if grid:
        small = _font(11)
        for x in range(GRID, width, GRID):
            draw.line([(x, 0), (x, height)], fill=(255, 64, 160, 46), width=1)
            draw.text((x + 2, 1), str(x), fill=(255, 64, 160, 200), font=small)
        for y in range(GRID, height, GRID):
            draw.line([(0, y), (width, y)], fill=(255, 64, 160, 46), width=1)
            draw.text((2, y + 1), str(y), fill=(255, 64, 160, 200), font=small)
    label_font = _font(12)
    for element in elements or []:
        left, top = shot.to_image(element.rect[0], element.rect[1])
        right, bottom = shot.to_image(element.rect[2], element.rect[3])
        outside = right <= 0 or bottom <= 0 or left >= width or top >= height
        if outside or right - left < 4 or bottom - top < 4:
            continue
        draw.rectangle([left, top, right, bottom], outline=(0, 200, 255, 150), width=1)
        box = draw.textbbox((0, 0), element.id, font=label_font)
        w, h = box[2] - box[0] + 4, box[3] - box[1] + 2
        x = max(0, left)
        y = top - h if top - h >= 0 else max(0, top)  # метка над элементом, у верхнего края — внутри
        draw.rectangle([x, y, x + w, y + h], fill=(0, 120, 200, 220))
        draw.text((x + 2, y - box[1] + 1), element.id, fill=(255, 255, 255, 255), font=label_font)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY)
    shot.url = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    return shot


def signature(image: Any) -> bytes:
    """Уменьшенная серая копия картинки: SIG_COLS клеток в ширину (средняя яркость клетки)."""
    from PIL import Image

    rows = max(1, round(SIG_COLS * image.height / max(1, image.width)))
    return image.convert("L").resize((SIG_COLS, rows), Image.Resampling.BOX).tobytes()


def pixels_signature(bgra: bytes, size: tuple[int, int]) -> bytes:
    """sig пикселей экрана (BGRA) — без картинки для модели: окно ещё меняется?"""
    from PIL import Image

    return signature(Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1))


def hot_cells(previous: bytes, current: bytes) -> set[int]:
    """Клетки, которые изменились между двумя sig одного размера."""
    return {i for i in range(len(current)) if abs(current[i] - previous[i]) > SIG_DIFF}


def changes(previous: bytes, current: bytes, cols: int) -> list[tuple[int, int, int, int]] | None:
    """Что изменилось между двумя sig: области (клетки: c0, r0, c1, r1 — правый и нижний край не включены),
    крупные — первыми, не больше MAX_CHANGES; изменилось почти всё — None. Одиночная клетка — мелочь (часы,
    мигающий курсор) — не в счёт. sig — одного размера."""
    rows = len(current) // cols
    hot = hot_cells(previous, current)
    if len(hot) > SIG_MOST * len(current):
        return None
    boxes: list[tuple[int, int, int, int, int]] = []
    seen: set[int] = set()
    for start in hot:
        if start in seen:
            continue
        seen.add(start)
        stack, cells = [start], []
        while stack:  # соседние клетки (и через одну — рядом стоящие куски одной области) — вместе
            i = stack.pop()
            cells.append(i)
            r, c = divmod(i, cols)
            for dr in (-2, -1, 0, 1, 2):
                for dc in (-2, -1, 0, 1, 2):
                    rr, cc = r + dr, c + dc
                    j = rr * cols + cc
                    if 0 <= rr < rows and 0 <= cc < cols and j in hot and j not in seen:
                        seen.add(j)
                        stack.append(j)
        if len(cells) < 2:
            continue
        rs, cs = [i // cols for i in cells], [i % cols for i in cells]
        boxes.append((len(cells), min(cs), min(rs), max(cs) + 1, max(rs) + 1))
    boxes.sort(reverse=True)
    return [box[1:] for box in boxes[:MAX_CHANGES]]


def message(shot: Shot, note: str = "", others: list[Shot] | None = None) -> dict[str, Any]:
    """Сообщение модели со снимком (role user: картинки в ответах инструментов понимают не все провайдеры);
    others — другие мониторы, следом за главной картинкой (одним сообщением: старые снимки скрываются
    вместе)."""
    if others:
        note = f"Другие мониторы — следующими картинками ({len(others)}). {note}".strip()
    content: list[dict[str, Any]] = []
    for i, part in enumerate([shot, *(others or [])]):
        text = part.caption() + (f" {note}" if note and i == 0 else "")
        content.append({"type": "text", "text": text})
        content.append({"type": "image_url", "image_url": {"url": part.url}})
    return {"role": "user", "content": content}
