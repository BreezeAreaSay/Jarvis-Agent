"""Распознавание текста Windows (Windows.Media.Ocr) для агента: пиксели куска экрана → строки с
прямоугольниками.

Распознаёт сама Windows (языки распознавания ставятся вместе с языковыми пакетами): быстро (на окно — сотые
доли секунды на Windows 11), без сети и без своих моделей. Мелкий шрифт не увеличиваем: 12 пт читается без
ошибок. Язык — русский, если он есть среди языков распознавания (интерфейс программ человека, скорее всего,
русский, а латиницу русский движок тоже читает); нет — язык профиля пользователя, потом английский.

Всё WinRT — в своём фоновом потоке MTA (MtaWorker, как у медиасессий): в STA ждать асинхронные операции
нельзя. Зависло или упало — у вызывающего пустой список, а распознавание отдыхает минуту (агент работает и без
него). Чистая часть — to_lines (слова → строки в точках экрана) — проверяется тестами на Linux.
"""

import logging
import threading
import time
from collections.abc import Sequence
from typing import Any

from jarvis.ai.ocr import OcrLine, Rect
from jarvis.platform.windows.media import MtaWorker, wait_async

log = logging.getLogger(__name__)

RECOGNIZE_TIMEOUT_S = 3.0  # распознавание окна целиком — в сотых долях секунды; дольше — что-то не так
CALL_TIMEOUT_S = 6.0  # весь вызов: ещё импорт WinRT и создание движка в первый раз, копии пикселей
PAUSE_S = 60.0  # не вышло — столько не пробовать (агент работает и без распознавания)
PREFERRED = "ru"  # есть русский среди языков распознавания — он; нет — язык профиля, потом английский
FALLBACK = "en"

# строка распознавания: текст и прямоугольники её слов (x, y, ширина, высота — точки картинки)
RawLine = tuple[str, Sequence[tuple[float, float, float, float]]]


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
        self._engine: Any = None
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

    def _engine_or_none(self) -> Any:
        """Движок распознавания: русский из доступных, иначе язык профиля пользователя, потом английский;
        нет ни одного — RuntimeError."""
        if self._engine is not None:
            return self._engine
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
        self._engine = engine
        try:
            self._language = str(engine.recognizer_language.language_tag)
        except Exception:
            self._language = "?"
        return engine

    def _recognize(self, bgra: bytes, size: tuple[int, int]) -> list[RawLine]:
        """Поток WinRT: BGRA → SoftwareBitmap → OcrEngine.recognize_async → строки со словами."""
        from winrt.windows.graphics.imaging import (  # type: ignore[import-not-found]
            BitmapAlphaMode,
            BitmapPixelFormat,
            SoftwareBitmap,
        )
        from winrt.windows.media.ocr import OcrEngine  # type: ignore[import-not-found]
        from winrt.windows.storage.streams import DataWriter  # type: ignore[import-not-found]

        engine = self._engine_or_none()
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
            result = wait_async(engine.recognize_async(bitmap), RECOGNIZE_TIMEOUT_S)
        finally:
            bitmap.close()
        lines: list[RawLine] = []
        for line in result.lines or []:
            words = []
            for word in line.words or []:
                box = word.bounding_rect
                words.append((float(box.x), float(box.y), float(box.width), float(box.height)))
            lines.append((str(line.text or ""), words))
        return lines
