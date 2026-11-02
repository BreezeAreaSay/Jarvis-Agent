"""Распознавание текста Windows (Windows.Media.Ocr) для агента: пиксели куска экрана → строки с
прямоугольниками.

Распознаёт сама Windows (языки распознавания ставятся вместе с языковыми пакетами): быстро (на окно — сотые
доли секунды на Windows 11), без сети и без своих моделей. Мелкий шрифт не увеличиваем: 12 пт читается без
ошибок. Язык — русский, если он есть среди языков распознавания (интерфейс программ человека, скорее всего,
русский); нет — язык профиля пользователя, потом английский.

Русский движок читает английские слова русскими буквами («Hello» → «Непо» на Windows 11), английский — русские
латиницей. Поэтому при русском движке окно читает и английский (если он есть), а слова, которые он прочёл
латиницей иначе, идут обоими прочтениями: «Непо|Hello» (merge_readings) — модель выберет сама.

Всё WinRT — в своём фоновом потоке MTA (MtaWorker, как у медиасессий): в STA ждать асинхронные операции
нельзя. Зависло или упало — у вызывающего пустой список, а распознавание отдыхает минуту (агент работает и без
него). Чистая часть — to_lines (слова → строки в точках экрана) — проверяется тестами на Linux.
"""

import logging
import re
import threading
import time
from collections.abc import Sequence
from typing import Any

from jarvis.ai.ocr import OcrLine, Rect
from jarvis.platform.windows.media import MtaWorker, wait_async

log = logging.getLogger(__name__)

RECOGNIZE_TIMEOUT_S = 3.0  # распознавание окна целиком — в сотых долях секунды; дольше — что-то не так
CALL_TIMEOUT_S = 8.0  # весь вызов: ещё импорт WinRT и создание движков в первый раз, копии пикселей
PAUSE_S = 60.0  # не вышло — столько не пробовать (агент работает и без распознавания)
PREFERRED = "ru"  # есть русский среди языков распознавания — он; нет — язык профиля, потом английский
FALLBACK = "en"

# строка распознавания: текст и прямоугольники её слов (x, y, ширина, высота — точки картинки)
Box = tuple[float, float, float, float]
RawLine = tuple[str, Sequence[Box]]
# строка одного движка — слова: (текст, прямоугольник)
ReadLine = list[tuple[str, Box]]

# буквы, которых нет среди похожих на латиницу: слово с ними — точно русское, второе прочтение ему не нужно
_ONLY_CYRILLIC = re.compile(r"[бгдёжзийлфцчшщъыьэюяБГДЁЖЗИЙЛФЦЧШЩЪЫЬЭЮЯ]")
_LATIN = re.compile(r"[A-Za-z]")
SAME_WORD = 0.3  # перекрытие прямоугольников (доля объединения): то же слово у двух движков


def _overlap(a: Box, b: Box) -> float:
    """Перекрытие прямоугольников (x, y, ширина, высота): площадь пересечения / площадь объединения."""
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    both = w * h
    return both / max(1e-9, a[2] * a[3] + b[2] * b[3] - both)


def merge_readings(primary: list[ReadLine], latin: list[ReadLine]) -> list[RawLine]:
    """Строки основного (русского) движка; слово, которое английский движок прочёл на том же месте латиницей
    иначе, а в русском прочтении нет букв, которых нет в латинице, — обоими прочтениями: «Непо|Hello». Строки,
    которых русский движок не прочёл вовсе, а английский прочёл латиницей, — добавляются."""
    others = [word for line in latin for word in line]
    covered: set[int] = set()
    out: list[RawLine] = []
    for line in primary:
        texts: list[str] = []
        for text, box in line:
            best, index = 0.0, -1
            for i, (_other, other_box) in enumerate(others):
                share = _overlap(box, other_box)
                if share > 0:
                    covered.add(i)
                if share > best:
                    best, index = share, i
            if best >= SAME_WORD:
                other = others[index][0]
                if (
                    other.isascii()
                    and _LATIN.search(other)
                    and other.casefold() != text.casefold()
                    and not _ONLY_CYRILLIC.search(text)
                ):
                    text = f"{text}|{other}"
            texts.append(text)
        out.append((" ".join(texts), [box for _text, box in line]))
    start = 0
    for line in latin:
        indexes = range(start, start + len(line))
        start += len(line)
        if line and not covered.intersection(indexes) and any(_LATIN.search(t) for t, _b in line):
            out.append((" ".join(t for t, _b in line), [b for _t, b in line]))
    return out


def to_lines(raw: list[RawLine], size: tuple[int, int], area: Rect) -> list[OcrLine]:
    """Строки распознавания (прямоугольники слов — в точках картинки size) → строки в точках экрана:
    картинка — кусок экрана area. Строка без слов или без текста пропускается."""
    width, height = size
    kx = (area[2] - area[0]) / max(1, width)
    ky = (area[3] - area[1]) / max(1, height)
    out: list[OcrLine] = []
    for text, words in raw:
        text = " ".join(str(text or "").split())
        boxes = [w for w in words if w[2] > 0 and w[3] > 0]
        if not text or not boxes:
            continue
        left = min(b[0] for b in boxes)
        top = min(b[1] for b in boxes)
        right = max(b[0] + b[2] for b in boxes)
        bottom = max(b[1] + b[3] for b in boxes)
        out.append(
            OcrLine(
                text,
                (
                    area[0] + round(left * kx),
                    area[1] + round(top * ky),
                    area[0] + round(right * kx),
                    area[1] + round(bottom * ky),
                ),
            )
        )
    return out


