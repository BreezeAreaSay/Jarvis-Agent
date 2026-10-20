"""Картинка экрана имитатора из состояния мира (Pillow): рабочий стол, окна снизу вверх, уведомления, панель
задач. Модель с картинками видит осмысленный экран: заголовки окон, кнопки, поля с текстом, списки, пузыри
сообщений — то же, что лежит в мире, а не заглушку.

Кириллице нужен шрифт с ней: Segoe UI / Arial в Windows, DejaVu / Liberation / FreeSans в Linux (или путь
в JARVIS_EVAL_FONT). Не нашёлся — встроенный шрифт Pillow (кириллицы в нём может не быть: отчёт об этом
предупредит, font_ok).

Картинка всего экрана кэшируется по версии мира: агент снимает экран несколько раз подряд (ожидание, пока
окно успокоится), а мир между снимками не меняется.
"""

import io
import os
from functools import cache
from typing import Any

from evals.sim.world import SCREEN, TASKBAR, TITLE_BAR, Rect, Widget, Window, World

FONT_PATHS = (
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)
BOLD = {
    "segoeui.ttf": "segoeuib.ttf",
    "arial.ttf": "arialbd.ttf",
    "Arial.ttf": "Arial Bold.ttf",
    "DejaVuSans.ttf": "DejaVuSans-Bold.ttf",
    "LiberationSans-Regular.ttf": "LiberationSans-Bold.ttf",
    "FreeSans.ttf": "FreeSansBold.ttf",
    "NotoSans-Regular.ttf": "NotoSans-Bold.ttf",
}

INK = (28, 28, 30)
MUTED = (110, 112, 118)
ACCENT = (0, 103, 192)
BORDER = (204, 206, 210)
SELECTED = (221, 236, 250)
DESKTOP = (24, 52, 86)


@cache
def font_path() -> str:
    """Шрифт с кириллицей ("" — не нашёлся)."""
    custom = os.environ.get("JARVIS_EVAL_FONT", "")
    for path in (custom, *FONT_PATHS):
        if path and os.path.isfile(path):
            return path
    return ""


def font_ok() -> bool:
    return bool(font_path())


@cache
def _font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    path = font_path()
    if path and bold:
        folder, name = os.path.split(path)
        heavy = os.path.join(folder, BOLD.get(name, name))
        path = heavy if os.path.isfile(heavy) else path
    if path:
        return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


