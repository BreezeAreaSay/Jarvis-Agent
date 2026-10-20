"""Синтез речи Jarvis: движки (piper, windows, edge, openai, fish, elevenlabs), очередь фраз, вывод и события.

say(text) → текст для голоса (jarvis.voice.textprep: без markdown, ссылок, эмодзи; латиница русскими
буквами для русских голосов) → предложения по очереди: следующее синтезируется, пока звучит текущее (первый
звук раньше) → «электроника» (settings.tts.effect) → вывод (jarvis.voice.player). interrupt — оборвать
текущую речь, silence() — замолчать и очистить очередь, wait — дождаться конца.

Выбранный движок не смог (нет ключа, сети) — событие tts.error {error, engine} и запасной: piper (если голос
скачан), потом голоса Windows; упавший движок минуту не трогаем, чтобы не ждать таймаута на каждой фразе
(а если движок сказал, сколько ждать, — столько: кончились кредиты ElevenLabs — до их обновления). Удачное
прослушивание в настройках снимает паузу.

Короткие фразы облачных движков (cacheable) помнятся в памяти: повторное «Готово, сэр.» не тратит кредиты
и не ждёт сеть. На диск (cache/tts) — только фразы без личного: частые ответы (add_stock: «Готово, сэр.»,
«Сейчас сделаю.», «Понял, учту.», ответы характера Джарвиса — их звук готовится заранее, при запуске и смене
голоса, не больше WARM_MAX_CHARS символов синтеза за раз) и те, что модули пометили сами (keep: «Открываю
Telegram.»); после перезапуска они звучат сразу. Остальное — только в памяти: в фразах бывает личное.
is_ready(text) — прозвучит ли фраза сразу (характер выбирает такие варианты первыми).

События: tts.state {speaking, text?}, tts.level {level} ~20 Гц — аватар пульсирует в такт речи.
speaking — правда с момента say() до конца звука: голосовой сервис в это время не слушает без «Джарвис» (эхо).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis import paths
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError
from jarvis.settings import Settings
from jarvis.voice import models as m
from jarvis.voice.dsp import as_mono, float_to_pcm16, pcm16_to_float, resample, silence
from jarvis.voice.engines import Engine, SynthOptions, create_engines
from jarvis.voice.fx import EARCON_RATE, earcon, electronic, fade
from jarvis.voice.player import Chunk, Player, Sink, SoundDeviceSink
from jarvis.voice.textprep import prepare, split_sentences

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

DEFAULT_TEST_TEXT = "Добрый день, сэр. Все системы работают в штатном режиме."
FAIL_COOLDOWN_S = 60.0  # упавший движок (сеть, ключ) столько не трогаем — сразу запасной
SENTENCE_GAP_S = 0.12
ERROR_EVERY_S = 30.0
FALLBACKS = ("piper", "windows")
CACHE_MAX_CHARS = 160  # помнить только короткие фразы: подтверждения и ответы в одно предложение
CACHE_MAX_BYTES = 24 * 1024 * 1024
DISK_MAX_FILES = 300  # фраз на диске (по свежести использования)
WARM_DELAY_S = 3.0  # частые ответы готовим не сразу: ползунок темпа двигают несколько раз подряд
# за одну подготовку синтезируем не больше стольких символов (уже готовые с диска не в счёт): кредиты
# облачного голоса тратятся понемногу — самое частое сразу, остальное — при следующих запусках
WARM_MAX_CHARS = 1200


class PhraseCache:
    """Звук коротких фраз облачных движков в памяти (LRU, PCM 16 бит); из любого потока."""

    def __init__(self, max_bytes: int = CACHE_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple[Any, ...], tuple[bytes, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[Any, ...]) -> tuple[np.ndarray, int] | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
        data, rate = item
        return pcm16_to_float(data), rate

    def has(self, key: tuple[Any, ...]) -> bool:
        with self._lock:
            return key in self._items

    def put(self, key: tuple[Any, ...], samples: np.ndarray, rate: int) -> None:
        data = float_to_pcm16(samples)
        if not data or len(data) > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old[0])
            self._items[key] = (data, int(rate))
            self._bytes += len(data)
            while self._bytes > self.max_bytes and self._items:
                _, (dropped, _) = self._items.popitem(last=False)
                self._bytes -= len(dropped)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass
class _Job:
    gen: int
    kind: str  # text | audio
    text: str = ""
    audio: tuple[np.ndarray, int] | None = None
    done: "asyncio.Future[None] | None" = None
    finished: bool = False


def _resolve(fut: "asyncio.Future[None]") -> None:
    if not fut.done():
        fut.set_result(None)


class Tts(BaseService):
    def __init__(
        self,
        ctx: "AppContext",
        *,
        sink_factory: Callable[[], Sink] | None = None,
        engines: dict[str, Engine] | None = None,
        models: m.ModelManager | None = None,
        auto_download: bool | None = None,
    ) -> None:
        super().__init__(ctx)
        self.models = models or m.manager_for(ctx)
        self.engines: dict[str, Any] = engines if engines is not None else create_engines(ctx, self.models)
        self.player = Player(
            sink_factory or self._default_sink,
            on_level=self._on_level,
            on_chunk=self._on_chunk,
            on_idle=self._update_state,
            on_error=self._on_player_error,
        )
        self.player.volume = ctx.settings.value.tts.volume / 100
        self._auto_download = auto_download
        self._lock = threading.RLock()
        self._gen = 0
        self._pending = 0
        self._queue: asyncio.Queue[_Job] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._published: str | None = None  # текст, о котором сказали tts.state speaking; None — молчим
        self._failed_until: dict[str, float] = {}
        self._error_at: dict[str, float] = {}
        self.cache = PhraseCache()
        self._unsubs: list[Callable[[], None]] = []
        self._stock: list[Callable[[], Iterable[str]]] = []  # источники частых ответов (add_stock)
        self._keep: set[str] = set()  # предложения без личного — их звук можно хранить на диске
        self._warm_task: asyncio.Task[None] | None = None
        self.warm_delay_s = WARM_DELAY_S
        self.warm_max_chars = WARM_MAX_CHARS

    # --- жизненный цикл ----------------------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._unsubs.append(self.ctx.settings.on_change(self._on_settings))
        self._unsubs.append(self.models.add_listener(self._on_model_event))
        self._unsubs.append(self.ctx.bus.on("secrets.changed", self._on_secrets))
        self._prepare_voice()
        self._schedule_warm()

    async def stop(self) -> None:
        for unsub in self._unsubs:
            with contextlib.suppress(Exception):
                unsub()
        self._unsubs.clear()
        warm, self._warm_task = self._warm_task, None
        if warm is not None:
            warm.cancel()
        self.silence()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(BaseException):
                await worker
        await asyncio.to_thread(self.player.close)
        for engine in self.engines.values():
            with contextlib.suppress(Exception):
                engine.close()

    # --- TtsService --------------------------------------------------------------------------------
    @property
    def speaking(self) -> bool:
        with self._lock:
            pending = self._pending
        return pending > 0 or self.player.busy

    async def say(self, text: str, *, interrupt: bool = False, wait: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        if interrupt:
            self.silence()
        job = self._submit(_Job(self._gen, "text", text=text))
        if wait and job.done is not None:
            await asyncio.shield(job.done)

    def silence(self) -> None:
        """Замолчать сейчас и очистить очередь (из любого потока)."""
        with self._lock:
            self._gen += 1
        self.player.stop()
        self._drain_queue()
        self._update_state()

    async def synthesize(self, text: str) -> tuple[Any, int]:
        settings = self.ctx.settings.value
        parts: list[np.ndarray] = []
        out_rate = 0
        sentences = split_sentences(prepare(text, russian=False))
        for sentence in sentences:
            audio = await self._synth(sentence, settings)
            if audio is None:
                continue
            samples, rate = audio
            if not out_rate:
                out_rate = rate
            elif rate != out_rate:
                samples = resample(samples, rate, out_rate)
            if parts:
                parts.append(silence(SENTENCE_GAP_S, out_rate))
            parts.append(samples)
        if not parts:
            if sentences:
                raise UserFacingError("Не удалось озвучить текст — ни один голос не сработал")
            return np.zeros(0, dtype=np.float32), 22050
        return np.concatenate(parts).astype(np.float32), out_rate

    async def play(self, samples: Any, rate: int, *, wait: bool = True) -> None:
        x = as_mono(samples)
        if not len(x) or rate <= 0:
            return
        job = self._submit(_Job(self._gen, "audio", audio=(x, int(rate))))
        if wait and job.done is not None:
            await asyncio.shield(job.done)

    # --- сверх протокола: API и голосовой сервис ---------------------------------------------------
    def add_stock(self, source: Callable[[], Iterable[str]]) -> None:
        """Частые короткие ответы («Готово, сэр.»): звук облачного голоса готовится заранее и хранится
        на диске — звучат сразу, без сети. source зовут при запуске и при смене голоса или обращения."""
        self._stock.append(source)
        self._stock_sentences()  # с диска — сразу, ещё до подготовки (её ждём warm_delay_s)
        self._schedule_warm()

    def keep(self, text: str) -> None:
        """В фразе нет личного («Открываю Telegram.»): её звук можно хранить на диске — и после перезапуска
        она прозвучит сразу."""
        self._keep.update(split_sentences(prepare(text, russian=False)))

    def is_ready(self, text: str) -> bool:
        """Фраза прозвучит сразу: голос локальный (синтез быстрый) или звук всех её предложений уже в памяти.
        Только чтение — для выбора варианта ответа (jarvis.voice.character)."""
        settings = self.ctx.settings.value
        engine = self.engines.get(settings.tts.engine)
        if engine is None or self._failed_until.get(engine.name, 0.0) > time.monotonic():
            return False
        if not getattr(engine, "cacheable", False):
            return not engine.available()
        voice = engine.voice(settings)
        sentences = split_sentences(prepare(text, russian=False))
        for sentence in sentences:
            spoken = prepare(sentence, russian=True) if engine.russian(voice) else sentence
            key = self._cache_key(engine, voice, spoken, settings)
            if key is None or not self.cache.has(key):
                return False
        return bool(sentences)

    def play_earcon(self, name: str) -> None:
        """Короткий сигнал (wake | done | error) вне очереди речи; речью не считается (из любого потока)."""
        try:
            samples = earcon(name, EARCON_RATE)
        except KeyError:
            return
        self.player.put(Chunk(-1, samples, EARCON_RATE, earcon=True), front=True)

    async def voices(self, engine: str | None = None) -> list[dict[str, Any]]:
        name = engine or self.ctx.settings.value.tts.engine
        found = self.engines.get(name)
        if found is None:
            raise UserFacingError(f"Неизвестный движок голоса: {name}")
        return list(await found.voices())

    async def test(
        self, text: str | None = None, engine: str | None = None, voice: str | None = None
    ) -> None:
        """Прослушать голос: именно этот движок и голос, без запасных; ошибка — UserFacingError."""
        settings = self.ctx.settings.value
        name = engine or settings.tts.engine
        found = self.engines.get(name)
        if found is None:
            raise UserFacingError(f"Неизвестный движок голоса: {name}")
        reason = found.available()
        if reason:
            raise UserFacingError(f"{found.title}: {reason}")
        chosen = voice or found.voice(settings)
        installed = getattr(found, "installed", None)
        if voice and installed is not None and not installed(voice):
            raise UserFacingError(f"Голос «{voice}» не скачан — скачайте его в настройках голоса")
        text = (text or "").strip() or DEFAULT_TEST_TEXT
        parts: list[np.ndarray] = []
        out_rate = 0
        for sentence in split_sentences(prepare(text, russian=False)):
            samples, rate = await self._synth_with(found, sentence, chosen, settings, strict=True)
            if not out_rate:
                out_rate = rate
            elif rate != out_rate:
                samples = resample(samples, rate, out_rate)
            if parts:
                parts.append(silence(SENTENCE_GAP_S, out_rate))
            parts.append(samples)
        if not parts:
            raise UserFacingError("Нечего произносить")
        self._failed_until.pop(name, None)  # заработал (новый ключ, кредиты обновились) — снова главный
        self.silence()
        await self.play(np.concatenate(parts), out_rate, wait=False)

    async def account(self, engine: str | None = None) -> dict[str, Any]:
        """Счёт движка (остаток кредитов ElevenLabs, модели); у движков без счёта — {supported: false}."""
        name = engine or self.ctx.settings.value.tts.engine
        found = self.engines.get(name)
        if found is None:
            raise UserFacingError(f"Неизвестный движок голоса: {name}")
        account = getattr(found, "account", None)
        if account is None:
            return {"supported": False}
        return dict(await account())

    def status(self) -> dict[str, Any]:
        s = self.ctx.settings.value.tts
        piper = self.engines.get("piper")
        return {
            "engine": s.engine,
            "speaking": self.speaking,
            "piper_voice": piper.pick(s.piper_voice) if piper is not None and hasattr(piper, "pick") else "",
            "engines": {name: e.available() for name, e in self.engines.items()},
        }

    # --- очередь ---------------------------------------------------------------------------------
    def _submit(self, job: _Job) -> _Job:
        loop = asyncio.get_running_loop()
        self._loop = loop
        job.done = loop.create_future()
        with self._lock:
            self._pending += 1
        if self._queue is None or self._worker is None or self._worker.done():
            self._queue = asyncio.Queue()
            self._worker = loop.create_task(self._work(self._queue), name="tts-worker")
        self._queue.put_nowait(job)
        return job

    async def _work(self, queue: "asyncio.Queue[_Job]") -> None:
        while True:
            job = await queue.get()
            try:
                if job.gen != self._gen:
                    self._finish(job)
                elif job.kind == "audio" and job.audio is not None:
                    samples, rate = job.audio
                    self.player.put(Chunk(job.gen, samples, rate))
                    self._mark(job)
                else:
                    await self._speak(job)
            except asyncio.CancelledError:
                self._finish(job)
                raise
            except Exception:
                log.exception("голос: озвучка фразы упала")
                self._finish(job)

    async def _speak(self, job: _Job) -> None:
        settings = self.ctx.settings.value
        for sentence in split_sentences(prepare(job.text, russian=False)):
            if job.gen != self._gen:
                break
            audio = await self._synth(sentence, settings)
            if audio is None or job.gen != self._gen:
                break
            samples, rate = audio
            self.player.put(Chunk(job.gen, samples, rate, text=job.text))
        self._mark(job)

    def _mark(self, job: _Job) -> None:
        self.player.put(Chunk(job.gen, marker=lambda: self._finish(job)))

    def _finish(self, job: _Job) -> None:
        """Фраза доиграла или сброшена (любой поток)."""
        with self._lock:
            if job.finished:
                return
            job.finished = True
            self._pending = max(0, self._pending - 1)
        loop, fut = self._loop, job.done
        if fut is not None and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(_resolve, fut)
        self._update_state()

    def _drain_queue(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._drain_now()
        else:
            loop.call_soon_threadsafe(self._drain_now)

    def _drain_now(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._finish(job)

    # --- синтез ----------------------------------------------------------------------------------
    def _chain(self, primary: str) -> list[str]:
        return list(dict.fromkeys([primary, *FALLBACKS]))

    async def _synth(self, sentence: str, settings: Settings) -> tuple[np.ndarray, int] | None:
        """Предложение → звук: выбранный движок, при сбое — запасные. Никто не смог — None."""
        primary = settings.tts.engine
        now = time.monotonic()
        errors: list[str] = []
        for name in self._chain(primary):
            engine = self.engines.get(name)
            if engine is None or self._failed_until.get(name, 0.0) > now:
                continue
            reason = engine.available()
            if reason:
                if name == primary:
                    self._report(name, engine, reason)
                continue
            try:
                return await self._synth_with(engine, sentence, engine.voice(settings), settings)
            except UserFacingError as e:
                errors.append(str(e))
                cooldown = getattr(e.__cause__, "cooldown_s", None)
                self._report(name, engine, str(e), primary=name == primary, cooldown=cooldown)
        if errors:
            self._publish_error("tts", "Не удалось озвучить: " + errors[-1])
        return None

    async def _synth_with(
        self, engine: Any, sentence: str, voice: str, settings: Settings, *, strict: bool = False
    ) -> tuple[np.ndarray, int]:
        text = prepare(sentence, russian=True) if engine.russian(voice) else sentence
        if not text.strip():
            raise UserFacingError("Нечего произносить")
        opts = SynthOptions(voice, settings.tts.speed, settings.tts.pitch, settings)
        key = self._cache_key(engine, voice, text, settings)
        cached = self.cache.get(key) if key is not None else None
        stored = key is not None and sentence in self._keep  # можно на диск: фраза без личного
        if cached is None and key is not None and stored:
            cached = await asyncio.to_thread(self._disk_get, key)
            if cached is not None:
                self.cache.put(key, *cached)
        if cached is not None:
            x, rate = cached
        else:
            try:
                samples, rate = await engine.synthesize(text, opts)
            except Exception as e:
                message = str(e) or type(e).__name__
                if not strict:
                    log.warning("голос: %s не озвучил фразу: %s", engine.name, message)
                raise UserFacingError(f"{engine.title}: {message}") from e
            x = as_mono(samples)
            if key is not None and len(x):
                self.cache.put(key, x, int(rate))
                if stored:
                    await asyncio.to_thread(self._disk_put, key, x, int(rate))
        if settings.tts.effect > 0.01:
            x = electronic(x, rate, settings.tts.effect)
        return fade(x, rate), int(rate)

    @staticmethod
    def _cache_key(engine: Any, voice: str, text: str, settings: Settings) -> tuple[Any, ...] | None:
        """Ключ фразы в памяти: облачный движок, голос, модель и её настройки, темп, тон, точный текст."""
        if not getattr(engine, "cacheable", False) or len(text) > CACHE_MAX_CHARS:
            return None
        fingerprint = getattr(engine, "cache_key", None)
        extra = fingerprint(settings) if callable(fingerprint) else ""
        tts = settings.tts
        return (engine.name, voice, extra, round(tts.speed, 2), round(tts.pitch, 2), text)

    # --- частые ответы на диске --------------------------------------------------------------------
    @staticmethod
    def _disk_dir() -> Path:
        return paths.cache_dir() / "tts"

    def _disk_path(self, key: tuple[Any, ...]) -> Path:
        return self._disk_dir() / (hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:40] + ".pcm")

    def _disk_get(self, key: tuple[Any, ...]) -> tuple[np.ndarray, int] | None:
        """Звук фразы с диска: строка JSON {key, rate}, затем PCM 16 бит. Нет или чужой — None."""
        path = self._disk_path(key)
        try:
            raw = path.read_bytes()
            os.utime(path)  # свежесть использования: старые фразы удаляются первыми
        except OSError:
            return None
        head, sep, data = raw.partition(b"\n")
        try:
            meta = json.loads(head)
        except ValueError:
            return None
        rate = int(meta.get("rate") or 0) if isinstance(meta, dict) else 0
        if not sep or not data or rate <= 0 or meta.get("key") != repr(key):
            return None
        return pcm16_to_float(data), rate

    def _disk_put(self, key: tuple[Any, ...], samples: np.ndarray, rate: int) -> None:
        path = self._disk_path(key)
        head = json.dumps({"key": repr(key), "rate": int(rate)}, ensure_ascii=False).encode("utf-8")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(head + b"\n" + float_to_pcm16(samples))
            tmp.replace(path)
            files = sorted(path.parent.glob("*.pcm"), key=lambda f: f.stat().st_mtime)
            for old in files[:-DISK_MAX_FILES]:
                old.unlink(missing_ok=True)
        except OSError as e:
            log.info("голос: фраза не сохранена на диск: %s", e)

    def _schedule_warm(self) -> None:
        """Подготовить частые ответы заново (после паузы; новый вызов отменяет прежний) — из любого потока."""
        loop = self._loop
        if loop is None or loop.is_closed() or not self._stock:
            return

        def start() -> None:
            if self._warm_task is not None:
                self._warm_task.cancel()
            self._warm_task = loop.create_task(self._warm())

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            start()
        else:
            loop.call_soon_threadsafe(start)

    async def _warm(self) -> None:
        """Частые ответы — заранее: с диска в память или, если их ещё нет, синтезом выбранного облачного
        голоса (раз на голос и его настройки; локальный голос и так быстрый). Синтез за один раз — не больше
        warm_max_chars символов (фразы с диска не в счёт): остальное подготовится при следующем запуске."""
        await asyncio.sleep(self.warm_delay_s)
        settings = self.ctx.settings.value
        engine = self.engines.get(settings.tts.engine)
        if engine is None or not getattr(engine, "cacheable", False):
            return
        voice = engine.voice(settings)
        budget = self.warm_max_chars
        for sentence in self._stock_sentences():
            if engine.available() or self._failed_until.get(engine.name, 0.0) > time.monotonic():
                return
            text = prepare(sentence, russian=True) if engine.russian(voice) else sentence
            key = self._cache_key(engine, voice, text, settings)
            stored = key is not None and (
                self.cache.has(key) or await asyncio.to_thread(self._disk_path(key).exists)
            )
            if not stored:
                if budget < len(text):
                    log.info("голос: частые ответы — остальное подготовлю при следующем запуске")
                    return
                budget -= len(text)
            try:
                await self._synth_with(engine, sentence, voice, settings, strict=True)
            except UserFacingError as e:
                log.info("голос: частые ответы заранее не озвучены: %s", e)
                return

    def _stock_sentences(self) -> list[str]:
        """Предложения частых ответов (по нынешнему обращению); их звук можно хранить на диске."""
        phrases: list[str] = []
        for source in self._stock:
            try:
                phrases.extend(source())
            except Exception:
                log.debug("частые ответы не получены", exc_info=True)
        split = (x for phrase in phrases for x in split_sentences(prepare(phrase, russian=False)))
        sentences = list(dict.fromkeys(split))
        self._keep.update(sentences)
        return sentences

    def _report(
        self, name: str, engine: Any, message: str, *, primary: bool = True, cooldown: float | None = None
    ) -> None:
        self._failed_until[name] = time.monotonic() + (cooldown if cooldown else FAIL_COOLDOWN_S)
        if primary:
            text = message if message.startswith(engine.title) else f"{engine.title}: {message}"
            self._publish_error(name, text)

    def _publish_error(self, key: str, text: str) -> None:
        now = time.monotonic()
        if now - self._error_at.get(key, -1e9) < ERROR_EVERY_S:
            return
        self._error_at[key] = now
        log.warning("голос: %s", text)
        self.ctx.bus.publish("tts.error", {"error": text, "engine": key})

    # --- события ---------------------------------------------------------------------------------
    def _on_chunk(self, chunk: Chunk) -> None:
        text = chunk.text[:300]
        with self._lock:
            if self._published == text:
                return
            self._published = text
        data: dict[str, Any] = {"speaking": True}
        if text:
            data["text"] = text
        self.ctx.bus.publish("tts.state", data)

    def _update_state(self) -> None:
        if self.speaking:
            return
        with self._lock:
            if self._published is None:
                return
            self._published = None
        self.ctx.bus.publish("tts.level", {"level": 0.0})
        self.ctx.bus.publish("tts.state", {"speaking": False})

    def _on_level(self, value: float) -> None:
        self.ctx.bus.publish("tts.level", {"level": value})

    def _on_player_error(self, message: str) -> None:
        self._publish_error("output", f"Звук: {message}")

    def _default_sink(self) -> Sink:
        return SoundDeviceSink(self.ctx.settings.value.tts.output_device)

    # --- настройки и модели ------------------------------------------------------------------------
    def _prepare_voice(self) -> None:
        """Голос piper: загрузить заранее, а если не скачан и выбран — скачать в фоне."""
        s = self.ctx.settings.value.tts
        piper = self.engines.get("piper")
        if piper is None:
            return
        voice_id = m.piper_id(s.piper_voice)
        if self.models.installed(voice_id):
            preload = getattr(piper, "preload", None)
            if preload is not None and s.engine == "piper":
                preload(s.piper_voice)
        elif s.engine == "piper" and m.auto_download_allowed(self.ctx, self._auto_download):
            with contextlib.suppress(Exception):
                self.models.start(voice_id)

    def _on_secrets(self, _event: Any) -> None:
        self._failed_until.clear()
        self._schedule_warm()  # появился ключ облачного голоса — частые ответы готовим заранее

    def _on_settings(self, old: Settings, new: Settings) -> None:
        o, n = old.tts, new.tts
        if o != n or old.profile.address != new.profile.address or old.ai.persona != new.ai.persona:
            self._schedule_warm()  # другой голос, темп, обращение или персона — другие частые ответы
        if o.volume != n.volume:
            self.player.volume = n.volume / 100
        if o.output_device != n.output_device:
            self.player.reset_sink()
        if o.engine != n.engine:
            self._failed_until.pop(n.engine, None)
        if o.piper_voice != n.piper_voice or o.engine != n.engine:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._prepare_voice)

    async def _on_model_event(self, event: str, model_id: str) -> None:
        if not model_id.startswith("piper-"):
            return
        piper = self.engines.get("piper")
        if event == "deleting" and piper is not None and hasattr(piper, "unload"):
            await asyncio.to_thread(piper.unload)
        elif event == "installed":
            self._failed_until.pop("piper", None)
            self._prepare_voice()
