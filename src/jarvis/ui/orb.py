"""Сфера Jarvis: отрисовка (paint_orb), сфера в окне (OrbWidget), сфера на рабочем столе (OrbWindow).

Числа (состояние, энергия, доли, языки ауры, искры) — в jarvis.ui.logic.OrbModel; здесь только QPainter:
QtCore/QtGui/QtWidgets, растр с антиалиасингом, без OpenGL и шейдеров. Таймер кадров (~60 к/с) работает,
только пока модель говорит «ещё анимирую»; затем один последний кадр и таймер стоп — в покое 0 % CPU.

Кадр дешёвый (64 px — ~0,7 мс на Linux offscreen): тело с бликом, ободок и ореол — готовые картинки (кэш по
размеру и цвету); доли, искры и ядро — один мягкий спрайт нужного цвета без поворотов (быстрый путь растра);
аура — один контур и один градиент в маленьком буфере, растянутом сглаживанием (мягкие края огня).
"""

import functools
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QConicalGradient,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from jarvis.config import UiConfig
from jarvis.ui import theme
from jarvis.ui.logic import (
    ORB_CORE_LIGHT,
    ORB_EXTENT,
    ORB_FLAME_DROP,
    ORB_FRAME_MS,
    RGB,
    OrbFlame,
    OrbFrame,
    OrbModel,
)
from pc import settings

log = logging.getLogger("jarvis")

_SCREEN = QPainter.CompositionMode.CompositionMode_Screen
_OVER = QPainter.CompositionMode.CompositionMode_SourceOver
_ARGB = QImage.Format.Format_ARGB32_Premultiplied
_SPRITE_PX = 64
_RIM_OUT = 1.14  # ободок светит чуть наружу


def _c(rgb: RGB, alpha: float = 1.0) -> QColor:
    return QColor(rgb[0], rgb[1], rgb[2], max(0, min(255, round(alpha * 255))))


def _q(rgb: RGB) -> RGB:
    """Цвет для ключа кэша: шаг 4 (глазу не заметно, а переходы оттенка не плодят картинки)."""
    return ((rgb[0] >> 2) << 2, (rgb[1] >> 2) << 2, (rgb[2] >> 2) << 2)


def _mix(a: RGB, b: RGB, k: float) -> QColor:
    return QColor(
        round(a[0] + (b[0] - a[0]) * k), round(a[1] + (b[1] - a[1]) * k), round(a[2] + (b[2] - a[2]) * k)
    )


def _canvas(px: int) -> tuple[QImage, QPainter]:
    img = QImage(px, px, _ARGB)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    return img, p


# --- готовые картинки (кэш) ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=160)
def _blob(rgb: RGB) -> QImage:
    """Мягкое круглое свечение цвета rgb: непрозрачное в центре, к краю — в ноль (64×64)."""
    img, p = _canvas(_SPRITE_PX)
    h = _SPRITE_PX / 2
    g = QRadialGradient(QPointF(h, h), h)
    g.setColorAt(0.0, _c(rgb, 1.0))
    g.setColorAt(0.28, _c(rgb, 0.62))
    g.setColorAt(0.6, _c(rgb, 0.2))
    g.setColorAt(1.0, _c(rgb, 0.0))
    p.fillRect(img.rect(), g)
    p.end()
    return img


@functools.lru_cache(maxsize=48)
def _body_image(px: int, core: RGB, edge: RGB) -> QImage:
    """Тело сферы диаметром px (физические пиксели): почти чёрное стекло и маленький блик."""
    img, p = _canvas(px + 2)
    c, r = QPointF((px + 2) / 2, (px + 2) / 2), px / 2
    g = QRadialGradient(QPointF(c.x(), c.y() + 0.1 * r), 1.1 * r)
    g.setColorAt(0.0, _c(core))
    g.setColorAt(0.62, _mix(core, edge, 0.45))
    g.setColorAt(1.0, _c(edge))
    p.setBrush(g)
    p.drawEllipse(c, r, r)
    # блик: маленький, вытянутый, слева сверху — стекло, а не матовый шар
    p.save()
    p.translate(c.x() - 0.36 * r, c.y() - 0.5 * r)
    p.rotate(-30)
    p.scale(0.34 * r, 0.15 * r)
    hl = QRadialGradient(QPointF(0, 0), 1.0)
    hl.setColorAt(0.0, QColor(255, 255, 255, 150))
    hl.setColorAt(0.5, QColor(255, 255, 255, 46))
    hl.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(hl)
    p.drawEllipse(QPointF(0, 0), 1.0, 1.0)
    p.restore()
    p.end()
    return img