class Renderer:
    def __init__(self, world: World) -> None:
        self.world = world
        self._cache: tuple[Any, Any] | None = None

    def screen(self) -> Any:
        """Весь экран — картинкой RGB 1920×1080."""
        world = self.world
        with world.lock:
            world.tick()
            key = (world.version, tuple(t.id for t, _ in world.visible_toasts()))
            if self._cache is not None and self._cache[0] == key:
                return self._cache[1]
            image = self._draw_screen()
            self._cache = (key, image)
            return image

    def pixels(self, rect: Rect | None) -> tuple[bytes, int, int]:
        """Кусок экрана — BGRA без сжатия (как platform.screen_pixels)."""
        left, top, right, bottom = rect or SCREEN
        left, top = max(SCREEN[0], left), max(SCREEN[1], top)
        right, bottom = min(SCREEN[2], max(left + 1, right)), min(SCREEN[3], max(top + 1, bottom))
        part = self.screen().crop((left, top, right, bottom))
        return part.tobytes("raw", "BGRX"), part.width, part.height

    def png(self, window: Window | None = None) -> bytes:
        """PNG экрана или одного окна (как PrintWindow: даже перекрытое)."""
        with self.world.lock:
            image = self.screen() if window is None else self._window_image(window)
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    # --- экран ---------------------------------------------------------------------------------------
    def _draw_screen(self) -> Any:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (SCREEN[2], SCREEN[3]), DESKTOP)
        draw = ImageDraw.Draw(image)
        for y in range(0, SCREEN[3], 8):  # лёгкий градиент обоев
            shade = int(18 * y / SCREEN[3])
            draw.rectangle([0, y, SCREEN[2], y + 8], fill=(24 + shade, 52 + shade, 86 + shade))
        for window in self.world.windows:
            if not window.minimized:
                image.paste(self._window_image(window), (window.rect[0], window.rect[1]))
        self._taskbar(draw)
        for toast, rect in self.world.visible_toasts():
            self._toast(draw, toast, rect)
        return image

    def _taskbar(self, draw: Any) -> None:
        top = SCREEN[3] - TASKBAR
        draw.rectangle([0, top, SCREEN[2], SCREEN[3]], fill=(32, 32, 36))
        x = SCREEN[2] // 2 - (len(self.world.taskbar_buttons()) + 1) * 26 + 8
        for dx, dy in ((0, 0), (14, 0), (0, 14), (14, 14)):  # «Пуск»
            draw.rectangle([x + dx, top + 10 + dy, x + dx + 11, top + 21 + dy], fill=(80, 170, 255))
        front = self.world.foreground
        for window, rect in self.world.taskbar_buttons():
            fill = (62, 62, 70) if window is front else (44, 44, 50)
            draw.rounded_rectangle(rect, radius=6, fill=fill)
            letter = (window.app.name or "?")[:1].upper()
            draw.rounded_rectangle(
                [rect[0] + 10, rect[1] + 8, rect[2] - 10, rect[3] - 8],
                radius=4,
                fill=_app_color(window.app.name),
            )
            _text(draw, (rect[0] + 22, rect[1] + 20), letter, 14, (255, 255, 255), bold=True, anchor="mm")
            if window is front:
                draw.rectangle([rect[0] + 14, rect[3] - 3, rect[2] - 14, rect[3] - 1], fill=(80, 170, 255))
        _text(draw, (SCREEN[2] - 70, top + 12), "12:30", 13, (240, 240, 240))
        _text(draw, (SCREEN[2] - 84, top + 28), "20.10.2026", 11, (200, 200, 200))

    def _toast(self, draw: Any, toast: Any, rect: Rect) -> None:
        draw.rounded_rectangle(rect, radius=8, fill=(43, 43, 48), outline=(70, 70, 78))
        _text(draw, (rect[0] + 16, rect[1] + 12), toast.app, 12, (190, 190, 196))
        _text(
            draw,
            (rect[0] + 16, rect[1] + 34),
            _fit(toast.title, 15, rect[2] - rect[0] - 32, True),
            15,
            (255, 255, 255),
            bold=True,
        )
        _text(
            draw,
            (rect[0] + 16, rect[1] + 60),
            _fit(toast.body, 14, rect[2] - rect[0] - 32),
            14,
            (230, 230, 234),
        )

    # --- окно ----------------------------------------------------------------------------------------
    def _window_image(self, window: Window) -> Any:
        from PIL import Image, ImageDraw

        width, height = window.size
        image = Image.new("RGB", (width, height), window.app.background)
        draw = ImageDraw.Draw(image)
        dark = sum(window.app.background) < 200
        bar = (32, 32, 36) if dark else (243, 243, 243)
        ink = (235, 235, 240) if dark else INK
        draw.rectangle([0, 0, width, TITLE_BAR], fill=bar)
        draw.rounded_rectangle([10, 8, 26, 24], radius=3, fill=_app_color(window.app.name))
        _text(draw, (36, 8), _fit(window.title, 13, width - 200), 13, ink)
        for i, glyph in enumerate(("—", "□", "✕")):
            _text(draw, (width - 128 + i * 44, 16), glyph, 13, ink, anchor="mm")
        for widget in window.widgets():
            _widget(draw, window, widget, dark)
        draw.rectangle([0, 0, width - 1, height - 1], outline=(90, 90, 98) if dark else BORDER)
        return image


