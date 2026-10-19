"""Распознавание ElevenLabs Scribe v2 Realtime — облачное, по ключу elevenlabs_api_key (тот же, что у голоса).

Зачем: текст фразы готов почти сразу после её конца. Звук уходит в сервис, пока человек говорит, а на паузе
в речи слушатель просит итог заранее (commit) — к концу фразы (ещё ~0,3 с тишины) текст уже пришёл.

Что уходит в сеть — только фразы, обращённые к Jarvis: поток открывается, когда фраза стала «его» («Джарвис»
услышан на ПК потоковой Vosk, клавиша «держи и говори», окно продолжения, просьба выслушать; без режима
префикса — каждая фраза). VAD, «Джарвис» и чужие разговоры остаются на ПК. GigaAM в этом режиме не нужна:
сервис недоступен — фразу распознаёт Vosk на ПК.

Протокол — как в официальном SDK elevenlabs (realtime/scribe.py): wss://api.elevenlabs.io/v1/speech-to-text/
realtime?model_id=scribe_v2_realtime&audio_format=pcm_16000&commit_strategy=manual[&language_code=ru],
заголовок xi-api-key. Клиент → {message_type: "input_audio_chunk", audio_base_64, commit: false,
sample_rate}; commit — тот же кусок с commit: true и пустым звуком. Сервис → session_started,
partial_transcript {text}, committed_transcript {text}, ошибки {message_type, error}: auth_error,
quota_exceeded, unaccepted_terms, rate_limited, commit_throttled, insufficient_audio_activity…

Поток — один на фразу и в своём потоке: сеть не задерживает разбор звука. После паузы звук придерживается —
тишину в конце фразы сервису не шлём (кредиты — по времени звука); речь продолжилась — придержанное уходит.
Сбой (нет сети, ключ, кредиты, регион) — finish() → None, и фразу распознаёт ПК; после серьёзной ошибки сервис
не зовётся cooldown_s (кредиты — час, ключ, условия, регион — 10 минут, перегрузка и сеть — полминуты).
Прокси — из поля «Прокси» Jarvis AI, иначе системный (VPN), как у остальных клиентов ядра (jarvis.netproxy).
"""

import base64
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
import numpy as np

from jarvis import netproxy
from jarvis import text as textops
from jarvis.voice.asr import normalize_result
from jarvis.voice.dsp import FRAME, RATE, float_to_pcm16
from jarvis.voice.engines.elevenlabs import error_detail

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
MODEL_ID = "scribe_v2_realtime"
CONNECT_TIMEOUT_S = 6.0
FINISH_WAIT_S = 2.0  # конец фразы → итог сервиса не дольше; иначе — распознаёт ПК
POLL_S = 0.01
CHUNK_FRAMES = 8  # кусок звука в одном сообщении — не больше 8 блоков (0,26 с): догоняем начало фразы
HOLD_MAX_FRAMES = round(5.0 * RATE / FRAME)  # придержанный после паузы звук — не больше 5 с
MAX_SESSION_S = 120.0  # поток без закрытия (сбой слушателя) — всё равно закрываем
THROTTLE_RETRY_S = 0.15
MAX_RETRIES = 2
NET_COOLDOWN_S = 30.0
BLOCKED_COOLDOWN_S = 600.0
QUOTA_COOLDOWN_S = 3600.0
ERROR_EVERY_S = 60.0  # уведомление о сбое — не чаще раза в минуту
NO_PERMISSION = "у ключа ElevenLabs нет права «Speech to Text» — включите его в настройках ключа"

_AUDIO, _COMMIT, _CLOSE = "audio", "commit", "close"
_TAGS = re.compile(r"[(\[][^)\]]{0,40}[)\]]")  # «(смех)», «[музыка]» — не слова


class ScribeError(RuntimeError):
    """Сервис не распознал фразу; текст — для человека, cooldown_s — сколько не звать (0 — можно сразу)."""

    def __init__(self, message: str, *, cooldown_s: float = 0.0) -> None:
        super().__init__(message)
        self.cooldown_s = cooldown_s


