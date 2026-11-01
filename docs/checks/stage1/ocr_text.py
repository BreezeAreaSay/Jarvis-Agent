"""1.1 без окон и мыши: распознавание текста Windows на картинке, нарисованной этим скриптом (Segoe UI,
12 и 9 пт).

Печатает язык распознавания, время и что прочитано против того, что нарисовано.
"""

import sys
import time

import _common
from PIL import Image, ImageDraw, ImageFont

from jarvis.platform.windows.ocr import WindowsOcr

LINES = [
    "Привет, мир! Отправить",
    "Hello World — Send message",
    "Счёт №125 от 15.10 — 0123456789",
    "OBS Studio: Начать запись",
    "Олег Петров: итоги встречи до вечера",
]
FONT = r"C:\Windows\Fonts\segoeui.ttf"


def picture(size_px: int) -> Image.Image:
    font = ImageFont.truetype(FONT, size_px)
    image = Image.new("RGB", (900, 40 + len(LINES) * (size_px * 2)), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(LINES):
        draw.text((20, 20 + i * size_px * 2), line, font=font, fill=(20, 20, 20))
    return image


def main() -> None:
    reader = WindowsOcr()
    out = []
    for points, pixels in ((12, 16), (9, 12)):  # пунктов при 100 % → точек
        image = picture(pixels)
        bgra = image.convert("RGBA").tobytes("raw", "BGRA")
        started = time.monotonic()
        lines = reader.recognize(bgra, image.size, (0, 0, *image.size))
        seconds = round(time.monotonic() - started, 3)
        read = [line.text for line in lines]
        out.append(
            {
                "кегль_пт": points,
                "секунд": seconds,
                "нарисовано": LINES,
                "прочитано": read,
                "совпало_строк": sum(1 for line in LINES if line in read),
            }
        )
    _common.report({"язык": reader.language, "python": sys.version.split()[0], "проверки": out})


if __name__ == "__main__":
    main()
