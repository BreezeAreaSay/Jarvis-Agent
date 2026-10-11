"""Сфера Jarvis офскрин (QT_QPA_PLATFORM=offscreen): отрисовка, OrbWidget, OrbWindow, ui_state.json.

Модель без Qt (OrbModel) — в tests/test_ui_logic.py.
"""

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # до создания QApplication

from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QImage, QMouseEvent, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from jarvis.config import UiConfig
from jarvis.ui import orb as orb_mod
from jarvis.ui.logic import ORB_EXTENT, ORB_FLASH_S, ORB_LEVELS, OrbModel
from jarvis.ui.orb import OrbWidget, OrbWindow, orb_side, paint_orb


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


def run(m: OrbModel, seconds: float, step: float = 1 / 60) -> None:
    for _ in range(round(seconds / step)):
        m.tick(step)


def calm(m: OrbModel) -> None:
    for _ in range(200):
        if not m.tick(0.1):
            return


def states() -> list[tuple[str, OrbModel]]:
    """Модель в каждом состоянии сферы (как в снимке 13-orb-states)."""
    out: list[tuple[str, OrbModel]] = [("idle", OrbModel())]
    m = OrbModel()
    m.wake()
    run(m, 0.2)
    out.append(("wake", m))
    m = OrbModel()
    m.wake()
    calm(m)
    m.key()
    out.append(("listen", m))
    for level in ORB_LEVELS:
        m = OrbModel()
        m.set_local(level == "local")
        m.wake()
        m.begin()
        m.set_level(level)
        run(m, 1.0)
        out.append((f"work-{level}", m))
    m = OrbModel()
    m.wake()
    m.begin()
    m.set_level("brain")
    m.chunk()
    run(m, 0.5)
    out.append(("speak", m))
    m = OrbModel()
    m.wake()
    m.begin()
    m.confirm(True)
    run(m, 0.6)
    out.append(("confirm", m))
    for ok in (True, False):
        m = OrbModel()
        m.wake()
        m.begin()
        m.done(ok)
        run(m, 0.12)
        out.append(("success" if ok else "error", m))
    return out