@functools.lru_cache(maxsize=48)
def _rim_image(px: int, rim: RGB) -> QImage:
    """Ободок сферы диаметром px: свет кромки изнутри (Френель), тонкая кромка и свечение наружу."""
    side = math.ceil(px * _RIM_OUT) + 2
    img, p = _canvas(side)
    c, r = QPointF(side / 2, side / 2), px / 2
    outer = r * _RIM_OUT
    g = QRadialGradient(c, outer)
    inside = 1 / _RIM_OUT
    g.setColorAt(0.0, _c(rim, 0.0))
    g.setColorAt(0.7 * inside, _c(rim, 0.0))
    g.setColorAt(0.9 * inside, _c(rim, 0.16))
    g.setColorAt(0.985 * inside, _c(rim, 0.62))
    g.setColorAt(inside, _c(rim, 0.5))
    g.setColorAt(inside + (1 - inside) * 0.35, _c(rim, 0.14))
    g.setColorAt(1.0, _c(rim, 0.0))
    p.setBrush(g)
    p.drawEllipse(c, outer, outer)
    # тонкая кромка: ярче снизу (свет изнутри) и слева сверху (отражение), тусклее по бокам
    cone = QConicalGradient(c, -90)
    for at, a in ((0.0, 0.95), (0.2, 0.3), (0.42, 0.5), (0.6, 0.85), (0.78, 0.25), (1.0, 0.95)):
        cone.setColorAt(at, _c(rim, a))
    width = max(0.8, r * 0.04)
    p.setPen(QPen(QBrush(cone), width))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(c, r - width / 2, r - width / 2)
    p.end()
    return img


@functools.lru_cache(maxsize=48)
def _halo_image(px: int, halo: RGB) -> QImage:
    """Внешнее свечение вокруг сферы диаметром px (поле — до 1 + ORB_EXTENT радиуса)."""
    side = math.ceil(px * (1 + ORB_EXTENT))
    img = QImage(side, side, _ARGB)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    c, reach = QPointF(side / 2, side / 2), side / 2
    g = QRadialGradient(c, reach)
    inside = 1 / (1 + ORB_EXTENT)
    g.setColorAt(0.0, _c(halo, 1.0))
    g.setColorAt(inside, _c(halo, 1.0))
    g.setColorAt(inside + (1 - inside) * 0.3, _c(halo, 0.42))
    g.setColorAt(inside + (1 - inside) * 0.65, _c(halo, 0.12))
    g.setColorAt(1.0, _c(halo, 0.0))
    p.fillRect(img.rect(), g)
    p.end()
    return img


def _cached(image: QImage, dpr: float) -> QImage:
    image.setDevicePixelRatio(dpr)
    return image


def _put(p: QPainter, image: QImage, c: QPointF, opacity: float = 1.0) -> None:
    """Картинку (её DPR уже выставлен) — центром в c."""
    w = image.width() / image.devicePixelRatio()
    h = image.height() / image.devicePixelRatio()
    if opacity < 1.0:
        p.setOpacity(opacity)
    p.drawImage(QPointF(c.x() - w / 2, c.y() - h / 2), image)
    if opacity < 1.0:
        p.setOpacity(1.0)


def _sprite(p: QPainter, rgb: RGB, x: float, y: float, rx: float, ry: float, alpha: float) -> None:
    """Мягкое свечение цвета rgb в эллипсе (rx, ry) без поворота — быстрый путь растра."""
    if alpha <= 0.01 or rx <= 0.2:
        return
    p.setOpacity(min(1.0, alpha))
    p.drawImage(QRectF(x - rx, y - ry, 2 * rx, 2 * ry), _blob(_q(rgb)))


# --- отрисовка ----------------------------------------------------------------------------------------


def orb_radius(side: float) -> float:
    """Радиус сферы в квадрате side × side: остальное — поля под ауру и искры."""
    return side / 2 / (1 + ORB_EXTENT)


