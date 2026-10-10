"""Значок Jarvis: рисунок кодом (QPainter), без файлов-картинок. Тот же рисунок — в трее и в .ico при сборке.

Знак — скруглённый квадрат с сине-фиолетовым градиентом и белой буквой «J». Состояния:
- ready — знак без отметки;
- busy — янтарная точка в правом нижнем углу (компоненты запускаются);
- local — зелёный знак (без облака), с 24 px — замок в углу;
- warn — оранжевый треугольник с «!» в углу.
Геометрия задана на сетке 16×16 и привязана к пикселям на 16/24/32, чтобы в трее линии были чёткими.
Отметка отделена от знака прозрачным зазором — читается и на светлой, и на тёмной панели задач.
"""

from functools import cache
from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

IconState = Literal["ready", "busy", "local", "warn"]
STATES: tuple[IconState, ...] = ("ready", "busy", "local", "warn")
SIZES = (16, 20, 24, 32, 48, 64, 256)

# градиент знака: (верх-лево, низ-право)
_TILE = {
    "ready": ("#5C8DFF", "#6B3CF0"),
    "busy": ("#5C8DFF", "#6B3CF0"),
    "warn": ("#5C8DFF", "#6B3CF0"),
    "local": ("#33B38E", "#1B6B5B"),
}
_LETTER = QColor("#FFFFFF")
_BUSY = QColor("#FFB21E")
_WARN = QColor("#FF7A1A")
_LOCK_BG = QColor("#0F4A3E")
_LOCK = QColor("#EFFFF8")
_MARK_DARK = QColor("#1E2230")


