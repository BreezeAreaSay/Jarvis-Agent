"""Окно Jarvis офскрин (QT_QPA_PLATFORM=offscreen): события, клавиши, подтверждения, PlainText, покой."""

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # до создания QApplication

from PySide6.QtCore import QAbstractAnimation, QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QTextEdit

from jarvis.config import UiConfig
from jarvis.events import Done, Items, Level, Status, TextChunk
from jarvis.ui import widgets
from jarvis.ui.logic import ConfirmRequest
from jarvis.ui.window import LauncherWindow

EVIL = '<b>x</b><a href="file:///C:/">y</a>'
FOUND = [rf"C:\Users\me\Documents\отчёт {i}.docx" for i in range(1, 6)]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


@pytest.fixture
def make(qapp: QApplication) -> Iterator[Any]:
    created: list[LauncherWindow] = []

    def factory(**kw: Any) -> LauncherWindow:
        w = LauncherWindow(UiConfig(**kw))
        created.append(w)
        return w

    yield factory
    for w in created:
        w.close()
        w.deleteLater()
    qapp.processEvents()


def record(signal: Any) -> list[Any]:
    got: list[Any] = []
    signal.connect(lambda *a: got.append(a[0] if len(a) == 1 else a))
    return got


def running(w: LauncherWindow) -> list[str]:
    """Что сейчас тратит CPU: активные QTimer и работающие анимации окна."""
    busy = [f"timer:{t.objectName() or t.interval()}" for t in w.findChildren(QTimer) if t.isActive()]
    busy += [
        f"anim:{type(a).__name__}"
        for a in w.findChildren(QAbstractAnimation)
        if a.state() == QAbstractAnimation.State.Running
    ]
    return busy


def settle(ms: int = 260) -> None:
    QTest.qWait(ms)


def blocks(w: LauncherWindow, cls: type) -> list[Any]:
    return [b for b in w._blocks if isinstance(b, cls)]


def unguard(w: LauncherWindow) -> None:
    """Сдвинуть время появления подтверждения: защита 700 мс уже прошла."""
    w._confirms.shown_at = time.monotonic() - 1.0
    w._confirm_tick()


# --- форма и показ ------------------------------------------------------------------------------------


def test_window_flags_and_hidden_at_start(make: Any) -> None:
    w = make()
    flags = w.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert (flags & Qt.WindowType.Tool) == Qt.WindowType.Tool
    assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not w.isVisible()
    assert running(w) == []


def test_show_places_top_center_with_focus(make: Any, qapp: QApplication) -> None:
    w = make(width=720)
    w.show_launcher()
    settle()
    assert w.isVisible()
    assert w._input.hasFocus() or qapp.focusWidget() is w._input
    area = w.screen().availableGeometry()
    panel = w._panel.geometry().translated(w.pos())
    assert panel.width() == 720
    assert abs(panel.center().x() - area.center().x()) <= 1
    top = (panel.top() - area.top()) / area.height()
    assert 0.18 <= top <= 0.22
    assert w._input.placeholderText() == "Скажи, что сделать…"
    assert not w._footer.isHidden()  # подсказка клавиш в пустом окне


def test_show_hide_animations_then_idle(make: Any) -> None:
    w = make()
    hidden = record(w.hidden)
    w.show_launcher()
    assert any(a.startswith("anim") for a in running(w))  # появление 140 мс
    settle()
    assert running(w) == []
    w.hide_launcher()
    settle()
    assert not w.isVisible()
    assert len(hidden) == 1
    assert running(w) == []


def test_no_animations_mode_is_instant(make: Any) -> None:
    w = make(animations=False)
    hidden = record(w.hidden)
    w.show_launcher()
    assert w.isVisible() and running(w) in ([], ["timer:0"])
    w.begin_request("громкость 20")
    w.on_event(Level("hands", "router"))
    w.on_event(Status("⚙ vol: 20", kind="tool", key="v"))
    QApplication.processEvents()
    assert w._strip.mode == "static"  # статичный индикатор
    assert not any(a.startswith("anim") for a in running(w))
    w.on_event(Done(True, "Громкость 20", level="hands"))
    QApplication.processEvents()
    assert running(w) == []
    w.hide_launcher()
    assert not w.isVisible() and len(hidden) == 1  # мгновенно