class WindowsOcr:
    """OcrBackend агента на Windows. Движок создаётся один раз в потоке WinRT и там же живёт."""

    def __init__(self, worker: MtaWorker | None = None) -> None:
        self._worker = worker or MtaWorker(name="jarvis-ocr")
        self._engines: list[Any] | None = None  # основной и (при русском) английский
        self._language = ""
        self._paused_until = 0.0
        self._lock = threading.Lock()  # один запрос за раз: агент снимает окно последовательно

    @property
    def language(self) -> str:
        """Язык распознавания (тег BCP-47) — для журнала и проверок; пока не распознавали — ""."""
        return self._language

    @property
    def paused(self) -> bool:
        """Распознавание недавно не удалось и отдыхает (PAUSE_S)."""
        return time.monotonic() < self._paused_until

    def recognize(self, bgra: bytes, size: tuple[int, int], area: Rect) -> list[OcrLine]:
        if self.paused or size[0] < 1 or size[1] < 1:
            return []
        started = time.monotonic()
        try:
            with self._lock:
                raw = self._worker.call(lambda: self._recognize(bgra, size), CALL_TIMEOUT_S)
        except Exception as e:
            self._paused_until = time.monotonic() + PAUSE_S
            log.warning("распознавание текста Windows не удалось: %r — пауза %d с", e, PAUSE_S)
            return []
        lines = to_lines(raw, size, area)
        log.debug(
            "распознано строк: %d за %.3f с (%dx%d, %s)",
            len(lines),
            time.monotonic() - started,
            size[0],
            size[1],
            self._language,
        )
        return lines

    def _engines_or_none(self) -> list[Any]:
        """Движки распознавания: русский из доступных, иначе язык профиля пользователя, потом английский; при
        русском — ещё английский, если он есть (английские слова русский читает русскими буквами). Нет ни
        одного — RuntimeError."""
        if self._engines is not None:
            return self._engines
        from winrt.windows.media.ocr import OcrEngine  # type: ignore[import-not-found]

        available = list(OcrEngine.available_recognizer_languages or [])

        def by_prefix(prefix: str) -> Any:
            language = next(
                (lang for lang in available if str(lang.language_tag).casefold().startswith(prefix)), None
            )
            return OcrEngine.try_create_from_language(language) if language is not None else None

        engine = by_prefix(PREFERRED)
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            engine = by_prefix(FALLBACK)
        if engine is None:
            raise RuntimeError("в Windows нет языка для распознавания текста (Параметры → Язык и регион)")
        try:
            self._language = str(engine.recognizer_language.language_tag)
        except Exception:
            self._language = "?"
        engines = [engine]
        if self._language.casefold().startswith(PREFERRED):
            latin = by_prefix(FALLBACK)
            if latin is not None:
                engines.append(latin)
        self._engines = engines
        return engines

    def _recognize(self, bgra: bytes, size: tuple[int, int]) -> list[RawLine]:
        """Поток WinRT: BGRA → SoftwareBitmap → OcrEngine.recognize_async → строки со словами."""
        from winrt.windows.graphics.imaging import (  # type: ignore[import-not-found]
            BitmapAlphaMode,
            BitmapPixelFormat,
            SoftwareBitmap,
        )
        from winrt.windows.media.ocr import OcrEngine  # type: ignore[import-not-found]
        from winrt.windows.storage.streams import DataWriter  # type: ignore[import-not-found]

        engines = self._engines_or_none()
        width, height = size
        limit = int(OcrEngine.max_image_dimension or 0)
        if limit and (width > limit or height > limit):
            log.info("картинка %dx%d больше предела распознавания %d — пропускаю", width, height, limit)
            return []
        writer = DataWriter()
        writer.write_bytes(bgra)
        buffer = writer.detach_buffer()
        # с режимом альфы — отдельный метод (у create_copy_from_buffer в pywinrt 3.x только четыре аргумента);
        # альфа снимка экрана не нужна: IGNORE
        bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
            buffer, BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.IGNORE
        )
        try:
            readings = [self._read(engine, bitmap) for engine in engines]
        finally:
            bitmap.close()
        if len(readings) > 1:
            return merge_readings(readings[0], readings[1])
        return [(" ".join(text for text, _box in line), [box for _text, box in line]) for line in readings[0]]

    @staticmethod
    def _read(engine: Any, bitmap: Any) -> list[ReadLine]:
        """Одно прочтение картинки движком: строки — слова с прямоугольниками (точки картинки)."""
        result = wait_async(engine.recognize_async(bitmap), RECOGNIZE_TIMEOUT_S)
        lines: list[ReadLine] = []
        for line in result.lines or []:
            words: ReadLine = []
            for word in line.words or []:
                box = word.bounding_rect
                text = str(getattr(word, "text", "") or "")
                words.append((text, (float(box.x), float(box.y), float(box.width), float(box.height))))
            if words and not any(text for text, _box in words):  # слова без текста — текст строки целиком
                words = [(str(line.text or ""), words[0][1]), *((t, b) for t, b in words[1:])]
            lines.append(words)
        return lines
