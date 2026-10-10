"""Окно-лаунчер Jarvis (PySide6): строка ввода, поток ответа, результаты find, подтверждения, тосты.

Создаётся один раз при старте, дальше только show_launcher()/hide_launcher(). Все методы — в UI-потоке;
on_event — O(1) на событие (текст дописывается, блоки добавляются, ничего не пересобирается).
Покой: без запроса не работает ни одна анимация и ни один таймер.
"""

import logging
import sys
import time

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QKeyEvent, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.config import UiConfig
from jarvis.events import Done, Items, Level, Status, TextChunk
from jarvis.ui import logic, theme
from jarvis.ui.logic import ConfirmRequest
from jarvis.ui.widgets import (
    Badge,
    ConfirmCard,
    Footer,
    Glyph,
    IconKind,
    ItemsBlock,
    Panel,
    PlainView,
    ProgressStrip,
    StatusIcon,
    StatusRow,
    Toast,
    paint_shadow,
)

log = logging.getLogger("jarvis")

PLACEHOLDER = "Скажи, что сделать…"
ACRYLIC_FILL = 0.86  # непрозрачность панели поверх системного acrylic


def _hfw(widget: QWidget, width: int) -> int:
    h = widget.heightForWidth(width)
    return h if h >= 0 else widget.sizeHint().height()


def _keep_height() -> QSizePolicy:
    sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    sp.setHeightForWidth(True)
    return sp


def _strip_gear(text: str) -> str:
    """«⚙ open: Telegram» → «open: Telegram» (значок окно рисует само)."""
    return text.lstrip().removeprefix("⚙").lstrip()


