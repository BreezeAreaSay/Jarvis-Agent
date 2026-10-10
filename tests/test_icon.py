"""Значок Jarvis: офскрин-рендер всех состояний и размеров; состояния различимы."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from jarvis.ui.icon import SIZES, STATES, icon, render_icon


@pytest.fixture(scope="module", autouse=True)
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


def _mean(img: QImage) -> tuple[float, float, float]:
    """Средний цвет непрозрачных пикселей (r, g, b)."""
    total = [0.0, 0.0, 0.0]
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = QColor(img.pixelColor(x, y))
            if c.alpha() > 200:
                total[0] += c.red()
                total[1] += c.green()
                total[2] += c.blue()
                n += 1
    return total[0] / n, total[1] / n, total[2] / n


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("size", [*SIZES, 40, 128])
def test_render_all_states_and_sizes(state: str, size: int) -> None:
    img = render_icon(state, size)
    assert not img.isNull()
    assert (img.width(), img.height()) == (size, size)
    assert img.hasAlphaChannel()
    assert img.pixelColor(0, 0).alpha() == 0  # скруглённый угол прозрачный
    c = size // 2
    assert img.pixelColor(c, c).alpha() == 255  # середина знака непрозрачна
    opaque = sum(img.pixelColor(x, y).alpha() > 200 for y in range(size) for x in range(size))
    assert opaque > size * size * 0.6  # знак заполняет значок, а не точка посередине


@pytest.mark.parametrize("size", SIZES)
def test_states_differ(size: int) -> None:
    images = {state: render_icon(state, size) for state in STATES}
    for i, a in enumerate(STATES):
        for b in STATES[i + 1 :]:
            assert images[a] != images[b], f"{a} и {b} совпадают на {size} px"


@pytest.mark.parametrize("size", SIZES)
def test_state_colors(size: int) -> None:
    r, g, b = _mean(render_icon("ready", size))
    assert b > g and b > r  # сине-фиолетовый
    r, g, b = _mean(render_icon("local", size))
    assert g > r and g > b * 0.9  # зелёный
    corner = size - max(2, size // 8)  # правый нижний угол — отметка
    busy = QColor(render_icon("busy", size).pixelColor(corner, corner))
    assert busy.red() > 200 and busy.green() > 120 and busy.blue() < 100  # янтарная точка
    warn = QColor(render_icon("warn", size).pixelColor(corner, corner))
    assert warn.red() > 200 and warn.blue() < 100  # оранжевый треугольник


def test_render_is_deterministic() -> None:
    assert render_icon("warn", 32) == render_icon("warn", 32)


def test_bad_arguments() -> None:
    with pytest.raises(ValueError):
        render_icon("sleeping", 32)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        render_icon("ready", 8)


def test_qicon_has_all_sizes() -> None:
    sizes = {s.width() for s in icon("ready").availableSizes()}
    assert set(SIZES) <= sizes
    assert icon("busy") is icon("busy")  # кэш: трей не перерисовывает при каждой смене состояния
