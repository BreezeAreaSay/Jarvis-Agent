"""Зрение агента: снимок экрана или окна, с которым он работает, для модели, понимающей картинки.

После каждого действия агент видит весь экран (render_screen): что открылось, всплыло, где курсор, — окно
впереди на нём обведено. Мониторов несколько — монитор окна впереди крупно (с метками и сеткой), остальные
следом мельче, для обзора (render_other): что открыто и всплыло там.
Инструмент screenshot — крупно одно окно (PrintWindow — даже перекрытое; свёрнутое не снять). Поверх — метки
элементов UI Automation окна впереди (e12 у кнопки — «Set-of-Marks»: модель нажимает ui_click по id, а не
гадает координаты) и бледная сетка через каждые 100 точек картинки с подписями по краям — по ней модель
называет точку для click_point. Shot хранит, какой прямоугольник экрана (физические точки) лежит на картинке,
и переводит точку картинки в точку экрана.

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
                "не нажимай."
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
    return _draw(image, rect, elements=elements, title=title, max_side=max_side, focus=focus)


def render_pixels(
    bgra: bytes,
    size: tuple[int, int],
    area: Rect,
    *,
    focus: Rect | None = None,
    elements: list[UiElement] | None = None,
    title: str = "",
    max_side: int = MAX_SIDE,
) -> Shot:
    """Пиксели экрана (BGRA, без PNG — быстрее) части экрана area (left, top, right, bottom: монитор окна
    впереди) → Shot экрана: окно обведено, его элементы подписаны, сетка."""
    from PIL import Image

    image = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    return _draw(image, area, elements=elements, title=title, max_side=max_side, focus=focus)


def render_other(bgra: bytes, size: tuple[int, int], area: Rect, main: Rect) -> Shot:
    """Пиксели другого монитора (area) → Shot для обзора: мельче, без сетки и меток; main — монитор окна
    впереди (главная картинка), от него — «справа», «слева», «над», «под»."""
    from PIL import Image

    image = Image.frombuffer("RGB", size, bgra, "raw", "BGRX", 0, 1)
    shot = _draw(image, area, max_side=OTHER_SIDE, grid=False)
    shot.where = side_of(area, main)
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
) -> Shot:
    from PIL import ImageDraw

    image.thumbnail((max_side, max_side))
    width, height = image.size
    shot = Shot("", width, height, rect, hwnd, title)
    draw = ImageDraw.Draw(image, "RGBA")
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