def message_error(kind: str, text: str) -> ScribeError:
    """Ошибка из сообщения сервиса {message_type, error} → ScribeError."""
    low = f"{kind} {text}".casefold()
    if kind == "quota_exceeded" or "quota" in low:
        return ScribeError("закончились кредиты ElevenLabs", cooldown_s=QUOTA_COOLDOWN_S)
    if kind == "auth_error":
        if "permission" in low:
            return ScribeError(NO_PERMISSION, cooldown_s=BLOCKED_COOLDOWN_S)
        return ScribeError("ключ ElevenLabs не подошёл", cooldown_s=BLOCKED_COOLDOWN_S)
    if kind in ("unaccepted_terms", "unaccepted_terms_error"):
        return ScribeError(
            "примите условия Speech to Text в кабинете elevenlabs.io", cooldown_s=BLOCKED_COOLDOWN_S
        )
    if kind in ("rate_limited", "resource_exhausted", "queue_overflow"):
        return ScribeError("сервис перегружен", cooldown_s=NET_COOLDOWN_S)
    if kind == "session_time_limit_exceeded":
        return ScribeError("фраза длиннее, чем сервис слушает за раз")
    if kind in ("chunk_size_exceeded", "input_error", "invalid_request"):
        reason = f"сервис не принял звук: {text}" if text else "сервис не принял звук"
        return ScribeError(reason, cooldown_s=60.0)
    return ScribeError(f"ошибка сервиса: {text}" if text else "ошибка сервиса", cooldown_s=NET_COOLDOWN_S)


def handshake_error(status: int, body: bytes) -> ScribeError:
    """Сервис не открыл соединение (HTTP-ответ на рукопожатие) → ScribeError."""
    detail, message = error_detail(httpx.Response(status, content=body))
    low = f"{detail} {message}".casefold()
    if "quota" in low or "credits" in low:
        return ScribeError("закончились кредиты ElevenLabs", cooldown_s=QUOTA_COOLDOWN_S)
    if "unusual_activity" in low or "unusual activity" in low:
        return ScribeError(
            "сервис отключил бесплатный тариф для этого подключения (VPN, прокси или несколько бесплатных "
            "аккаунтов)",
            cooldown_s=BLOCKED_COOLDOWN_S,
        )
    if "permission" in low:
        return ScribeError(NO_PERMISSION, cooldown_s=BLOCKED_COOLDOWN_S)
    if status == 401:
        return ScribeError("неверный ключ ElevenLabs", cooldown_s=BLOCKED_COOLDOWN_S)
    if status == 403:
        return ScribeError(
            "сервис недоступен из вашего региона — включите VPN или укажите прокси в настройках Jarvis AI",
            cooldown_s=BLOCKED_COOLDOWN_S,
        )
    if status == 429:
        return ScribeError("сервис перегружен", cooldown_s=NET_COOLDOWN_S)
    tail = f": {message}" if message else ""
    return ScribeError(f"сервис ответил {status}{tail}", cooldown_s=NET_COOLDOWN_S)


def tidy(text: str, *, punct: bool) -> str:
    """Текст сервиса → как у моделей на ПК: без пометок «(смех)»; для команд — строчными."""
    return normalize_result(_TAGS.sub(" ", text), keep_case=punct)


def _ws_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)


