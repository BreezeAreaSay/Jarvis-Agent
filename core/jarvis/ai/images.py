"""Картинки Jarvis AI: рисует по описанию через OpenAI Images (gpt-image-1) по ключу человека; своё фото —
образцом (POST /images/edits). Инструмент чата generate_image («нарисуй…») и POST /api/images/generate.

Ключ — секрет openai_api_key, а если ИИ работает через OpenAI — и ai_api_key (как у голосов OpenAI). Адрес —
settings.ai.base_url, если провайдер ИИ — OpenAI (свой адрес-посредник), иначе https://api.openai.com/v1;
прокси — settings.ai.proxy. Подписка ChatGPT (Codex) и другие провайдеры картинки через API не рисуют — нужен
ключ OpenAI, и ошибка говорит, где его задать.

Запрос: POST {base}/images/generations {model, prompt, n: 1, size, quality, output_format: jpeg,
output_compression: 90}; с образцом — POST {base}/images/edits (multipart: image или image[] — до 4 картинок,
input_fidelity: high — лица и детали ближе к образцу). Ответ — data[0].b64_json и usage (токены). Картинка
приходит JPEG и хранится data URL в meta.images итогового ответа ассистента (meta.generated — что просили
нарисовать), как картинки человека в его сообщениях: удаляется вместе с чатом и в «Приватности» отдельно
(категория «Нарисованные картинки», jarvis.retention).

Как картинка попадает в ответ: инструмент вызывает Ai._call_tool, и перед вызовом ядро шлёт chat.tool
{status: running, message_id} — по нему служба знает id будущего ответа. Готовая картинка сразу уходит
событием chat.image {chat_id, message_id, image, info} (интерфейс показывает её, пока модель дописывает
текст), а когда ответ сохранён (chat.message ассистента с этим id), служба дописывает картинки в его meta и
рассылает chat.message ещё раз (следом за исходным, после chat.done).

Расход: строка в таблице usage (provider openai, модель gpt-image-1, токены из ответа; цена — по токенам,
без них — по таблице качества и размера); месячный лимит settings.ai.monthly_budget проверяется до запроса.
"""

import asyncio
import base64
import binascii
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from jarvis.ai.format import one_line
from jarvis.ai.llm import month_start
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.ai.tools import ToolEnv, ToolOutput
    from jarvis.bus import Event
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.openai.com/v1"
MODEL = os.environ.get("JARVIS_IMAGE_MODEL", "").strip() or "gpt-image-1"
TOOL_NAME = "generate_image"
SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
QUALITIES = ("low", "medium", "high")
OUTPUT_FORMAT = "jpeg"  # в 5–8 раз меньше PNG: картинка живёт в базе и ходит по WebSocket
COMPRESSION = 90
MAX_REFERENCES = 4
MAX_PROMPT = 4000
TIMEOUT = httpx.Timeout(180.0, connect=15.0)  # картинка рисуется 20–60 с, высокое качество — дольше
PENDING_TTL_S = 900.0  # картинка ждёт сохранения ответа не дольше 15 минут

# цена gpt-image-1, $ за 1 млн токенов: текст на входе, картинки на входе, картинка на выходе
PRICE_TEXT_IN = 5.0
PRICE_IMAGE_IN = 10.0
PRICE_IMAGE_OUT = 40.0
# токены картинки на выходе, если ответ без usage: качество → (квадрат, широкая или высокая)
OUTPUT_TOKENS = {"low": (272, 408), "medium": (1056, 1584), "high": (4160, 6240)}

NO_KEY = (
    "Чтобы рисовать, нужен ключ OpenAI API — добавьте его в «Настройки → Jarvis AI → Рисование картинок» "
    "(или выберите провайдером OpenAI). Подписка ChatGPT картинки через API не рисует"
)
_DATA_URL = re.compile(r"^data:(image/(?:png|jpe?g|webp|gif));base64,(.+)$", re.I | re.S)


class ImageError(UserFacingError):
    """Картинку нарисовать не получилось; текст — для человека."""


@dataclass
class _Pending:
    chat_id: str
    message_id: str  # "" — id ответа неизвестен: первый же итог ассистента в этом чате
    images: list[str] = field(default_factory=list)
    info: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)