class LauncherWindow(QWidget):
    """Окно Jarvis. Контракт для app.py — см. docs/architecture.md (jarvis.app и jarvis.ui)."""

    submitted = Signal(str)
    cancel_requested = Signal()
    open_item = Signal(int)
    hidden = Signal()

    def __init__(self, ui_cfg: UiConfig, parent: QWidget | None = None) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        super().__init__(parent, flags)
        self.setObjectName("launcher")
        self.setWindowTitle("Jarvis")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._cfg = ui_cfg
        self._anim = bool(ui_cfg.animations)
        self._history = logic.History()
        self._confirms = logic.ConfirmQueue()
        self._autohide = logic.AutoHide(ui_cfg.autohide_s)
        self._busy = False
        self._cancel_sent = False
        self._level = ""
        self._local = False
        self._got_text = False
        self._hiding = False
        self._buttons_shown = False
        self._stick_bottom = True
        self._items_count = 0
        self._blocks: list[QWidget] = []
        self._rows: dict[str, StatusRow] = {}
        self._spinners: set[StatusIcon] = set()
        self._text_view: PlainView | None = None
        self._cancel_row: StatusRow | None = None
        self._screen_h = 1080
        self._panel_w = max(360, int(ui_cfg.width))
        self._home = QPoint(0, 0)

        self.setStyleSheet(theme.stylesheet())
        self.setFont(theme.font(theme.FONT_ROW))
        self._build()
        self._build_motion()
        self._update_sections()
        self.resize(self._panel_w + self._margin_w(), self._window_height())
        self._apply_dwm()
        # стили, раскладка и кэш глифов — сразу после старта, а не при первом хоткее (бюджет ≤100 мс)
        QTimer.singleShot(0, self, self._warm_up)

    # --- построение -----------------------------------------------------------------------------------

    def _margin_w(self) -> int:
        left, _, right, _ = theme.SHADOW_MARGIN
        return left + right

    def _margin_h(self) -> int:
        _, top, _, bottom = theme.SHADOW_MARGIN
        return top + bottom

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.SHADOW_MARGIN)
        outer.setSpacing(0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._panel = Panel(self)
        outer.addWidget(self._panel)
        lay = QVBoxLayout(self._panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # строка ввода
        self._input_row = QWidget(self._panel)
        self._input_row.setFixedHeight(theme.INPUT_H)
        row = QHBoxLayout(self._input_row)
        row.setContentsMargins(theme.PAD_X, 0, theme.PAD_X - 4, 0)
        row.setSpacing(10)
        self._glyph = Glyph(self._input_row)
        row.addWidget(self._glyph)
        self._input = QLineEdit(self._input_row)
        self._input.setObjectName("input")
        self._input.setFont(theme.font(theme.FONT_INPUT, display=True))
        self._input.setPlaceholderText(PLACEHOLDER)
        self._input.setFrame(False)
        self._input.setMaxLength(4000)
        pal = self._input.palette()
        pal.setColor(QPalette.ColorRole.PlaceholderText, theme.color(theme.TEXT_FAINT))
        pal.setColor(QPalette.ColorRole.Text, theme.color(theme.TEXT))
        self._input.setPalette(pal)
        self._input.installEventFilter(self)
        row.addWidget(self._input, 1)
        self._level_badge = Badge(self._input_row, animations=self._anim)
        self._level_badge.hide()
        row.addWidget(self._level_badge)
        self._local_badge = Badge(self._input_row, animations=False, dot=True)
        self._local_badge.set_badge("локально", theme.LOCAL_MODE)
        self._local_badge.hide()
        row.addWidget(self._local_badge)
        lay.addWidget(self._input_row)

        self._strip = ProgressStrip(self._panel)
        lay.addWidget(self._strip)

        # ответ
        self._scroll = QScrollArea(self._panel)
        self._scroll.setObjectName("scroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setObjectName("scrollViewport")
        self._scroll.viewport().setAutoFillBackground(False)
        self._body = QWidget()
        self._body.setObjectName("body")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(*theme.BODY_PAD)
        self._body_layout.setSpacing(2)
        self._body_layout.addStretch(1)
        self._scroll.setWidget(self._body)
        bar = self._scroll.verticalScrollBar()
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_scroll_range)
        # высоту окна считает _relayout; при росте и сжатии уступает только ответ, а не карточка и тост
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._scroll.setMinimumHeight(0)
        lay.addWidget(self._scroll, 1)

        # подтверждение
        self._card_wrap = QWidget(self._panel)
        wrap = QVBoxLayout(self._card_wrap)
        wrap.setContentsMargins(10, 4, 10, 10)
        self._card = ConfirmCard(self._card_wrap, animations=self._anim)
        self._card.no.clicked.connect(lambda: self._confirm_click(False))
        self._card.yes.clicked.connect(lambda: self._confirm_click(True))
        self._card.no.installEventFilter(self)
        self._card.details.installEventFilter(self)
        wrap.addWidget(self._card)
        self._card_wrap.setSizePolicy(_keep_height())
        lay.addWidget(self._card_wrap)

        # тост
        self._toast_wrap = QWidget(self._panel)
        wrap = QVBoxLayout(self._toast_wrap)
        wrap.setContentsMargins(10, 0, 10, 10)
        self._toast = Toast(self._toast_wrap)
        wrap.addWidget(self._toast)
        self._toast_wrap.setSizePolicy(_keep_height())
        lay.addWidget(self._toast_wrap)

        self._footer = Footer(self._panel)
        lay.addWidget(self._footer)
        lay.addStretch(0)

        self._scroll.hide()
        self._card_wrap.hide()
        self._toast_wrap.hide()

    def _build_motion(self) -> None:
        # бегущий градиент и спиннеры — одна анимация, только пока идёт запрос и окно видно
        self._driver = QVariantAnimation(self)
        self._driver.setStartValue(0.0)
        self._driver.setEndValue(1.0)
        self._driver.setDuration(theme.DRIVER_MS)
        self._driver.setLoopCount(-1)
        self._driver.valueChanged.connect(self._on_tick)

        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.setDuration(theme.SHOW_MS)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(theme.SHOW_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._show_anim = QParallelAnimationGroup(self)
        self._show_anim.addAnimation(self._fade_in)
        self._show_anim.addAnimation(self._slide)

        self._hide_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._hide_anim.setDuration(theme.HIDE_MS)
        self._hide_anim.setEndValue(0.0)
        self._hide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hide_anim.finished.connect(self._finish_hide)

        self._height_anim = QVariantAnimation(self)
        self._height_anim.setDuration(theme.HEIGHT_MS)
        self._height_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._height_anim.valueChanged.connect(self._on_height)

        self._buttons_anim = QPropertyAnimation(self._card.fx, b"opacity", self)
        self._buttons_anim.setDuration(theme.BUTTONS_FADE_MS)
        self._buttons_anim.setStartValue(0.0)
        self._buttons_anim.setEndValue(1.0)
        self._buttons_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(0)
        self._relayout_timer.timeout.connect(self._relayout)

        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self._on_autohide)

        self._confirm_timer = QTimer(self)
        self._confirm_timer.setInterval(theme.CONFIRM_TICK_MS)
        self._confirm_timer.timeout.connect(self._confirm_tick)

    def _apply_dwm(self) -> None:
        """Тёмная рамка, скругления Windows 11 и (по желанию) acrylic; не вышло — сплошной фон."""
        if sys.platform != "win32":
            return
        from jarvis.ui import dwm

        try:
            acrylic = dwm.apply(int(self.winId()), self._cfg.backdrop)
        except Exception as e:  # оформление необязательно — окно работает и без него
            log.debug("DWM: %s", e)
            acrylic = False
        self._panel.fill_alpha = ACRYLIC_FILL if acrylic else 1.0

    # --- контракт для app.py --------------------------------------------------------------------------

    def show_launcher(self) -> None:
        """Показать окно сверху по центру экрана с курсором, активировать и сразу дать фокус вводу."""
        self._place()
        if self._hiding or self._hide_anim.state() == QAbstractAnimation.State.Running:
            self._hide_anim.stop()
            self._hiding = False
            self.setWindowOpacity(1.0)
            self.move(self._home)
        if not self.isVisible():
            self._height_anim.stop()
            self.resize(self.width(), self._window_height())
            if self._anim:
                self.setWindowOpacity(0.0)
                self.move(self._home.x(), self._home.y() - theme.SHOW_SHIFT_PX)
                self.show()
                self._fade_in.setStartValue(0.0)
                self._slide.setStartValue(self.pos())
                self._slide.setEndValue(self._home)
                self._show_anim.start()
            else:
                self.setWindowOpacity(1.0)
                self.move(self._home)
                self.show()
        self.raise_()
        if not self.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating):
            self.activateWindow()
        if self._confirms.current is not None and self._buttons_shown:
            self._card.no.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        else:
            self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            self._input.selectAll()
        self._sync_motion()

    def hide_launcher(self) -> None:
        """Спрятать (90 мс). Неотвеченные подтверждения — «нет»: человек их больше не видит."""
        self._autohide.cancel()
        self._autohide_timer.stop()
        if self._confirms.current is not None:
            self._confirms.deny_all()
            self._render_confirm()
        if not self.isVisible() or self._hiding:
            return
        self._show_anim.stop()
        if self._anim:
            self._hiding = True
            self._sync_motion()
            self._hide_anim.setStartValue(self.windowOpacity())
            self._hide_anim.start()
        else:
            self._finish_hide()

    def begin_request(self, text: str) -> None:
        """Начался запрос: очистить прошлый ответ, включить индикатор работы."""
        self._autohide.cancel()
        self._autohide_timer.stop()
        if text and self._input.text() != text:
            self._input.setText(text)
        self._input.selectAll()
        self._history.reset()
        self._start_busy()

    def on_event(self, ev: object) -> None:
        """Событие запроса (jarvis.events) — в UI-потоке."""
        if isinstance(ev, Level):
            self._on_level(ev)
        elif isinstance(ev, Status):
            self._on_status(ev)
        elif isinstance(ev, TextChunk):
            self._on_text(ev.text)
        elif isinstance(ev, Items):
            self._on_items(ev.items)
        elif isinstance(ev, Done):
            self._on_done(ev)

    def ask_confirm(self, req: ConfirmRequest) -> None:
        """Поставить подтверждение в очередь; на экране — по одному. Окно показывается само."""
        if req.done:
            return
        self._confirms.push(req)
        if not self.isVisible() or self._hiding:
            self.show_launcher()
        self._render_confirm()

    def set_local_mode(self, on: bool) -> None:
        """Локальный режим: спокойный бейдж «локально» в строке ввода."""
        self._local = bool(on)
        self._local_badge.setVisible(self._local)
        self._refresh_level_badge()

    def show_toast(self, text: str, kind: str = "error") -> None:
        """Ошибка или предупреждение внутри окна (до следующего запроса)."""
        self._autohide.cancel()
        self._autohide_timer.stop()
        self._toast.set_toast(text, kind)
        self._toast_wrap.show()
        self._update_sections()
        self._schedule_relayout()

    def is_busy(self) -> bool:
        return self._busy

    # --- события запроса ------------------------------------------------------------------------------

    def _start_busy(self) -> None:
        self._reset_body()
        self._busy = True
        self._cancel_sent = False
        self._level = ""
        self._refresh_level_badge()
        self._set_tone(QColor(theme.ACCENT))
        self._update_sections()
        self._sync_motion()
        self._schedule_relayout()

    def _on_level(self, ev: Level) -> None:
        if not self._busy:
            self._start_busy()
        self._level = ev.level
        self._refresh_level_badge()
        self._set_tone(QColor(theme.LEVEL_COLORS.get(ev.level, theme.ACCENT)))

    def _on_status(self, ev: Status) -> None:
        text = _strip_gear(ev.text)
        if ev.kind == "warn":
            self.show_toast(text, "warn")
            return
        kind: IconKind = "dot"
        if ev.done:
            kind = "ok" if ev.ok else "fail"
        elif ev.kind == "tool" or ev.key:
            kind = "spin"
        row = self._rows.get(ev.key) if ev.key else None
        if row is None:
            row = StatusRow(text, kind, color=self._tone().name())
            self._add_block(row)
            if ev.key:
                self._rows[ev.key] = row
        else:
            row.set_state(text, kind)
        if kind == "spin":
            self._spinners.add(row.icon)
        else:
            self._spinners.discard(row.icon)
        self._sync_motion()

    def _on_text(self, chunk: str) -> None:
        if not chunk:
            return
        view = self._text_view
        if view is None or not self._blocks or self._blocks[-1] is not view:
            view = self._new_text_view()
        view.append_text(chunk)
        self._got_text = True
        self._schedule_relayout()

    def _on_items(self, items: list[str]) -> None:
        if not items:
            return
        block = ItemsBlock(list(items))
        block.activated.connect(self._open_item)
        self._add_block(block)
        self._items_count = len(items)

    def _on_done(self, ev: Done) -> None:
        cancelled = bool(ev.cancelled)
        final: IconKind = "stop" if cancelled else ("ok" if ev.ok else "fail")
        for icon in self._spinners:
            icon.set_kind(final)
        self._spinners.clear()
        self._busy = False
        self._cancel_sent = False
        cancel_row, self._cancel_row = self._cancel_row, None
        if cancelled:
            if cancel_row is not None:
                cancel_row.set_state("Отменено", "stop")
            else:
                self._add_block(StatusRow("Отменено", "stop"))
        else:
            if cancel_row is not None:
                self._remove_block(cancel_row)  # запрос успел закончиться сам
            self._show_result(ev)
        self._update_sections()
        self._sync_motion()
        self._schedule_relayout()
        if self._autohide.arm(ev.ok and not cancelled, ev.autohide):
            self._autohide_timer.start(int(self._autohide.delay_s * 1000))

    def _show_result(self, ev: Done) -> None:
        """Итог запроса: ошибка — тостом; текст без потока — подписью, строкой ✓ или абзацем."""
        if not ev.ok:
            self.show_toast(ev.text or "Не получилось", "error")
            return
        if not ev.text or self._got_text:
            return
        found = next((b for b in self._blocks if isinstance(b, ItemsBlock)), None)
        if found is not None:  # «Нашёл 5 файлов» — подписью над списком
            self._add_block(StatusRow(ev.text, "dot"), before=found)
        elif ev.autohide:  # действие без ответа: «Открыл Telegram»
            self._add_block(StatusRow(ev.text, "ok", strong=True))
        else:
            self._new_text_view().set_text(ev.text)

    # --- блоки ответа ---------------------------------------------------------------------------------

    def _new_text_view(self) -> PlainView:
        view = PlainView(font=theme.font(theme.FONT_BODY), margins=(theme.GUTTER, 3, theme.ROW_PAD_X, 5))
        view.installEventFilter(self)
        self._text_view = view
        self._add_block(view)
        return view

    def _add_block(self, widget: QWidget, before: QWidget | None = None) -> None:
        if before is not None and before in self._blocks:
            self._body_layout.insertWidget(self._body_layout.indexOf(before), widget)
            self._blocks.insert(self._blocks.index(before), widget)
        else:
            self._body_layout.insertWidget(self._body_layout.count() - 1, widget)
            self._blocks.append(widget)
        widget.show()
        if self._scroll.isHidden():
            self._scroll.show()
            self._stick_bottom = True
        self._update_sections()
        self._schedule_relayout()

    def _remove_block(self, widget: QWidget) -> None:
        if widget in self._blocks:
            self._blocks.remove(widget)
            self._body_layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
            self._schedule_relayout()

    def _reset_body(self) -> None:
        for block in self._blocks:
            self._body_layout.removeWidget(block)
            block.hide()
            block.deleteLater()
        self._blocks.clear()
        self._rows.clear()
        self._spinners.clear()
        self._text_view = None
        self._cancel_row = None
        self._items_count = 0
        self._got_text = False
        self._scroll.hide()
        self._toast_wrap.hide()

    def _open_item(self, index: int) -> None:
        self._autohide.cancel()
        self._autohide_timer.stop()
        self.open_item.emit(index)

    # --- подтверждения --------------------------------------------------------------------------------

    def _render_confirm(self) -> None:
        req = self._confirms.current
        if req is None:
            self._confirm_timer.stop()
            self._buttons_anim.stop()
            self._card.request = None
            if not self._card_wrap.isHidden():
                self._card_wrap.hide()
                if self.isVisible():
                    self._input.setFocus(Qt.FocusReason.OtherFocusReason)
            self._update_sections()
            self._schedule_relayout()
            return
        if self._card.request is not req:
            self._card.set_request(req, len(self._confirms) - 1)
            self._buttons_anim.stop()
            self._buttons_shown = False
            self._card.set_buttons_shown(False)
        else:
            self._card.set_more(len(self._confirms) - 1)
        self._card_wrap.show()
        self._update_sections()
        self._confirm_tick()
        self._sync_motion()
        self._schedule_relayout()

    def _confirm_tick(self) -> None:
        now = time.monotonic()
        if self._confirms.prune(now):
            self._render_confirm()
            return
        req = self._confirms.current
        if req is None:
            self._confirm_timer.stop()
            return
        left = req.remaining(now, self._confirms.timeout_s)
        self._card.set_remaining(left, left / self._confirms.timeout_s)
        if not self._buttons_shown and not self._confirms.guarded(now):
            self._buttons_shown = True
            self._card.set_buttons_shown(True)
            if self._anim and self.isVisible():
                self._card.fx.setOpacity(0.0)
                self._buttons_anim.start()
            if self.isVisible() and (self._input.hasFocus() or self.focusWidget() is None):
                self._card.no.setFocus(Qt.FocusReason.OtherFocusReason)

    def _confirm_key(self, name: str) -> None:
        if self._confirms.key(name) is not None:
            self._render_confirm()

    def _confirm_click(self, approved: bool) -> None:
        if self._confirms.click(approved) is not None:
            self._render_confirm()

    # --- клавиши и мышь -------------------------------------------------------------------------------

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:
        t = ev.type()
        if t == QEvent.Type.KeyPress and isinstance(ev, QKeyEvent):
            return self._on_key(obj, ev)
        if t == QEvent.Type.MouseButtonPress:
            self._autohide.cancel()
            self._autohide_timer.stop()
        return False

    def keyPressEvent(self, ev: QKeyEvent) -> None:
        if not self._on_key(self, ev):
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev: object) -> None:
        self._autohide.cancel()
        self._autohide_timer.stop()
        super().mousePressEvent(ev)  # type: ignore[arg-type]

    def _on_key(self, obj: QObject, ev: QKeyEvent) -> bool:
        """Клавиши окна. True — обработано (дальше не передавать)."""
        self._autohide.cancel()
        self._autohide_timer.stop()
        key = ev.key()
        enter = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if (enter or key == Qt.Key.Key_Escape) and ev.isAutoRepeat():
            # удержанная клавиша (например, Ctrl+Enter «отправить» в мессенджере, пока окно всплыло само)
            # не отвечает на подтверждение и не шлёт запрос повторно
            return True
        if self._confirms.current is not None:
            if enter:
                self._confirm_key("ctrl+enter" if ctrl else "enter")
                return True
            if key == Qt.Key.Key_Escape:
                self._confirm_key("esc")
                return True
            if key == Qt.Key.Key_Space and obj is self._card.no:
                return False  # пробел на «Нет» — обычное нажатие кнопки, это «нет»
        if key == Qt.Key.Key_Escape:
            action = logic.esc_action(
                confirm_shown=self._confirms.current is not None,
                busy=self._busy,
                cancel_sent=self._cancel_sent,
            )
            if action == "cancel":
                self._cancel_request()
            elif action == "hide":
                self.hide_launcher()
            return True
        if enter:
            if self._confirms.current is None:
                self._submit()
            return True
        if obj is self._input and key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            return True  # фокус остаётся в строке ввода
        if obj is self._input and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            text = self._input.text()
            value = self._history.up(text) if key == Qt.Key.Key_Up else self._history.down(text)
            if value is not None:
                self._input.setText(value)
                self._input.end(False)
            return True
        if obj is not self._input and ev.text() and ev.text().isprintable() and not ctrl:
            if self._confirms.current is not None and obj is self._card.no:
                return True  # набор текста при открытой карточке не уходит в «Нет»
            self._input.setFocus(Qt.FocusReason.OtherFocusReason)
            self._input.insert(ev.text())
            return True
        return False

    def _cancel_request(self) -> None:
        """Первый Esc при идущем запросе: сразу видно «Отменяю…», ядро получает cancel_requested."""
        self._cancel_sent = True
        if self._cancel_row is None:
            self._cancel_row = StatusRow("Отменяю…", "dot")
            self._add_block(self._cancel_row)
        self.cancel_requested.emit()

    def _submit(self) -> None:
        text = self._input.text()
        index = logic.parse_index(text, self._items_count)
        if index is not None:
            self._input.clear()
            self._open_item(index)
            return
        text = text.strip()
        if not text:
            return
        self._history.add(text)
        self.submitted.emit(text)

    # --- окно: события Qt -----------------------------------------------------------------------------

    def changeEvent(self, ev: QEvent) -> None:
        super().changeEvent(ev)
        if ev.type() != QEvent.Type.ActivationChange or self.isActiveWindow():
            return
        # потеря фокуса прячет окно, только если нет запроса и открытого подтверждения
        if self.isVisible() and not self._hiding and not self._busy and self._confirms.current is None:
            self.hide_launcher()

    def leaveEvent(self, ev: QEvent) -> None:
        super().leaveEvent(ev)
        if self._autohide.leave():
            self._autohide_timer.start(int(self._autohide.delay_s * 1000))

    def closeEvent(self, ev: object) -> None:
        if ev.spontaneous():  # type: ignore[attr-defined]
            ev.ignore()  # type: ignore[attr-defined]
            self.hide_launcher()
            return
        self._stop_motion()
        super().closeEvent(ev)  # type: ignore[arg-type]

    def paintEvent(self, ev: object) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        panel = QRectF(self._panel.geometry())
        if self._panel.fill_alpha < 1.0:  # acrylic: тень не должна темнить полупрозрачную панель
            outside = QPainterPath()
            outside.addRect(QRectF(self.rect()))
            inside = QPainterPath()
            inside.addRoundedRect(panel, theme.RADIUS, theme.RADIUS)
            p.setClipPath(outside.subtracted(inside))
        paint_shadow(p, panel, theme.RADIUS, theme.SHADOW_OFFSET_Y)
        p.end()

    # --- движение и покой -----------------------------------------------------------------------------

    def _sync_motion(self) -> None:
        """Включить ровно то, что сейчас нужно; в покое — ни одной анимации и ни одного таймера."""
        visible = self.isVisible() and not self._hiding
        if self._busy:
            self._strip.set_mode("run" if self._anim else "static")
        else:
            self._strip.set_mode("line" if self._blocks else "off")
        if self._busy and visible and self._anim:
            if self._driver.state() != QAbstractAnimation.State.Running:
                self._driver.start()
        else:
            self._driver.stop()
        if self._confirms.current is not None and visible:
            if not self._confirm_timer.isActive():
                self._confirm_timer.start()
        else:
            self._confirm_timer.stop()

    def _stop_motion(self) -> None:
        for anim in (self._driver, self._show_anim, self._hide_anim, self._height_anim, self._buttons_anim):
            anim.stop()
        for timer in (self._relayout_timer, self._autohide_timer, self._confirm_timer):
            timer.stop()

    def _on_tick(self, value: object) -> None:
        phase = float(value) if isinstance(value, int | float) else 0.0
        self._strip.set_phase(phase)
        angle = phase * 720.0
        for icon in self._spinners:
            icon.set_angle(angle)

    def _warm_up(self) -> None:
        if self.isVisible():
            return
        self.ensurePolished()
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self.grab()

    def _finish_hide(self) -> None:
        self._hiding = True  # потеря фокуса внутри hide() не должна прятать окно второй раз
        self._stop_motion()
        self.hide()
        self._hiding = False
        self.setWindowOpacity(1.0)
        self.move(self._home)
        if not self._busy:
            self._reset_body()
            self._level = ""
            self._refresh_level_badge()
            self._set_tone(QColor(theme.ACCENT))
            self._update_sections()
            self._strip.set_mode("off")
            self.resize(self.width(), self._window_height())
        self.hidden.emit()

    def _on_autohide(self) -> None:
        hovered = self.isVisible() and self.underMouse()
        if not self._autohide.fire(hovered):
            return
        if self._busy or self._confirms.current is not None:
            return
        self.hide_launcher()

    # --- геометрия ------------------------------------------------------------------------------------

    def _place(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self._screen_h = area.height()
        self._panel_w = max(360, min(int(self._cfg.width), area.width() - 32))
        left, top, _, _ = theme.SHADOW_MARGIN
        x = area.x() + (area.width() - self._panel_w) // 2 - left
        y = area.y() + int(area.height() * theme.TOP_FRACTION) - top
        self._home = QPoint(x, y)
        width = self._panel_w + self._margin_w()
        if self.width() != width:
            self.resize(width, self.height())

    def _measure(self) -> tuple[int, int]:
        """(обязательная часть панели, ответ) при текущей ширине."""
        w = self._panel_w
        fixed = theme.INPUT_H + theme.STRIP_H
        for section in (self._card_wrap, self._toast_wrap):
            if not section.isHidden():
                fixed += _hfw(section, w)
        if not self._footer.isHidden():
            fixed += theme.FOOTER_H
        body = 0
        if not self._scroll.isHidden() and self._blocks:
            bar = self._scroll.verticalScrollBar()
            scrolling = self._scroll.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if scrolling and not bar.isHidden():
                w -= bar.width()  # ответ уже прокручивается — мерить по ширине видимой области
            body = _hfw(self._body, w)
        return fixed, body

    def _panel_height(self) -> int:
        fixed, body = self._measure()
        return logic.target_height(fixed, body, self._screen_h)

    def _window_height(self) -> int:
        return self._panel_height() + self._margin_h()

    def _schedule_relayout(self) -> None:
        if not self._relayout_timer.isActive():
            self._relayout_timer.start()

    def _relayout(self) -> None:
        fixed, body = self._measure()
        panel_h = logic.target_height(fixed, body, self._screen_h)
        overflow = body > panel_h - fixed
        policy = Qt.ScrollBarPolicy.ScrollBarAsNeeded if overflow else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        if self._scroll.verticalScrollBarPolicy() != policy:
            self._scroll.setVerticalScrollBarPolicy(policy)
        target = panel_h + self._margin_h()
        if target == self.height() and self._height_anim.state() != QAbstractAnimation.State.Running:
            return
        if self._anim and self.isVisible() and not self._hiding:
            if (
                self._height_anim.state() == QAbstractAnimation.State.Running
                and self._height_anim.endValue() == target
            ):
                return
            self._height_anim.stop()
            self._height_anim.setStartValue(self.height())
            self._height_anim.setEndValue(target)
            self._height_anim.start()
        else:
            self._height_anim.stop()
            self.resize(self.width(), target)

    def _on_height(self, value: object) -> None:
        if isinstance(value, int):
            self.resize(self.width(), value)

    def _on_scrolled(self, value: int) -> None:
        bar = self._scroll.verticalScrollBar()
        self._stick_bottom = value >= bar.maximum() - 4

    def _on_scroll_range(self, _min: int, maximum: int) -> None:
        if self._stick_bottom:
            self._scroll.verticalScrollBar().setValue(maximum)

    # --- мелочи ---------------------------------------------------------------------------------------

    def _update_sections(self) -> None:
        empty = (
            not self._busy and not self._blocks and self._card_wrap.isHidden() and self._toast_wrap.isHidden()
        )
        self._footer.setHidden(not empty)

    def _tone(self) -> QColor:
        return QColor(theme.LEVEL_COLORS.get(self._level, theme.ACCENT))

    def _set_tone(self, value: QColor) -> None:
        self._strip.set_color(value)
        self._glyph.set_color(value)
        for icon in self._spinners:
            icon.set_color(value)

    def _refresh_level_badge(self) -> None:
        level = self._level
        if not level or (level == "local" and self._local):
            self._level_badge.hide()
            return
        self._level_badge.set_badge(
            logic.LEVEL_LABELS.get(level, level), theme.LEVEL_COLORS.get(level, theme.ACCENT)
        )
        self._level_badge.show()