class ScribeStream:
    """Одна фраза. feed/pause/resume/partial/finish/close — из потока распознавания; сеть — в своём потоке."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        proxy: str | None = None,
        on_error: Callable[[ScribeError], None] | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.headers = headers
        self.proxy = proxy
        self.on_error = on_error
        self._connect_fn = connect or _ws_connect
        self._q: queue.SimpleQueue[tuple[str, np.ndarray | None]] = queue.SimpleQueue()
        self._cond = threading.Condition()
        self._texts: list[str] = []  # итоги кусков фразы по порядку
        self._partial = ""
        self._requested = 0  # commit отправлено (или поставлено в очередь)
        self._answered = 0  # ответов на commit: итог, «нет речи», «слишком часто»
        self._retries = 0
        self._retry_at = 0.0
        self._error: ScribeError | None = None
        self._closing = False
        self._held: list[np.ndarray] | None = None  # звук после паузы: уйдёт, если речь продолжится
        self._fresh = 0  # блоков ушло после последнего commit (поток распознавания)
        self.started = time.monotonic()
        self.connected_ms: float | None = None
        self._thread = threading.Thread(target=self._run, name="jarvis-scribe", daemon=True)
        self._thread.start()

    # --- поток распознавания -------------------------------------------------------------------------
    def feed(self, frame: np.ndarray) -> None:
        if self._error is not None or self._closing:
            return
        if self._held is not None:
            self._held.append(frame)
            if len(self._held) > HOLD_MAX_FRAMES:
                del self._held[0]
            return
        self._q.put((_AUDIO, frame))
        self._fresh += 1

    def pause(self) -> None:
        """Речь стихла: итог сказанного — заранее; дальнейший звук придерживаем."""
        if self._held is not None or self._error is not None or self._closing:
            return
        self._held = []
        if not self._fresh:  # после прошлого commit звука не было — просить нечего
            return
        self._fresh = 0
        with self._cond:
            self._requested += 1
        self._q.put((_COMMIT, None))

    def resume(self) -> None:
        """Речь продолжилась: придержанный звук — сервису."""
        held, self._held = self._held, None
        for frame in held or ():
            self._q.put((_AUDIO, frame))
            self._fresh += 1

    def failed(self) -> bool:
        return self._error is not None

    def partial(self) -> str:
        with self._cond:
            parts = [*self._texts, self._partial]
        return textops.clean(_TAGS.sub(" ", " ".join(p for p in parts if p)))

    def finish(self, timeout: float = FINISH_WAIT_S, *, punct: bool = False) -> str | None:
        """Итог фразы: все куски по порядку. Не успел, сбой — None (распознает ПК)."""
        if self._held is None:  # после последней паузы была речь — её итог ещё не просили
            self.pause()
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._error is None and self._answered < self._requested:
                left = deadline - time.monotonic()
                if left <= 0:
                    log.info("голос: ElevenLabs не прислал итог за %.1f с — распознаю на ПК", timeout)
                    return None
                self._cond.wait(left)
            if self._error is not None:
                return None
            text = " ".join(t for t in self._texts if t)
        return tidy(text, punct=punct)

    def close(self) -> None:
        self._closing = True
        self._held = None
        self._q.put((_CLOSE, None))

    @property
    def error(self) -> ScribeError | None:
        return self._error

    # --- свой поток: сеть ----------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            ws = self._open()
        except ScribeError as e:
            self._fail(e)
            return
        try:
            with ws:  # соединение закрывается на выходе
                if not self._closing:
                    self._loop(ws)
        except Exception as e:
            if not self._closing:
                from websockets.exceptions import ConnectionClosed

                if isinstance(e, ConnectionClosed):
                    self._fail(ScribeError("сервис закрыл соединение", cooldown_s=NET_COOLDOWN_S))
                else:
                    log.exception("голос: поток ElevenLabs упал")
                    self._fail(ScribeError(f"сбой распознавания: {e}", cooldown_s=NET_COOLDOWN_S))

    def _open(self) -> Any:
        from websockets.exceptions import InvalidStatus

        t = time.monotonic()
        try:
            ws = self._connect_fn(
                self.url,
                additional_headers=self.headers,
                proxy=self.proxy,
                open_timeout=CONNECT_TIMEOUT_S,
                compression=None,
            )
        except InvalidStatus as e:
            raise handshake_error(e.response.status_code, bytes(e.response.body or b"")) from e
        except ImportError as e:  # SOCKS-прокси без python-socks
            raise ScribeError(f"прокси не поддерживается: {e}", cooldown_s=BLOCKED_COOLDOWN_S) from e
        except (OSError, TimeoutError) as e:
            via = f" через прокси {self.proxy}" if self.proxy else ""
            raise ScribeError(f"нет связи с ElevenLabs{via}: {e}", cooldown_s=NET_COOLDOWN_S) from e
        except Exception as e:  # InvalidHandshake, InvalidURI, ProxyError…
            raise ScribeError(f"нет связи с ElevenLabs: {e}", cooldown_s=NET_COOLDOWN_S) from e
        self.connected_ms = (time.monotonic() - t) * 1000
        return ws

    def _loop(self, ws: Any) -> None:
        pending: list[np.ndarray] = []
        while True:
            try:
                item: tuple[str, np.ndarray | None] | None = self._q.get(timeout=POLL_S)
            except queue.Empty:
                item = None
            while item is not None:
                kind, frame = item
                if kind == _CLOSE:
                    return
                if kind == _AUDIO and frame is not None:
                    pending.append(frame)
                    if len(pending) >= CHUNK_FRAMES:
                        self._send_audio(ws, pending)
                        pending = []
                elif kind == _COMMIT:
                    self._send_audio(ws, pending)
                    pending = []
                    self._send_commit(ws)
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    item = None
            if pending:
                self._send_audio(ws, pending)
                pending = []
            if self._retry_at and time.monotonic() >= self._retry_at:
                self._retry_at = 0.0
                self._send_commit(ws)
            self._receive(ws)
            if time.monotonic() - self.started > MAX_SESSION_S:
                log.warning("голос: поток ElevenLabs открыт дольше %.0f с — закрываю", MAX_SESSION_S)
                return

    @staticmethod
    def _chunk(audio: bytes, commit: bool) -> str:
        return json.dumps(
            {
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(audio).decode("ascii"),
                "commit": commit,
                "sample_rate": RATE,
            }
        )

    def _send_audio(self, ws: Any, frames: list[np.ndarray]) -> None:
        if frames:
            ws.send(self._chunk(float_to_pcm16(np.concatenate(frames)), False))

    def _send_commit(self, ws: Any) -> None:
        ws.send(self._chunk(b"", True))

    def _receive(self, ws: Any) -> None:
        while True:
            try:
                raw = ws.recv(timeout=0)
            except TimeoutError:
                return
            self._handle(raw)

    def _handle(self, raw: Any) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            log.debug("голос: ElevenLabs прислал не JSON: %r", raw[:200] if isinstance(raw, str) else "…")
            return
        if not isinstance(data, dict):
            return
        kind = str(data.get("message_type") or "")
        if kind == "partial_transcript":
            with self._cond:
                self._partial = str(data.get("text") or "")
        elif kind == "committed_transcript":
            with self._cond:
                self._texts.append(str(data.get("text") or "").strip())
                self._partial = ""
                self._answered += 1
                self._cond.notify_all()
        elif kind == "insufficient_audio_activity":  # в куске не было речи — пустой итог
            with self._cond:
                self._answered += 1
                self._cond.notify_all()
        elif kind == "commit_throttled":  # commit слишком часто: тот же звук — ещё раз чуть позже
            with self._cond:
                self._answered += 1
                if self._retries < MAX_RETRIES:
                    self._retries += 1
                    self._requested += 1
                    self._retry_at = time.monotonic() + THROTTLE_RETRY_S
                self._cond.notify_all()
        elif kind in ("session_started", "committed_transcript_with_timestamps"):
            return
        elif kind == "warning":
            log.info("голос: ElevenLabs предупреждает: %s", data.get("warning"))
        elif kind:
            self._fail(message_error(kind, str(data.get("error") or data.get("message") or "")))

    def _fail(self, error: ScribeError) -> None:
        with self._cond:
            if self._error is not None:
                return
            self._error = error
            self._cond.notify_all()
        self._closing = True
        if self.on_error is not None:
            try:
                self.on_error(error)
            except Exception:
                log.exception("голос: обработчик ошибки ElevenLabs упал")


def stream_url(settings: "Settings") -> str:
    """Адрес потока: модель, PCM 16 кГц, commit по команде слушателя, язык команд (auto — узнаёт сервис)."""
    params = {
        "model_id": MODEL_ID,
        "audio_format": f"pcm_{RATE}",
        "commit_strategy": "manual",
        "include_timestamps": "false",
    }
    lang = settings.voice.language
    if lang != "auto":
        params["language_code"] = lang
    return f"{WS_URL}?{urlencode(params)}"


def proxy_for(settings: "Settings") -> str | None:
    """Прокси соединения: поле «Прокси» Jarvis AI, иначе системный (VPN), иначе напрямую."""
    return netproxy.normalize(settings.ai.proxy) or netproxy.system_https() or None


class Scribe:
    """Облачное распознавание для голосового сервиса: выбрано ли, ключ, пауза после ошибки, поток."""

    title = "Распознавание ElevenLabs"

    def __init__(self, ctx: "AppContext", *, connect: Callable[..., Any] | None = None) -> None:
        self.ctx = ctx
        self.connect = connect
        self.last_error = ""
        self._until = 0.0
        self._notified = -1e9
        self._lock = threading.Lock()

    def key(self) -> str:
        return self.ctx.secrets.get("elevenlabs_api_key")

    def selected(self) -> bool:
        return self.ctx.settings.value.voice.asr_model == "elevenlabs"

    def available(self) -> str:
        """Пустая строка — можно звать; иначе — почему нет."""
        if not self.selected():
            return "выбрано распознавание на ПК"
        if not self.key():
            return "нет ключа ElevenLabs — добавьте его в настройках голоса Jarvis"
        if time.monotonic() < self._until:
            return self.last_error or "пауза после ошибки"
        return ""

    def stream(self) -> ScribeStream | None:
        """Поток на фразу или None (не выбрано, нет ключа, пауза после ошибки)."""
        if self.available():
            return None
        settings = self.ctx.settings.value
        return ScribeStream(
            stream_url(settings),
            {"xi-api-key": self.key()},
            proxy=proxy_for(settings),
            on_error=self._failed,
            connect=self.connect,
        )

    def reset(self) -> None:
        """Ключ или настройки поменялись — пауза после ошибки больше не действует."""
        with self._lock:
            self._until = 0.0
            self.last_error = ""

    def _failed(self, error: ScribeError) -> None:
        text = str(error)
        with self._lock:
            self.last_error = text
            if error.cooldown_s:
                self._until = time.monotonic() + error.cooldown_s
            now = time.monotonic()
            notify = now - self._notified >= ERROR_EVERY_S
            if notify:
                self._notified = now
        log.warning("голос: ElevenLabs: %s — распознаю на ПК", text)
        if notify:
            self.ctx.bus.publish(
                "toast",
                {
                    "title": self.title,
                    "message": f"{text[:1].upper()}{text[1:]}. Пока распознаю на компьютере",
                    "status": "error",
                    "icon": "mic",
                    "ts": time.time(),
                },
            )
