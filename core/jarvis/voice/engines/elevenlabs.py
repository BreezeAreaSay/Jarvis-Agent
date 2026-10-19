"""Голоса ElevenLabs (POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}): самые живые голоса.

Бесплатный тариф — около 10 000 кредитов в месяц (символ текста — кредит, у eleven_flash_v2_5 — полкредита),
только для личного некоммерческого использования; ключ — в кабинете elevenlabs.io (API Keys).
Заголовок xi-api-key: <elevenlabs_api_key>. Запрос: ?output_format=pcm_24000, JSON text, model_id
(settings.tts.elevenlabs_model), voice_settings {stability, similarity_boost, style, use_speaker_boost,
speed}.
Ответ — PCM 24 кГц 16 бит моно. Тон — частотой воспроизведения, темп выравнивается speed (у сервиса
0,7–1,2). eleven_v3 принимает стабильность только 0 / 0,5 / 1 и без style и speed. language_code не шлём:
multilingual_v2 его не понимает, а язык сервис узнаёт по тексту сам.

Ошибки — по detail.status: кончились кредиты (quota_exceeded) — движок не трогаем до сброса счётчика,
бесплатный тариф отключён за VPN (detected_unusual_activity), у ключа нет права, голос из библиотеки
на бесплатном тарифе… Прокси — settings.ai.proxy (сервис закрыт для части регионов).
Список голосов — GET /v2/voices; остаток кредитов и модели — account(): /v1/user/subscription и /v1/models.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import numpy as np

from jarvis.voice.dsp import pcm16_to_float
from jarvis.voice.engines.base import SynthOptions, TtsEngineError, clamp, gender_name

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.settings import Settings

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"
PCM_RATE = 24000
OUTPUT_FORMAT = "pcm_24000"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # George — британский мужской голос из стандартных (есть и бесплатно)
DEFAULT_MODEL = "eleven_flash_v2_5"
SPEED_MIN, SPEED_MAX = 0.7, 1.2
QUOTA_COOLDOWN_S = 3600.0  # кредиты кончились, а когда обновятся — неизвестно
MAX_QUOTA_COOLDOWN_S = 32 * 24 * 3600.0
BLOCKED_COOLDOWN_S = 600.0  # ключ неверный, нет права, бесплатный тариф отключён — пока человек не поправит
NO_KEY = "нет ключа ElevenLabs — добавьте его в настройках голоса"

ACCENTS = {
    "british": "британский",
    "american": "американский",
    "australian": "австралийский",
    "irish": "ирландский",
    "scottish": "шотландский",
    "indian": "индийский",
    "african": "африканский",
    "swedish": "шведский",
    "italian": "итальянский",
    "russian": "русский",
    "transatlantic": "трансатлантический",
}


class QuotaError(TtsEngineError):
    """Кончились кредиты: движок не зовём до их обновления (cooldown_s уточняется по подписке)."""


def error_detail(resp: httpx.Response) -> tuple[str, str]:
    """(статус, сообщение) из ответа: detail {status|code|type, message}, список ошибок 422 или текст."""
    try:
        body = resp.json()
    except ValueError:
        return "", resp.text[:200]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        status = detail.get("status") or detail.get("code") or detail.get("type") or detail.get("error") or ""
        message = detail.get("message") or detail.get("msg") or ""
        return str(status).casefold(), str(message)[:300]
    if isinstance(detail, list):
        parts: list[str] = []
        for item in detail[:3]:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in item.get("loc") or [] if p != "body")
                msg = str(item.get("msg") or "")
                parts.append(f"{loc}: {msg}" if loc and msg else msg or loc)
        return "", "; ".join(p for p in parts if p)[:300]
    return "", str(detail or "")[:300]


def eleven_error(resp: httpx.Response, right: str = "Text to Speech") -> TtsEngineError:
    """Ответ с ошибкой → понятная человеку TtsEngineError (с паузой для движка, если ждать бесполезно).

    Порядок важен: кончившиеся кредиты, отключённый бесплатный тариф и нехватка прав приходят тоже с 401.
    """
    status, message = error_detail(resp)
    low = f"{status} {message}".casefold()
    code = resp.status_code
    if "quota_exceeded" in low or "exceeds your quota" in low or "credits remaining" in low:
        return QuotaError(
            "закончились бесплатные кредиты этого месяца — пока говорю запасным голосом",
            cooldown_s=QUOTA_COOLDOWN_S,
        )
    if "unusual_activity" in low or "unusual activity" in low:
        return TtsEngineError(
            "сервис отключил бесплатный тариф для этого подключения (VPN, прокси или несколько бесплатных "
            "аккаунтов) — отключите VPN или перейдите на платный тариф",
            cooldown_s=BLOCKED_COOLDOWN_S,
        )
    if "missing_permission" in low or "missing the permission" in low:
        return TtsEngineError(
            f"у ключа нет права «{right}» — включите его в настройках ключа на elevenlabs.io",
            cooldown_s=BLOCKED_COOLDOWN_S,
        )
    if code == 402 or "payment_required" in low or "library voice" in low or "free users" in low:
        return TtsEngineError(
            "этот голос из библиотеки доступен только на платном тарифе — выберите стандартный голос"
        )
    if "voice_not_found" in low or (code in (400, 404) and "voice" in low):
        return TtsEngineError("голос не найден — выберите другой в настройках голоса")
    if "model" in low and code in (400, 404, 422):
        return TtsEngineError("эта модель недоступна — выберите другую в настройках голоса")
    if code == 401 or "invalid_api_key" in low:
        return TtsEngineError("неверный ключ API", cooldown_s=BLOCKED_COOLDOWN_S)
    if code == 429 or "system_busy" in low or "too_many_concurrent" in low or "rate_limit" in low:
        return TtsEngineError("сервис перегружен или слишком много запросов — попробуйте позже")
    if code == 403:
        return TtsEngineError("сервис недоступен из вашего региона — укажите прокси в настройках ИИ")
    return TtsEngineError(f"сервис ответил {code}: {message}" if message else f"сервис ответил {code}")


def voice_settings(settings: "Settings", model: str, speed: float) -> dict[str, Any]:
    """voice_settings запроса: стабильность, сходство, выразительность, темп (с поправкой на тон)."""
    tts = settings.tts
    stability = clamp(tts.elevenlabs_stability, 0.0, 1.0)
    similarity = round(clamp(tts.elevenlabs_similarity, 0.0, 1.0), 2)
    if model.startswith("eleven_v3"):
        # v3: стабильность — только «творчески» 0, «естественно» 0,5 или «ровно» 1; style и speed не принимает
        snapped = min((0.0, 0.5, 1.0), key=lambda v: abs(v - stability))
        return {"stability": snapped, "similarity_boost": similarity}
    out: dict[str, Any] = {
        "stability": round(stability, 2),
        "similarity_boost": similarity,
        "style": round(clamp(tts.elevenlabs_style, 0.0, 1.0), 2),
        "use_speaker_boost": True,
    }
    speed = clamp(speed, SPEED_MIN, SPEED_MAX)
    if abs(speed - 1.0) > 0.01:
        out["speed"] = round(speed, 2)
    return out


def _voice_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    voice_id = str(raw.get("voice_id") or "")
    if not voice_id:
        return None
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    name = str(raw.get("name") or voice_id).split(" - ", 1)[0].strip() or voice_id
    accent = str(labels.get("accent") or "").casefold()
    accent_ru = ACCENTS.get(accent, accent)
    category = str(raw.get("category") or "")
    if category and category not in ("premade", "high_quality"):
        name = f"{name} (ваш)"
    if accent_ru:
        name = f"{name} — {accent_ru}"
    lang = str(labels.get("language") or "")
    verified = raw.get("verified_languages")
    if not lang and isinstance(verified, list) and verified and isinstance(verified[0], dict):
        lang = str(verified[0].get("language") or "")
    return {
        "id": voice_id,
        "name": name[:60],
        "lang": lang or "multi",
        "gender": gender_name(labels.get("gender")),
        "category": category,
    }


def _russian(languages: Any) -> bool:
    """Модель говорит по-русски (список языков пуст — не знаем, считаем, что да)."""
    if not isinstance(languages, list) or not languages:
        return True
    for lang in languages:
        if isinstance(lang, dict):
            code = str(lang.get("language_id") or "").casefold()
            name = str(lang.get("name") or "").casefold()
            if code in ("ru", "rus") or code.startswith("ru-") or "russian" in name:
                return True
    return False


class ElevenLabsEngine:
    name = "elevenlabs"
    title = "ElevenLabs (интернет, по ключу)"
    cacheable = True  # короткие фразы Tts помнит: повтор не тратит кредиты

    def __init__(self, ctx: "AppContext", transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.ctx = ctx
        self.transport = transport

    def key(self) -> str:
        return self.ctx.secrets.get("elevenlabs_api_key")

    def available(self) -> str:
        return "" if self.key() else NO_KEY

    def voice(self, settings: "Settings") -> str:
        return settings.tts.elevenlabs_voice_id.strip() or DEFAULT_VOICE

    def russian(self, voice: str) -> bool:
        return False  # латиницу ElevenLabs читает сам

    def cache_key(self, settings: "Settings") -> str:
        tts = settings.tts
        model = tts.elevenlabs_model.strip() or DEFAULT_MODEL
        knobs = (tts.elevenlabs_stability, tts.elevenlabs_similarity, tts.elevenlabs_style)
        return model + "|" + "|".join(f"{v:.2f}" for v in knobs)

    def _client(self, settings: "Settings", timeout: float) -> httpx.AsyncClient:
        proxy = settings.ai.proxy.strip() or None
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            proxy=None if self.transport is not None else proxy,
            transport=self.transport,
        )

    async def synthesize(self, text: str, opts: SynthOptions) -> tuple[np.ndarray, int]:
        key = self.key()
        if not key:
            raise TtsEngineError(NO_KEY)
        settings = opts.settings or self.ctx.settings.value
        model = settings.tts.elevenlabs_model.strip() or DEFAULT_MODEL
        voice = (opts.voice or settings.tts.elevenlabs_voice_id).strip() or DEFAULT_VOICE
        pitch = clamp(opts.pitch, 0.8, 1.2)
        body = {
            "text": text,
            "model_id": model,
            "voice_settings": voice_settings(settings, model, opts.speed / pitch),
        }
        try:
            async with self._client(settings, 45.0) as client:
                resp = await client.post(
                    f"{API}/v1/text-to-speech/{quote(voice, safe='')}",
                    params={"output_format": OUTPUT_FORMAT},
                    json=body,
                    headers={"xi-api-key": key},
                )
                if resp.status_code != 200:
                    error = eleven_error(resp)
                    if isinstance(error, QuotaError):
                        error.cooldown_s = await self._until_reset(client, key) or QUOTA_COOLDOWN_S
                    raise error
        except httpx.TimeoutException as e:
            raise TtsEngineError("сервис не ответил вовремя") from e
        except httpx.HTTPError as e:
            raise TtsEngineError(f"нет связи с ElevenLabs: {e}") from e
        if "json" in resp.headers.get("content-type", ""):
            raise TtsEngineError("сервис вместо звука прислал ответ без голоса")
        samples = pcm16_to_float(resp.content)
        if not len(samples):
            raise TtsEngineError("сервис не вернул звук")
        return samples, max(8000, round(PCM_RATE * pitch))

    async def _until_reset(self, client: httpx.AsyncClient, key: str) -> float | None:
        """Через сколько секунд обновятся кредиты (по подписке); не узнали — None."""
        try:
            resp = await client.get(f"{API}/v1/user/subscription", headers={"xi-api-key": key}, timeout=8.0)
            reset = resp.json().get("next_character_count_reset_unix") if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        if not isinstance(reset, int | float) or reset <= 0:
            return None
        return clamp(reset - time.time(), 60.0, MAX_QUOTA_COOLDOWN_S)

    async def voices(self) -> list[dict[str, Any]]:
        settings = self.ctx.settings.value
        configured = settings.tts.elevenlabs_voice_id.strip()
        items: list[dict[str, Any]] = []
        key = self.key()
        if key:
            try:
                async with self._client(settings, 15.0) as client:
                    resp = await client.get(
                        f"{API}/v2/voices", params={"page_size": "100"}, headers={"xi-api-key": key}
                    )
                if resp.status_code == 200:
                    raw = resp.json().get("voices") or []
                    items = [v for v in (_voice_item(r) for r in raw if isinstance(r, dict)) if v]
                else:
                    log.info("голос: список голосов ElevenLabs: %s", eleven_error(resp, "Voices (Read)"))
            except (httpx.HTTPError, ValueError, AttributeError) as e:
                log.info("голос: список голосов ElevenLabs не получен: %s", e)
        # сначала стандартные голоса (есть и на бесплатном тарифе), потом свои
        items.sort(key=lambda v: (v["category"] not in ("premade", "high_quality", ""), v["name"].casefold()))
        seen = {v["id"] for v in items}
        if configured and configured not in seen:
            items.insert(0, {"id": configured, "name": f"Голос {configured[:8]}…", "lang": "", "gender": ""})
        return items

    async def account(self) -> dict[str, Any]:
        """Остаток кредитов и модели, говорящие по-русски: для настроек голоса."""
        out: dict[str, Any] = {
            "supported": True,
            "tier": "",
            "status": "",
            "used": None,
            "limit": None,
            "reset_at": None,
            "models": [],
            "error": "",
        }
        key = self.key()
        if not key:
            out["error"] = NO_KEY
            return out
        headers = {"xi-api-key": key}
        try:
            async with self._client(self.ctx.settings.value, 15.0) as client:
                sub, models = await asyncio.gather(
                    client.get(f"{API}/v1/user/subscription", headers=headers),
                    client.get(f"{API}/v1/models", headers=headers),
                )
        except httpx.TimeoutException:
            out["error"] = "сервис не ответил вовремя"
            return out
        except httpx.HTTPError as e:
            out["error"] = f"нет связи с ElevenLabs: {e}"
            return out
        if sub.status_code == 200:
            try:
                data = sub.json()
                out["tier"] = str(data.get("tier") or "")
                out["status"] = str(data.get("status") or "")
                out["used"] = data.get("character_count")
                out["limit"] = data.get("character_limit")
                reset = data.get("next_character_count_reset_unix")
                if isinstance(reset, int | float) and reset > 0:
                    out["reset_at"] = datetime.fromtimestamp(reset, tz=UTC).isoformat()
            except (ValueError, AttributeError):
                out["error"] = "непонятный ответ сервиса о подписке"
        else:
            out["error"] = str(eleven_error(sub, "User (Read)"))
        if models.status_code == 200:
            try:
                raw = models.json()
            except ValueError:
                raw = []
            for model in raw if isinstance(raw, list) else []:
                if not isinstance(model, dict) or not model.get("model_id"):
                    continue
                if model.get("can_do_text_to_speech") is False or not _russian(model.get("languages")):
                    continue
                item: dict[str, Any] = {"id": str(model["model_id"]), "name": str(model.get("name") or "")}
                rates = model.get("model_rates")
                cost = rates.get("character_cost_multiplier") if isinstance(rates, dict) else None
                if isinstance(cost, int | float):
                    item["cost"] = cost  # кредитов за символ: у flash — 0,5
                out["models"].append(item)
        return out

    def close(self) -> None:
        return None