def paint_orb(p: QPainter, rect: QRectF, model: OrbModel, t: float | None = None) -> None:
    """Нарисовать кадр модели в rect (сфера по центру, радиус — orb_radius). t — часы анимации."""
    f = model.frame(t)
    r = orb_radius(min(rect.width(), rect.height()))
    if r <= 0.5:
        return
    device = p.device()
    dpr = device.devicePixelRatioF() if device is not None else 1.0
    px = max(2, round(2 * r * dpr))  # диаметр сферы в физических пикселях — ключ кэша картинок
    c = rect.center()
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setPen(Qt.PenStyle.NoPen)
    p.setCompositionMode(_SCREEN)
    _put(p, _cached(_halo_image(px, _q(f.palette.halo)), dpr), c, f.halo)
    _flames(p, c, r, f, dpr)
    _ring(p, c, r, f)
    p.setCompositionMode(_OVER)
    _put(p, _cached(_body_image(px, _q(f.palette.core), _q(f.palette.edge)), dpr), c)
    p.setCompositionMode(_SCREEN)
    _inner(p, c, r, f)
    _put(p, _cached(_rim_image(px, _q(f.palette.rim)), dpr), c, f.rim)
    _flash(p, c, r, f)
    _sparks(p, c, r, f)
    p.restore()


def _flame_path(c: QPointF, r: float, flame: OrbFlame) -> QPainterPath:
    """Слой ауры одним контуром: кольцо у основания и языки (объединение — правило winding)."""
    cx, cy = c.x(), c.y()
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    path.addEllipse(c, r * flame.ring, r * flame.ring)
    for b1x, b1y, c1x, c1y, c2x, c2y, tx, ty, c3x, c3y, c4x, c4y, b2x, b2y in flame.tongues:
        path.moveTo(cx + b1x * r, cy + b1y * r)
        path.cubicTo(cx + c1x * r, cy + c1y * r, cx + c2x * r, cy + c2y * r, cx + tx * r, cy + ty * r)
        path.cubicTo(cx + c3x * r, cy + c3y * r, cx + c4x * r, cy + c4y * r, cx + b2x * r, cy + b2y * r)
        path.closeSubpath()
    return path


_buffers: dict[int, QImage] = {}
_FLAME_PX_PER_R = 28.0  # разрешение буфера пламени: столько пикселей на радиус сферы — мягкость огня
_FLAME_MIN_SCALE = 0.5  # большая сфера: буфер не меньше половины разрешения — края языков без ступенек


def _flame_layer(p: QPainter, c: QPointF, r: float, flame: OrbFlame) -> None:
    """Слой одной заливкой: у основания — сердцевина, к кончикам — дым и прозрачность (вверху — позже)."""
    g = QRadialGradient(QPointF(c.x(), c.y() + ORB_FLAME_DROP * r), r * flame.reach)
    for at, rgb, alpha in flame.stops:
        g.setColorAt(at, _c(rgb, alpha))
    p.setBrush(g)
    p.drawPath(_flame_path(c, r, flame))


def _flames(p: QPainter, c: QPointF, r: float, f: OrbFrame, dpr: float) -> None:
    """Аура — тень Монарха: языки фиолетового пламени, кончики уходят в тёмный дым.

    Рисуется в маленький буфер (~28 пикселей на радиус, не меньше половины разрешения) и растягивается
    сглаживанием: края мягкие, как у огня, а пикселей для растра — в разы меньше.
    """
    reach = r * (1 + ORB_EXTENT)
    scale = min(1.0, max(_FLAME_PX_PER_R / (r * dpr), _FLAME_MIN_SCALE))
    side = max(2, math.ceil(2 * reach * dpr * scale))
    buf = _buffers.get(side)
    if buf is None:
        buf = _buffers[side] = QImage(side, side, _ARGB)
    buf.fill(Qt.GlobalColor.transparent)
    bp = QPainter(buf)
    bp.setRenderHint(QPainter.RenderHint.Antialiasing)
    bp.setPen(Qt.PenStyle.NoPen)
    k = side / (2 * reach)
    for flame in f.flames:
        _flame_layer(bp, QPointF(side / 2, side / 2), r * k, flame)
    bp.end()
    p.setCompositionMode(_OVER)
    p.drawImage(QRectF(c.x() - reach, c.y() - reach, 2 * reach, 2 * reach), buf)
    p.setCompositionMode(_SCREEN)


def _ring(p: QPainter, c: QPointF, r: float, f: OrbFrame) -> None:
    """Кольцо-волна при пробуждении и вспышке (края градиента прозрачные — без антиалиасинга)."""
    if f.ring <= 0 or f.ring_alpha <= 0.01:
        return
    w = 0.08
    outer = r * (f.ring + w)
    g = QRadialGradient(c, outer)
    g.setColorAt(0.0, _c(f.ring_rgb, 0.0))
    g.setColorAt((f.ring - w) / (f.ring + w), _c(f.ring_rgb, 0.0))
    g.setColorAt(f.ring / (f.ring + w), _c(f.ring_rgb, f.ring_alpha))
    g.setColorAt(1.0, _c(f.ring_rgb, 0.0))
    p.fillRect(QRectF(c.x() - outer, c.y() - outer, 2 * outer, 2 * outer), g)


