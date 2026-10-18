"""Слушатель — машина состояний голосового ввода. Звук, модели и действия ему дают снаружи (Host), поэтому
логика проверяется в тестах без микрофона и моделей.

Поток: блок 512 отсчётов → VAD. Тишина — блок в кольцо (≈0,5 с до начала речи). Речь началась — фраза:
кольцо + блоки, потоковая модель по ходу (voice.partial не чаще 5 раз в секунду; «Джарвис» в частичном тексте
— сразу voice.wake, а если Jarvis говорит — замолчать: перебивание). Конец речи (VAD), предел длины или
отпущенная клавиша — итоговый текст GigaAM по всей фразе (нет GigaAM — текст потоковой модели) →
voice.final {text, wake, command} → решение:

- просьба ждёт фразу (listen_now / ask_yes_no / capture_text / «Джарвис» без команды) — фраза её;
  новая просьба отменяет прежнюю;
- «Джарвис, <команда>» — команда; «Джарвис» один — сигнал «слушаю» и ~6 с ждём команду без активатора;
- без активатора — команда, только если prefix_mode выключен, идёт окно продолжения (follow_up после ответа)
  или фраза сказана с клавишей «держи и говори»; иначе это чужой разговор — его не показываем. Окон
  продолжения подряд — не больше MAX_FOLLOW_CHAIN: иначе фоновая речь (видео, чужой разговор) попадает в окно,
  Jarvis отвечает, окно открывается снова — и он «слушает без остановки»;
- пока Jarvis говорит, без активатора ничего не принимается: в микрофон попадает его же голос (эхо).

Управляющие вызовы (request, follow_up, ptt…) можно делать из любого потока: они встают в очередь и
выполняются в потоке распознавания перед следующим блоком — сама машина однопоточная.
"""

import logging
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from rapidfuzz import fuzz

from jarvis import text as textops
from jarvis.settings import VoiceSettings
from jarvis.voice.audio import PreRoll
from jarvis.voice.dsp import FRAME, RATE

log = logging.getLogger(__name__)

WAKE_WINDOW_S = 6.0  # после одиночного «Джарвис» команда принимается без активатора
LISTEN_TIMEOUT_S = 8.0  # кнопка микрофона / хоткей: столько ждём начала фразы
ECHO_TAIL_S = 0.25  # после конца речи Jarvis её хвост ещё звучит в комнате
PARTIAL_EVERY_S = 0.2  # voice.partial — не чаще 5 раз в секунду (по времени звука, а не часов)
PARTIAL_FRAMES = max(1, round(PARTIAL_EVERY_S * RATE / FRAME))
HEAD_S = 2.5  # длинная непрошеная фраза: GigaAM сначала только по началу (есть ли там «Джарвис»)
MIN_PTT_S = 0.3  # короче — случайное нажатие клавиши
VAD_LAG_S = 0.3  # VAD узнаёт о речи с задержкой: фраза началась примерно на столько раньше
MAX_FOLLOW_CHAIN = 2  # команд подряд из окна продолжения (без «Джарвис»); дальше — снова по активатору


class Host(Protocol):
    """Что слушателю нужно от сервиса: настройки, события, выполнение, связь с синтезом речи."""

    def settings(self) -> VoiceSettings: ...
    def publish(self, type: str, data: dict[str, Any]) -> None: ...
    def dispatch(self, command: str) -> None: ...
    def tts_speaking(self) -> bool: ...
    def silence_tts(self) -> None: ...
    def earcon(self, name: str) -> None: ...
    def activity(self, value: str) -> None: ...


class Session(Protocol):
    """Фраза потоковой модели."""

    def feed(self, frame: np.ndarray) -> None: ...
    def partial(self) -> str: ...
    def finish(self) -> str: ...


FinalFn = Callable[[np.ndarray, bool], str]  # (звук фразы, нужна ли пунктуация) → текст


@dataclass
class Request:
    """Просьба выслушать следующую фразу: kind command — выполнить, text / yesno — вернуть текст."""

    kind: str
    timeout: float
    punct: bool = False
    on_done: Callable[[str | None], None] | None = None
    source: str = ""  # wake | listen | capture | yesno
    deadline: float = 0.0
    done: bool = False

    def finish(self, value: str | None) -> None:
        if self.done:
            return
        self.done = True
        if self.on_done is not None:
            try:
                self.on_done(value)
            except Exception:
                log.exception("голос: обработчик просьбы упал")