def decode_data_url(url: str) -> tuple[bytes, str]:
    """data:image/png;base64,… → (байты, mime)."""
    m = _DATA_URL.match((url or "").strip())
    if not m:
        raise ImageError("Образец должен быть картинкой PNG, JPEG, WebP или GIF")
    try:
        data = base64.b64decode(m.group(2), validate=False)
    except (binascii.Error, ValueError) as e:
        raise ImageError("Образец повреждён — приложите картинку ещё раз") from e
    mime = m.group(1).lower().replace("image/jpg", "image/jpeg")
    return data, mime


def estimate_cost(usage: dict[str, Any] | None, quality: str, size: str) -> tuple[int, int, float]:
    """(токены на входе, токены на выходе, цена $) — по usage ответа или по таблице качества и размера."""
    if isinstance(usage, dict) and (usage.get("input_tokens") or usage.get("output_tokens")):
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        details = usage.get("input_tokens_details") or {}
        image_in = int(details.get("image_tokens") or 0) if isinstance(details, dict) else 0
        text_in = max(0, tokens_in - image_in)
        cost = (text_in * PRICE_TEXT_IN + image_in * PRICE_IMAGE_IN + tokens_out * PRICE_IMAGE_OUT) / 1e6
        return tokens_in, tokens_out, round(cost, 6)
    square, wide = OUTPUT_TOKENS.get(quality, OUTPUT_TOKENS["medium"])
    tokens_out = square if size in ("1024x1024", "auto", "") else wide
    return 0, tokens_out, round(tokens_out * PRICE_IMAGE_OUT / 1e6, 6)


def openai_image_error(resp: httpx.Response) -> str:
    """Ответ OpenAI с ошибкой → понятный текст по-русски."""
    detail, code = "", ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
            code = str(err.get("code") or err.get("type") or "")
        elif isinstance(err, str):
            detail = err
    except ValueError:
        detail = resp.text[:200]
    low = f"{code} {detail}".lower()
    status = resp.status_code
    if "moderation" in low or "safety" in low or "content_policy" in low:
        return "OpenAI не стал это рисовать: запрос не прошёл проверку безопасности — опишите иначе"
    if "verif" in low:
        return (
            "OpenAI: чтобы рисовать моделью gpt-image-1, подтвердите организацию (Verify Organization) "
            "на platform.openai.com → Settings → Organization"
        )
    if status == 401:
        return "OpenAI: неверный ключ API — проверьте его в настройках"
    if status == 403:
        return "OpenAI недоступен из вашего региона — укажите прокси в настройках Jarvis AI"
    if status == 429:
        return "OpenAI: превышен лимит запросов или на счёте нет денег"
    if status == 404:
        return f"OpenAI: модель {MODEL} не найдена по этому адресу ({one_line(detail, 120) or status})"
    if status >= 500:
        return "OpenAI сейчас не отвечает — попробуйте через минуту"
    return f"OpenAI ответил {status}: {one_line(detail, 200)}" if detail else f"OpenAI ответил {status}"


