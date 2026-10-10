"""Детали окна Jarvis: бейджи, индикатор работы, строки «⚙ …», список find, карточка подтверждения, тост.

Все недоверенные строки показываются только как обычный текст: виджеты рисуют их QPainter/QTextLayout
(HTML не разбирается никогда) или QTextEdit с setPlainText/insertText. Ссылки не кликабельны.
"""

import math
from typing import Literal

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QTextBlockFormat,
    QTextCursor,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from jarvis.ui import logic, theme
from jarvis.ui.logic import ConfirmRequest

IconKind = Literal["spin", "ok", "fail", "dot", "stop", "warn", "info"]


def _hfw_policy(h: QSizePolicy.Policy = QSizePolicy.Policy.Preferred) -> QSizePolicy:
    sp = QSizePolicy(h, QSizePolicy.Policy.Preferred)
    sp.setHeightForWidth(True)
    return sp


def plain_label(text: str = "", parent: QWidget | None = None, *, name: str = "") -> QLabel:
    """QLabel, который никогда не разбирает HTML и не открывает ссылки."""
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setOpenExternalLinks(False)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    if name:
        label.setObjectName(name)
    label.setText(logic.display_text(text))
    return label


# --- текст --------------------------------------------------------------------------------------------


class WrapLabel(QWidget):
    """Простой текст с переносом по словам, а длинные пути и URL — где угодно. Рисуется QTextLayout."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        font: QFont,
        color: str = theme.TEXT,
        line_factor: float = 1.25,
    ) -> None:
        super().__init__(parent)
        self._text = ""
        self._font = font
        self._color = QColor(color)
        self._step = math.ceil(QFontMetricsF(font).lineSpacing() * line_factor)
        self._heights: dict[int, int] = {}
        self._layout_cache: tuple[int, QTextLayout] | None = None
        self.setSizePolicy(_hfw_policy())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setText(text)

    @property
    def line_height(self) -> int:
        return self._step

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        text = logic.display_text(text)
        if text == self._text:
            return
        self._text = text
        self._heights.clear()
        self._layout_cache = None
        self.updateGeometry()
        self.update()

    def set_color(self, value: str) -> None:
        self._color = QColor(value)
        self.update()

    def _make_layout(self, width: int) -> tuple[QTextLayout, int]:
        layout = QTextLayout(self._text.replace("\n", "\u2028"), self._font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        y = 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            line.setPosition(QPointF(0, y + (self._step - line.height()) / 2))
            y += self._step
        layout.endLayout()
        return layout, max(y, self._step)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        if width not in self._heights:
            if len(self._heights) > 8:
                self._heights.clear()
            self._heights[width] = self._make_layout(width)[1]
        return self._heights[width]

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self._font)
        lines = self._text.split("\n") or [""]
        w = min(560, max(fm.horizontalAdvance(line) for line in lines) + 2)
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._step)

    def paintEvent(self, event: object) -> None:
        if not self._text:
            return
        w = self.width()
        if self._layout_cache is None or self._layout_cache[0] != w:
            self._layout_cache = (w, self._make_layout(w)[0])
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(self._color)
        self._layout_cache[1].draw(p, QPointF(0, 0))
        p.end()


class PlainView(QTextEdit):
    """Выделяемый простой текст (ответ мозга, details): только setPlainText/insertText, высота по тексту."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        font: QFont,
        max_height: int = 0,
        line_height: int = 145,
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        name: str = "",
    ) -> None:
        super().__init__(parent)
        if name:
            self.setObjectName(name)
        self._max_height = max_height
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(font)
        self._margins = margins
        self.setViewportMargins(*margins)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        policy = Qt.ScrollBarPolicy.ScrollBarAsNeeded if max_height else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        self.setVerticalScrollBarPolicy(policy)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(0)
        self.setSizePolicy(_hfw_policy(QSizePolicy.Policy.Expanding))
        self._format = QTextBlockFormat()
        self._format.setLineHeight(
            float(line_height), QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        )
        self._apply_format()

    def _apply_format(self) -> None:
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.mergeBlockFormat(self._format)

    def text(self) -> str:
        return self.toPlainText()

    def set_text(self, text: str) -> None:
        self.setPlainText(logic.display_text(text))
        self._apply_format()
        self.updateGeometry()

    def append_text(self, text: str) -> None:
        """Дописать кусок в конец: O(длина куска), без пересборки документа."""
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(logic.display_text(text))
        self.updateGeometry()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        left, top, right, bottom = self._margins
        text_w = max(1, width - left - right)
        doc = self.document()
        if abs(doc.textWidth() - text_w) <= 0.5:
            h = doc.size().height()  # документ уже разложен на эту ширину — дёшево
        elif not self._max_height and abs(self.viewport().width() - text_w) <= 1:
            doc.setTextWidth(text_w)  # та же ширина, что у видимой области: QTextEdit разложит так же
            h = doc.size().height()
        else:  # другая ширина — меряем на копии, видимый документ не трогаем
            clone = doc.clone()
            clone.setTextWidth(text_w)
            h = clone.size().height()
        h = math.ceil(h) + top + bottom + 1
        return min(h, self._max_height) if self._max_height else h

    def sizeHint(self) -> QSize:
        w = self.width() if self.width() > 50 else 600
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)


