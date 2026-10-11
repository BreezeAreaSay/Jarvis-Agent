"""Голос без Qt: push-to-talk → запись → распознавание → событие для окна.

Поток "jarvis-voice" — машина состояний idle → pressed → listening → recognizing → idle; все входы идут через
одну очередь: события хука (jarvis.ptt), отмена (Esc), результат распознавания (поток "jarvis-asr" грузит
модель один раз при старте и распознаёт по одной записи). Наружу — emit(событие); app превращает его
в сигнал Qt (UI-поток):
- Listening — PTT удерживается ≥ min_hold_ms без других клавиш: окно «Слушаю…» (активное окно — снять ДО
  показа окна, как по хоткею);
- Loudness(value) — уровень 0..1 ≈20 раз в секунду, пока слушаем (сфера);
- Recognizing — отпущено, распознаём;
- Heard(text, …) — текст в строку ввода и сразу запрос (поправить и отправить заново можно как обычно);
- Failed(text) — микрофон, модель или «ничего не расслышал»;
- ListenCancelled(reason) — сочетание клавиш (правый Ctrl+C — не голос), Esc, сон ПК.

Запись начинается сразу при нажатии (первые слова не теряются), но окно появляется только после min_hold:
обычное короткое нажатие правого Ctrl тихо выбрасывается. Нажатие PTT сначала глушит речь Jarvis
(on_press → Narrator.cancel): пока Jarvis говорит, микрофон не пишет. Esc в любом окне (хук) — тоже
(on_escape) и бросает запись. Потерянное отпускание (хук снят Windows, отпустили в окне администратора)
ловит опрос GetAsyncKeyState.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jarvis import asr, mic, ptt

log = logging.getLogger("jarvis")

POLL_S = 0.1  # опрос физического состояния клавиши, пока она «нажата»
ECHO_GAP_S = 0.15  # PTT перебил речь Jarvis: хвост звука в динамиках успевает стихнуть до начала записи
RELEASE_POLLS = 3  # столько опросов подряд «отпущена» — отпускание потеряно, считаем отпущенной
NOT_HEARD = "Не расслышал — повтори, удерживая {key}"


@dataclass(frozen=True)
class Listening:
    """PTT удерживается: показать окно в режиме «Слушаю…»."""


@dataclass(frozen=True)
class Loudness:
    value: float


@dataclass(frozen=True)
class Recognizing:
    """Отпущено — распознаём."""


@dataclass(frozen=True)
class Heard:
    text: str
    asr_ms: float
    audio_s: float
    model: str = ""


@dataclass(frozen=True)
class Failed:
    text: str


@dataclass(frozen=True)
class ListenCancelled:
    reason: str  # keys | esc | resume | stop


VoiceEvent = Listening | Loudness | Recognizing | Heard | Failed | ListenCancelled


def enabled(cfg: Any) -> bool:
    """Голосовой ввод включён: ptt_key задан и разбирается, модель распознавания скачана."""
    if not str(getattr(cfg, "ptt_key", "")).strip():
        return False
    try:
        ptt.parse_key(cfg.ptt_key)
        asr.model_files(cfg.model)
    except (ValueError, asr.AsrError):
        return False
    return True


class Voice:
    """PTT, запись и распознавание. start() — из рабочего потока (ставит хук и ждёт его); stop() — выход."""

    def __init__(
        self,
        cfg: Any,
        emit: Callable[[VoiceEvent], None],
        *,
        on_press: Callable[[], Any] | None = None,
        on_escape: Callable[[], Any] | None = None,
        journal: Any = None,
        recognizer: Any = None,
        recorder: Any = None,
        hook_factory: Callable[[int, Any], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.emit = emit
        self.on_press = on_press
        self.on_escape = on_escape
        self.journal = journal
        self.recognizer = recognizer or asr.Recognizer(cfg.model, cfg.threads, cfg.vad)
        self.recorder = recorder or mic.Recorder(cfg.device, cfg.max_record_s, on_level=self._on_level)
        self._hook_factory = hook_factory or ptt.Hook
        self._hook: Any = None
        self.key = ptt.key_label(ptt.parse_key(cfg.ptt_key)) if cfg.ptt_key else ""
        self.min_hold_s = max(0.0, cfg.min_hold_ms / 1000)
        self.state = "idle"  # idle | pressed | listening | held | recognizing
        self.status = "off"  # off | loading | ready | error
        self.error = ""
        self._q: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._asr_q: queue.SimpleQueue[tuple[int, Any, float] | None] = queue.SimpleQueue()
        self._t_down = 0.0
        self._t_up = 0.0
        self._job = 0
        self._up_polls = 0
        self._mic_error = ""
        self._threads: list[threading.Thread] = []

    # --- жизненный цикл --------------------------------------------------------------------------

    def start(self) -> None:
        """Потоки voice и asr (модель грузится в фоне) и хук клавиши. Хук не встал — status "error"."""
        self.status = "loading"
        self._threads = [
            threading.Thread(target=self._asr_loop, name="jarvis-asr", daemon=True),
            threading.Thread(target=self._loop, name="jarvis-voice", daemon=True),
        ]
        for t in self._threads:
            t.start()
        if not self.cfg.ptt_key:
            return
        try:
            self._hook = self._hook_factory(ptt.parse_key(self.cfg.ptt_key), self._q)
            self._hook.start()
        except Exception as e:
            self._hook = None
            self.status, self.error = "error", f"клавиша голоса не работает: {e}"
            log.warning("голос: %s", self.error)

    def stop(self) -> None:
        """Снять хук, бросить запись, остановить потоки (не ждёт распознавания)."""
        if self._hook is not None:
            try:
                self._hook.stop()
            except Exception as e:
                log.warning("голос: хук не снят: %s", e)
            self._hook = None
        self._q.put(("stop", time.monotonic()))
        self._asr_q.put(None)
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(2.0)
        self._threads = []

    def cancel(self) -> None:
        """Esc в окне: бросить запись или распознавание."""
        self._q.put(("cancel", time.monotonic()))

    # --- распознавание (поток jarvis-asr) --------------------------------------------------------

    def _asr_loop(self) -> None:
        mic.warmup()
        try:
            self.recognizer.load()
        except Exception as e:
            self.error = str(e)
            self.status = "error"
            log.warning("голос: распознавание недоступно: %s", e)
        else:
            if self.status != "error":  # ошибка хука важнее
                self.status = "ready"
        while True:
            item = self._asr_q.get()
            if item is None:
                return
            job, recording, t_up = item
            wait_ms = round(max(0.0, time.monotonic() - t_up) * 1000, 1)  # модель ещё грузилась
            try:
                result: Any = self.recognizer.recognize(recording)
            except Exception as e:
                log.warning("голос: распознавание упало: %s", e)
                result = e
            self._q.put(("asr", time.monotonic(), job, result, wait_ms))

    # --- машина состояний (поток jarvis-voice) ---------------------------------------------------

    def _loop(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=self._timeout())
            except queue.Empty:
                self._tick(time.monotonic())
                continue
            kind = item[0]
            if kind == "stop":
                self._drop("stop")
                return
            try:
                self._handle(kind, item)
            except Exception:
                log.exception("голос: обработка %s", kind)
                self._drop("error")
            self._tick(time.monotonic())

    def _timeout(self) -> float | None:
        if self.state == "pressed":
            left = self._t_down + self.min_hold_s - time.monotonic()
            return max(0.0, min(POLL_S, left))
        if self.state in ("listening", "held"):
            return POLL_S
        if self._hook is not None and getattr(self._hook, "down", False):
            return POLL_S  # хук считает клавишу нажатой, а мы уже не слушаем — ждём отпускания
        return None

    def _handle(self, kind: str, item: tuple[Any, ...]) -> None:
        t = item[1]
        if kind == "down":
            self._down(t)
        elif kind == "up":
            self._up(t)
        elif kind == "other":
            self._other()
        elif kind == "resume":
            if self.state in ("pressed", "listening", "held"):
                self._drop("resume")
        elif kind == "cancel":
            self._cancel()
        elif kind == "esc":
            self._escape()
        elif kind == "asr":
            self._asr_done(item[2], item[3], item[4])

    def _down(self, t: float) -> None:
        if self.state in ("pressed", "listening", "held"):
            return
        if self.state == "recognizing":
            self._job += 1  # новая фраза важнее: старый результат выбросим
        if self.on_press is not None:
            try:
                if self.on_press():  # глушит речь: пока Jarvis говорит, микрофон не пишет
                    time.sleep(ECHO_GAP_S)  # буфер динамиков доигрывает — не записать себя
            except Exception:
                log.exception("голос: on_press")
        self._t_down, self._up_polls, self._mic_error = t, 0, ""
        self.state = "pressed"
        if self.status == "error" and not self.recognizer.ready:
            return  # модель недоступна — после min_hold скажем об этом, писать незачем
        try:
            self.recorder.start()
        except Exception as e:
            self._mic_error = str(e)

    def _tick(self, now: float) -> None:
        """Таймеры: min_hold → Listening; потерянное отпускание; предел длины записи."""
        if self.state == "pressed" and now - self._t_down >= self.min_hold_s:
            failure = self._mic_error or (self.error if self.status == "error" else "")
            if failure:
                self.recorder.abort()
                self.state = "held"
                self.emit(Failed(failure))
            else:
                self.state = "listening"
                self.emit(Listening())
        if self.state in ("pressed", "listening", "held"):
            self._watch_key(now)
            if self.state == "listening" and getattr(self.recorder, "full", False):
                log.info("голос: предел записи %.0f с — распознаю", self.cfg.max_record_s)
                self._up(now)
        elif self._hook is not None and getattr(self._hook, "down", False):
            self._watch_key(now)

    def _watch_key(self, now: float) -> None:
        hook = self._hook
        if hook is None:
            return
        if hook.key_down():
            self._up_polls = 0
            return
        self._up_polls += 1
        if self._up_polls < RELEASE_POLLS:
            return
        self._up_polls = 0
        if self.state in ("pressed", "listening", "held"):
            log.info("голос: отпускание клавиши потеряно — считаю отпущенной")
            self._up(now)
        else:
            hook.release()

    def _up(self, t: float) -> None:
        if self._hook is not None:
            self._hook.release()
        state, self.state = self.state, "idle"
        if state == "pressed":
            self.recorder.abort()  # короткое нажатие — обычный правый Ctrl
        elif state == "listening":
            recording = self.recorder.stop()
            self._job += 1
            self._t_up = t
            self.state = "recognizing"
            self.emit(Recognizing())
            self._asr_q.put((self._job, recording, t))

    def _other(self) -> None:
        """Другая клавиша при удержании PTT: это сочетание (правый Ctrl+C), а не голос."""
        if self.state == "pressed":
            self.recorder.abort()
            self.state = "idle"
        elif self.state in ("listening", "held"):
            self._drop("keys")

    def _cancel(self) -> None:
        if self.state in ("pressed", "listening", "held"):
            self._drop("esc")
        elif self.state == "recognizing":
            self._job += 1
            self.state = "idle"
            self.emit(ListenCancelled("esc"))

    def _escape(self) -> None:
        """Esc в любом окне (его видит хук): заглушить речь Jarvis и бросить запись или распознавание."""
        if self.on_escape is not None:
            try:
                self.on_escape()
            except Exception:
                log.exception("голос: on_escape")
        self._cancel()

    def _drop(self, reason: str) -> None:
        state, self.state = self.state, "idle"
        if state in ("pressed", "listening", "held"):
            self.recorder.abort()
        if state in ("listening", "held", "recognizing"):
            self._job += 1
            self.emit(ListenCancelled(reason))

    def _asr_done(self, job: int, result: Any, wait_ms: float) -> None:
        if job != self._job or self.state != "recognizing":
            return  # отменено или перебито новой фразой
        self.state = "idle"
        total_ms = round((time.monotonic() - self._t_up) * 1000, 1)
        if isinstance(result, Exception):
            self.emit(Failed(f"Не распознал: {result}"))
            return
        self._journal(result, total_ms, wait_ms)
        if not result.text:
            self.emit(Failed(NOT_HEARD.format(key=self.key or "клавишу")))
            return
        log.info(
            "голос: %.1f с звука → текст за %.0f мс (распознавание %.0f мс)",
            result.audio_s,
            total_ms,
            result.asr_ms,
        )
        self.emit(Heard(result.text, result.asr_ms, result.audio_s, self.recognizer.model))

    def _journal(self, result: Any, total_ms: float, wait_ms: float) -> None:
        if self.journal is None:
            return
        record = {
            "kind": "voice",
            "model": self.recognizer.model,
            "threads": getattr(self.recognizer, "threads", None),
            "asr_ms": result.asr_ms,
            "vad_ms": result.vad_ms,
            "release_to_text_ms": total_ms,
            "wait_ms": wait_ms,
            "audio_s": result.audio_s,
            "speech_s": result.speech_s,
            "empty": not result.text,
            "text": result.text,
        }
        try:
            self.journal.write(record)
        except Exception:
            log.exception("голос: запись журнала")

    # --- уровень (поток PortAudio) ---------------------------------------------------------------

    def _on_level(self, value: float) -> None:
        if self.state == "listening":
            self.emit(Loudness(value))