@dataclass
class Utterance:
    start: float
    manual: bool
    echo: bool
    session: Session | None
    frames: list[np.ndarray] = field(default_factory=list)
    count: int = 0  # блоков после срабатывания (без кольца)
    partial: str = ""
    wake_early: bool = False
    next_partial: int = 0  # номер блока, на котором снова спросить потоковую модель

    @property
    def seconds(self) -> float:
        return self.count * FRAME / RATE


def find_wake_anywhere(text: str, wake_words: list[str]) -> tuple[bool, str]:
    """Активатор в любом месте фразы (когда в микрофоне ещё и голос Jarvis): (найден, текст после него)."""
    words = text.split()
    for i in range(0, max(1, len(words)), 3):
        found, rest = textops.find_wake(" ".join(words[i:]), wake_words)
        if found:
            return True, rest
    return False, textops.clean(text)


LOOSE_EARLY = 52  # потоковая модель уже слышала «Джарвис»: первое слово итога — его искажение
LOOSE = 64  # без подтверждения: начинается как активатор («дж…») или сказано одно слово
WAKE_LETTERS = "джшчзj"


def match_wake(text: str, wake_words: list[str], *, early: bool = False) -> tuple[bool, str]:
    """Активатор в начале фразы: строгое jarvis.text.find_wake, затем мягче — для искажений GigaAM.

    Замер на синтезе ruslan: GigaAM слышит «Джарвис» и как «джарлес», «джарлиз», «джералис», «жарес» — строгий
    порог (72) их не берёт. Мягкий порог — только с подтверждением: потоковая модель уже услышала активатор
    (early), или слово начинается как активатор, или во фразе одно это слово.
    """
    found, rest = textops.find_wake(text, wake_words)
    if found or not text.strip():
        return found, rest
    words = text.split()
    for n in (1, 2):
        if n > len(words):
            break
        cand = textops.normalize("".join(words[:n]))
        if not cand or cand[0] not in WAKE_LETTERS:
            continue
        for wake in wake_words:
            target = textops.normalize(wake)
            if len(target) < 3 or abs(len(cand) - len(target)) > 3:
                continue
            score = fuzz.ratio(cand, target)
            if early:
                ok = score >= LOOSE_EARLY
            else:
                alone = len(words) == n
                ok = score >= LOOSE and (cand.startswith(target[:2]) or (alone and cand[:2] == target[1:3]))
            if ok:
                return True, textops.clean(" ".join(words[n:]))
    return False, textops.clean(text)


def _after_wake(raw: str, rest: str) -> str:
    """Хвост исходного текста после активатора — с пунктуацией: «Джарвис, купи хлеба.» → «купи хлеба.»."""
    if not rest:
        return ""
    words = raw.split()
    n = len(rest.split())
    return " ".join(words[len(words) - n :]) if n <= len(words) else rest


