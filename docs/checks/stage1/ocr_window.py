"""1.1 в настоящем окне: что видит UI Automation, решит ли агент распознавать текст, сколько это стоит.

uv run python ..\\docs\\checks\\stage1\\ocr_window.py --exe Telegram.exe --expect Поиск
--focus — вывести окно вперёд (спросить человека!); --show — напечатать распознанные строки (личное —
только с согласия человека; по умолчанию — только числа и найдены ли слова из --expect).
"""

import argparse
import time

import _common

from jarvis.ai import ocr
from jarvis.ai.uia import WindowsUi
from jarvis.platform import create_platform
from jarvis.platform.windows.ocr import WindowsOcr


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--focus", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    platform = create_platform("windows")
    window = _common.window_by_exe(platform, args.exe)
    if args.focus:
        platform.window_action("focus", window.hwnd)
        time.sleep(0.5)
        window = _common.window_by_exe(platform, args.exe)
    ui = WindowsUi(platform)
    started = time.monotonic()
    snapshot = ui.snapshot(hwnd=window.hwnd)
    uia_s = time.monotonic() - started
    left, top, width, height = platform.virtual_screen()
    rect = ocr.clip(window.rect, (left, top, left + width, top + height))
    if rect is None:
        raise SystemExit("окно слишком мелкое или за пределами экрана")
    bgra, w, h = platform.screen_pixels(rect)
    reader = WindowsOcr()
    reader.recognize(bgra, (w, h), rect)  # первый раз — создание движка
    started = time.monotonic()
    lines = reader.recognize(bgra, (w, h), rect)
    ocr_s = time.monotonic() - started
    elements, cut = ocr.to_elements(lines, snapshot, rect)
    data = {
        "exe": window.exe,
        "dpi": _common.dpi(window.hwnd),
        "окно": window.rect,
        "картинка_окна": (w, h),
        "uia_секунд": round(uia_s, 3),
        "uia_элементов": len(snapshot.elements),
        "uia_с_именем_без_заголовка": ocr.named_elements(snapshot),
        "агент_распознаёт": ocr.wants_ocr(snapshot, window.exe),
        "язык": reader.language,
        "ocr_секунд": round(ocr_s, 3),
        "ocr_строк": len(lines),
        "строк_в_снимке": len(elements),
        "обрезано": cut,
        "найдено": {
            word: any(word.casefold() in e.name.casefold() for e in elements) for word in args.expect
        },
    }
    if args.show:
        data["строки"] = [f"{e.name} {e.rect}" for e in elements[:20]]
    _common.report(data)


if __name__ == "__main__":
    main()
