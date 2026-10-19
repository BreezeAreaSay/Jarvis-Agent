"""Голоса Fish Audio (POST https://api.fish.audio/v1/tts): клонированные и готовые голоса по reference_id.

Заголовки: Authorization: Bearer <fish_api_key>, model: <settings.tts.fish_model> (s1, speech-1.6…).
Тело JSON: text, reference_id (settings.tts.fish_voice_id), format wav, latency normal, normalize,
prosody.speed.
Ответ — WAV. Тон — частотой воспроизведения, темп выравнивается prosody.speed.
Список голосов: свои модели (GET /model?self=true) и популярные русские (language=ru).
"""

import logging
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np

from jarvis.voice.dsp import decode_wav, pcm16_to_float
from jarvis.voice.engines.base import SynthOptions, TtsEngineError, clamp

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

API = "https://api.fish.audio"
PCM_RATE = 44100


def fish_error(resp: httpx.Response) -> str:
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            raw = body.get("message") or body.get("detail") or body.get("error") or ""
            detail = raw if isinstance(raw, str) else str(raw)[:200]
    except ValueError:
        detail = resp.text[:200]
    code = resp.status_code
    if code in (401, 403):
        return "Fish Audio: неверный ключ API"
    if code == 402:
        return "Fish Audio: на счёте нет средств"
    if code == 404:
        return "Fish Audio: голос не найден — проверьте ID голоса"
    if code == 429:
        return "Fish Audio: слишком много запросов, попробуйте позже"
    return f"Fish Audio ответил {code}: {detail}" if detail else f"Fish Audio ответил {code}"


class FishEngine:
    name = "fish"
    title = "Fish Audio (интернет, по ключу)"
    cacheable = True  # короткие фразы Tts помнит: повтор не тратит деньги и не ждёт сеть

    def __init__(self, ctx: "AppContext", transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.ctx = ctx
        self.transport = transport

    def key(self) -> str:
        return self.ctx.secrets.get("fish_api_key")

    def available(self) -> str:
        return "" if self.key() else "нет ключа Fish Audio — добавьте его в настройках"

    def voice(self, settings: "Settings") -> str:
        return settings.tts.fish_voice_id

    def russian(self, voice: str) -> bool:
        return False

    def cache_key(self, settings: "Settings") -> str:
        return settings.tts.fish_model

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0), transport=self.transport)

    async def synthesize(self, text: str, opts: SynthOptions) -> tuple[np.ndarray, int]:
        key = self.key()
        if not key:
            raise TtsEngineError("нет ключа Fish Audio — добавьте его в настройках")
        settings = opts.settings or self.ctx.settings.value
        pitch = clamp(opts.pitch, 0.8, 1.2)
        body: dict[str, Any] = {"text": text, "format": "wav", "latency": "normal", "normalize": True}
        voice = (opts.voice or settings.tts.fish_voice_id).strip()
        if voice:
            body["reference_id"] = voice
        speed = clamp(opts.speed / pitch, 0.5, 2.0)
        if abs(speed - 1.0) > 0.01:
            body["prosody"] = {"speed": round(speed, 2), "volume": 0}
        headers = {"Authorization": f"Bearer {key}", "model": settings.tts.fish_model or "s1"}
        try:
            async with self._client(60.0) as client:
                resp = await client.post(f"{API}/v1/tts", json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise TtsEngineError("Fish Audio не ответил вовремя") from e
        except httpx.HTTPError as e:
            raise TtsEngineError(f"нет связи с Fish Audio: {e}") from e
        if resp.status_code != 200:
            raise TtsEngineError(fish_error(resp))
        data = resp.content
        try:
            samples, rate = decode_wav(data)
        except ValueError:
            if "json" in resp.headers.get("content-type", ""):
                raise TtsEngineError(fish_error(resp)) from None
            samples, rate = pcm16_to_float(data), PCM_RATE  # сырой PCM
        if not len(samples):
            raise TtsEngineError("Fish Audio не вернул звук")
        return samples, max(8000, round(rate * pitch))

    async def voices(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        key = self.key()
        configured = self.ctx.settings.value.tts.fish_voice_id.strip()
        if key:
            headers = {"Authorization": f"Bearer {key}"}
            queries = (
                {"self": "true", "page_size": "50"},
                {"language": "ru", "page_size": "30", "sort_by": "score"},
            )
            try:
                async with self._client(15.0) as client:
                    for params in queries:
                        resp = await client.get(f"{API}/model", params=params, headers=headers)
                        if resp.status_code != 200:
                            log.info("голос: список голосов Fish Audio: %s", fish_error(resp))
                            continue
                        for model in resp.json().get("items") or []:
                            voice_id = str(model.get("_id") or model.get("id") or "")
                            if not voice_id or voice_id in seen:
                                continue
                            seen.add(voice_id)
                            langs = model.get("languages") or []
                            items.append(
                                {
                                    "id": voice_id,
                                    "name": str(model.get("title") or voice_id),
                                    "lang": str(langs[0]) if langs else "",
                                    "gender": "",
                                }
                            )
            except (httpx.HTTPError, ValueError) as e:
                log.info("голос: список голосов Fish Audio не получен: %s", e)
        if configured and configured not in seen:
            items.insert(0, {"id": configured, "name": f"Голос {configured[:8]}…", "lang": "", "gender": ""})
        return items

    def close(self) -> None:
        return None
