"""Голосовой сервис: микрофон → Silero VAD → Vosk small по ходу речи → GigaAM v3 по концу фразы → «Джарвис»
→ ctx.dispatcher.handle(команда, "voice"). Всё локально: звук никуда не уходит.

Потоки: PortAudio кладёт блоки в очередь; поток «jarvis-voice» читает микрофон, считает уровень (voice.level
~15 Гц) и ведёт слушателя (jarvis.voice.listener): VAD, распознавание и решения — там же, не в цикле ядра.
Модели распознавания грузятся один раз (Recognizers) и работают только пока человек говорит.

Состояние voice.state: off → loading (скачиваю / загружаю модели) → idle ⇄ listening ⇄ processing,
speaking — пока говорит Jarvis; error — с причиной (нет моделей, микрофон, сбой загрузки).

Микрофон работает, пока голос включён (settings.voice.enabled) или идёт разовая просьба — кнопка
микрофона, диктовка в чат, «да/нет» голосом, клавиша «держи и говори»: тогда он открывается на время
просьбы и закрывается через несколько секунд после неё.

Модели: при первом запуске недостающие (VAD, потоковая, GigaAM, голос piper) скачиваются в фоне сами
(на платформе-заглушке — нет: тесты и демо без сети; переопределяет JARVIS_VOICE_AUTODOWNLOAD=1|0).
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.confirm import parse_yes_no
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError, VoiceState
from jarvis.settings import Settings, VoiceSettings
from jarvis.voice import models as m
from jarvis.voice.asr import AsrError, Recognizers, speech_segments
from jarvis.voice.audio import MicInput
from jarvis.voice.dsp import RATE, LevelMeter, level, resample
from jarvis.voice.listener import LISTEN_TIMEOUT_S, Listener, Session
from jarvis.voice.ptt import PttError, PttHook

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

LINGER_S = 8.0  # разовая просьба закончилась — микрофон ещё столько открыт (следующая — без задержки)
NO_MODELS = "Голосовые модели не скачаны — скачайте их в настройках голоса"
RU_LANGS = ("ru", "ru-ru", "rus", "russian", "русский")


class _NoVad:
    """VAD до загрузки моделей: речи нет."""

    def accept(self, frame: np.ndarray) -> bool:
        return False

    def reset(self) -> None:
        return None


def _set_future(fut: "asyncio.Future[Any]", value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


class Voice(BaseService):
    state: VoiceState = "off"

    def __init__(
        self,
        ctx: "AppContext",
        *,
        models: m.ModelManager | None = None,
        recognizers: Any = None,
        mic_factory: Callable[[str], Any] | None = None,
        vad_factory: Callable[[float], Any] | None = None,
        auto_download: bool | None = None,
    ) -> None:
        super().__init__(ctx)
        self.models = models or m.manager_for(ctx)
        self.recognizers: Any = recognizers or Recognizers(self.models)
        self.mic_factory = mic_factory or (lambda device: MicInput(device))
        self.vad_factory = vad_factory or self.recognizers.make_vad
        self._auto_download = auto_download
        self.listener = Listener(self, vad=_NoVad(), stream_factory=self._new_session, final=self._final_text)
        self.reason = ""
        self.error = ""
        self.input_device = ""
        self.ptt_error = ""
        self._phase = "off"  # off | downloading | loading | running | error
        self._activity = "idle"
        self._mic_error = ""
        self._busy = 0
        self._temp = 0
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reconfig: set[str] = set()
        self._started = False
        self._unsubs: list[Callable[[], None]] = []
        self._download_task: asyncio.Task[None] | None = None
        self._linger: asyncio.TimerHandle | None = None
        self._ptt: PttHook | None = None
        self._ptt_held = False
        self._published: tuple[str, str] | None = None
        self._user_deleted: set[str] = set()  # удалены человеком — сами заново не качаем

    # --- жизненный цикл ----------------------------------------------------------------------------
    async def start(self) -> None:
        self._started = True
        self._unsubs.append(self.ctx.settings.on_change(self._on_settings))
        self._unsubs.append(self.ctx.bus.on("tts.state", lambda _e: self._publish_state()))
        self._unsubs.append(self.models.add_listener(self._on_model_event))
        await self._apply()
        self._setup_ptt()

    async def stop(self) -> None:
        self._started = False
        for unsub in self._unsubs:
            with contextlib.suppress(Exception):
                unsub()
        self._unsubs.clear()
        self._stop_ptt()
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None
        if self._download_task is not None:
            self._download_task.cancel()
        await self._stop_pipeline()
        with contextlib.suppress(Exception):
            await self.models.cancel_all()
        self.recognizers.unload()

    # --- VoiceService ------------------------------------------------------------------------------
    async def set_enabled(self, enabled: bool) -> None:
        if self.ctx.settings.value.voice.enabled != bool(enabled):
            self.ctx.settings.patch({"voice": {"enabled": bool(enabled)}})
        await self._apply()

    async def listen_now(self) -> None:
        """Кнопка микрофона / хоткей: следующая фраза — команда без «Джарвис» (ждём её ~8 с)."""
        if not self.can_listen():
            raise UserFacingError(NO_MODELS)
        with contextlib.suppress(Exception):
            self.ctx.tts.silence()
        loop = asyncio.get_running_loop()
        done: asyncio.Future[str | None] = loop.create_future()
        self._acquire()
        try:
            self.listener.listen_now(
                LISTEN_TIMEOUT_S, on_done=lambda v: loop.call_soon_threadsafe(_set_future, done, v)
            )
            self.ctx.spawn(self._hold(done, LISTEN_TIMEOUT_S), "voice-listen")
        except Exception:
            self._release()
            raise
        await self._apply_session()

    async def stop_listening(self) -> None:
        self.listener.stop()

    def follow_up(self, seconds: float | None = None) -> None:
        secs = float(self.ctx.settings.value.voice.follow_up_s if seconds is None else seconds)
        if self._phase == "running" or self._thread is not None:
            self.listener.follow_up(secs)

    async def ask_yes_no(self, timeout: float = 8.0) -> bool | None:
        if not self.can_listen():
            return None
        text = await self._capture("yesno", timeout, punct=False)
        if text is None:
            return None
        return parse_yes_no(text)

    async def capture_text(self, timeout: float = 15.0, *, punct: bool = True) -> str:
        if not self.can_listen():
            raise UserFacingError(NO_MODELS)
        return await self._capture("text", timeout, punct=punct) or ""

    async def transcribe(self, samples: Any, rate: int, *, language: str = "ru") -> str:
        lang = (language or "ru").strip().casefold()
        x = await self.ctx.run_io(resample, samples, int(rate), RATE)
        try:
            return await self.ctx.run_io(self._transcribe_sync, x, lang)
        except AsrError as e:
            raise UserFacingError(f"Распознавание не удалось: {e}") from e

    # --- состояние для API -------------------------------------------------------------------------
    def can_listen(self) -> bool:
        """Есть VAD и хоть одна модель распознавания (GigaAM или потоковая)."""
        if not self.models.installed(m.VAD_ID):
            return False
        return any(self.models.installed(i) for i in (*m.ASR_IDS, m.STREAM_ID))

    def has_multilingual(self) -> bool:
        """Скачан Whisper — распознаются языки кроме русского (субтитры, голосовые на других языках)."""
        return any(self.models.installed(i) for i in m.WHISPER_IDS)

    def listening_ids(self, settings: Settings | None = None) -> list[str]:
        s = settings or self.ctx.settings.value
        return [i for i in self.models.required_ids(s) if not i.startswith("piper-")]

    def models_ready(self) -> bool:
        return not self.models.missing(self.listening_ids())

    def status(self) -> dict[str, Any]:
        s = self.ctx.settings.value.voice
        return {
            "state": self.state,
            "reason": self.reason if self.state == "loading" else "",
            "enabled": s.enabled,
            "error": self.error or self._mic_error,
            "wake_words": list(s.wake_words),
            "models_ready": self.models_ready(),
            "input_device": self.input_device or s.input_device,
            "ptt_error": self.ptt_error,
        }

    def models_list(self) -> list[dict[str, Any]]:
        return self.models.catalog(self.models.required_ids(self.ctx.settings.value))

    # --- Host для слушателя (поток распознавания) --------------------------------------------------
    def settings(self) -> VoiceSettings:
        return self.ctx.settings.value.voice

    def publish(self, type: str, data: dict[str, Any]) -> None:
        self.ctx.bus.publish(type, data)

    def dispatch(self, command: str) -> None:
        if self.ctx.loop is None:
            log.warning("голос: цикл ядра не запущен — команда %r потеряна", command)
            return
        with self._lock:
            self._busy += 1
        self.ctx.call_soon(lambda: self._run_dispatch(command))
        self._publish_state()

    def tts_speaking(self) -> bool:
        try:
            return bool(self.ctx.tts.speaking)
        except Exception:
            return False

    def silence_tts(self) -> None:
        try:
            self.ctx.tts.silence()
        except Exception:
            log.exception("голос: не удалось заглушить речь")

    def earcon(self, name: str) -> None:
        if not self.ctx.settings.value.voice.earcons:
            return
        play = getattr(self.ctx.tts, "play_earcon", None)
        if play is None:
            return
        try:
            play(name)
        except Exception:
            log.debug("голос: сигнал %s не проигрался", name, exc_info=True)

    def activity(self, value: str) -> None:
        self._activity = value
        self._publish_state()

    # --- внутреннее: состояние ---------------------------------------------------------------------
    def _compute_state(self) -> tuple[str, str]:
        if self._phase == "error":
            return "error", self.error
        if self._phase in ("downloading", "loading"):
            return "loading", self.reason
        if self._phase == "off":
            return "off", ""
        if self._mic_error:
            return "error", self._mic_error
        if self.tts_speaking():
            return "speaking", ""
        if self._busy > 0 or self._activity == "processing":
            return "processing", ""
        if self._activity == "listening":
            return "listening", ""
        return "idle", ""

    def _publish_state(self) -> None:
        with self._lock:
            state, reason = self._compute_state()
            if self._published == (state, reason):
                return
            self._published = (state, reason)
            self.state = state  # type: ignore[assignment]
        data: dict[str, Any] = {"state": state}
        if reason:
            data["reason"] = reason
        self.ctx.bus.publish("voice.state", data)

    def _set_phase(self, phase: str, *, reason: str = "", error: str = "") -> None:
        with self._lock:
            self._phase = phase
            self.reason = reason
            self.error = error
        self._publish_state()

    def _auto_download_allowed(self) -> bool:
        return m.auto_download_allowed(self.ctx, self._auto_download)

    def _wanted(self) -> bool:
        return self._started and (self.ctx.settings.value.voice.enabled or self._temp > 0)

    async def _apply(self) -> None:
        """Привести конвейер в соответствие с настройками и разовыми просьбами."""
        if not self._wanted():
            await self._stop_pipeline()
            if self._phase != "off":
                self._set_phase("off")
            else:
                self._publish_state()
            return
        required = self.models.required_ids(self.ctx.settings.value)
        missing = [i for i in self.models.missing(required) if i not in self._user_deleted]
        if missing and self._auto_download_allowed() and self.ctx.settings.value.voice.enabled:
            self._start_download(missing)
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.can_listen():
            if self._download_task is not None and not self._download_task.done():
                self._set_phase("downloading", reason="Скачиваю голосовые модели…")
            else:
                self._set_phase("error", error=NO_MODELS)
            return
        self._start_pipeline()

    def _start_download(self, ids: list[str]) -> None:
        if self._download_task is not None and not self._download_task.done():
            return

        async def run() -> None:
            if not self.can_listen():
                self._set_phase("downloading", reason="Скачиваю голосовые модели…")
            errors = await self.models.ensure(ids)
            if errors and not self.can_listen():
                first = next(iter(errors.values()))
                self._set_phase("error", error=f"Не удалось скачать голосовые модели: {first}")
                return
            await self._apply()

        self._download_task = self.ctx.spawn(run(), "voice-models")

    async def _on_model_event(self, event: str, model_id: str) -> None:
        if event == "installed":
            self._user_deleted.discard(model_id)
            if self._started:
                with self._lock:
                    self._reconfig.add("final")
                await self._apply()
        elif event == "deleting":
            if model_id in self.recognizers.loaded() or model_id == m.VAD_ID:
                await self._stop_pipeline()
                self.recognizers.unload(model_id)
        elif event == "deleted":
            self._user_deleted.add(model_id)
            if self._started:
                await self._apply()

    # --- внутреннее: разовые просьбы ---------------------------------------------------------------
    def _acquire(self) -> None:
        self._temp += 1
        if self._linger is not None:
            self._linger.cancel()
            self._linger = None

    def _release(self) -> None:
        self._temp = max(0, self._temp - 1)
        self._schedule_linger()

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[None]:
        self._acquire()
        try:
            await self._apply_session()
            yield
        finally:
            self._release()

    async def _apply_session(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            await self._apply()

    def _schedule_linger(self) -> None:
        if self._temp > 0 or self.ctx.settings.value.voice.enabled or not self._started:
            return
        loop = asyncio.get_running_loop()
        if self._linger is not None:
            self._linger.cancel()
        self._linger = loop.call_later(LINGER_S, lambda: self.ctx.spawn(self._apply(), "voice-linger"))

    async def _hold(self, done: "asyncio.Future[str | None]", timeout: float) -> None:
        """Держать микрофон открытым, пока просьба не закончится (или с запасом по времени)."""
        try:
            limit = timeout + self.ctx.settings.value.voice.max_utterance_s + 15.0
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(done), limit)
        finally:
            self._release()

    async def _capture(self, kind: str, timeout: float, *, punct: bool) -> str | None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[str | None] = loop.create_future()
        async with self._session():
            req = self.listener.request(
                kind,
                timeout,
                punct=punct,
                on_done=lambda v: loop.call_soon_threadsafe(_set_future, done, v),
                source="capture" if kind == "text" else kind,
            )
            limit = timeout + self.ctx.settings.value.voice.max_utterance_s + 15.0
            try:
                return await asyncio.wait_for(asyncio.shield(done), limit)
            except TimeoutError:
                self.listener.cancel(req)
                return None
            except asyncio.CancelledError:
                self.listener.cancel(req)
                raise

    # --- внутреннее: выполнение команды ------------------------------------------------------------
    async def _run_dispatch(self, command: str) -> None:
        result: dict[str, Any] | None = None
        try:
            result = await self.ctx.dispatcher.handle(command, "voice")
        except Exception:
            log.exception("голос: команда %r упала", command)
            self.earcon("error")
        finally:
            with self._lock:
                self._busy = max(0, self._busy - 1)
            self._publish_state()
        if result:
            self._feedback(result)

    def _feedback(self, result: dict[str, Any]) -> None:
        """Сигнал, если Jarvis ничего не скажет (тихий режим, подтверждения выключены)."""
        voice = self.ctx.settings.value.voice
        kind = result.get("kind")
        if kind == "command":
            res = result.get("result") or {}
            ok = bool(res.get("ok"))
            quiet = voice.silent_mode or (ok and voice.speak_confirmations == "off" and not res.get("say"))
            if quiet and not (res.get("data") or {}).get("cancelled"):
                self.earcon("done" if ok else "error")
        elif kind == "unknown" and voice.silent_mode:
            self.earcon("error")

    # --- внутреннее: конвейер ----------------------------------------------------------------------
    def _start_pipeline(self) -> None:
        self._stop = threading.Event()
        self._set_phase("loading", reason="Загружаю модели распознавания…")
        thread = threading.Thread(target=self._worker, args=(self._stop,), name="jarvis-voice", daemon=True)
        self._thread = thread
        thread.start()

    async def _stop_pipeline(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        if thread.is_alive() and thread is not threading.current_thread():
            await asyncio.to_thread(thread.join, 5.0)
        self._thread = None
        with self._lock:
            self._mic_error = ""

    def _new_session(self) -> Session | None:
        rec = self.recognizers.streaming()
        return rec.session() if rec is not None else None

    def _final_text(self, audio: np.ndarray, punct: bool) -> str:
        asr = self.recognizers.pick_offline(self.ctx.settings.value.voice.asr_model, punct=punct)
        if asr is not None:
            return str(asr.recognize(audio))
        stream = self.recognizers.streaming()
        return str(stream.recognize(audio)) if stream is not None else ""

    def _load_engines(self) -> Any:
        s = self.ctx.settings.value.voice
        vad = self.vad_factory(s.sensitivity)
        try:
            self.recognizers.pick_offline(s.asr_model)  # GigaAM — секунда-две, заранее, не на первой фразе
        except AsrError as e:
            log.warning("голос: %s", e)
        try:
            self.recognizers.streaming()
        except AsrError as e:
            log.warning("голос: потоковая модель недоступна: %s", e)
        return vad

    def _worker(self, stop: threading.Event) -> None:
        listener = self.listener
        try:
            vad = self._load_engines()
        except Exception as e:
            log.exception("голос: модели не загрузились")
            self._set_phase("error", error=f"Модели распознавания не загрузились: {e}")
            return
        if stop.is_set():
            return
        listener.vad = vad
        listener.utt = None
        listener.preroll.clear()
        with self._lock:
            self._reconfig.discard("vad")
            self._reconfig.discard("final")
        mic = self.mic_factory(self.ctx.settings.value.voice.input_device)
        meter = LevelMeter(15.0)
        self._set_phase("running")
        log.info("голос: слушаю")
        try:
            while not stop.is_set():
                if self._reconfig:
                    mic = self._reconfigure(mic, listener)
                frame = mic.read(0.1)
                self._watch_mic(mic)
                if frame is None:
                    listener.tick()
                    continue
                value = meter.feed(level(frame), time.monotonic())
                if value is not None:
                    self.ctx.bus.publish("voice.level", {"level": value})
                listener.process(frame)
        except Exception as e:
            log.exception("голос: поток распознавания упал")
            self._set_phase("error", error=f"Голос остановился из-за ошибки: {e}")
        finally:
            with contextlib.suppress(Exception):
                mic.close()
            listener.utt = None
            self.ctx.bus.publish("voice.level", {"level": 0.0})
            log.info("голос: микрофон закрыт")

    def _watch_mic(self, mic: Any) -> None:
        error = str(getattr(mic, "error", "") or "")
        name = str(getattr(mic, "device_name", "") or "")
        if name:
            self.input_device = name
        if error != self._mic_error:
            with self._lock:
                self._mic_error = error
            self._publish_state()

    def _reconfigure(self, mic: Any, listener: Listener) -> Any:
        with self._lock:
            todo, self._reconfig = self._reconfig, set()
        s = self.ctx.settings.value.voice
        if "mic" in todo:
            with contextlib.suppress(Exception):
                mic.close()
            mic = self.mic_factory(s.input_device)
            listener.utt = None
        if "vad" in todo:
            try:
                listener.vad = self.vad_factory(s.sensitivity)
                listener.utt = None
            except Exception:
                log.exception("голос: VAD не перенастроился")
        if "final" in todo:
            try:
                self.recognizers.pick_offline(s.asr_model)
                self.recognizers.streaming()
            except AsrError as e:
                log.warning("голос: %s", e)
        return mic

    def _on_settings(self, old: Settings, new: Settings) -> None:
        o, n = old.voice, new.voice
        with self._lock:
            if o.input_device != n.input_device:
                self._reconfig.add("mic")
            if o.sensitivity != n.sensitivity:
                self._reconfig.add("vad")
            if o.asr_model != n.asr_model:
                self._reconfig.add("final")
        if o.enabled != n.enabled or o.asr_model != n.asr_model or old.tts.piper_voice != new.tts.piper_voice:
            self.ctx.call_soon(self._apply)
        if o.ptt_key != n.ptt_key:
            loop = self.ctx.loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._setup_ptt)
        if o.enabled != n.enabled or o.wake_words != n.wake_words or o.prefix_mode != n.prefix_mode:
            self._publish_state()

    # --- внутреннее: распознавание записи ----------------------------------------------------------
    def _transcribe_sync(self, x: np.ndarray, lang: str) -> str:
        if lang in RU_LANGS:
            asr = self.recognizers.pick_offline(self.ctx.settings.value.voice.asr_model, punct=True)
            if asr is not None:
                recognize: Callable[[np.ndarray], str] = asr.recognize
            else:
                stream = self.recognizers.streaming()
                if stream is None:
                    raise UserFacingError(
                        "Нет модели распознавания русского — скачайте GigaAM v3 в настройках голоса"
                    )
                recognize = stream.recognize
        else:
            whisper = self.recognizers.whisper()
            if whisper is None:
                raise UserFacingError(
                    f"Для языка «{lang}» нужна многоязычная модель — скачайте Whisper (base или tiny) "
                    "в настройках голоса"
                )
            code = "" if lang == "auto" else lang.split("-")[0]

            def recognize(seg: np.ndarray) -> str:
                return str(whisper.recognize(seg, code))

        segments = speech_segments(x, self.recognizers.vad_model())
        texts = [recognize(x[a:b]) for a, b in segments]
        return " ".join(t.strip() for t in texts if t and t.strip())

    # --- клавиша «держи и говори» ------------------------------------------------------------------
    def _setup_ptt(self) -> None:
        self._stop_ptt()
        key = self.ctx.settings.value.voice.ptt_key.strip()
        self.ptt_error = ""
        if not key or not self._started:
            return
        try:
            hook = PttHook(key, self._ptt_down, self._ptt_up, self._ptt_other)
            hook.start()
        except (ValueError, PttError) as e:
            self.ptt_error = f"Клавиша «держи и говори» не работает: {e}"
            log.warning("голос: %s", self.ptt_error)
            return
        self._ptt = hook

    def _stop_ptt(self) -> None:
        hook, self._ptt = self._ptt, None
        if hook is not None:
            hook.stop()

    def _ptt_down(self) -> None:
        if not self.can_listen():
            return
        self._ptt_held = True
        self.listener.ptt(True)
        self.ctx.call_soon(self._ptt_session)

    def _ptt_up(self) -> None:
        self._ptt_held = False
        self.listener.ptt(False)

    def _ptt_other(self) -> None:
        self._ptt_held = False
        self.listener.ptt_cancel()

    async def _ptt_session(self) -> None:
        async with self._session():
            while self._ptt_held and self._started:
                await asyncio.sleep(0.1)