# --- мелкие рисованные элементы -----------------------------------------------------------------------


class Badge(QWidget):
    """Пилюля (уровень ответа, режим «локально», «просит GPT»); смена цвета — плавная."""

    def __init__(self, parent: QWidget | None = None, *, animations: bool = True, dot: bool = False) -> None:
        super().__init__(parent)
        self._text = ""
        self._dot = dot
        self._color = QColor(theme.ACCENT)
        self._font = theme.font(theme.FONT_TINY, QFont.Weight.DemiBold)
        self._animations = animations
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(theme.COLOR_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_color)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def text(self) -> str:
        return self._text

    def current_color(self) -> QColor:
        return QColor(self._color)

    def set_badge(self, text: str, color: str) -> None:
        target = QColor(color)
        if text != self._text:
            self._text = text
            self.updateGeometry()
        self._anim.stop()
        if self._animations and self.isVisible() and self._color != target:
            self._anim.setStartValue(QColor(self._color))
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._color = target
        self.update()

    def _on_color(self, value: object) -> None:
        if isinstance(value, QColor):
            self._color = value
            self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self._font)
        extra = 12 if self._dot else 0
        return QSize(fm.horizontalAdvance(self._text) + 18 + extra, 22)

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill = QColor(self._color)
        fill.setAlphaF(0.15)
        border = QColor(self._color)
        border.setAlphaF(0.32)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        x = 9.0
        if self._dot:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._color)
            p.drawEllipse(QPointF(x + 3, r.center().y()), 3, 3)
            x += 12
        p.setPen(self._color)
        p.setFont(self._font)
        p.drawText(QRectF(x, 0, self.width() - x, self.height()), Qt.AlignmentFlag.AlignVCenter, self._text)
        p.end()


