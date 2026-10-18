"""Снимки: пустой снимок, обрезка невидимых рамок, BGRA → PNG (чистый код — на Linux)."""

import io

from PIL import Image

from jarvis.platform.windows import screen as sc


def test_is_blank() -> None:
    assert sc.is_blank(bytes(4 * 100), 10, 10)
    assert sc.is_blank(b"\x00\x00\x00\xff" * 100, 10, 10)  # только альфа
    pixels = bytearray(4 * 100)
    pixels[4 * 77 + 1] = 5  # одна зелёная точка
    assert not sc.is_blank(bytes(pixels), 10, 10)
    assert sc.is_blank(b"", 0, 0)


def test_crop_box_removes_invisible_borders() -> None:
    window = (-8, -8, 1928, 1048)  # GetWindowRect развёрнутого окна
    frame = (0, 0, 1920, 1040)  # видимые границы (DWM)
    assert sc.crop_box(window, frame, (1936, 1056)) == (8, 8, 1928, 1048)


def test_crop_box_scales_for_dpi_unaware_window() -> None:
    window = (100, 100, 1600, 1150)  # физические точки (150 %)
    frame = (106, 100, 1594, 1144)
    box = sc.crop_box(window, frame, (1000, 700))  # снимок — в логических точках окна
    assert box == (4, 0, 996, 696)


def test_crop_box_nothing_to_crop() -> None:
    assert sc.crop_box((0, 0, 100, 100), None, (100, 100)) is None
    assert sc.crop_box((0, 0, 100, 100), (0, 0, 100, 100), (100, 100)) is None
    assert sc.crop_box((0, 0, 0, 0), (0, 0, 10, 10), (100, 100)) is None


def test_encode_png_converts_bgra() -> None:
    bgra = bytes([0, 0, 255, 0, 255, 0, 0, 0])  # красная и синяя точки (порядок B, G, R, A)
    png = sc.encode_png(bgra, 2, 1)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size == (2, 1)
    assert img.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    assert img.convert("RGB").getpixel((1, 0)) == (0, 0, 255)
    cropped = Image.open(io.BytesIO(sc.encode_png(bgra, 2, 1, (1, 0, 2, 1))))
    assert cropped.size == (1, 1)
