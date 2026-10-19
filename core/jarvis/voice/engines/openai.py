"""Голоса OpenAI (POST {base}/audio/speech): gpt-4o-mini-tts слушается указаний «как говорить» —
«как J.A.R.V.I.S.: спокойно, учтиво, с лёгкой иронией». Ответ — PCM 24 кГц 16 бит моно (response_format pcm).

Ключ — секрет openai_api_key, а если ИИ работает через OpenAI — ai_api_key. Прокси — settings.ai.proxy
(OpenAI закрыт для части регионов). Тон — частотой воспроизведения, темп при этом выравнивается
параметром speed.
"""

import logging
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np

from jarvis.voice.dsp import pcm16_to_float
from jarvis.voice.engines.base import SynthOptions, TtsEngineError, clamp

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.openai.com/v1"
PCM_RATE = 24000
VOICES = (
    ("onyx", "Onyx", "male"),
    ("echo", "Echo", "male"),
    ("ash", "Ash", "male"),
    ("ballad", "Ballad", "male"),
    ("verse", "Verse", "male"),
    ("cedar", "Cedar", "male"),
    ("fable", "Fable", ""),
    ("alloy", "Alloy", ""),
    ("sage", "Sage", "female"),
    ("coral", "Coral", "female"),
    ("nova", "Nova", "female"),
    ("shimmer", "Shimmer", "female"),
    ("marin", "Marin", "female"),
)


def openai_error(resp: httpx.Response) -> str:
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("code") or "")
        elif isinstance(err, str):
            detail = err
    except ValueError:
        detail = resp.text[:200]
    code = resp.status_code
    if code == 401:
        return "OpenAI: неверный ключ API"
    if code == 403:
        return "OpenAI недоступен из вашего региона — укажите прокси в настройках ИИ"
    if code == 429:
        return "OpenAI: превышен лимит запросов или на счёте нет денег"
    if code == 404:
        return f"OpenAI: модель или адрес не найдены ({detail or code})"
    return f"OpenAI ответил {code}: {detail}" if detail else f"OpenAI ответил {code}"


class OpenAIEngine:
    name = "openai"
    title = "Голоса OpenAI (интернет, по ключу)"
    cacheable = True  # короткие фразы Tts помнит: повтор не тратит деньги и не ждёт сеть

    def __init__(self, ctx: "AppContext", transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.ctx = ctx
        self.transport = transport

    def key(self) -> str:
        key = self.ctx.secrets.get("openai_api_key")
        if not key and self.ctx.settings.value.ai.provider == "openai":
            key = self.ctx.secrets.get("ai_api_key")
        return key

    def base_url(self) -> str:
        ai = self.ctx.settings.value.ai
        if ai.provider == "openai" and ai.base_url.strip():
            return ai.base_url.strip().rstrip("/")
        return DEFAULT_BASE

    def available(self) -> str:
        return "" if self.key() else "нет ключа OpenAI — добавьте его в настройках"

    def voice(self, settings: "Settings") -> str:
        return settings.tts.openai_voice or "onyx"

    def russian(self, voice: str) -> bool:
        return False  # латиницу OpenAI читает сам

    def cache_key(self, settings: "Settings") -> str:
        return f"{settings.tts.openai_model}|{settings.tts.openai_instructions}"

    async def synthesize(self, text: str, opts: SynthOptions) -> tuple[np.ndarray, int]:
        key = self.key()
        if not key:
            raise TtsEngineError("нет ключа OpenAI — добавьте его в настройках")
        settings = opts.settings or self.ctx.settings.value
        tts = settings.tts
        pitch = clamp(opts.pitch, 0.8, 1.2)
        body: dict[str, Any] = {
            "model": tts.openai_model or "gpt-4o-mini-tts",
            "voice": opts.voice or tts.openai_voice or "onyx",
            "input": text,
            "response_format": "pcm",
        }
        speed = clamp(opts.speed / pitch, 0.25, 4.0)
        if abs(speed - 1.0) > 0.01:
            body["speed"] = round(speed, 2)
        if tts.openai_instructions.strip() and not body["model"].startswith("tts-"):
            body["instructions"] = tts.openai_instructions.strip()  # tts-1 указаний не понимает
        proxy = settings.ai.proxy.strip() or None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(45.0, connect=10.0),
                proxy=None if self.transport is not None else proxy,
                transport=self.transport,
            ) as client:
                resp = await client.post(
                    f"{self.base_url()}/audio/speech",
                    json=body,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.TimeoutException as e:
            raise TtsEngineError("OpenAI не ответил вовремя") from e
        except httpx.HTTPError as e:
            raise TtsEngineError(f"нет связи с OpenAI: {e}") from e
        if resp.status_code != 200:
            raise TtsEngineError(openai_error(resp))
        samples = pcm16_to_float(resp.content)
        if not len(samples):
            raise TtsEngineError("OpenAI не вернул звук")
        return samples, max(8000, round(PCM_RATE * pitch))

    async def voices(self) -> list[dict[str, Any]]:
        return [{"id": v, "name": name, "lang": "multi", "gender": gender} for v, name, gender in VOICES]

    def close(self) -> None:
        return None
