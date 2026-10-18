"""«Живые субтитры»: текст того, что звучит в колонках (видео, созвон, стрим), — с переводом.

Системный звук (WASAPI loopback) читается в своём потоке с частотой 16 кГц; простой детектор речи режет его на
фразы по 1–8 с; каждая распознаётся ctx.voice.transcribe(…, language=config.source_lang) и, если язык другой
и ИИ настроен, переводится на config.target_lang. Интерфейс получает plugin.subtitles.text
{id, text, translation, lang, pending}: сразу распознанный текст (pending: true, если ждём перевод),
потом — то же id с переводом.
"""

import asyncio
import collections
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from jarvis import text as textops
from jarvis.plugins.audio import RATE, AudioSource, CaptureThread, EnergySegmenter, SoundcardSource
from jarvis.plugins.base import ConfigField, Option, Plugin, PluginAction, error_text
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

LANGUAGES = {
    "ru": ("Русский", "русский"),
    "en": ("Английский", "английский"),
    "uk": ("Украинский", "украинский"),
    "de": ("Немецкий", "немецкий"),
    "fr": ("Французский", "французский"),
    "es": ("Испанский", "испанский"),
    "it": ("Итальянский", "итальянский"),
    "pt": ("Португальский", "португальский"),
    "pl": ("Польский", "польский"),
    "tr": ("Турецкий", "турецкий"),
    "ja": ("Японский", "японский"),
    "ko": ("Корейский", "корейский"),
    "zh": ("Китайский", "китайский"),
}
SOURCE_OPTIONS = (
    Option("auto", "Определять автоматически"),
    *(Option(code, names[0]) for code, names in LANGUAGES.items()),
)
TARGET_OPTIONS = (
    *(Option(code, names[0]) for code, names in LANGUAGES.items()),
    Option("none", "Не переводить"),
)
# что распознавание «слышит» в музыке и тишине — показывать не нужно
HALLUCINATIONS = (
    "продолжение следует",
    "субтитры сделал",
    "субтитры создавал",
    "субтитры подогнал",
    "редактор субтитров",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
)
QUEUE_MAX = 4  # не успеваем распознавать — выбрасываем самые старые фразы, субтитры должны быть живыми
CONTEXT_LINES = 2


