"""Этап 2: снимок окна Telegram Desktop — сколько времени (одним запросом UI Automation вместо обхода дерева)
и что в нём находит действие агента telegram_send: поле поиска, левую колонку, шапку чата, поле сообщения.

    uv run python ..\\docs\\checks\\stage2\\tg_snapshot.py --focus

--focus — вывести окно вперёд (спросить человека!); --show — напечатать элементы снимка (личное — только с
согласия человека; по умолчанию — числа, типы элементов и да/нет).
"""

import argparse
import time
from collections import Counter

import _common

from jarvis.ai import ocr
from jarvis.ai import telegram_desktop as tg
from jarvis.ai.uia import WindowsUi
from jarvis.platform import create_platform
from jarvis.platform.windows.ocr import WindowsOcr


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default="Telegram.exe")
    parser.add_argument("--focus", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--times", type=int, default=3)
    args = parser.parse_args()

    platform = create_platform("windows")
    window = _common.window_by_exe(platform, args.exe)
    if args.focus:
        platform.window_action("focus", window.hwnd)
        time.sleep(0.5)
        window = _common.window_by_exe(platform, args.exe)
    ui = WindowsUi(platform)
    seconds = []
    snapshot = None
    for _ in range(max(1, args.times)):
        started = time.monotonic()
        snapshot = ui.snapshot(hwnd=window.hwnd)
        seconds.append(round(time.monotonic() - started, 3))
    assert snapshot is not None
    left, top, width, height = platform.virtual_screen()
    rect = ocr.clip(window.rect, (left, top, left + width, top + height))
    lines = []
    ocr_s = None
    if rect is not None:
        bgra, w, h = platform.screen_pixels(rect)
        reader = WindowsOcr()
        reader.recognize(bgra, (w, h), rect)  # первый раз — ещё и создание движка
        started = time.monotonic()
        lines = reader.recognize(bgra, (w, h), rect)
        ocr_s = round(time.monotonic() - started, 3)
    texts, _cut = ocr.to_elements(lines, snapshot, rect or window.rect)
    for i, element in enumerate(texts, 1):
        element.id = f"t{i}"
    whole = type(snapshot)(
        snapshot.window,
        snapshot.app,
        [*snapshot.elements, *texts],
        hwnd=snapshot.hwnd,
        pid=snapshot.pid,
        rect=snapshot.rect or window.rect,
    )
    search = tg.search_field(whole)
    edge = tg.split_x(whole, search)
    box = tg.message_field(whole, edge)
    data = {
        "exe": window.exe,
        "dpi": _common.dpi(window.hwnd),
        "окно": window.rect,
        "снимок_секунд": seconds,
        "предел_секунд": 0.5,
        "uia_элементов": len(snapshot.elements),
        "uia_обрезан": snapshot.truncated,
        "uia_типы": dict(Counter(e.kind for e in snapshot.elements).most_common(12)),
        "распознано_строк": len(texts),
        "распознавание_секунд": ocr_s,
        "поле_поиска": None if search is None else ("UI Automation" if not search.ocr else "распознано"),
        "левая_колонка_до_x": edge,
        "строк_в_левой_колонке": len(tg.results(whole, search)),
        "строк_в_шапке": len(tg.header(whole, edge)),
        "поле_сообщения": None if box is None else ("UI Automation" if not box.ocr else "распознано"),
        "поле_сообщения_имя": box.name if box is not None and not box.ocr else "",
        "сообщений_видно": len(tg.messages(whole, edge, box)),
    }
    if args.show:
        data["элементы"] = [f"{e.id} {e.kind} «{e.name}» {e.rect}" for e in whole.elements]
    _common.report(data)


if __name__ == "__main__":
    main()