# --- элементы -------------------------------------------------------------------------------------------
def _widget(draw: Any, window: Window, w: Widget, dark: bool) -> None:
    left, top, right, bottom = w.rect
    if right - left < 2 or bottom - top < 2:
        return
    ink = (235, 235, 240) if dark else INK
    muted = (160, 160, 168) if dark else MUTED
    color = w.color
    focused = window.focus == w.key and w.editable
    kind, style = w.kind, w.style
    if kind == "Pane":
        draw.rectangle(w.rect, fill=color or ((24, 24, 28) if dark else (247, 247, 249)))
        return
    if style in ("bubble-in", "bubble-out"):
        fill = color or ((239, 253, 222) if style == "bubble-out" else (255, 255, 255))
        draw.rounded_rectangle(w.rect, radius=12, fill=fill)
        lines = _wrap(w.text, w.size, right - left - 24)
        for i, line in enumerate(lines):
            _text(draw, (left + 12, top + 8 + i * (w.size + 6)), line, w.size, INK)
        if w.value:
            _text(draw, (right - 12, bottom - 8), w.value, 11, MUTED, anchor="rs")
        return
    if style in ("on", "off"):  # переключатель
        on = style == "on"
        knob = 10
        track = [left, top + 4, left + 44, bottom - 4]
        draw.rounded_rectangle(
            track, radius=12, fill=ACCENT if on else None, outline=ACCENT if on else muted, width=2
        )
        cx = track[2] - 12 if on else track[0] + 12
        cy = (track[1] + track[3]) // 2
        draw.ellipse(
            [cx - knob // 2 - 1, cy - knob // 2 - 1, cx + knob // 2, cy + knob // 2],
            fill=(255, 255, 255) if on else muted,
        )
        if w.text:
            _text(draw, (left + 56, (top + bottom) // 2), w.text, w.size, ink, anchor="lm")
        return
    if style == "card":  # обложка плейлиста или файла
        draw.rounded_rectangle([left, top, right, bottom - 36], radius=8, fill=color or (90, 90, 120))
        if w.icon:
            _icon(draw, w.icon, ((left + right) // 2, (top + bottom - 36) // 2), 40, (255, 255, 255))
        _text(
            draw,
            (left + 4, bottom - 30),
            _fit(w.text, w.size, right - left - 8, True),
            w.size,
            ink,
            bold=True,
        )
        return
    if kind in ("Button", "SplitButton"):
        if style == "primary":
            fill, fg, outline = color or ACCENT, (255, 255, 255), None
        elif style == "danger":
            fill, fg, outline = (196, 43, 28), (255, 255, 255), None
        elif style == "flat":
            fill, fg, outline = None, ink, None
        else:
            fill, fg, outline = (
                color or ((52, 52, 58) if dark else (251, 251, 252)),
                ink,
                ((80, 80, 88) if dark else BORDER),
            )
        if not w.enabled:
            fg = muted
        draw.rounded_rectangle(w.rect, radius=min(6, (bottom - top) // 2), fill=fill, outline=outline)
        center = ((left + right) // 2, (top + bottom) // 2)
        if w.icon and not w.text:
            _icon(draw, w.icon, center, min(right - left, bottom - top) - 14, fg)
        elif w.icon:
            _icon(draw, w.icon, (left + 18, center[1]), 16, fg)
            _text(
                draw, (left + 34, center[1]), _fit(w.text, w.size, right - left - 40), w.size, fg, anchor="lm"
            )
        else:
            _text(
                draw,
                center,
                _fit(w.text, w.size, right - left - 12),
                w.size,
                fg,
                anchor="mm",
                bold=style == "primary",
            )
        return
    if kind in ("Edit", "ComboBox", "Document"):
        draw.rectangle(
            w.rect,
            fill=(44, 44, 50) if dark else (255, 255, 255),
            outline=ACCENT if focused else ((80, 80, 88) if dark else BORDER),
        )
        if focused:
            draw.rectangle([left, bottom - 2, right, bottom], fill=ACCENT)
        value = window.values.get(w.key, w.value)
        shown = "•" * len(value) if w.password else value
        x0 = left + (34 if w.icon else 10)
        if w.icon:
            _icon(draw, w.icon, (left + 18, (top + bottom) // 2), 14, muted)
        if kind == "Document" or (w.multiline and bottom - top > 60):
            lines = [
                ln
                for part in (shown or "").split("\n")
                for ln in (_wrap(part, w.size, right - left - 20) or [""])
            ]
            if not shown and w.placeholder:
                _text(draw, (x0, top + 10), w.placeholder, w.size, muted)
            for i, line in enumerate(lines[: max(1, (bottom - top - 12) // (w.size + 6))]):
                _text(draw, (x0, top + 10 + i * (w.size + 6)), line, w.size, ink)
            return
        middle = (top + bottom) // 2
        if shown:
            _text(
                draw,
                (x0, middle),
                _fit(shown.replace("\n", " ⏎ "), w.size, right - x0 - 10, tail=True),
                w.size,
                ink,
                anchor="lm",
            )
        elif w.placeholder:
            _text(
                draw, (x0, middle), _fit(w.placeholder, w.size, right - x0 - 10), w.size, muted, anchor="lm"
            )
        if kind == "ComboBox":
            _text(draw, (right - 16, middle), "⌄", w.size, muted, anchor="mm")
        return
    if kind in ("ListItem", "DataItem", "TreeItem", "TabItem", "MenuItem", "Hyperlink"):
        if style == "selected":
            draw.rounded_rectangle(w.rect, radius=4, fill=color or ((60, 60, 70) if dark else SELECTED))
        x = left + 10
        middle = (top + bottom) // 2
        if w.icon == "avatar":
            r = min(24, (bottom - top) // 2 - 6)
            cx, cy = left + 14 + r, middle
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_app_color(w.text))
            initials = "".join(p[:1] for p in w.text.replace("(", " ").split()[:2]).upper()
            _text(draw, (cx, cy), initials, 15, (255, 255, 255), bold=True, anchor="mm")
            x = cx + r + 12
        elif w.icon:
            _icon(draw, w.icon, (left + 18, middle), 16, ACCENT if w.icon == "folder" else muted)
            x = left + 36
        if w.value and bottom - top >= 40:
            _text(
                draw,
                (x, middle - 11),
                _fit(w.text, w.size, right - x - 60, True),
                w.size,
                ink,
                anchor="lm",
                bold=True,
            )
            _text(
                draw,
                (x, middle + 12),
                _fit(w.value, w.size - 2, right - x - 12),
                w.size - 2,
                muted,
                anchor="lm",
            )
        else:
            _text(
                draw,
                (x, middle),
                _fit(w.text, w.size, right - x - 8),
                w.size,
                ACCENT if kind == "Hyperlink" else ink,
                anchor="lm",
                bold=style == "bold",
            )
            if w.value:
                _text(draw, (right - 10, middle), w.value, w.size - 2, muted, anchor="rm")
        return
    # Text, Image и прочее — подпись (у картинки со значком — только значок: имя — для UI Automation)
    if w.icon and (kind == "Image" or not w.text):
        _icon(
            draw,
            w.icon,
            ((left + right) // 2, (top + bottom) // 2),
            min(right - left, bottom - top) - 4,
            color or muted,
        )
        return
    fill = color or (muted if style == "muted" else ink)
    bold = style in ("bold", "header")
    lines = (
        _wrap(w.text, w.size, right - left)
        if bottom - top >= 2 * w.size + 6
        else [_fit(w.text, w.size, right - left, bold)]
    )
    for i, line in enumerate(lines[: max(1, (bottom - top) // (w.size + 6))]):
        _text(draw, (left, top + i * (w.size + 6)), line, w.size, fill, bold=bold)


def _icon(draw: Any, name: str, center: tuple[int, int], size: int, color: Any) -> None:
    cx, cy = center
    s = max(6, size) / 2
    if name == "play":
        draw.polygon(
            [(cx - s * 0.6, cy - s * 0.8), (cx - s * 0.6, cy + s * 0.8), (cx + s * 0.8, cy)], fill=color
        )
    elif name == "pause":
        draw.rectangle([cx - s * 0.6, cy - s * 0.7, cx - s * 0.2, cy + s * 0.7], fill=color)
        draw.rectangle([cx + s * 0.2, cy - s * 0.7, cx + s * 0.6, cy + s * 0.7], fill=color)
    elif name in ("next", "prev"):
        k = 1 if name == "next" else -1
        draw.polygon(
            [(cx - k * s * 0.7, cy - s * 0.6), (cx - k * s * 0.7, cy + s * 0.6), (cx + k * s * 0.3, cy)],
            fill=color,
        )
        draw.rectangle([cx + k * s * 0.3 - 2, cy - s * 0.6, cx + k * s * 0.3 + 2, cy + s * 0.6], fill=color)
    elif name == "search":
        r = s * 0.55
        draw.ellipse([cx - r - 2, cy - r - 2, cx + r - 2, cy + r - 2], outline=color, width=2)
        draw.line([(cx + r * 0.5, cy + r * 0.5), (cx + s * 0.9, cy + s * 0.9)], fill=color, width=2)
    elif name == "menu":
        for dy in (-s * 0.5, 0, s * 0.5):
            draw.line([(cx - s * 0.7, cy + dy), (cx + s * 0.7, cy + dy)], fill=color, width=2)
    elif name == "send":
        draw.polygon(
            [
                (cx - s * 0.8, cy - s * 0.7),
                (cx + s * 0.9, cy),
                (cx - s * 0.8, cy + s * 0.7),
                (cx - s * 0.4, cy),
            ],
            fill=color,
        )
    elif name == "trash":
        draw.rectangle([cx - s * 0.55, cy - s * 0.45, cx + s * 0.55, cy + s * 0.8], outline=color, width=2)
        draw.line([(cx - s * 0.8, cy - s * 0.6), (cx + s * 0.8, cy - s * 0.6)], fill=color, width=2)
        draw.line([(cx - s * 0.2, cy - s * 0.85), (cx + s * 0.2, cy - s * 0.85)], fill=color, width=2)
    elif name == "folder":
        draw.polygon(
            [
                (cx - s, cy - s * 0.6),
                (cx - s * 0.2, cy - s * 0.6),
                (cx, cy - s * 0.35),
                (cx + s, cy - s * 0.35),
                (cx + s, cy + s * 0.7),
                (cx - s, cy + s * 0.7),
            ],
            fill=(232, 182, 60),
        )
    elif name == "file":
        draw.polygon(
            [
                (cx - s * 0.6, cy - s),
                (cx + s * 0.3, cy - s),
                (cx + s * 0.7, cy - s * 0.6),
                (cx + s * 0.7, cy + s),
                (cx - s * 0.6, cy + s),
            ],
            fill=(255, 255, 255),
            outline=color,
        )
        for dy in (-0.2, 0.15, 0.5):
            draw.line([(cx - s * 0.35, cy + s * dy), (cx + s * 0.45, cy + s * dy)], fill=color, width=1)
    elif name == "close":
        draw.line([(cx - s * 0.6, cy - s * 0.6), (cx + s * 0.6, cy + s * 0.6)], fill=color, width=2)
        draw.line([(cx - s * 0.6, cy + s * 0.6), (cx + s * 0.6, cy - s * 0.6)], fill=color, width=2)
    elif name == "spinner":
        draw.arc([cx - s, cy - s, cx + s, cy + s], start=30, end=300, fill=color, width=3)
    elif name == "gear":
        draw.ellipse([cx - s * 0.7, cy - s * 0.7, cx + s * 0.7, cy + s * 0.7], outline=color, width=3)
        draw.ellipse([cx - s * 0.25, cy - s * 0.25, cx + s * 0.25, cy + s * 0.25], fill=color)
    else:
        draw.ellipse([cx - s * 0.6, cy - s * 0.6, cx + s * 0.6, cy + s * 0.6], outline=color, width=2)


def _app_color(name: str) -> tuple[int, int, int]:
    palette = (
        (42, 171, 238),
        (30, 185, 84),
        (232, 93, 4),
        (124, 77, 255),
        (0, 120, 212),
        (214, 51, 132),
        (0, 150, 136),
        (255, 160, 0),
        (96, 125, 139),
        (229, 57, 53),
    )
    return palette[sum(map(ord, name or "?")) % len(palette)]


def _text(
    draw: Any,
    xy: tuple[float, float],
    text: str,
    size: int,
    fill: Any,
    *,
    bold: bool = False,
    anchor: str = "la",
) -> None:
    if text:
        draw.text(xy, text, font=_font(size, bold), fill=fill, anchor=anchor)


def _width(text: str, size: int, bold: bool = False) -> float:
    return float(_font(size, bold).getlength(text))


def _fit(text: str, size: int, width: float, bold: bool = False, tail: bool = False) -> str:
    """Текст в ширину: не влез — с многоточием (tail — видно конец: поле ввода с длинным текстом)."""
    text = " ".join((text or "").split()) if not tail else (text or "")
    if _width(text, size, bold) <= width:
        return text
    while text and _width(("…" + text) if tail else (text + "…"), size, bold) > width:
        text = text[1:] if tail else text[:-1]
    return ("…" + text) if tail else (text + "…")


def _wrap(text: str, size: int, width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if _width(candidate, size) <= width or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines
