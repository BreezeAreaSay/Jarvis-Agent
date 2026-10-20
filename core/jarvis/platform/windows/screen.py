"""Снимки: весь виртуальный экран (mss, все мониторы) или одно окно (PrintWindow с PW_RENDERFULLCONTENT —
окно целиком, даже перекрытое другими). Результат — PNG.

Окно снимается в его собственном DPI-режиме (SetThreadDpiAwarenessContext): старые программы на мониторе 150 %
не обрезаются. Невидимые рамки Windows 10/11 обрезаются по DWMWA_EXTENDED_FRAME_BOUNDS. Чёрный снимок (окно
защищено от снимков или рисует мимо GDI) — повтор снимком области экрана, где окно.
"""

import contextlib
import ctypes
import io
import logging
from collections.abc import Iterator
from typing import Any

from jarvis.platform.base import PlatformError
from jarvis.platform.windows import winapi as wa

log = logging.getLogger(__name__)

MAX_SIDE = 8192  # больше по стороне не снимаем (обрезка справа и снизу)
PNG_LEVEL = 3  # быстрее уровня 6 в несколько раз, файл крупнее на 10–20 %
SAMPLE_POINTS = 4096

Rect = tuple[int, int, int, int]


def is_blank(bgra: bytes, width: int, height: int) -> bool:
    """Все точки чёрные (альфа не в счёт): окно не отдало изображение."""
    n = min(width * height, len(bgra) // 4)
    if n <= 0:
        return True
    step = max(1, n // SAMPLE_POINTS)
    for i in range(0, n, step):  # обычный снимок узнаётся по первым же точкам
        j = i * 4
        if bgra[j] or bgra[j + 1] or bgra[j + 2]:
            return False
    view = memoryview(bgra)[: n * 4]
    return not any(bytes(view[k::4]).strip(b"\x00") for k in range(3))


def crop_box(window: Rect, frame: Rect | None, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Прямоугольник видимой части окна внутри снимка PrintWindow.

    window — GetWindowRect (физические точки), frame — DWM extended frame bounds (физические), size — размер
    снимка (в логических точках окна без поддержки DPI — тогда масштабируем). Нечего обрезать — None.
    """
    if frame is None:
        return None
    width, height = size
    ww, wh = window[2] - window[0], window[3] - window[1]
    if ww <= 0 or wh <= 0 or width <= 0 or height <= 0:
        return None
    sx, sy = width / ww, height / wh
    left = max(0, round((frame[0] - window[0]) * sx))
    top = max(0, round((frame[1] - window[1]) * sy))
    right = min(width, round((frame[2] - window[0]) * sx))
    bottom = min(height, round((frame[3] - window[1]) * sy))
    if right - left < 2 or bottom - top < 2 or (left, top, right, bottom) == (0, 0, width, height):
        return None
    return left, top, right, bottom


def encode_png(bgra: bytes, width: int, height: int, box: tuple[int, int, int, int] | None = None) -> bytes:
    from PIL import Image

    image = Image.frombuffer("RGB", (width, height), bgra, "raw", "BGRX", 0, 1)
    if box is not None:
        image = image.crop(box)
    out = io.BytesIO()
    image.save(out, "PNG", compress_level=PNG_LEVEL)
    return out.getvalue()


def grab_pixels(rect: Rect | None = None) -> tuple[bytes, int, int, Any]:
    """Пиксели (BGRA) виртуального экрана (все мониторы) или прямоугольника экрана: (байты, ширина, высота,
    снимок mss). Из любого потока: у каждого вызова свой mss."""
    try:
        import mss
    except ImportError as e:
        raise PlatformError("Снимок экрана недоступен: нет модуля mss") from e
    try:
        with mss.MSS() as sct:
            if rect is None:
                region: Any = sct.monitors[0]
            else:
                region = {
                    "left": rect[0],
                    "top": rect[1],
                    "width": rect[2] - rect[0],
                    "height": rect[3] - rect[1],
                }
            shot = sct.grab(region)
            return bytes(shot.bgra), int(shot.size[0]), int(shot.size[1]), shot
    except Exception as e:
        log.warning("снимок экрана: %r", e)
        raise PlatformError("Не удалось снять экран") from e


def monitors() -> list[Rect]:
    """Все мониторы (left, top, right, bottom) — в тех же физических точках, что и снимки."""
    try:
        import mss
    except ImportError as e:
        raise PlatformError("Мониторы не узнать: нет модуля mss") from e
    try:
        with mss.MSS() as sct:
            found = sct.monitors[1:]
            return [(m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]) for m in found]
    except Exception as e:
        log.warning("мониторы: %r", e)
        raise PlatformError("Не удалось узнать мониторы") from e


def grab_screen(rect: Rect | None = None) -> bytes:
    """PNG виртуального экрана (все мониторы) или прямоугольника экрана."""
    bgra, width, height, shot = grab_pixels(rect)
    try:
        return encode_png(bgra, width, height)
    except Exception:
        from mss import tools

        png = tools.to_png(shot.rgb, (width, height))
        if not png:
            raise PlatformError("Не удалось сохранить снимок экрана") from None
        return png


@contextlib.contextmanager
def dpi_of_window(w: Any, hwnd: int) -> Iterator[None]:
    """Поток — в DPI-режиме окна hwnd (снимок в его собственных точках), потом — как было."""
    old = None
    if w.SetThreadDpiAwarenessContext is not None and w.GetWindowDpiAwarenessContext is not None:
        context = w.GetWindowDpiAwarenessContext(hwnd)
        if context:
            old = w.SetThreadDpiAwarenessContext(context)
    try:
        yield
    finally:
        if old:
            w.SetThreadDpiAwarenessContext(old)


def print_window(w: Any, hwnd: int, width: int, height: int) -> bytes | None:
    """PrintWindow(PW_RENDERFULLCONTENT) в DIB-секцию 32 бита; DC и битмап освобождаются в finally."""
    if width <= 0 or height <= 0:
        return None
    screen = w.GetDC(None)
    if not screen:
        return None
    mem = bitmap = old = None
    try:
        mem = w.CreateCompatibleDC(screen)
        if not mem:
            return None
        bits = ctypes.c_void_p()
        info = wa.bitmap_info(width, height)
        bitmap = w.CreateDIBSection(
            screen, ctypes.byref(info), wa.DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits.value:
            log.warning("снимок окна: CreateDIBSection %dx%d (ошибка %d)", width, height, wa.last_error())
            return None
        old = w.SelectObject(mem, bitmap)
        if not old:
            return None
        if not w.PrintWindow(hwnd, mem, wa.PW_RENDERFULLCONTENT):
            log.info("PrintWindow не удался (ошибка %d)", wa.last_error())
            return None
        w.GdiFlush()
        return ctypes.string_at(bits.value, width * height * 4)
    finally:
        if old:
            w.SelectObject(mem, old)
        if bitmap:
            w.DeleteObject(bitmap)
        if mem:
            w.DeleteDC(mem)
        w.ReleaseDC(None, screen)


def grab_window(hwnd: int, window_api: Any) -> bytes:
    """PNG окна hwnd. window_api — WindowApi (сведения об окне)."""
    w = window_api.w
    if not window_api.is_window(hwnd):
        raise PlatformError("Такого окна нет")
    if w.IsIconic(hwnd):
        raise PlatformError("Окно свёрнуто — разверните его, чтобы снять")
    if window_api.is_hung(hwnd):  # PrintWindow ждал бы зависшее окно
        raise PlatformError("Окно не отвечает")
    physical = window_api.window_rect(hwnd)
    frame = window_api.frame_rect(hwnd)
    if physical is None:
        raise PlatformError("Не удалось узнать размер окна")
    with dpi_of_window(w, hwnd):
        logical = window_api.window_rect(hwnd) or physical
        width = min(logical[2] - logical[0], MAX_SIDE)
        height = min(logical[3] - logical[1], MAX_SIDE)
        bgra = print_window(w, hwnd, width, height)
    if bgra is not None and not is_blank(bgra, width, height):
        return encode_png(bgra, width, height, crop_box(physical, frame, (width, height)))
    log.info("PrintWindow дал пустой снимок — снимаю область экрана")
    png = grab_screen(frame or physical)
    return png