class Listener:
    def __init__(
        self,
        host: Host,
        *,
        vad: Any,
        stream_factory: Callable[[], Session | None] | None = None,
        final: FinalFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.vad = vad
        self.stream_factory = stream_factory
        self.final = final
        self.clock = clock
        self.requests: list[Request] = []
        self.utt: Utterance | None = None
        self.preroll = PreRoll()
        self.follow_until = 0.0
        self.follow_pending: float | None = None
        self.follow_chain = 0  # сколько команд подряд пришло из окна продолжения
        self._cmds: queue.SimpleQueue[Callable[[float], None]] = queue.SimpleQueue()
        self._tts_last = -1e9
        self._was_speaking = False
        self._activity = "idle"

    # --- управление (любой поток) ------------------------------------------------------------------
    def post(self, fn: Callable[[float], None]) -> None:
        self._cmds.put(fn)

    def request(
        self,
        kind: str,
        timeout: float,
        *,
        punct: bool = False,
        on_done: Callable[[str | None], None] | None = None,
        source: str = "",
    ) -> Request:
        """Следующая фраза — для этой просьбы. Новая просьба отменяет прежние (их on_done(None))."""
        req = Request(kind, timeout, punct, on_done, source or kind)
        self.post(lambda now: self._add_request(req, now))
        return req

    def listen_now(
        self, timeout: float = LISTEN_TIMEOUT_S, on_done: Callable[[str | None], None] | None = None
    ) -> Request:
        return self.request("command", timeout, on_done=on_done, source="listen")

    def cancel(self, req: Request) -> None:
        def apply(now: float) -> None:
            if req in self.requests:
                self.requests.remove(req)
            req.finish(None)

        self.post(apply)

    def stop(self) -> None:
        """Перестать слушать: просьбы отменены, окно продолжения закрыто, начатая обращённая фраза брошена."""

        def apply(now: float) -> None:
            for req in self.requests:
                req.finish(None)
            self.requests.clear()
            self.follow_until, self.follow_pending = 0.0, None
            utt = self.utt
            if utt is not None and (utt.manual or utt.wake_early or not self.host.settings().prefix_mode):
                self._drop()

        self.post(apply)

    def follow_up(self, seconds: float) -> None:
        """Окно продолжения: после ответа Jarvis (когда он договорит) фраза принимается без активатора."""

        def apply(now: float) -> None:
            if seconds <= 0 or self.follow_chain >= MAX_FOLLOW_CHAIN:
                if seconds > 0:
                    log.info(
                        "голос: %s ответа подряд без «Джарвис» — дальше жду активатор", self.follow_chain
                    )
                self.follow_until, self.follow_pending = 0.0, None
            elif self.host.tts_speaking():
                self.follow_pending = seconds
            else:
                self.follow_until = now + seconds

        self.post(apply)

    def ptt(self, pressed: bool) -> None:
        """Клавиша «держи и говори»: нажата — фраза началась, отпущена — закончилась."""
        self.post(lambda now: self._ptt(pressed, now))

    def ptt_cancel(self) -> None:
        """Клавиша нажата вместе с другой (правый Ctrl+C) — это не голос."""

        def apply(now: float) -> None:
            if self.utt is not None and self.utt.manual:
                self._drop()

        self.post(apply)

    # --- обработка (поток распознавания) -----------------------------------------------------------
    def process(self, frame: np.ndarray) -> None:
        now = self.clock()
        self._drain(now)
        speaking = self._track_tts(now)
        is_speech = bool(self.vad.accept(frame))
        utt = self.utt
        if utt is None:
            self.preroll.push(frame)
            if is_speech:
                self._begin(now, manual=False, speaking=speaking)
            else:
                self._expire(now)
            self._update_activity(now)
            return
        utt.frames.append(frame)
        utt.count += 1
        if speaking and not utt.manual:
            utt.echo = True
        self._feed(utt, frame, now)
        limit = self.host.settings().max_utterance_s
        if utt.manual:
            if utt.seconds >= limit:
                self._finish(now)
        elif not is_speech:
            self._finish(now)
        elif utt.seconds >= limit:
            log.info("голос: фраза длиннее %d с — распознаю, что есть", limit)
            self._finish(now)
            self.vad.reset()
        self._update_activity(now)

    def tick(self) -> None:
        """Блоков нет (микрофон переподключается): таймеры и команды всё равно идут."""
        now = self.clock()
        self._drain(now)
        self._track_tts(now)
        if self.utt is None:
            self._expire(now)
        self._update_activity(now)

    @property
    def activity(self) -> str:
        return self._activity

    # --- внутреннее --------------------------------------------------------------------------------
    def _drain(self, now: float) -> None:
        while True:
            try:
                fn = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                fn(now)
            except Exception:
                log.exception("голос: команда слушателя упала")

    def _track_tts(self, now: float) -> bool:
        speaking = bool(self.host.tts_speaking())
        if speaking:
            self._tts_last = now
        elif self._was_speaking:
            if self.follow_pending:
                self.follow_until = now + self.follow_pending
                self.follow_pending = None
            utt = self.utt
            if utt is not None and utt.echo and not utt.wake_early and not utt.manual:
                # Jarvis договорил посреди «фразы» — это был его голос; ответ человека VAD начнёт заново
                self._drop()
        self._was_speaking = speaking
        return speaking

    def _add_request(self, req: Request, now: float) -> None:
        for old in self.requests:
            old.finish(None)
        self.requests = [req]
        req.deadline = now + req.timeout
        if req.source == "listen":
            self.host.earcon("wake")

    def _begin(self, now: float, *, manual: bool, speaking: bool) -> None:
        pre = self.preroll.take()
        echo = not manual and (speaking or (now - self._tts_last) < ECHO_TAIL_S)
        session: Session | None = None
        if self.stream_factory is not None:
            try:
                session = self.stream_factory()
            except Exception:
                log.exception("голос: потоковая модель не начала фразу")
        start = now if manual else now - VAD_LAG_S
        utt = Utterance(start=start, manual=manual, echo=echo, session=session)
        self.utt = utt
        for frame in pre:
            utt.frames.append(frame)
            self._feed(utt, frame, now, partial=False)

    def _feed(self, utt: Utterance, frame: np.ndarray, now: float, partial: bool = True) -> None:
        if utt.session is None:
            return
        try:
            utt.session.feed(frame)
        except Exception:
            log.exception("голос: потоковая модель упала — дальше без неё")
            utt.session = None
            return
        if not partial or utt.count < utt.next_partial:
            return
        utt.next_partial = utt.count + PARTIAL_FRAMES
        try:
            text = utt.session.partial()
        except Exception:
            log.exception("голос: потоковая модель упала")
            utt.session = None
            return
        if not text or text == utt.partial:
            return
        utt.partial = text
        if not utt.wake_early and self._wake(text, utt)[0]:
            utt.wake_early = True
            self.host.publish("voice.wake", {})
            if utt.echo or self.host.tts_speaking():
                self.host.silence_tts()  # перебивание: «Джарвис, стоп» поверх речи Jarvis
        if self._addressed(utt, self.host.settings()):  # чужой разговор и эхо интерфейсу не показываем
            self.host.publish("voice.partial", {"text": text, "wake": utt.wake_early})

    def _wake(self, text: str, utt: Utterance) -> tuple[bool, str]:
        words = self.host.settings().wake_words
        if utt.echo:
            return find_wake_anywhere(text, words)
        return match_wake(text, words, early=utt.wake_early)

    def _ptt(self, pressed: bool, now: float) -> None:
        utt = self.utt
        if pressed:
            if utt is not None and utt.manual:
                return
            self.host.silence_tts()
            if utt is None:
                self._begin(now, manual=True, speaking=False)
            else:
                utt.manual, utt.echo = True, False
            self.host.earcon("wake")
            return
        if utt is None or not utt.manual:
            return
        if utt.seconds < MIN_PTT_S:
            self._drop()
            return
        self._finish(now)

    def _drop(self) -> None:
        self.utt = None
        try:
            self.vad.reset()
        except Exception:
            log.debug("голос: VAD не сбросился", exc_info=True)

    def _active(self) -> Request | None:
        return self.requests[-1] if self.requests else None

    def _in_follow(self, t: float) -> bool:
        return self.follow_until > 0 and t <= self.follow_until

    def _addressed(self, utt: Utterance, s: VoiceSettings) -> bool:
        """Фраза обращена к Jarvis (её показываем и распознаём целиком), а не фоновый разговор или эхо."""
        if utt.manual or utt.wake_early:
            return True
        if utt.echo:
            return False
        return not s.prefix_mode or bool(self.requests) or self._in_follow(utt.start)

    def _final_text(self, utt: Utterance, audio: np.ndarray, addressed: bool, stream_text: str) -> str:
        if self.final is None or not len(audio):
            return stream_text
        req = self._active()
        punct = bool(req is not None and req.punct)
        try:
            if utt.echo and not utt.wake_early and utt.session is not None:
                # голос Jarvis в микрофоне: GigaAM — только если потоковая услышала активатор
                if not self._wake(stream_text, utt)[0]:
                    return stream_text
                return self.final(audio, punct)
            if addressed or len(audio) <= int((HEAD_S + 0.5) * RATE):
                return self.final(audio, punct)
            # непрошеная длинная фраза (разговор, телевизор): сначала начало — есть ли там активатор
            head = self.final(audio[: int(HEAD_S * RATE)], False)
            if self._wake(head, utt)[0]:
                return self.final(audio, punct)
            return stream_text or head
        except Exception:
            log.exception("голос: итоговое распознавание упало — беру потоковый текст")
            return stream_text

    def _finish(self, now: float) -> None:
        utt = self.utt
        if utt is None:
            return
        self.utt = None
        s = self.host.settings()
        addressed = self._addressed(utt, s)
        if addressed:
            self._set_activity("processing")
        stream_text = ""
        if utt.session is not None:
            try:
                stream_text = utt.session.finish()
            except Exception:
                log.exception("голос: потоковая модель не закончила фразу")
        audio = np.concatenate(utt.frames) if utt.frames else np.zeros(0, dtype=np.float32)
        raw = " ".join(self._final_text(utt, audio, addressed, stream_text).split())
        self._decide(utt, raw, now, s)

    def _publish_final(self, text: str, wake: bool, command: str) -> None:
        self.host.publish("voice.final", {"text": text, "wake": wake, "command": command})

    def _decide(self, utt: Utterance, raw: str, now: float, s: VoiceSettings) -> None:
        """raw — текст модели как есть (у диктовки — с пунктуацией); команды — без знаков по краям."""
        text = textops.clean(raw)
        found, rest = self._wake(text, utt) if text else (False, "")
        wake = found or utt.wake_early
        if utt.wake_early and not found:
            rest = text  # потоковая слышала «Джарвис», итоговая — нет: команда — вся фраза
        if not text:
            if self._addressed(utt, s):
                self._publish_final("", wake, "")
                if utt.manual:
                    self.host.earcon("error")  # нажал, сказал — не расслышали
            return
        if utt.echo and not wake:
            log.debug("голос: пока Jarvis говорит, без активатора не слушаю: %r", text)
            return
        if utt.echo and rest and textops.is_stop(rest.split()[0]):
            rest = textops.clean(rest.split()[0])  # «Джарвис, стоп» поверх его речи: дальше — его слова
        req = self._active()
        if req is not None:
            if wake and not rest and req.kind == "command":
                req.deadline = now + WAKE_WINDOW_S  # «Джарвис» ещё раз — продолжаем ждать команду
                self.host.earcon("wake")
                self._publish_final(text, True, "")
                return
            self.requests.remove(req)
            value = rest if wake else text
            if req.kind == "command":
                self._publish_final(text, wake, value)
                req.finish(value)
                self.follow_chain = 0  # команду ждали (кнопка, хоткей, «Джарвис») — к нему обратились
                self._dispatch(value)
            else:
                if req.kind == "text":  # диктовка: точки и запятые модели сохраняем
                    value = _after_wake(raw, rest) if wake else raw
                self._publish_final(raw if req.kind == "text" else text, wake, "")
                req.finish(value)
            return
        if wake and not rest:
            # одно «Джарвис» — ждём команду следующей фразой
            if not utt.wake_early:
                self.host.publish("voice.wake", {})
            self._add_request(Request("command", WAKE_WINDOW_S, source="wake"), now)
            self.host.earcon("wake")
            self._publish_final(text, True, "")
            return
        command = ""
        via_follow = False
        if wake:
            command = rest
        elif utt.manual or not s.prefix_mode:
            command = text
        elif self._in_follow(utt.start):
            command, via_follow = text, True
        if not command:
            log.debug("голос: фраза не мне: %r", text)  # фоновый разговор — не показываем и не выполняем
            return
        self._publish_final(text, wake, command)
        self.follow_chain = self.follow_chain + 1 if via_follow else 0
        self._dispatch(command)

    def _dispatch(self, command: str) -> None:
        self.follow_until, self.follow_pending = 0.0, None
        if command:
            self.host.dispatch(command)

    def _expire(self, now: float) -> None:
        for req in list(self.requests):
            if req.deadline and now > req.deadline:
                self.requests.remove(req)
                if req.kind == "command" and req.source in ("wake", "listen"):
                    self.host.earcon("done")
                req.finish(None)
        if self.follow_until and now > self.follow_until:
            self.follow_until = 0.0

    def _update_activity(self, now: float) -> None:
        utt = self.utt
        addressed = utt is not None and self._addressed(utt, self.host.settings())
        follow = bool(self.follow_until) and now <= self.follow_until
        self._set_activity("listening" if addressed or self.requests or follow else "idle")

    def _set_activity(self, value: str) -> None:
        if value != self._activity:
            self._activity = value
            self.host.activity(value)