class ProgressStrip(QWidget):
    """Полоска под строкой ввода: бегущий градиент, пока идёт запрос; тонкий разделитель в покое."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode: Literal["off", "line", "run", "static"] = "off"
        self._phase = 0.0
        self._color = QColor(theme.ACCENT)
        self.setFixedHeight(theme.STRIP_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_mode(self, mode: Literal["off", "line", "run", "static"]) -> None:
        if mode != self.mode:
            self.mode = mode
            self.update()

    def set_phase(self, phase: float) -> None:
        self._phase = phase
        if self.mode == "run":
            self.update()

    def set_color(self, value: QColor) -> None:
        self._color = QColor(value)
        self.update()

    def paintEvent(self, event: object) -> None:
        if self.mode == "off":
            return
        p = QPainter(self)
        w, h = self.width(), self.height()
        if self.mode == "line":
            p.fillRect(QRectF(0, h - 1, w, 1), theme.color(theme.BORDER, theme.BORDER_ALPHA))
            p.end()
            return
        p.fillRect(self.rect(), theme.color(self._color.name(), 0.13))
        if self.mode == "static":
            p.fillRect(self.rect(), theme.color(self._color.name(), 0.55))
            p.end()
            return
        band = w * 0.42
        eased = 0.5 - 0.5 * math.cos(math.pi * self._phase)
        x = -band + (w + band) * eased
        grad = QLinearGradient(x, 0, x + band, 0)
        edge = QColor(self._color)
        edge.setAlphaF(0.0)
        grad.setColorAt(0.0, edge)
        grad.setColorAt(0.5, self._color)
        grad.setColorAt(1.0, edge)
        p.fillRect(QRectF(x, 0, band, h), grad)
        p.end()


class StatusIcon(QWidget):
    """Значок строки: спиннер, ✓, ✗, точка, «стоп», «!» — рисуется, не зависит от шрифта."""

    def __init__(
        self, parent: QWidget | None = None, *, kind: IconKind = "dot", box_h: int = 16, color: str = ""
    ) -> None:
        super().__init__(parent)
        self.kind: IconKind = kind
        self._angle = 0.0
        self._color = QColor(color or theme.ACCENT)
        self.setFixedSize(16, max(16, box_h))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_kind(self, kind: IconKind) -> None:
        if kind != self.kind:
            self.kind = kind
            self.update()

    def set_color(self, value: QColor | str) -> None:
        self._color = QColor(value)
        self.update()

    def set_angle(self, angle: float) -> None:
        self._angle = angle
        if self.kind == "spin":
            self.update()

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(8, self.height() / 2)
        if self.kind == "spin":
            track = QColor(self._color)
            track.setAlphaF(0.2)
            p.setPen(QPen(track, 1.8))
            p.drawEllipse(c, 5.6, 5.6)
            pen = QPen(self._color, 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            rect = QRectF(c.x() - 5.6, c.y() - 5.6, 11.2, 11.2)
            p.drawArc(rect, int(-self._angle * 16), 100 * 16)
        elif self.kind in ("ok", "fail", "warn", "info"):
            # «!» берёт цвет владельца (карточка подтверждения, тост), ✓ и ✗ — всегда успех и ошибка
            tones = {"ok": theme.SUCCESS, "fail": theme.ERROR, "info": theme.ACCENT}
            base = QColor(self._color) if self.kind == "warn" else QColor(tones[self.kind])
            bg = QColor(base)
            bg.setAlphaF(0.17)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawEllipse(c, 7.5, 7.5)
            pen = QPen(base, 1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            x, y = c.x(), c.y()
            if self.kind == "ok":
                path = QPainterPath(QPointF(x - 3.3, y + 0.2))
                path.lineTo(QPointF(x - 0.9, y + 2.6))
                path.lineTo(QPointF(x + 3.4, y - 2.4))
                p.drawPath(path)
            elif self.kind == "fail":
                p.drawLine(QPointF(x - 2.6, y - 2.6), QPointF(x + 2.6, y + 2.6))
                p.drawLine(QPointF(x + 2.6, y - 2.6), QPointF(x - 2.6, y + 2.6))
            else:
                p.drawLine(QPointF(x, y - 3.4), QPointF(x, y + 0.6))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(base)
                p.drawEllipse(QPointF(x, y + 3.2), 1.0, 1.0)
        elif self.kind == "stop":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.TEXT_FAINT))
            p.drawRoundedRect(QRectF(c.x() - 3.5, c.y() - 3.5, 7, 7), 1.6, 1.6)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.TEXT_FAINT))
            p.drawEllipse(c, 2.4, 2.4)
        p.end()


class Glyph(QWidget):
    """Значок слева от строки ввода: кольцо с точкой цвета текущего уровня."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(theme.ACCENT)
        self.setFixedSize(22, 22)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_color(self, value: QColor) -> None:
        self._color = QColor(value)
        self.update()

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(11, 11)
        ring = QColor(self._color)
        ring.setAlphaF(0.85)
        p.setPen(QPen(ring, 2.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, 7.5, 7.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(c, 2.6, 2.6)
        p.end()


# --- блоки ответа -------------------------------------------------------------------------------------


class StatusRow(QWidget):
    """Строка «⚙ …»: значок (спиннер → ✓/✗) и текст."""

    def __init__(
        self,
        text: str,
        kind: IconKind,
        parent: QWidget | None = None,
        *,
        strong: bool = False,
        color: str = "",
    ) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.ICON_X, 3, theme.ROW_PAD_X, 3)
        lay.setSpacing(theme.ICON_GAP)
        weight = QFont.Weight.Medium if strong else QFont.Weight.Normal
        self.label = WrapLabel(
            text, font=theme.font(theme.FONT_ROW, weight), color=theme.TEXT if strong else theme.TEXT_MUTED
        )
        self.icon = StatusIcon(kind=kind, box_h=self.label.line_height, color=color)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.label, 1)
        self.setSizePolicy(_hfw_policy(QSizePolicy.Policy.Expanding))

    def text(self) -> str:
        return self.label.text()

    def set_state(self, text: str, kind: IconKind) -> None:
        if text:
            self.label.setText(text)
        self.icon.set_kind(kind)