# --- события ------------------------------------------------------------------------------------------


def test_events_change_widgets(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("закрой окно хрома")
    assert w.is_busy()
    assert w._strip.mode == "run"
    w.on_event(Level("hands", "router:default", "4b"))
    assert w._level_badge.text() == "руки" and not w._level_badge.isHidden()
    w.on_event(Status("⚙ close: Google Chrome", kind="tool", key="c1"))
    rows = blocks(w, widgets.StatusRow)
    assert len(rows) == 1 and rows[0].icon.kind == "spin"
    assert rows[0].text() == "close: Google Chrome"
    w.on_event(Status("⚙ close: Google Chrome", kind="tool", key="c1", done=True, ok=False))
    rows = blocks(w, widgets.StatusRow)
    assert len(rows) == 1 and rows[0].icon.kind == "fail"  # та же строка: спиннер → ✗
    w.on_event(Status("⚙ буфер: привет", kind="tool", key="b1", done=True, ok=True))
    assert blocks(w, widgets.StatusRow)[-1].icon.kind == "ok"
    w.on_event(TextChunk("Первая часть, "))
    w.on_event(TextChunk("вторая часть."))
    views = blocks(w, widgets.PlainView)
    assert len(views) == 1 and views[0].text() == "Первая часть, вторая часть."
    w.on_event(Items(FOUND))
    items = blocks(w, widgets.ItemsBlock)
    assert len(items) == 1 and [r.text() for r in items[0].rows] == FOUND
    w.on_event(Done(True, level="hands"))
    assert not w.is_busy()
    assert w._strip.mode == "line"
    settle()
    assert running(w) == []


def test_text_stream_appends_without_rebuild(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("gpt: расскажи")
    w.on_event(Level("brain", "prefix"))
    w.on_event(TextChunk("а"))
    view = blocks(w, widgets.PlainView)[0]
    for _ in range(50):
        w.on_event(TextChunk("б"))
    assert blocks(w, widgets.PlainView) == [view]  # дописывается тот же виджет
    assert view.text() == "а" + "б" * 50
    w.on_event(Status("⚙ шаг", kind="tool", key="s"))
    w.on_event(TextChunk("после шага"))
    assert len(blocks(w, widgets.PlainView)) == 2  # текст после строки «⚙» — новым блоком, по порядку


def test_window_grows_smoothly_and_caps(make: Any) -> None:
    w = make()
    w.show_launcher()
    settle()
    h0 = w.height()
    w.begin_request("gpt: длинно")
    w.on_event(Level("brain", "prefix"))
    w.on_event(TextChunk("строка ответа\n" * 3))
    QApplication.processEvents()
    assert w._height_anim.state() == QAbstractAnimation.State.Running  # плавно, 120 мс
    settle()
    h1 = w.height()
    assert h1 > h0
    w.on_event(TextChunk("ещё строка\n" * 200))
    settle()
    panel_h = w._panel.height()
    assert panel_h <= int(w._screen_h * 0.4) + 1  # не выше ~40 % экрана
    assert w._scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    w.on_event(Done(True, level="brain"))
    settle()
    assert running(w) == []


def test_overflow_text_width_matches_viewport(make: Any) -> None:
    """Длинный ответ с полосой прокрутки: текст разложен ровно по видимой ширине (правый край не срезан)."""
    w = make()
    w.show_launcher()
    w.begin_request("gpt: длинно")
    w.on_event(Level("brain", "prefix"))
    for i in range(40):
        w.on_event(TextChunk(f"строка {i} " + "слово " * 20 + "\n"))
        QTest.qWait(2)
    settle(300)
    assert not w._scroll.verticalScrollBar().isHidden()
    view = blocks(w, widgets.PlainView)[0]
    assert view.document().textWidth() == pytest.approx(view.viewport().width(), abs=0.5)
    w.on_event(TextChunk("ещё " * 40))
    settle()
    w._relayout()  # высота уже упёрлась в предел — после замера раскладки не будет
    assert view.document().textWidth() == pytest.approx(view.viewport().width(), abs=0.5)


def test_done_error_shows_toast_and_finishes_spinners(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("громкость 20")
    w.on_event(Level("hands", "router"))
    w.on_event(Status("⚙ vol: 20", kind="tool", key="v"))
    w.on_event(Done(False, "Руки не ответили за 4 с", level="hands"))
    assert not w._toast_wrap.isHidden()
    assert w._toast.text() == "Руки не ответили за 4 с" and w._toast.kind == "error"
    assert blocks(w, widgets.StatusRow)[0].icon.kind == "fail"
    assert w._driver.state() != QAbstractAnimation.State.Running


def test_status_warn_is_toast(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("x")
    w.on_event(Status("Мало видеопамяти: 31 т/с", kind="warn"))
    assert not w._toast_wrap.isHidden() and w._toast.kind == "warn"
    assert blocks(w, widgets.StatusRow) == []


def test_done_text_and_cancelled(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("который час")
    w.on_event(Level("grammar", "clock"))
    w.on_event(Done(True, "Сейчас 14:05", level="grammar"))
    assert blocks(w, widgets.PlainView)[0].text() == "Сейчас 14:05"
    w.begin_request("gpt: долго")
    assert w._blocks == []  # новый запрос очищает прошлый ответ
    w.on_event(Level("brain", "prefix"))
    w.on_event(Done(False, "", level="brain", cancelled=True))
    assert blocks(w, widgets.StatusRow)[0].text() == "Отменено"
    assert w._toast_wrap.isHidden()


def test_find_caption_goes_above_list(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("найди отчёт")
    w.on_event(Level("grammar", "find"))
    w.on_event(Items(FOUND))
    w.on_event(Done(True, "Нашёл 5 файлов", level="grammar"))
    assert isinstance(w._blocks[0], widgets.StatusRow) and w._blocks[0].text() == "Нашёл 5 файлов"
    assert isinstance(w._blocks[1], widgets.ItemsBlock)


def test_level_badge_colors_and_local_mode(make: Any) -> None:
    w = make()
    w.set_local_mode(True)
    w.show_launcher()
    settle()
    assert not w._local_badge.isHidden() and w._local_badge.text() == "локально"
    w.begin_request("локально: почему небо голубое")
    w.on_event(Level("local", "prefix"))
    assert w._level_badge.isHidden()  # «локально» уже показан бейджем режима
    w.on_event(Level("brain", "ask_gpt"))
    assert w._level_badge.text() == "GPT" and not w._level_badge.isHidden()
    w.set_local_mode(False)
    assert w._local_badge.isHidden()


def test_badge_color_changes_smoothly(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("x")
    w.on_event(Level("grammar", "r"))
    settle()
    w.on_event(Level("brain", "ask_gpt"))
    assert w._level_badge._anim.state() == QAbstractAnimation.State.Running
    settle()
    assert w._level_badge.current_color().name() == "#a78bfa"


# --- PlainText ----------------------------------------------------------------------------------------


def test_untrusted_strings_are_plain_text(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request(EVIL)
    w.on_event(Level("hands", "router"))
    w.on_event(Status(f"⚙ open: {EVIL}", kind="tool", key="o", done=True, ok=True))
    w.on_event(TextChunk(EVIL))
    w.on_event(Items([EVIL, rf"C:\Users\me\{EVIL}.docx"]))
    w.show_toast(EVIL)
    w.ask_confirm(ConfirmRequest(EVIL, EVIL, "brain"))
    QApplication.processEvents()

    assert w._input.text() == EVIL
    assert blocks(w, widgets.StatusRow)[0].text() == f"open: {EVIL}"
    assert blocks(w, widgets.ItemsBlock)[0].rows[0].text() == EVIL
    assert w._toast.text() == EVIL
    assert w._card.summary.text() == EVIL
    views = [*blocks(w, widgets.PlainView), w._card.details]
    for view in views:
        assert view.toPlainText() == EVIL
        assert not view.acceptRichText()
        flags = view.textInteractionFlags()
        assert not flags & Qt.TextInteractionFlag.LinksAccessibleByMouse
        block = view.document().begin()
        while block.isValid():  # ни одного фрагмента-ссылки и жирного начертания
            it = block.begin()
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                assert not fmt.isAnchor() and fmt.fontWeight() < 600
                it += 1
            block = block.next()
    for label in w.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert not label.openExternalLinks()
        assert not label.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse
    for edit in w.findChildren(QTextEdit):
        assert edit.isReadOnly() and not edit.acceptRichText()


def test_bidi_override_is_neutralized(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("найди")
    w.on_event(Items(["C:\\Users\\me\\Documents\\отчёт\u202excod.exe"]))
    row = blocks(w, widgets.ItemsBlock)[0].rows[0]
    assert "\u202e" not in row.text() and "\ufffd" in row.text()


# --- клавиши ------------------------------------------------------------------------------------------


def test_enter_submits_and_history(make: Any) -> None:
    w = make()
    sent = record(w.submitted)
    w.show_launcher()
    QTest.keyClick(w._input, Qt.Key.Key_Return)
    assert sent == []  # пустую строку — нет
    w._input.setText("  открой телегу  ")
    QTest.keyClick(w._input, Qt.Key.Key_Return)
    w._input.setText("громкость 20")
    QTest.keyClick(w._input, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
    assert sent == ["открой телегу", "громкость 20"]
    w._input.setText("черновик")
    QTest.keyClick(w._input, Qt.Key.Key_Up)
    assert w._input.text() == "громкость 20"
    QTest.keyClick(w._input, Qt.Key.Key_Up)
    assert w._input.text() == "открой телегу"
    QTest.keyClick(w._input, Qt.Key.Key_Down)
    QTest.keyClick(w._input, Qt.Key.Key_Down)
    assert w._input.text() == "черновик"


def test_digit_enter_opens_item_and_click(make: Any) -> None:
    w = make()
    opened = record(w.open_item)
    sent = record(w.submitted)
    w.show_launcher()
    w.begin_request("найди отчёт")
    w.on_event(Level("grammar", "find"))
    w.on_event(Items(FOUND))
    w.on_event(Done(True, level="grammar"))
    settle()
    w._input.setText("2.")
    QTest.keyClick(w._input, Qt.Key.Key_Return)
    assert opened == [1] and sent == []
    w._input.setText("9")
    QTest.keyClick(w._input, Qt.Key.Key_Return)
    assert opened == [1] and sent == ["9"]  # нет такого номера — обычная отправка
    row = blocks(w, widgets.ItemsBlock)[0].rows[3]
    QTest.mouseClick(row, Qt.MouseButton.LeftButton)
    assert opened == [1, 3]


def test_item_click_through_window_routing(make: Any) -> None:
    """Клик через оконную систему (как на Windows): нажатие и отпускание доходят до пункта."""
    w = make(animations=False)
    opened = record(w.open_item)
    w.show_launcher()
    w.on_event(Items(FOUND))
    settle()
    row = blocks(w, widgets.ItemsBlock)[0].rows[2]
    pos = row.mapTo(w, row.rect().center())
    QTest.mouseClick(w.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    assert opened == [2]


def test_item_hover_highlight(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.on_event(Items(FOUND))
    row = blocks(w, widgets.ItemsBlock)[0].rows[1]
    QApplication.sendEvent(row, QEvent(QEvent.Type.Enter))
    assert row.hovered
    QApplication.sendEvent(row, QEvent(QEvent.Type.Leave))
    assert not row.hovered


def test_esc_cancels_then_hides(make: Any) -> None:
    w = make(animations=False)
    cancels = record(w.cancel_requested)
    hidden = record(w.hidden)
    w.show_launcher()
    w.begin_request("gpt: расскажи про Vulkan")
    QTest.keyClick(w._input, Qt.Key.Key_Escape)
    assert len(cancels) == 1 and w.isVisible()
    QTest.keyClick(w._input, Qt.Key.Key_Escape)  # второй Esc — скрыть
    assert len(cancels) == 1 and not w.isVisible() and len(hidden) == 1


def test_esc_feedback_then_cancelled(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("gpt: расскажи про Vulkan")
    w.on_event(Level("brain", "prefix"))
    w.on_event(TextChunk("Vulkan — это"))
    QTest.keyClick(w._input, Qt.Key.Key_Escape)
    row = blocks(w, widgets.StatusRow)[-1]
    assert row.text() == "Отменяю…"  # видно сразу, до ответа ядра
    w.on_event(Done(False, level="brain", cancelled=True))
    assert blocks(w, widgets.StatusRow) == [row] and row.text() == "Отменено" and row.icon.kind == "stop"
    assert w._toast_wrap.isHidden()


def test_cancel_row_removed_if_request_finished_anyway(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("громкость 20")
    QTest.keyClick(w._input, Qt.Key.Key_Escape)
    w.on_event(Done(True, "Громкость 20", level="hands", autohide=False))
    assert [r.text() for r in blocks(w, widgets.StatusRow)] == []
    assert blocks(w, widgets.PlainView)[0].text() == "Громкость 20"


def test_autorepeat_enter_and_ctrl_enter_ignored(make: Any) -> None:
    w = make()
    sent = record(w.submitted)
    w.show_launcher()
    w._input.setText("открой телегу")
    held = _key(Qt.Key.Key_Return, autorepeat=True)
    assert w._on_key(w._input, held) is True
    assert sent == []  # удержанный Enter не шлёт запрос повторно
    req = ConfirmRequest("Завершить chrome.exe?", "", "brain")
    w.ask_confirm(req)
    unguard(w)
    w._on_key(w._input, _key(Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier, autorepeat=True))
    assert not req.done  # удержанный Ctrl+Enter (например, «отправить» в мессенджере) не подтверждает
    w._on_key(w._input, _key(Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier))
    assert req.approved is True


def test_tab_keeps_focus_in_input(make: Any) -> None:
    w = make()
    w.show_launcher()
    assert w._on_key(w._input, _key(Qt.Key.Key_Tab)) is True


def test_esc_without_request_hides(make: Any) -> None:
    w = make(animations=False)
    cancels = record(w.cancel_requested)
    w.show_launcher()
    QTest.keyClick(w._input, Qt.Key.Key_Escape)
    assert cancels == [] and not w.isVisible()


def test_typing_on_text_goes_to_input(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.on_event(TextChunk("ответ"))
    view = blocks(w, widgets.PlainView)[0]
    QTest.keyClick(view, Qt.Key.Key_A)
    assert w._input.text().endswith("a")


# --- подтверждения ------------------------------------------------------------------------------------


def test_confirm_card_and_700ms_rule(make: Any) -> None:
    w = make()
    w.show_launcher()
    req = ConfirmRequest("Завершить chrome.exe (12 процессов)?", "PID 4120, 4188", "brain")
    w.ask_confirm(req)
    assert not w._card_wrap.isHidden()
    assert w._card.summary.text() == "Завершить chrome.exe (12 процессов)?"
    assert not w._card.gpt.isHidden() and w._card.gpt.text() == "просит GPT"
    assert not w._card.no.isEnabled() and w._card.fx.opacity() == 0.0  # кнопки ждут 700 мс
    QTest.keyClick(w._input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(w._input, Qt.Key.Key_Escape)
    assert not req.done  # нажатия в первые 700 мс игнорируются
    assert w._confirm_timer.isActive()  # отсчёт — пока карточка видна
    QTest.qWait(800)
    assert w._card.no.isEnabled() and w._card.yes.isEnabled()
    settle()
    assert w._card.fx.opacity() == pytest.approx(1.0)  # проявились
    QTest.keyClick(w._input, Qt.Key.Key_Return)
    assert req.done and req.approved is False  # Enter никогда не подтверждает
    assert w._card_wrap.isHidden()
    assert not w._confirm_timer.isActive()


def test_ctrl_enter_confirms(make: Any) -> None:
    w = make()
    w.show_launcher()
    req = ConfirmRequest("Переместить отчёт.docx в корзину?", "", "user")
    w.ask_confirm(req)
    assert w._card.gpt.isHidden() and w._card.details.isHidden()
    unguard(w)
    QTest.keyClick(w._card.no, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert req.approved is True


def test_confirm_buttons_click(make: Any) -> None:
    w = make()
    w.show_launcher()
    a = ConfirmRequest("a", "", "user")
    b = ConfirmRequest("b", "", "brain")
    w.ask_confirm(a)
    w.ask_confirm(b)  # второй источник — та же очередь, на экране по одному
    assert w._card.request is a and w._card.more.text() == "ещё 1"
    unguard(w)
    settle()  # окно доросло до карточки
    QTest.mouseClick(w._card.yes, Qt.MouseButton.LeftButton)
    assert a.approved is True
    assert w._card.request is b and not w._card.no.isEnabled()  # у следующего своя защита
    unguard(w)
    settle()
    QTest.mouseClick(w._card.no, Qt.MouseButton.LeftButton)
    assert b.approved is False
    assert w._card_wrap.isHidden()


def test_esc_with_confirm_is_no_not_cancel(make: Any) -> None:
    w = make()
    cancels = record(w.cancel_requested)
    w.show_launcher()
    w.begin_request("gpt: закрой хром")
    req = ConfirmRequest("Завершить chrome.exe?", "", "brain")
    w.ask_confirm(req)
    unguard(w)
    QTest.keyClick(w._input, Qt.Key.Key_Escape)
    assert req.approved is False and cancels == [] and w.isVisible()


def test_confirm_timeout_removes_card(make: Any) -> None:
    w = make()
    w.show_launcher()
    req = ConfirmRequest("Выключить компьютер?", "", "user", created=time.monotonic() - 61)
    w.ask_confirm(req)
    QApplication.processEvents()
    assert req.done and req.approved is False
    assert w._card_wrap.isHidden() and not w._confirm_timer.isActive()


def test_confirm_resolved_by_worker_disappears(make: Any) -> None:
    w = make()
    w.show_launcher()
    req = ConfirmRequest("a", "", "user")
    w.ask_confirm(req)
    req.wait(0.001)  # рабочий поток устал ждать
    QTest.qWait(250)
    assert w._card_wrap.isHidden()


def test_confirm_shows_hidden_window_and_hide_denies(make: Any) -> None:
    w = make(animations=False)
    req = ConfirmRequest("Открыть https://example.com/?", "", "brain")
    w.ask_confirm(req)
    assert w.isVisible()  # окно показывается само
    w._on_key(w._input, _key(Qt.Key.Key_Escape))  # Esc в первые 700 мс — игнор
    assert not req.done and w.isVisible()
    w.hide_launcher()
    assert req.approved is False  # спрятали — «нет»
    assert running(w) == []


def test_deactivate_hides_only_when_idle(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    w = make(animations=False)
    monkeypatch.setattr(w, "isActiveWindow", lambda: False)
    w.show_launcher()
    w.ask_confirm(ConfirmRequest("a", "", "brain"))
    QApplication.sendEvent(w, QEvent(QEvent.Type.ActivationChange))
    assert w.isVisible()  # карточка открыта — не прятать
    w._confirms.deny_all()
    w._render_confirm()
    w.begin_request("gpt: x")
    QApplication.sendEvent(w, QEvent(QEvent.Type.ActivationChange))
    assert w.isVisible()  # идёт запрос — не прятать
    w.on_event(Done(True, level="brain"))
    QApplication.sendEvent(w, QEvent(QEvent.Type.ActivationChange))
    assert not w.isVisible()


# --- автоскрытие и покой ------------------------------------------------------------------------------


def test_autohide_after_action(make: Any) -> None:
    w = make(autohide_s=0.05)
    hidden = record(w.hidden)
    w.show_launcher()
    w.begin_request("открой телегу")
    w.on_event(Level("grammar", "open"))
    w.on_event(Done(True, "Открыл Telegram", level="grammar", autohide=True))
    assert blocks(w, widgets.StatusRow)[-1].icon.kind == "ok"
    QTest.qWait(400)
    assert not w.isVisible() and len(hidden) == 1
    assert running(w) == []


def test_keypress_cancels_autohide(make: Any) -> None:
    w = make(autohide_s=0.05)
    w.show_launcher()
    w.begin_request("открой телегу")
    w.on_event(Done(True, "Открыл Telegram", level="grammar", autohide=True))
    QTest.keyClick(w._input, Qt.Key.Key_A)
    QTest.qWait(250)
    assert w.isVisible()
    assert running(w) == []


def test_idle_after_done_and_hide(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("gpt: расскажи")
    w.on_event(Level("brain", "prefix"))
    w.on_event(Status("⚙ поиск", kind="tool", key="s"))
    QApplication.processEvents()
    assert w._driver.state() == QAbstractAnimation.State.Running  # градиент и спиннер — пока идёт запрос
    w.on_event(TextChunk("ответ"))
    w.on_event(Done(True, level="brain"))
    assert w._driver.state() != QAbstractAnimation.State.Running
    settle()
    assert running(w) == []
    w.hide_launcher()
    settle()
    assert running(w) == []
    assert not w.isVisible()


def test_hidden_while_busy_stops_motion_and_resumes(make: Any) -> None:
    w = make()
    w.show_launcher()
    w.begin_request("gpt: долго")
    w.on_event(Level("brain", "prefix"))
    w.hide_launcher()
    settle()
    assert running(w) == []  # невидимое окно не анимируется
    assert w.is_busy()
    w.show_launcher()
    assert w._driver.state() == QAbstractAnimation.State.Running
    w.on_event(Done(True, level="brain"))
    settle()
    assert running(w) == []


def test_hide_clears_answer_when_idle(make: Any) -> None:
    w = make(animations=False)
    w.show_launcher()
    w.begin_request("который час")
    w.on_event(Done(True, "Сейчас 14:05", level="grammar"))
    QApplication.processEvents()
    h_answer = w.height()
    w.hide_launcher()
    assert w._blocks == [] and w.height() < h_answer
    w.show_toast("Хоткей занят — Ctrl+Alt+J")  # тост, пришедший при скрытом окне, дождётся показа
    w.show_launcher()
    assert not w._toast_wrap.isHidden()


def test_spontaneous_close_only_hides(make: Any) -> None:
    w = make(animations=False)
    w.show_launcher()
    from PySide6.QtGui import QCloseEvent

    ev = QCloseEvent()
    ev.setAccepted(True)
    w.closeEvent(_spontaneous(ev))
    assert not w.isVisible()


def _key(
    key: Qt.Key, mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier, autorepeat: bool = False
) -> Any:
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QEvent.Type.KeyPress, key, mods, "", autorepeat)


def _spontaneous(ev: Any) -> Any:
    class Spontaneous:
        def spontaneous(self) -> bool:
            return True

        def ignore(self) -> None:
            ev.ignore()

    return Spontaneous()


# --- DWM (Windows 11: скругления, тёмная рамка, acrylic) ---------------------------------------------


class FakeDwm:
    def __init__(self, build: int = 22631, hr: int = 0) -> None:
        self._build = build
        self.hr = hr
        self.calls: list[tuple[int, int, int]] = []

    def build(self) -> int:
        return self._build

    def set_dword(self, hwnd: int, attr: int, value: int) -> int:
        self.calls.append((hwnd, attr, value))
        return self.hr


def test_dwm_not_windows_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.ui import dwm

    monkeypatch.setattr(dwm, "_api", None)
    monkeypatch.setattr(dwm.sys, "platform", "linux")
    assert dwm.set_corners(123) is False
    assert dwm.set_dark(123) is False
    assert dwm.set_backdrop(123, "acrylic") is False
    assert dwm.apply(123, "acrylic") is False


def test_dwm_windows11_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.ui import dwm

    fake = FakeDwm(build=22631)
    monkeypatch.setattr(dwm, "_api", fake)
    assert dwm.apply(0x1234, "acrylic") is True
    assert fake.calls == [(0x1234, 20, 1), (0x1234, 33, 2), (0x1234, 38, 3)]
    fake.calls.clear()
    assert dwm.apply(0x1234, "solid") is False  # сплошной фон — backdrop не трогаем
    assert fake.calls == [(0x1234, 20, 1), (0x1234, 33, 2)]
    assert dwm.set_corners(0) is False  # нет окна


def test_dwm_windows10_and_old_11_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.ui import dwm

    win10 = FakeDwm(build=19045)
    monkeypatch.setattr(dwm, "_api", win10)
    assert dwm.set_corners(7) is False and dwm.set_backdrop(7, "acrylic") is False
    assert win10.calls == []
    win11_21h2 = FakeDwm(build=22000)
    monkeypatch.setattr(dwm, "_api", win11_21h2)
    assert dwm.set_corners(7) is True
    assert dwm.set_backdrop(7, "acrylic") is False  # SYSTEMBACKDROP_TYPE — с 22H2


def test_dwm_hresult_error_and_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.ui import dwm

    fake = FakeDwm(hr=-2147024809)  # E_INVALIDARG
    monkeypatch.setattr(dwm, "_api", fake)
    assert dwm.apply(5, "acrylic") is False

    class Broken(FakeDwm):
        def set_dword(self, hwnd: int, attr: int, value: int) -> int:
            raise OSError("dwmapi недоступен")

    monkeypatch.setattr(dwm, "_api", Broken())
    assert dwm.set_dark(5) is False


def test_dwm_ctypes_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    from ctypes import wintypes

    from jarvis.ui import dwm

    seen: dict[str, Any] = {}

    class Fn:
        argtypes: Any = None
        restype: Any = None

        def __call__(self, hwnd: Any, attr: int, data: Any, size: int) -> int:
            seen.update(hwnd=hwnd, attr=attr, size=size)
            return 0

    class FakeDll:
        def __init__(self, name: str, use_last_error: bool = False) -> None:
            seen["dll"] = name
            seen["last_error"] = use_last_error
            self.DwmSetWindowAttribute = Fn()

    monkeypatch.setattr(ctypes, "WinDLL", FakeDll, raising=False)
    api = dwm._Api()
    fn = api._set
    assert fn.argtypes == [wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD]
    assert fn.restype is ctypes.c_long  # HRESULT
    assert api.set_dword(0x10, dwm.DWMWA_WINDOW_CORNER_PREFERENCE, dwm.DWMWCP_ROUND) == 0
    assert seen["dll"] == "dwmapi" and seen["last_error"] is True
    assert seen["attr"] == 33 and seen["size"] == 4


def test_window_acrylic_fill_follows_dwm(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.ui import dwm
    from jarvis.ui import window as window_mod

    monkeypatch.setattr(window_mod.sys, "platform", "win32")
    monkeypatch.setattr(dwm, "_api", FakeDwm(build=22631))
    assert make(backdrop="acrylic")._panel.fill_alpha < 1.0
    monkeypatch.setattr(dwm, "_api", FakeDwm(build=19045))  # Windows 10 — сплошной фон
    assert make(backdrop="acrylic")._panel.fill_alpha == 1.0
    monkeypatch.setattr(dwm, "_api", FakeDwm(hr=-1))
    assert make(backdrop="acrylic")._panel.fill_alpha == 1.0
