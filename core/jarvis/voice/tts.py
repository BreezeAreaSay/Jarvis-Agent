"""Синтез речи Jarvis: движки (piper, windows, edge, openai, fish), очередь фраз, воспроизведение и события.

say(text) → текст для голоса (jarvis.voice.textprep: без markdown, ссылок, эмодзи; латиница русскими
буквами для русских голосов) → предложения по очереди: следующее синтезируется, пока звучит текущее (первый
звук раньше) → «электроника» (settings.tts.effect) → вывод (jarvis.voice.player). interrupt — оборвать
текущую речь, silence() — замолчать и очистить очередь, wait — дождаться конца.

Выбранный движок не смог (нет ключа, сети) — событие tts.error {error, engine} и запасной: piper (если голос
скачан), потом голоса Windows; упавший движок минуту не трогаем, чтобы не ждать таймаута на каждой фразе.

События: tts.state {speaking, text?}, tts.level {level} ~20 Гц — аватар пульсирует в такт речи.
speaking — правда с момента say() до конца звука: голосовой сервис в это время не слушает без «Джарвис» (эхо).
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.service_base import BaseService
from jarvis.services import UserFacingError
from jarvis.settings import Settings
from jarvis.voice import models as m
from jarvis.voice.dsp import as_mono, resample, silence
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
        self._unsubs: list[Callable[[], None]] = []

    # --- жизненный цикл ----------------------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._unsubs.append(self.ctx.settings.on_change(self._on_settings))
        self._unsubs.append(self.models.add_listener(self._on_model_event))
        self._unsubs.append(self.ctx.bus.on("secrets.changed", lambda _e: self._failed_until.clear()))
        self._prepare_voice()

    async def stop(self) -> None:
        for unsub in self._unsubs:
            with contextlib.suppress(Exception):
                unsub()
        self._unsubs.clear()
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
        self.silence()
        await self.play(np.concatenate(parts), out_rate, wait=False)

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
                self._report(name, engine, str(e), primary=name == primary)
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
        try:
            samples, rate = await engine.synthesize(text, opts)
        except Exception as e:
            message = str(e) or type(e).__name__
            if not strict:
                log.warning("голос: %s не озвучил фразу: %s", engine.name, message)
            raise UserFacingError(f"{engine.title}: {message}") from e
        x = as_mono(samples)
        if settings.tts.effect > 0.01:
            x = electronic(x, rate, settings.tts.effect)
        return fade(x, rate), int(rate)

    def _report(self, name: str, engine: Any, message: str, *, primary: bool = True) -> None:
        self._failed_until[name] = time.monotonic() + FAIL_COOLDOWN_S
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

    def _on_settings(self, old: Settings, new: Settings) -> None:
        o, n = old.tts, new.tts
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