def split_path(path: str) -> tuple[str, str]:
    """«C:\\Users\\me\\Documents\\отчёт.docx» → («отчёт.docx», «C:\\Users\\me\\Documents»)."""
    stripped = path.rstrip("\\/") or path
    i = max(stripped.rfind("\\"), stripped.rfind("/"))
    if i < 0:
        return stripped, ""
    return stripped[i + 1 :] or stripped, stripped[:i] or stripped[: i + 1]


class ItemRow(QWidget):
    """Пункт результатов find: номер, имя и папка; подсветка при наведении, клик — открыть."""

    clicked = Signal(int)

    def __init__(self, index: int, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self._path = logic.display_text(path)
        self._name, self._dir = split_path(self._path)
        self.hovered = False
        self._f_num = theme.font(theme.FONT_TINY, QFont.Weight.DemiBold)
        self._f_name = theme.font(theme.FONT_ROW, QFont.Weight.Medium)
        self._f_dir = theme.font(theme.FONT_SMALL)
        self._f_hint = theme.font(theme.FONT_TINY)
        self.setFixedHeight(theme.ITEM_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def text(self) -> str:
        return self._path

    def set_hovered(self, on: bool) -> None:
        if on != self.hovered:
            self.hovered = on
            self.update()

    def enterEvent(self, event: object) -> None:
        self.set_hovered(True)

    def leaveEvent(self, event: object) -> None:
        self.set_hovered(False)

    def mousePressEvent(self, event: object) -> None:
        event.accept()  # type: ignore[attr-defined] # отпускание кнопки придёт сюда же

    def mouseReleaseEvent(self, event: object) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):  # type: ignore[attr-defined]
            self.clicked.emit(self.index)

    def sizeHint(self) -> QSize:
        return QSize(400, theme.ITEM_H)

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if self.hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(theme.SURFACE_HOVER))
            p.drawRoundedRect(QRectF(0, 1, w, h - 2), theme.RADIUS_ROW, theme.RADIUS_ROW)
        key = QRectF(theme.ICON_X - 2, (h - 20) / 2, 20, 20)
        p.setPen(QPen(theme.color("#FFFFFF", 0.10), 1))
        p.setBrush(theme.color(theme.ACCENT, 0.22) if self.hovered else theme.color("#FFFFFF", 0.05))
        p.drawRoundedRect(key.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        p.setFont(self._f_num)
        p.setPen(theme.color(theme.TEXT if self.hovered else theme.TEXT_MUTED))
        p.drawText(key, Qt.AlignmentFlag.AlignCenter, str(self.index + 1))

        x = float(theme.GUTTER)
        right = 74.0 if self.hovered else 10.0
        avail = max(40.0, w - x - right)
        fm_name, fm_dir = QFontMetrics(self._f_name), QFontMetrics(self._f_dir)
        name = self._name
        limit = int(avail * (0.68 if self._dir else 1.0))
        if fm_name.horizontalAdvance(name) > limit:
            name = fm_name.elidedText(name, Qt.TextElideMode.ElideMiddle, limit)
        name_w = fm_name.horizontalAdvance(name)
        p.setFont(self._f_name)
        p.setPen(theme.color(theme.TEXT))
        p.drawText(QRectF(x, 0, name_w + 2, h), Qt.AlignmentFlag.AlignVCenter, name)
        dir_w = int(avail - name_w - 12)
        if self._dir and dir_w > 36:
            folder = fm_dir.elidedText(self._dir, Qt.TextElideMode.ElideMiddle, dir_w)
            p.setFont(self._f_dir)
            p.setPen(theme.color(theme.TEXT_MUTED if self.hovered else theme.TEXT_FAINT))
            p.drawText(QRectF(x + name_w + 12, 0, dir_w, h), Qt.AlignmentFlag.AlignVCenter, folder)
        if self.hovered:
            p.setFont(self._f_hint)
            p.setPen(theme.color(theme.TEXT_MUTED))
            hint = QRectF(w - right, 0, right - 12, h)
            p.drawText(hint, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "открыть")
        p.end()


class ItemsBlock(QWidget):
    """Нумерованный список результатов find."""

    activated = Signal(int)

    def __init__(self, items: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(0)
        self.rows: list[ItemRow] = []
        for i, path in enumerate(items):
            row = ItemRow(i, path, self)
            row.clicked.connect(self.activated)
            lay.addWidget(row)
            self.rows.append(row)
        self.hint = WrapLabel(
            "Цифра и Enter или клик — открыть", font=theme.font(theme.FONT_TINY), color=theme.TEXT_FAINT
        )
        self.hint.setContentsMargins(0, 0, 0, 0)
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(theme.GUTTER, 4, theme.ROW_PAD_X, 0)
        hint_row.addWidget(self.hint)
        lay.addLayout(hint_row)
        self.setSizePolicy(_hfw_policy(QSizePolicy.Policy.Expanding))


# --- подтверждение, тост, подсказки -------------------------------------------------------------------


class ConfirmCard(QFrame):
    """Блок подтверждения: акцентная рамка, summary, details, «просит GPT», кнопки и полоска отсчёта 60 с."""

    def __init__(self, parent: QWidget | None = None, *, animations: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("confirmCard")
        self.request: ConfirmRequest | None = None
        self._fraction = 1.0
        self._animations = animations
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.ICON_X, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(theme.ICON_GAP)
        self.summary = WrapLabel(
            "", font=theme.font(theme.FONT_BODY, QFont.Weight.DemiBold), color=theme.TEXT, line_factor=1.3
        )
        self.icon = StatusIcon(kind="warn", box_h=self.summary.line_height, color=theme.CONFIRM)
        self.gpt = Badge(animations=False)
        self.gpt.set_badge("просит GPT", theme.LEVEL_COLORS["brain"])
        self.more = plain_label("", name="confirmHint")
        self.more.setFont(theme.font(theme.FONT_TINY))
        head.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.summary, 1)
        head.addWidget(self.more, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.gpt, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        self.details = PlainView(
            font=theme.font(theme.FONT_SMALL),
            max_height=132,
            line_height=140,
            margins=(16 + theme.ICON_GAP, 0, 0, 0),
            name="details",
        )
        lay.addWidget(self.details)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(16 + theme.ICON_GAP, 2, 0, 0)
        bottom.setSpacing(8)
        self.hint = plain_label("", name="confirmHint")
        self.hint.setFont(theme.font(theme.FONT_TINY))
        bottom.addWidget(self.hint, 1, Qt.AlignmentFlag.AlignVCenter)
        self.buttons = QWidget(self)
        row = QHBoxLayout(self.buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.no = QPushButton("Нет", self.buttons)
        self.no.setObjectName("confirmNo")
        self.no.setDefault(True)
        self.no.setAutoDefault(False)
        self.no.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.yes = QPushButton("Да (Ctrl+Enter)", self.buttons)
        self.yes.setObjectName("confirmYes")
        self.yes.setAutoDefault(False)
        self.yes.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # «да» — только Ctrl+Enter или мышью
        for b in (self.no, self.yes):
            b.setFont(theme.font(theme.FONT_ROW, QFont.Weight.Medium))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.no)
        row.addWidget(self.yes)
        self.fx = QGraphicsOpacityEffect(self.buttons)
        self.fx.setOpacity(1.0)
        self.buttons.setGraphicsEffect(self.fx)
        bottom.addWidget(self.buttons, 0)
        lay.addLayout(bottom)
        self.setSizePolicy(_hfw_policy(QSizePolicy.Policy.Expanding))

    def set_request(self, req: ConfirmRequest, more: int) -> None:
        self.request = req
        self.summary.setText(req.summary or "Подтвердить действие?")
        self.details.set_text(req.details)
        self.details.setHidden(not req.details.strip())
        self.gpt.setHidden(not req.is_brain)
        self.set_more(more)
        self.updateGeometry()

    def set_more(self, more: int) -> None:
        self.more.setText(f"ещё {more}" if more > 0 else "")
        self.more.setHidden(more <= 0)

    def set_remaining(self, seconds: float, fraction: float) -> None:
        text = f"Enter или Esc — нет · {math.ceil(seconds)} с"
        if self.hint.text() != text:
            self.hint.setText(text)
        fraction = max(0.0, min(1.0, fraction))
        if abs(fraction - self._fraction) > 0.0005:
            self._fraction = fraction
            self.update(0, self.height() - 4, self.width(), 4)

    def set_buttons_shown(self, on: bool) -> None:
        """Кнопки скрыты и выключены, пока идёт защита 700 мс; проявляются после неё."""
        self.no.setEnabled(on)
        self.yes.setEnabled(on)
        self.fx.setOpacity(1.0 if on else 0.0)

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        path = QPainterPath()
        path.addRoundedRect(r, theme.RADIUS_CARD, theme.RADIUS_CARD)
        p.fillPath(path, theme.color(theme.CONFIRM, 0.07))
        p.save()
        p.setClipPath(path)
        bar = QRectF(r.left(), r.bottom() - 2.5, r.width(), 2.5)
        p.fillRect(bar, theme.color(theme.CONFIRM, 0.14))
        p.fillRect(
            QRectF(bar.left(), bar.top(), bar.width() * self._fraction, bar.height()),
            theme.color(theme.CONFIRM, 0.85),
        )
        p.restore()
        p.setPen(QPen(theme.color(theme.CONFIRM, 0.7), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()


class Toast(QWidget):
    """Сообщение об ошибке или предупреждение внутри окна."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = "error"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.ICON_X, 8, 12, 8)
        lay.setSpacing(theme.ICON_GAP)
        self.label = WrapLabel("", font=theme.font(theme.FONT_ROW), color=theme.TEXT)
        self.icon = StatusIcon(kind="warn", box_h=self.label.line_height, color=theme.ERROR)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.label, 1)
        self.setSizePolicy(_hfw_policy(QSizePolicy.Policy.Expanding))

    def text(self) -> str:
        return self.label.text()

    def set_toast(self, text: str, kind: str) -> None:
        self.kind = kind if kind in theme.TOAST_COLORS else "error"
        self.label.setText(text)
        self.icon.set_color(theme.TOAST_COLORS[self.kind])
        self.update()

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tone = theme.TOAST_COLORS[self.kind]
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(theme.color(tone, 0.32), 1))
        p.setBrush(theme.color(tone, 0.10))
        p.drawRoundedRect(r, 10, 10)
        p.end()


class Footer(QWidget):
    """Неброская подсказка клавиш под пустой строкой ввода."""

    HINTS = (("Enter", "отправить"), ("↑ ↓", "история"), ("Esc", "скрыть"))
    PREFIXES = "gpt:  ·  думай:  ·  локально:"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(theme.FOOTER_H)
        self._f_key = theme.font(theme.FONT_TINY, QFont.Weight.Medium)
        self._f_text = theme.font(theme.FONT_TINY)

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(QRectF(0, 0, w, 1), theme.color(theme.BORDER, theme.BORDER_ALPHA * 0.8))
        fm_key, fm_text = QFontMetrics(self._f_key), QFontMetrics(self._f_text)
        x = float(theme.PAD_X)
        cy = (h + 1) / 2
        for key, label in self.HINTS:
            kw = fm_key.horizontalAdvance(key) + 10
            cap = QRectF(x, cy - 9, kw, 18)
            p.setPen(QPen(theme.color("#FFFFFF", 0.09), 1))
            p.setBrush(theme.color("#FFFFFF", 0.045))
            p.drawRoundedRect(cap.adjusted(0.5, 0.5, -0.5, -0.5), 4.5, 4.5)
            p.setFont(self._f_key)
            p.setPen(theme.color(theme.TEXT_MUTED))
            p.drawText(cap, Qt.AlignmentFlag.AlignCenter, key)
            x += kw + 7
            p.setFont(self._f_text)
            p.setPen(theme.color(theme.TEXT_FAINT))
            tw = fm_text.horizontalAdvance(label)
            p.drawText(QRectF(x, 0, tw + 2, h), Qt.AlignmentFlag.AlignVCenter, label)
            x += tw + 18
        p.setFont(self._f_text)
        p.setPen(theme.color(theme.TEXT_FAINT))
        rest = QRectF(x, 0, w - x - theme.PAD_X, h)
        p.drawText(rest, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.PREFIXES)
        p.end()


class Panel(QWidget):
    """Видимая панель окна: скруглённый фон и тонкая рамка (окно с прозрачным фоном рисует её само)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.fill_alpha = 1.0

    def paintEvent(self, event: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(theme.color(theme.BORDER, theme.BORDER_ALPHA), 1))
        p.setBrush(theme.color(theme.BG, self.fill_alpha))
        p.drawRoundedRect(r, theme.RADIUS, theme.RADIUS)
        p.end()


# --- тень ---------------------------------------------------------------------------------------------

SHADOW_SPREAD = 18
_shadow_cache: dict[int, QImage] = {}


def _shadow_image(radius: int) -> QImage:
    """Мягкая тень скруглённого прямоугольника для 9-частной отрисовки (кэш на процесс)."""
    if radius in _shadow_cache:
        return _shadow_cache[radius]
    n, edge = SHADOW_SPREAD, SHADOW_SPREAD + radius
    size = 2 * edge + 1
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    shape = QRectF(n, n, 2 * radius + 1, 2 * radius + 1)
    for i in range(n, 0, -1):
        alpha = 0.022 * (1.0 - i / (n + 1)) ** 0.6
        p.setBrush(theme.color(theme.SHADOW, alpha))
        p.drawRoundedRect(shape.adjusted(-i, -i, i, i), radius + i, radius + i)
    p.setBrush(theme.color(theme.SHADOW, 0.10))
    p.drawRoundedRect(shape, radius, radius)
    p.end()
    _shadow_cache[radius] = img
    return img


def paint_shadow(p: QPainter, panel: QRectF, radius: int, dy: float) -> None:
    """Нарисовать тень под панелью: углы как есть, края растянуты, середину закрывает панель."""
    img = _shadow_image(radius)
    e = SHADOW_SPREAD + radius
    t = panel.translated(0, dy).adjusted(-SHADOW_SPREAD, -SHADOW_SPREAD, SHADOW_SPREAD, SHADOW_SPREAD)
    mid_w, mid_h = t.width() - 2 * e, t.height() - 2 * e
    if mid_w < 0 or mid_h < 0:
        return
    s = float(img.width())
    pieces = [
        (QRectF(t.left(), t.top(), e, e), QRectF(0, 0, e, e)),
        (QRectF(t.right() - e, t.top(), e, e), QRectF(s - e, 0, e, e)),
        (QRectF(t.left(), t.bottom() - e, e, e), QRectF(0, s - e, e, e)),
        (QRectF(t.right() - e, t.bottom() - e, e, e), QRectF(s - e, s - e, e, e)),
        (QRectF(t.left() + e, t.top(), mid_w, e), QRectF(e, 0, 1, e)),
        (QRectF(t.left() + e, t.bottom() - e, mid_w, e), QRectF(e, s - e, 1, e)),
        (QRectF(t.left(), t.top() + e, e, mid_h), QRectF(0, e, e, 1)),
        (QRectF(t.right() - e, t.top() + e, e, mid_h), QRectF(s - e, e, e, 1)),
    ]
    for target, source in pieces:
        p.drawImage(target, img, source)