def _inner(p: QPainter, c: QPointF, r: float, f: OrbFrame) -> None:
    """Внутри стекла: свет, стекающий вниз, дуги энергии, клубящиеся доли с хвостами, яркое ядро.

    Режим Screen: пересечения долей дают яркие фиолетово-белые ядра.
    """
    pal = f.palette
    cx, cy = c.x(), c.y()
    _sprite(p, pal.lobes[1], cx, cy + 0.5 * r, 0.46 * r, 0.4 * r, 0.12 + 0.3 * min(1.0, f.glow))
    for w in f.wisps:
        if w.alpha <= 0.01:
            continue
        box = QRectF(cx + (w.x - w.radius) * r, cy + (w.y - w.radius) * r, 2 * w.radius * r, 2 * w.radius * r)
        cone = QConicalGradient(QPointF(box.center()), w.start)
        k = w.span / 360
        cone.setColorAt(0.0, _c(w.color, 0.0))
        cone.setColorAt(k * 0.6, _c(w.color, w.alpha))
        cone.setColorAt(k, _c(w.color, 0.0))
        cone.setColorAt(1.0, _c(w.color, 0.0))
        path = QPainterPath()
        path.arcMoveTo(box, w.start)
        path.arcTo(box, w.start, w.span)
        p.setPen(QPen(QBrush(cone), max(0.6, w.width * r), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    for lobe in f.lobes:
        for x, y, lr, fade in reversed(lobe.trail):
            _sprite(p, lobe.color, cx + x * r, cy + y * r, lr * r, lr * r, lobe.alpha * fade)
        _sprite(p, lobe.color, cx + lobe.x * r, cy + lobe.y * r, lobe.r * r, lobe.r * r, lobe.alpha)
    _sprite(p, ORB_CORE_LIGHT, cx, cy + 0.06 * r, 0.36 * r, 0.36 * r, f.core)
    p.setOpacity(1.0)


def _flash(p: QPainter, c: QPointF, r: float, f: OrbFrame) -> None:
    """Вспышка успеха (лавандово-белая) или ошибки (малиновая)."""
    if f.flash <= 0.01:
        return
    reach = r * (1 + ORB_EXTENT) * 0.9
    g = QRadialGradient(c, reach)
    g.setColorAt(0.0, _c(f.flash_rgb, f.flash * 0.75))
    g.setColorAt(0.5 / (1 + ORB_EXTENT), _c(f.flash_rgb, f.flash * 0.45))
    g.setColorAt(1.05 / (1 + ORB_EXTENT), _c(f.flash_rgb, f.flash * 0.3))
    g.setColorAt(1.0, _c(f.flash_rgb, 0.0))
    p.fillRect(QRectF(c.x() - reach, c.y() - reach, 2 * reach, 2 * reach), g)


def _sparks(p: QPainter, c: QPointF, r: float, f: OrbFrame) -> None:
    """Фиолетовые искры над сферой во время работы: яркая точка и след вниз."""
    for s in f.sparks:
        size = max(0.8, s.r * r * 2.2)
        x, y = c.x() + s.x * r, c.y() + s.y * r
        _sprite(p, f.palette.spark, x, y + size * (s.stretch - 1), size, size * s.stretch, s.alpha)
    p.setOpacity(1.0)


# --- виджет ---------------------------------------------------------------------------------------------


class OrbWidget(QWidget):
    """Сфера в квадрате size × size. Таймер кадров — только пока модель анимируется и виджет виден."""

    def __init__(
        self, model: OrbModel | None = None, parent: QWidget | None = None, *, size: int = 40
    ) -> None:
        super().__init__(parent)
        self.model = model if model is not None else OrbModel()
        self._timer = QTimer(self)
        self._timer.setObjectName("orbFrames")
        self._timer.setInterval(ORB_FRAME_MS)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._frame)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.model.subscribe(self._kick)

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def _kick(self) -> None:
        """Модель изменилась: перерисовать; анимируется — запустить таймер (если виджет виден)."""
        self.update()
        if self.model.animating and self.isVisible() and not self._timer.isActive():
            self.model.advance(time.monotonic())
            self._timer.start()

    def _frame(self) -> None:
        alive = self.model.advance(time.monotonic())
        self.update()
        if not alive:
            self._timer.stop()  # последний кадр нарисован — дальше покой

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._kick()

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        paint_orb(p, QRectF(self.rect()), self.model)
        p.end()


# --- сфера на рабочем столе -------------------------------------------------------------------------------

ORB_MARGIN_PX = 24  # от сферы до края доступной области экрана (над панелью задач)
ORB_LINGER_MS = 2000  # режим auto: столько сфера остаётся после скрытия окна и конца запроса
ORB_FADE_IN_MS = 180
ORB_FADE_OUT_MS = 420
STATE_FILE = "ui_state.json"
WINDOW_TITLE = "Jarvis.Orb"

_state_lock = threading.Lock()


def orb_side(orb_size: int) -> int:
    """Сторона окна сферы: диаметр сферы + поля под ауру и искры."""
    return math.ceil(orb_size * (1 + ORB_EXTENT))


def state_path() -> Path:
    return settings.data_dir() / STATE_FILE


def load_state() -> dict[str, Any]:
    """<data>\\ui_state.json (позиция сферы); нет файла или он битый — пусто."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(update: dict[str, Any]) -> None:
    """Дописать ключи в <data>\\ui_state.json атомарно (tmp + os.replace). Не из UI-потока: диск."""
    with _state_lock:
        path = state_path()
        data = {**load_state(), **update}
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("состояние окна не сохранено: %s", e)
            tmp.unlink(missing_ok=True)


def save_state_async(update: dict[str, Any]) -> None:
    """save_state в рабочем потоке: UI-поток не ждёт диск."""
    threading.Thread(target=save_state, args=(dict(update),), name="jarvis-ui-state", daemon=True).start()


def saved_center(state: dict[str, Any]) -> QPoint | None:
    """Центр сферы из состояния ({"orb": {"x": …, "y": …}}); нет или не числа — None."""
    orb = state.get("orb")
    if not isinstance(orb, dict):
        return None
    x, y = orb.get("x"), orb.get("y")
    if not isinstance(x, int) or not isinstance(y, int) or isinstance(x, bool) or isinstance(y, bool):
        return None
    return QPoint(x, y)


def _on_screen(point: QPoint) -> bool:
    return any(screen.availableGeometry().contains(point) for screen in QGuiApplication.screens())


class OrbWindow(OrbWidget):
    """Плавающая сфера на рабочем столе: без рамки, поверх всех, не на панели задач и без фокуса.

    Та же модель, что у сферы в окне. Левая кнопка — перетащить (позиция — в <data>\\ui_state.json, запись
    в рабочем потоке); клик без перетаскивания — сигнал clicked; правая — меню. Режимы [ui] orb: always —
    видна всегда (в покое неподвижна), auto — появляется при показе окна и работе, гаснет через ~2 с после
    того, как окно спрятано и запрос закончен. set_suppressed(True) — полноэкранное окно впереди: спрятаться.
    """

    clicked = Signal()

    def __init__(
        self,
        model: OrbModel,
        ui_cfg: UiConfig,
        *,
        state: dict[str, Any] | None = None,
        writer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(model, None, size=orb_side(ui_cfg.orb_size))
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setWindowTitle(WINDOW_TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode = ui_cfg.orb if ui_cfg.orb in ("always", "auto") else "always"
        self.orb_size = int(ui_cfg.orb_size)
        self._anim = bool(ui_cfg.animations)
        self._writer = writer if writer is not None else save_state_async
        self._suppressed = False
        self._user_hidden = False
        self._fading_out = False
        self._press: QPoint | None = None
        self._origin = QPoint()
        self._dragging = False

        self._linger = QTimer(self)
        self._linger.setObjectName("orbLinger")
        self._linger.setSingleShot(True)
        self._linger.setInterval(ORB_LINGER_MS)
        self._linger.timeout.connect(self._fade_out)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._fade_done)

        self._menu = QMenu(self)
        self._menu.setStyleSheet(theme.stylesheet())  # тёмное меню, как в окне Jarvis
        self._open_action = QAction("Открыть Jarvis", self._menu)
        self._open_action.triggered.connect(self.clicked)
        self._hide_action = QAction("Спрятать сферу", self._menu)
        self._hide_action.triggered.connect(self.hide_by_user)
        self._menu.addAction(self._open_action)
        self._menu.addAction(self._hide_action)

        self.place(load_state() if state is None else state)
        model.subscribe(self._sync)

    # --- для app.py

    def start(self) -> None:
        """Первый показ по режиму (always — сразу, без анимации)."""
        self._sync(instant=True)

    def set_menu_actions(self, actions: Sequence[QAction]) -> None:
        """Пункты меню трея (те же QAction) — под «Открыть Jarvis» и «Спрятать сферу»."""
        self._menu.addSeparator()
        for action in actions:
            self._menu.addAction(action)

    def menu(self) -> QMenu:
        return self._menu

    def set_suppressed(self, on: bool) -> None:
        """Впереди полноэкранное окно (игра, видео): спрятаться сразу; ушло — вернуться по режиму."""
        if bool(on) != self._suppressed:
            self._suppressed = bool(on)
            self._sync(instant=self._suppressed)

    @property
    def suppressed(self) -> bool:
        return self._suppressed

    def hide_by_user(self) -> None:
        """«Спрятать сферу» из меню: до перезапуска Jarvis (насовсем — [ui] orb = "off")."""
        self._user_hidden = True
        self._sync(instant=True)

    def close_orb(self) -> None:
        """Выход Jarvis: отписаться от модели, остановить всё и спрятать."""
        self.model.unsubscribe(self._sync)
        self.model.unsubscribe(self._kick)
        self._linger.stop()
        self._fade.stop()
        self._timer.stop()
        self.hide()

    # --- место на экране

    def default_center(self) -> QPoint:
        """Правый нижний угол доступной области основного экрана, над панелью задач."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return QPoint(self.width() // 2, self.height() // 2)
        area = screen.availableGeometry()
        r = self.orb_size // 2
        return QPoint(area.right() - ORB_MARGIN_PX - r, area.bottom() - ORB_MARGIN_PX - r)

    def place(self, state: dict[str, Any]) -> None:
        """Позиция из состояния, если центр сферы на каком-либо экране; иначе — угол по умолчанию."""
        center = saved_center(state)
        if center is None or not _on_screen(center):
            center = self.default_center()
        self.move(center - QPoint(self.width() // 2, self.height() // 2))

    def center(self) -> QPoint:
        """Центр сферы (глобальные координаты): обратное к place() без сдвига на пиксель."""
        return QPoint(self.x() + self.width() // 2, self.y() + self.height() // 2)

    # --- видимость

    def _wanted(self) -> bool:
        if self._user_hidden or self._suppressed:
            return False
        return self.mode == "always" or not self.model.resting

    def _sync(self, instant: bool = False) -> None:
        if self._wanted():
            self._linger.stop()
            if not self.isVisible() or self._fading_out:
                self._appear(instant)
        elif self.isVisible() and not self._fading_out:
            if instant or self.mode != "auto":
                self._vanish()
            elif not self._linger.isActive():
                self._linger.start()  # окно спрятано, запрос кончился — погаснуть чуть позже

    def _appear(self, instant: bool) -> None:
        self._fade.stop()
        self._fading_out = False
        animate = self._anim and not instant
        start = self.windowOpacity() if self.isVisible() else 0.0  # гасла — разгорается с того же места
        self.setWindowOpacity(start if animate else 1.0)
        if not self.isVisible():
            self.show()
        if animate:
            self._fade.setDuration(ORB_FADE_IN_MS)
            self._fade.setStartValue(start)
            self._fade.setEndValue(1.0)
            self._fade.start()

    def _fade_out(self) -> None:
        if self._wanted() or not self.isVisible():
            return
        if not self._anim:
            self._vanish()
            return
        self._fading_out = True
        self._fade.stop()
        self._fade.setDuration(ORB_FADE_OUT_MS)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _fade_done(self) -> None:
        if self._fading_out:
            self._vanish()

    def _vanish(self) -> None:
        self._linger.stop()
        self._fade.stop()
        self._fading_out = False
        self.hide()
        self.setWindowOpacity(1.0)

    # --- мышь

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._origin = self.pos()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._press
        if not self._dragging and delta.manhattanLength() >= QApplication.startDragDistance():
            self._dragging = True
        if self._dragging:
            self.move(self._origin + delta)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            super().mouseReleaseEvent(event)
            return
        dragged, self._press, self._dragging = self._dragging, None, False
        event.accept()
        if dragged:
            c = self.center()
            self._writer({"orb": {"x": c.x(), "y": c.y()}})
        else:
            self.clicked.emit()

    def contextMenuEvent(self, event: Any) -> None:
        self._menu.popup(event.globalPos())
        event.accept()