def render(m: OrbModel, size: int, dpr: float = 1.0) -> QImage:
    img = QImage(round(size * dpr), round(size * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    paint_orb(p, QRectF(0, 0, size, size), m)
    p.end()
    return img


def running(obj: Any) -> list[str]:
    """Активные таймеры и анимации — всё, что тратит CPU."""
    busy = [f"timer:{t.objectName() or t.interval()}" for t in obj.findChildren(QTimer) if t.isActive()]
    busy += [
        f"anim:{type(a).__name__}"
        for a in obj.findChildren(QAbstractAnimation)
        if a.state() == QAbstractAnimation.State.Running
    ]
    return busy


# --- отрисовка --------------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [16, 40, 64, 109, 160])
@pytest.mark.parametrize("dpr", [1.0, 2.0])
def test_paint_all_states(qapp: QApplication, size: int, dpr: float) -> None:
    for name, m in states():
        img = render(m, size, dpr)
        c = img.width() // 2
        core = img.pixelColor(c, c)
        assert core.alpha() == 255, name  # тело сферы непрозрачно
        if name in ("idle", "listen"):
            assert core.lightness() < 70, name  # в покое — почти чёрное ядро
        assert img.pixelColor(0, 0).alpha() < 40, name  # угол квадрата — почти прозрачен


def test_paint_is_deterministic(qapp: QApplication) -> None:
    a, b = OrbModel(), OrbModel()
    for m in (a, b):
        m.wake()
        m.begin()
        m.set_level("hands")
        run(m, 0.7)
    assert render(a, 64) == render(b, 64)


def test_aura_stays_inside_widget(qapp: QApplication) -> None:
    """Всё, что рисует кадр, — внутри квадрата: за его пределами пусто."""
    m = OrbModel()
    m.wake()
    m.begin()
    m.set_level("brain")
    m.chunk()
    run(m, 0.4)
    size, pad = 64, 32
    img = QImage(size + 2 * pad, size + 2 * pad, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    paint_orb(p, QRectF(pad, pad, size, size), m)
    p.end()
    for x in range(img.width()):
        for y in (0, pad - 2, img.height() - pad + 1, img.height() - 1):
            assert img.pixelColor(x, y).alpha() == 0, (x, y)


def test_orb_side() -> None:
    assert orb_side(64) == 109
    assert orb_side(40) >= 40 * (1 + ORB_EXTENT)
    assert orb_mod.orb_radius(orb_side(64)) == pytest.approx(32, abs=0.5)


# --- OrbWidget ----------------------------------------------------------------------------------------


def test_widget_timer_only_while_animating(qapp: QApplication) -> None:
    w = OrbWidget(size=40)
    w.show()
    qapp.processEvents()
    assert not w.is_animating()  # покой: ни одного таймера
    w.model.wake()
    assert w.is_animating()
    calm(w.model)
    QTest.qWait(40)
    assert not w.is_animating()
    assert running(w) == []
    w.hide()
    w.model.wake()
    assert not w.is_animating()  # невидимый виджет не анимирует
    w.show()
    assert w.is_animating()  # показали — догоняет модель
    w.deleteLater()


def test_widget_without_animations(qapp: QApplication) -> None:
    w = OrbWidget(OrbModel(animations=False), size=40)
    w.show()
    for step in (w.model.wake, w.model.key, w.model.begin, lambda: w.model.done(True)):
        step()
        assert not w.is_animating()
    w.deleteLater()


def test_two_widgets_share_model_time(qapp: QApplication) -> None:
    """Сфера в окне и на рабочем столе рисуют одну модель: время не идёт вдвое быстрее."""
    m = OrbModel()
    a, b = OrbWidget(m, size=40), OrbWidget(m, size=40)
    a.show()
    b.show()
    m.wake()
    m.advance(100.0)
    t0 = m.t
    m.advance(100.05)
    t1 = m.t
    m.advance(100.05)  # второй виджет в тот же момент
    assert m.t == t1 > t0
    a.deleteLater()
    b.deleteLater()


# --- OrbWindow ----------------------------------------------------------------------------------------


@pytest.fixture
def make(qapp: QApplication) -> Iterator[Any]:
    created: list[OrbWindow] = []

    def factory(state: dict[str, Any] | None = None, model: OrbModel | None = None, **kw: Any) -> OrbWindow:
        saved: list[dict[str, Any]] = []
        cfg = UiConfig(**kw)
        w = OrbWindow(
            model if model is not None else OrbModel(animations=cfg.animations),
            cfg,
            state={} if state is None else state,
            writer=saved.append,
        )
        w.saved = saved  # type: ignore[attr-defined]
        created.append(w)
        return w

    yield factory
    for w in created:
        w.close_orb()
        w.deleteLater()
    qapp.processEvents()


def mouse(w: OrbWindow, kind: QEvent.Type, local: QPoint, buttons: Qt.MouseButton) -> None:
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    ev = QMouseEvent(
        kind,
        QPointF(local),
        QPointF(w.mapToGlobal(local)),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(w, ev)


def test_window_flags(make: Any) -> None:
    w = make()
    flags = w.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert (flags & Qt.WindowType.Tool) == Qt.WindowType.Tool  # не на панели задач
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus  # не крадёт фокус у игры
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert w.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert w.width() == w.height() == orb_side(64)
    assert make(orb_size=120).width() == orb_side(120)


def test_default_position_bottom_right(make: Any, qapp: QApplication) -> None:
    w = make()
    area = qapp.primaryScreen().availableGeometry()
    c = w.center()
    r = w.orb_size // 2
    assert c.x() == area.right() - orb_mod.ORB_MARGIN_PX - r
    assert c.y() == area.bottom() - orb_mod.ORB_MARGIN_PX - r
    assert area.contains(w.geometry())  # окно целиком на экране, над панелью задач


@pytest.mark.parametrize("size", [64, 65, 120])
def test_restore_saved_position(make: Any, size: int) -> None:
    w = make(state={"orb": {"x": 200, "y": 150}}, orb_size=size)
    assert w.center() == QPoint(200, 150)  # сохранить → восстановить: без сдвига на пиксель


@pytest.mark.parametrize(
    "state",
    [
        {"orb": {"x": 50_000, "y": 150}},  # монитор отключили
        {"orb": {"x": -900, "y": -900}},
        {"orb": {"x": "200", "y": 150}},
        {"orb": {"x": True, "y": 150}},
        {"orb": [1, 2]},
        {},
    ],
)
def test_restore_off_screen_or_broken_is_default(make: Any, state: dict[str, Any]) -> None:
    w = make(state=state)
    assert w.center() == w.default_center()


def test_click_emits_clicked(make: Any) -> None:
    w = make()
    w.start()
    clicked: list[bool] = []
    w.clicked.connect(lambda: clicked.append(True))
    c = QPoint(w.width() // 2, w.height() // 2)
    left = Qt.MouseButton.LeftButton
    mouse(w, QEvent.Type.MouseButtonPress, c, left)
    mouse(w, QEvent.Type.MouseMove, c + QPoint(3, 0), left)  # дрожание руки — ещё клик
    mouse(w, QEvent.Type.MouseButtonRelease, c + QPoint(3, 0), Qt.MouseButton.NoButton)
    assert clicked == [True]
    assert w.saved == []  # клик позицию не пишет


def test_drag_moves_and_saves_position(make: Any) -> None:
    w = make(state={"orb": {"x": 300, "y": 300}})
    w.start()
    clicked: list[bool] = []
    w.clicked.connect(lambda: clicked.append(True))
    start = w.pos()
    c = QPoint(w.width() // 2, w.height() // 2)
    left = Qt.MouseButton.LeftButton
    mouse(w, QEvent.Type.MouseButtonPress, c, left)
    mouse(w, QEvent.Type.MouseMove, c + QPoint(40, -30), left)
    mouse(w, QEvent.Type.MouseButtonRelease, c + QPoint(40, -30), Qt.MouseButton.NoButton)
    assert w.pos() == start + QPoint(40, -30)
    assert clicked == []
    assert w.saved == [{"orb": {"x": 340, "y": 270}}]


def test_mode_always_visible_and_idle_cpu(make: Any) -> None:
    w = make(orb="always")
    w.start()
    assert w.isVisible()
    QApplication.processEvents()
    assert running(w) == []  # в покое — ни таймера, ни анимации
    w.model.wake()
    assert w.is_animating()
    w.model.hide()
    calm(w.model)
    QTest.qWait(40)
    assert w.isVisible() and running(w) == []


def test_mode_auto_appears_and_fades(make: Any) -> None:
    w = make(orb="auto")
    w.start()
    assert not w.isVisible()  # покой — сферы нет
    w.model.wake()  # хоткей
    assert w.isVisible()
    w.model.begin()
    w.model.hide()  # окно спрятали, запрос идёт — сфера показывает работу
    assert w.isVisible() and not w._linger.isActive()
    w.model.done(True)
    calm(w.model)  # вспышка догорела — покой
    QTest.qWait(40)
    assert w.model.resting
    assert w._linger.isActive()  # гаснет не сразу, через ~2 с
    w._linger.stop()
    w._fade_out()
    assert w._fade.state() == QAbstractAnimation.State.Running
    w._fade.setCurrentTime(w._fade.totalDuration())
    QApplication.processEvents()
    assert not w.isVisible()
    assert running(w) == []
    w.model.wake()  # снова хоткей — появляется
    assert w.isVisible()


def test_mode_auto_returns_during_linger(make: Any) -> None:
    w = make(orb="auto")
    w.start()
    w.model.wake()
    w.model.hide()
    calm(w.model)
    QTest.qWait(40)
    assert w._linger.isActive()
    w.model.wake()
    assert not w._linger.isActive() and w.isVisible()


def test_mode_auto_without_animations_hides_after_linger(make: Any) -> None:
    w = make(orb="auto", animations=False)
    w.start()
    w.model.wake()
    assert w.isVisible()
    w.model.hide()
    assert w._linger.isActive()
    w._linger.timeout.emit()
    assert not w.isVisible() and running(w) == []


def test_unknown_mode_is_always(make: Any) -> None:
    w = make(orb="off")  # off сюда не доходит: app.py окно не создаёт
    assert w.mode == "always"


def test_fullscreen_suppresses(make: Any) -> None:
    w = make(orb="always")
    w.start()
    w.set_suppressed(True)
    assert not w.isVisible() and w.suppressed
    w.model.wake()
    assert not w.isVisible()  # пока впереди игра — не показываться
    w.set_suppressed(False)
    assert w.isVisible()


def test_hide_by_user_until_restart(make: Any) -> None:
    w = make(orb="auto")
    w.start()
    w.model.wake()
    assert w.isVisible()
    w.hide_by_user()
    assert not w.isVisible()
    w.model.begin()
    w.model.wake()
    assert not w.isVisible()


def test_menu_has_open_hide_and_tray_actions(make: Any) -> None:
    w = make()
    w.start()
    clicked: list[bool] = []
    w.clicked.connect(lambda: clicked.append(True))
    extra = QAction("Выход", w)
    w.set_menu_actions([extra])
    texts = [a.text() for a in w.menu().actions() if not a.isSeparator()]
    assert texts == ["Открыть Jarvis", "Спрятать сферу", "Выход"]
    w.menu().actions()[0].trigger()
    assert clicked == [True]
    w.menu().actions()[1].trigger()
    assert not w.isVisible()


def test_close_orb_unsubscribes(make: Any) -> None:
    m = OrbModel()
    w = make(model=m)
    w.start()
    w.close_orb()
    assert not w.isVisible()
    m.wake()
    assert not w.isVisible() and running(w) == []


def test_flash_end_reaches_auto_mode(make: Any) -> None:
    """Конец вспышки происходит в tick(), не во входе: подписчики узнают о смене состояния."""
    w = make(orb="auto")
    w.start()
    w.model.wake()
    w.model.begin()
    w.model.done(False)
    w.model.hide()  # окно спрятали посреди вспышки — она догорает
    assert w.model.state == "error" and not w._linger.isActive()
    run(w.model, ORB_FLASH_S + 0.05)
    assert w.model.state == "idle" and w._linger.isActive()


# --- ui_state.json ------------------------------------------------------------------------------------


def test_state_roundtrip_atomic_merge() -> None:
    path = orb_mod.state_path()
    assert orb_mod.load_state() == {}
    orb_mod.save_state({"orb": {"x": 1, "y": 2}})
    orb_mod.save_state({"other": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"orb": {"x": 1, "y": 2}, "other": True}
    assert [p.name for p in path.parent.iterdir()] == ["ui_state.json"]  # tmp не осталось


def test_state_broken_file_is_empty() -> None:
    path = orb_mod.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{не json", encoding="utf-8")
    assert orb_mod.load_state() == {}
    path.write_text("[1, 2]", encoding="utf-8")
    assert orb_mod.load_state() == {}


def test_state_write_error_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(src: Any, dst: Any) -> None:
        raise PermissionError("занято")

    monkeypatch.setattr(orb_mod.os, "replace", broken)
    orb_mod.save_state({"orb": {"x": 1, "y": 2}})
    assert "не сохранено" in caplog.text
    assert not list(orb_mod.state_path().parent.glob("*.tmp"))


def test_save_async_not_in_ui_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    done = threading.Event()

    def save(update: dict[str, Any]) -> None:
        seen.append(threading.current_thread().name)
        done.set()

    monkeypatch.setattr(orb_mod, "save_state", save)
    orb_mod.save_state_async({"orb": {"x": 1, "y": 2}})
    assert done.wait(5)
    assert seen == ["jarvis-ui-state"]


def test_state_in_data_dir() -> None:
    assert orb_mod.state_path() == Path(os.environ["JARVIS_DATA_DIR"]) / "ui_state.json"