def guess_lang(text: str) -> str:
    """Язык фразы по буквам (для автоопределения): ru, uk, en, de, fr, es, pt, pl, tr, ja, ko, zh или ""."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return ""
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    if cyrillic / len(letters) > 0.5:
        return "uk" if any(c in "іїєґІЇЄҐ" for c in text) else "ru"
    if any("぀" <= c <= "ヿ" for c in letters):
        return "ja"
    if any("가" <= c <= "힯" for c in letters):
        return "ko"
    if any("一" <= c <= "鿿" for c in letters):
        return "zh"
    lower = text.lower()
    for lang, marks in (
        ("de", "äöüß"),
        ("es", "ñ¿¡"),
        ("pt", "ãõ"),
        ("pl", "ąęłńśźż"),
        ("tr", "ğış"),
        ("fr", "çèêëàâîôûœ"),
    ):
        if any(c in lower for c in marks):
            return lang
    return "en"


def is_noise_text(text: str) -> bool:
    norm = textops.normalize(text)
    if len(norm.replace(" ", "")) < 2:
        return True
    return any(norm.startswith(h) for h in HALLUCINATIONS)


class Subtitles(Plugin):
    id = "subtitles"
    name = "Живые субтитры"
    description = "Текст того, что звучит в колонках — видео, созвоны, стримы, — с переводом на русский."
    icon = "subtitles"
    color = ("#38BDF8", "#0284C7")
    fields = (
        ConfigField("source_lang", "select", "Язык звука", default="auto", options=SOURCE_OPTIONS),
        ConfigField("target_lang", "select", "Переводить на", default="ru", options=TARGET_OPTIONS),
        ConfigField(
            "speaker",
            "device",
            "Чей звук слушать",
            default="",
            device="output",
            hint="Колонки или наушники; пусто — устройство по умолчанию",
        ),
    )
    actions = (PluginAction("start", "Включить"), PluginAction("stop", "Выключить"))
    examples = ("включи субтитры", "выключи субтитры")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        # тесты подставляют свой источник звука
        self.source_factory: Callable[[dict[str, Any]], AudioSource] | None = None
        self._capture: CaptureThread | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._worker: asyncio.Task[Any] | None = None
        self._translations: set[asyncio.Task[Any]] = set()
        self._translate_slots = asyncio.Semaphore(2)
        self._seq = 0
        self._recent: collections.deque[str] = collections.deque(maxlen=CONTEXT_LINES)
        self._last: dict[str, Any] = {}
        self._dropped = 0

    @property
    def active(self) -> bool:
        return self._capture is not None

    async def stop(self) -> None:
        await self.end()

    def status(self) -> dict[str, Any]:
        config = self.config
        return {
            "active": self.active,
            "source_lang": config.get("source_lang"),
            "target_lang": config.get("target_lang"),
            "last": self._last,
            "segments": self._seq,
        }

    # --- сеанс ----------------------------------------------------------------------------------------
    def _source(self) -> AudioSource:
        config = self.config
        if self.source_factory is not None:
            return self.source_factory(config)
        return SoundcardSource("loopback", str(config.get("speaker") or ""))

    async def begin(self) -> bool:
        if self.active:
            return False
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=QUEUE_MAX)
        segmenter = EnergySegmenter(RATE, min_s=1.0, max_s=8.0)

        def on_block(block: np.ndarray) -> None:  # поток захвата
            for segment in segmenter.feed(block):
                loop.call_soon_threadsafe(self._enqueue, queue, segment)

        def on_error(message: str) -> None:  # поток захвата
            loop.call_soon_threadsafe(self._capture_failed, message)

        self.error = ""
        self._queue = queue
        self._capture = CaptureThread(self._source(), on_block, on_error, name="subtitles")
        self._capture.start()
        self._worker = self.spawn(self._consume(queue), "transcribe")
        self.publish("state", {"active": True})
        self.changed()
        return True

    async def end(self) -> bool:
        capture, self._capture = self._capture, None
        if capture is None:
            return False
        await self.ctx.run_io(capture.stop, 2.0)
        worker, self._worker = self._worker, None
        tasks = [
            t for t in (worker, *self._translations) if t is not None and t is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2.0)
        self._translations.clear()
        self._queue = None
        self.publish("state", {"active": False})
        self.changed()
        return True

    def _enqueue(self, queue: asyncio.Queue[np.ndarray], segment: np.ndarray) -> None:
        if queue is not self._queue:
            return
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                self._dropped += 1
        queue.put_nowait(segment)

    def _capture_failed(self, message: str) -> None:
        if self._capture is None:
            return
        self.toast("Живые субтитры", f"Звук не захватывается: {message}", status="error")
        self.set_error(message)
        self.ctx.spawn(self.end(), "subtitles-end")

    async def _consume(self, queue: asyncio.Queue[np.ndarray]) -> None:
        while True:
            segment = await queue.get()
            await self.process(segment)

    async def process(self, segment: np.ndarray) -> dict[str, Any] | None:
        """Распознать фразу, показать, при необходимости перевести. Вернёт опубликованную строку."""
        config = self.config
        source = str(config.get("source_lang") or "auto")
        target = str(config.get("target_lang") or "none")
        try:
            heard = await self.ctx.voice.transcribe(segment, RATE, language=source)
        except UserFacingError as e:
            # нет модели для этого языка — дальше всё равно не распознать
            self.toast("Живые субтитры", str(e), status="error")
            self.set_error(str(e))
            self.ctx.spawn(self.end(), "subtitles-end")
            return None
        except Exception as e:
            log.warning("субтитры: фраза не распознана (%s)", error_text(e))
            return None
        heard = textops.clean(heard or "")
        if not heard or is_noise_text(heard):
            return None
        lang = source if source != "auto" else guess_lang(heard)
        need_translation = target != "none" and bool(lang) and lang != target and self._ai_ready()
        self._seq += 1
        item = {
            "id": self._seq,
            "text": heard,
            "translation": "",
            "lang": lang,
            "pending": need_translation,
            "ts": time.time(),
        }
        self.publish("text", item)
        self._last = item
        context = list(self._recent)
        self._recent.append(heard)
        if need_translation:
            task = self.spawn(self._translate_item(item, context, target), "translate")
            self._translations.add(task)
            task.add_done_callback(self._translations.discard)
        return item

    def _ai_ready(self) -> bool:
        try:
            return bool(self.ctx.ai.configured)
        except Exception:
            return False

    async def _translate_item(self, item: dict[str, Any], context: list[str], target: str) -> None:
        async with self._translate_slots:
            translation = await self.translate(item["text"], target, context)
        done = {**item, "translation": translation, "pending": False}
        if self._last.get("id") == item["id"]:
            self._last = done
        self.publish("text", done)

    async def translate(self, text: str, target: str, context: list[str] | None = None) -> str:
        language = LANGUAGES.get(target, (target, target))[1]
        system = (
            f"Ты — синхронный переводчик субтитров. Переведи фразу на {language} язык естественно и "
            "кратко. Верни только перевод, без кавычек и пояснений. Если фраза уже на нужном языке — "
            "верни её как есть."
        )
        user = f"Фраза: {text}"
        if context:
            user = "Предыдущие фразы (для контекста, не переводить):\n" + "\n".join(context) + "\n\n" + user
        try:
            result = await asyncio.wait_for(
                self.ctx.ai.complete(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.2,
                    max_tokens=200 + len(text) * 2,
                ),
                timeout=15.0,
            )
        except Exception as e:
            log.info("субтитры: перевод не удался (%s)", error_text(e))
            return ""
        return textops.clean(str(result or "")).strip('"«»“”')

    # --- действия и команды ---------------------------------------------------------------------------
    async def act_start(self, args: dict[str, Any]) -> dict[str, Any]:
        await self.begin()
        return {"active": True}

    async def act_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        await self.end()
        return {"active": False}

    async def act_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.active:
            await self.end()
        else:
            await self.begin()
        return {"active": self.active}

    def commands(self) -> list[DynamicCommand]:
        return [
            self.command(
                "start",
                "Включить субтитры",
                ["включи субтитры", "живые субтитры", "покажи субтитры", "включи перевод видео"],
                self._cmd_start,
            ),
            self.command(
                "stop",
                "Выключить субтитры",
                ["выключи субтитры", "убери субтитры", "скрой субтитры", "выключи перевод видео"],
                self._cmd_stop,
            ),
        ]

    async def _cmd_start(self, slots: dict[str, Any]) -> ExecResult:
        started = await self.begin()
        return ExecResult(True, "Живые субтитры", "Включены" if started else "Уже включены", icon=self.icon)

    async def _cmd_stop(self, slots: dict[str, Any]) -> ExecResult:
        stopped = await self.end()
        return ExecResult(
            True, "Живые субтитры", "Выключены" if stopped else "Не были включены", icon=self.icon
        )