class _Grid:
    """Сетка 16×16 → пиксели; на мелких размерах линии привязываются к пиксельной сетке."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.margin = 0.0 if size <= 32 else round(size * 0.035)
        self.k = (size - 2 * self.margin) / 16
        # толщина буквы: на мелких размерах — целое число пикселей (20 px → 2, а не размытые 2,5)
        self.stroke = max(2.0, float(round(2 * self.k))) if size <= 32 else 2 * self.k

    def __call__(self, u: float) -> float:
        return self.margin + u * self.k

    def snap(self, v: float, width: float) -> float:
        """Середина линии толщины width: чётная — на целое, нечётная — на .5 (только мелкие размеры)."""
        if self.size > 32:
            return v
        offset = 0.5 if round(width) % 2 else 0.0
        return round(v - offset) + offset

    def rect(self, x: float, y: float, w: float, h: float) -> QRectF:
        return QRectF(self(x), self(y), w * self.k, h * self.k)


def _tile(p: QPainter, g: _Grid, state: str) -> None:
    rect = g.rect(0, 0, 16, 16)
    radius = 3.9 * g.k
    top, bottom = _TILE[state]
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QColor(top))
    grad.setColorAt(1.0, QColor(bottom))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(rect, radius, radius)
    if g.size >= 32:  # мягкий блик сверху — объём на больших размерах
        shine = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.center().y()))
        shine.setColorAt(0.0, QColor(255, 255, 255, 50))
        shine.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(shine))
        p.drawRoundedRect(rect, radius, radius)


def _letter(p: QPainter, g: _Grid, shift: float) -> None:
    """Буква «J»: верхняя черта, ствол, крюк. shift — сдвиг влево-вверх (в клетках): угол — отметке."""
    w = g.stroke
    bar_y = g.snap(g(4 - shift), w)
    stem_x = g.snap(g(11 - shift), w)
    r = 3 * g.k
    bottom = g.snap(g(12 - shift), w)
    cy = bottom - r
    path = QPainterPath()
    path.moveTo(g(7 - shift), bar_y)
    path.lineTo(stem_x, bar_y)
    path.lineTo(stem_x, cy)
    path.arcTo(QRectF(stem_x - 2 * r, cy - r, 2 * r, 2 * r), 0, -180)
    pen = QPen(_LETTER, w)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)


def _clear(p: QPainter, path: QPainterPath, gap: float) -> None:
    """Прозрачный зазор шириной gap вокруг фигуры отметки."""
    p.save()
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    pen = QPen(QColor(0, 0, 0), 2 * gap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(QColor(0, 0, 0))
    p.drawPath(path)
    p.restore()


def _gap(g: _Grid) -> float:
    return max(1.0, 0.9 * g.k)


def _circle_badge(g: _Grid) -> tuple[QPointF, float]:
    """Центр и радиус круглой отметки: 7 клеток на мелких размерах, чуть меньше на крупных."""
    r = 3.5 if g.size <= 32 else 3.1
    c = 16 - r
    return QPointF(g(c), g(c)), r * g.k


def _busy(p: QPainter, g: _Grid) -> None:
    center, r = _circle_badge(g)
    path = QPainterPath()
    path.addEllipse(center, r, r)
    _clear(p, path, _gap(g))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_BUSY)
    p.drawPath(path)
    if g.size >= 48:  # три точки «работаю»
        dot = r * 0.17
        p.setBrush(_MARK_DARK)
        for i in (-1, 0, 1):
            p.drawEllipse(QPointF(center.x() + i * r * 0.5, center.y()), dot, dot)


def _warn(p: QPainter, g: _Grid) -> None:
    # треугольник 7.5×7 клеток в правом нижнем углу; скругление углов — обводкой того же цвета
    stroke = max(1.0, 0.7 * g.k)
    inset = stroke / 2
    left, right, top, bottom = g(8.5) + inset, g(16) - inset, g(8.6) + inset, g(16) - inset
    path = QPainterPath()
    path.moveTo((left + right) / 2, top)
    path.lineTo(right, bottom)
    path.lineTo(left, bottom)
    path.closeSubpath()
    _clear(p, path, _gap(g) + inset)
    pen = QPen(_WARN, stroke)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(_WARN)
    p.drawPath(path)
    # «!»: черта и точка, по пикселям
    bar_w = max(1.0, round(0.8 * g.k))
    cx = g.snap((left + right) / 2, bar_w)
    h = bottom - top
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_MARK_DARK)
    bar_top = round(top + h * 0.34)
    bar_bottom = round(top + h * 0.68)
    p.drawRect(QRectF(cx - bar_w / 2, bar_top, bar_w, max(1.0, bar_bottom - bar_top)))
    dot_top = round(top + h * 0.78)
    p.drawRect(QRectF(cx - bar_w / 2, dot_top, bar_w, max(1.0, round(h * 0.12))))


def _lock(p: QPainter, g: _Grid) -> None:
    center, r = _circle_badge(g)
    path = QPainterPath()
    path.addEllipse(center, r, r)
    _clear(p, path, _gap(g))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_LOCK_BG)
    p.drawPath(path)
    # замок внутри круга: дужка и корпус
    body = QRectF(center.x() - r * 0.5, center.y() - r * 0.1, r * 1.0, r * 0.72)
    sw = max(1.0, r * 0.2)
    shackle = QRectF(center.x() - r * 0.3, center.y() - r * 0.58, r * 0.6, r * 0.8)
    arc = QPainterPath()
    arc.arcMoveTo(shackle, 0)
    arc.arcTo(shackle, 0, 180)
    arc.lineTo(shackle.left(), body.top())
    arc.moveTo(shackle.right(), shackle.center().y())
    arc.lineTo(shackle.right(), body.top())
    pen = QPen(_LOCK, sw)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(arc)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_LOCK)
    p.drawRoundedRect(body, r * 0.12, r * 0.12)


def render_icon(state: IconState, size: int) -> QImage:
    """Значок state размером size×size (ARGB, прозрачный фон). Работает и без QApplication."""
    if state not in _TILE:
        raise ValueError(f"неизвестное состояние значка: {state!r}")
    if size < 16:
        raise ValueError(f"значок меньше 16 px не рисуется: {size}")
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    g = _Grid(size)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _tile(p, g, state)
    badge = state in ("busy", "warn") or (state == "local" and size >= 24)
    _letter(p, g, shift=1.0 if badge else 0.0)
    if state == "busy":
        _busy(p, g)
    elif state == "warn":
        _warn(p, g)
    elif badge:
        _lock(p, g)
    p.end()
    return img


@cache
def icon(state: IconState) -> QIcon:
    """QIcon со всеми размерами SIZES (нужен QGuiApplication: QPixmap)."""
    result = QIcon()
    for size in SIZES:
        result.addPixmap(QPixmap.fromImage(render_icon(state, size)))
    return result