class Images(BaseService):
    def __init__(self, ctx: "AppContext", transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(ctx)
        self.transport = transport  # тесты подставляют httpx.MockTransport
        self.clock: Callable[[], float] = time.monotonic
        self._replies: dict[str, str] = {}  # чат → id ответа, в котором сейчас рисуется картинка
        self._pending: list[_Pending] = []
        self._offs = [ctx.bus.on("chat.tool", self._on_tool), ctx.bus.on("chat.message", self._on_message)]

    async def stop(self) -> None:
        for off in self._offs:
            off()
        self._offs = []

    # --- ключ и адрес -----------------------------------------------------------------------------
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

    def status(self) -> dict[str, Any]:
        """{available, model, problem} — для интерфейса («Рисование картинок» в настройках)."""
        problem = "" if self.key() else NO_KEY
        return {"available": not problem, "model": MODEL, "problem": problem}

    def _check_budget(self) -> None:
        budget = self.ctx.settings.value.ai.monthly_budget
        if budget <= 0:
            return
        row = self.ctx.db.query_one(
            "SELECT COALESCE(SUM(cost), 0) AS cost FROM usage WHERE ts >= ?", (month_start(),)
        )
        spent = float(row["cost"]) if row else 0.0
        if spent >= budget:
            raise ImageError(
                f"Достигнут месячный лимит расходов на ИИ (${spent:.2f} из ${budget:.2f}) — "
                "увеличьте его в настройках Jarvis AI"
            )

    def _record(self, tokens_in: int, tokens_out: int, cost: float) -> None:
        try:
            self.ctx.db.insert(
                "usage",
                {
                    "ts": time.time(),
                    "provider": "openai",
                    "model": MODEL,
                    "prompt_tokens": tokens_in,
                    "completion_tokens": tokens_out,
                    "cost": cost,
                },
            )
        except Exception:
            log.exception("расход на картинку не записан")

    # --- рисование --------------------------------------------------------------------------------
    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        quality: str = "medium",
        references: list[str] | None = None,
    ) -> dict[str, Any]:
        """Нарисовать картинку: {image: data URL, info: {prompt, size, model, quality, cost}}."""
        prompt = " ".join((prompt or "").split())[:MAX_PROMPT]
        if not prompt:
            raise ImageError("Не описано, что нарисовать")
        key = self.key()
        if not key:
            raise ImageError(NO_KEY)
        size = size if size in SIZES.values() or size == "auto" else "1024x1024"
        quality = quality if quality in QUALITIES else "medium"
        refs = [decode_data_url(u) for u in (references or [])[:MAX_REFERENCES]]
        self._check_budget()

        params: dict[str, Any] = {
            "model": MODEL,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "output_format": OUTPUT_FORMAT,
            "output_compression": COMPRESSION,
        }
        proxy = self.ctx.settings.value.ai.proxy.strip() or None
        headers = {"Authorization": f"Bearer {key}"}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                proxy=None if self.transport is not None else proxy,
                transport=self.transport,
            ) as client:
                if refs:
                    if MODEL in ("gpt-image-1", "gpt-image-1.5"):
                        params["input_fidelity"] = "high"  # лицо и детали — как на фото
                    field_name = "image" if len(refs) == 1 else "image[]"
                    files = [
                        (field_name, (f"reference-{i + 1}.{mime.split('/')[1]}", data, mime))
                        for i, (data, mime) in enumerate(refs)
                    ]
                    resp = await client.post(
                        f"{self.base_url()}/images/edits",
                        data={k: str(v) for k, v in params.items()},
                        files=files,
                        headers=headers,
                    )
                else:
                    resp = await client.post(
                        f"{self.base_url()}/images/generations", json=params, headers=headers
                    )
        except httpx.TimeoutException as e:
            raise ImageError("OpenAI рисовал слишком долго — попробуйте ещё раз или попроще") from e
        except httpx.HTTPError as e:
            raise ImageError(f"Нет связи с OpenAI: {e}") from e
        if resp.status_code != 200:
            raise ImageError(openai_image_error(resp))
        try:
            body = resp.json()
            item = (body.get("data") or [])[0]
            b64 = str(item.get("b64_json") or "")
        except (ValueError, IndexError, AttributeError, TypeError) as e:
            raise ImageError("OpenAI прислал непонятный ответ — картинки в нём нет") from e
        if not b64:
            raise ImageError("OpenAI не прислал картинку")
        fmt = str(body.get("output_format") or OUTPUT_FORMAT).lower()
        tokens_in, tokens_out, cost = estimate_cost(body.get("usage"), quality, size)
        self._record(tokens_in, tokens_out, cost)
        log.info(
            "картинка нарисована за %.1f с (%s, %s, %s, образцов %d, $%.3f)",
            time.monotonic() - started,
            MODEL,
            size,
            quality,
            len(refs),
            cost,
        )
        info = {"prompt": prompt, "size": str(body.get("size") or size), "model": MODEL, "quality": quality}
        return {"image": f"data:image/{fmt};base64,{b64}", "info": info, "cost": cost}

    # --- инструмент чата --------------------------------------------------------------------------
    def references_for(self, chat_id: str) -> list[str]:
        """Картинки из последнего сообщения человека с вложениями — образец для «нарисуй по фото»."""
        chats = getattr(self.ctx.ai, "chats", None)
        if not chat_id or chats is None:
            return []
        for row in reversed(chats.rows(chat_id)):
            images = row["meta"].get("images") if row["role"] == "user" else None
            if images:
                return [str(u) for u in images if isinstance(u, str)][:MAX_REFERENCES]
        return []

    async def tool(self, env: "ToolEnv", args: dict[str, Any]) -> "ToolOutput":
        """Инструмент generate_image: нарисовать, показать в чате, ответить модели коротко."""
        from jarvis.ai.tools import ToolOutput

        prompt = " ".join(str(args.get("prompt") or "").split())
        title = f"Нарисовать: {one_line(prompt, 60)}" if prompt else "Нарисовать"
        size = SIZES.get(str(args.get("format") or "square"), "1024x1024")
        quality = str(args.get("quality") or "medium")
        refs = self.references_for(env.chat_id) if args.get("use_reference") else []
        if args.get("use_reference") and not refs:
            return ToolOutput.fail(
                "к сообщениям нет картинки-образца — попроси пользователя приложить фото (кнопка «+» у поля)",
                title=title,
            )
        result = await self.generate(prompt, size=size, quality=quality, references=refs)
        self.deliver(env.chat_id, result["image"], result["info"])
        return ToolOutput(
            "Картинка нарисована и уже показана пользователю в чате с кнопкой «Скачать». Не вставляй ссылку "
            "и не описывай картинку подробно — одной короткой фразой скажи, что готово"
            + (" (по его фото)" if refs else "")
            + ".",
            title=title,
            detail="Готово",
        )

    def deliver(self, chat_id: str, image: str, info: dict[str, Any]) -> None:
        """Показать картинку сразу (chat.image) и дописать её в итог ответа, когда он сохранится."""
        message_id = self._replies.get(chat_id, "")
        self.ctx.bus.publish(
            "chat.image", {"chat_id": chat_id, "message_id": message_id, "image": image, "info": info}
        )
        if not chat_id:
            return
        now = self.clock()
        self._pending = [p for p in self._pending if now - p.created < PENDING_TTL_S]
        entry = next((p for p in self._pending if p.chat_id == chat_id and p.message_id == message_id), None)
        if entry is None:
            entry = _Pending(chat_id, message_id, created=now)
            self._pending.append(entry)
        entry.images.append(image)
        entry.info.append(info)

    # --- события чата -----------------------------------------------------------------------------
    def _on_tool(self, event: "Event") -> None:
        data = event.data
        if data.get("name") == TOOL_NAME and data.get("status") == "running" and data.get("chat_id"):
            self._replies[str(data["chat_id"])] = str(data.get("message_id") or "")

    def _on_message(self, event: "Event") -> None:
        chat_id = str(event.data.get("chat_id") or "")
        message = event.data.get("message") or {}
        if not chat_id or message.get("role") != "assistant" or not self._pending:
            return
        message_id = str(message.get("id") or "")
        mine = [p for p in self._pending if p.chat_id == chat_id and p.message_id in (message_id, "")]
        if not mine:
            return
        self._pending = [p for p in self._pending if p not in mine]
        if self._replies.get(chat_id) == message_id:
            self._replies.pop(chat_id, None)
        self._attach(chat_id, message_id, mine)

    def _attach(self, chat_id: str, message_id: str, entries: list[_Pending]) -> None:
        from jarvis.ai.chats import ui_message

        chats = getattr(self.ctx.ai, "chats", None)
        row = chats.message(chat_id, message_id) if chats is not None else None
        if row is None:
            log.warning("картинка не прикреплена: ответа %s в чате %s нет", message_id, chat_id)
            return
        meta = dict(row["meta"])
        images = list(meta.get("images") or [])
        info = list(meta.get("generated") or [])
        for entry in entries:
            images.extend(entry.images)
            info.extend(entry.info)
        meta["images"] = images
        meta["generated"] = info
        updated = chats.set_meta(chat_id, message_id, meta)
        if updated is None:
            return
        data = {"chat_id": chat_id, "message": ui_message(updated)}
        # повтор chat.message — после того, как исходное событие дошло до всех подписчиков
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.ctx.bus.publish("chat.message", data)
        else:
            loop.call_soon(self.ctx.bus.publish, "chat.message", data)
