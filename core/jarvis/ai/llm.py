"""Клиент OpenAI-совместимого API /chat/completions для всех провайдеров Jarvis AI.

Умеет: потоковые ответы (SSE) — куски текста и вызовы инструментов собираются по индексу; обычные ответы;
картинки в сообщениях (data URL); ответ строго в JSON (response_format json_schema, а если провайдер его не
понимает — просьба в промпте и извлечение JSON из текста); таймауты, повторы при 429/5xx и обрывах связи,
прокси (http/socks5) из настроек, учёт токенов в таблице usage и месячный лимит расходов.

Провайдеры отличаются мелочами (temperature у «думающих» моделей, max_completion_tokens у OpenAI,
stream_options): известное описано в jarvis.ai.providers, остальное клиент узнаёт по ответу 400 и повторяет
запрос без лишнего параметра. Ошибки для человека — LlmError (наследник UserFacingError) с текстом по-русски.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from jarvis import __version__
from jarvis.ai.providers import PRESETS, ProviderPreset, estimate_cost, get_preset, price_for
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext
    from jarvis.settings import AiSettings, Secrets

log = logging.getLogger(__name__)

NO_KEY = "Укажите ключ API в настройках Jarvis AI"
BAD_KEY = "Ключ API не подходит"
REGION = "Провайдер недоступен из вашего региона — включите VPN или укажите прокси"
RATE_LIMIT = "Лимит запросов исчерпан — подождите немного или проверьте тариф у провайдера"
NO_CREDIT = "Закончился баланс у провайдера ИИ — пополните счёт или выберите другого провайдера"
NETWORK = "Нет связи с провайдером ИИ"
TIMEOUT = "Провайдер ИИ не ответил вовремя — попробуйте ещё раз"
DISABLED = "Jarvis AI выключен в настройках"
NO_URL = "Укажите адрес сервера ИИ в настройках Jarvis AI"
NO_MODEL = "Укажите модель в настройках Jarvis AI"
BAD_FORMAT = "Модель ответила не в нужном формате — попробуйте ещё раз или выберите другую модель"
TOO_LONG = "Разговор слишком длинный для модели — начните новый чат"
BAD_PROXY = "Неверный адрес прокси в настройках Jarvis AI"

# модели не для чата в списке /models
_NOT_CHAT = (
    "embed",
    "whisper",
    "tts",
    "dall-e",
    "moderation",
    "transcribe",
    "realtime",
    "audio",
    "image",
    "davinci",
    "babbage",
    "sora",
    "rerank",
    "guard",
)


class LlmError(UserFacingError):
    """Запрос к ИИ не удался; текст — для человека."""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class LlmFormatError(LlmError):
    """Модель не вернула JSON, который просили."""


# --- конфигурация ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    preset: ProviderPreset
    base_url: str  # без «/» в конце
    api_key: str
    model: str
    temperature: float
    proxy: str
    vision: bool
    enabled: bool
    local: bool  # сервер на этом ПК или в домашней сети: без ключа, без прокси, бесплатно

    @property
    def chat_url(self) -> str:
        return self.base_url + "/chat/completions"

    def problem(self) -> str | None:
        """Почему запрос сделать нельзя (текст для человека) или None."""
        if not self.enabled:
            return DISABLED
        if not self.base_url:
            return NO_URL
        if self.preset.needs_key and not self.api_key:
            return NO_KEY
        if not self.model:
            return NO_MODEL
        return None


def _is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def resolve_config(settings: "AiSettings", secrets: "Secrets", model: str | None = None) -> LlmConfig:
    """Настройки + секреты → параметры запроса: адрес, ключ, модель, прокси."""
    preset = get_preset(settings.provider)
    base = (settings.base_url.strip() or preset.base_url).strip().rstrip("/")
    key = secrets.get("ai_api_key")
    if not key and preset.id == "openai":
        key = secrets.get("openai_api_key")  # ключ OpenAI, заданный для голоса
    chosen = (model or "").strip() or settings.model.strip()
    if not (model or "").strip() and preset.default_model and chosen not in preset.models:
        # сменили провайдера, а модель осталась от прежнего (по умолчанию) — берём модель нового провайдера
        other_defaults = {p.default_model for p in PRESETS.values() if p.id != preset.id}
        if not chosen or chosen in other_defaults:
            chosen = preset.default_model
    local = preset.local or _is_local_url(base)
    return LlmConfig(
        provider=preset.id,
        preset=preset,
        base_url=base,
        api_key=key,
        model=chosen,
        temperature=settings.temperature,
        proxy="" if local else settings.proxy.strip(),
        vision=settings.vision,
        enabled=settings.enabled,
        local=local,
    )


# --- результат ---------------------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"  # JSON-строка, как её прислала модель

    def parse_arguments(self) -> dict[str, Any]:
        raw = (self.arguments or "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            value = extract_json(raw)
            if value is None:
                raise ValueError("аргументы инструмента — не JSON") from None
        if not isinstance(value, dict):
            raise ValueError("аргументы инструмента должны быть объектом JSON")
        return value

    def to_message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None  # $; None — посчитать по таблице цен
    estimated: bool = False  # провайдер не сообщил расход, токены оценены по длине текста

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost = (self.cost or 0.0) + (other.cost or 0.0)

    def to_json(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost": round(self.cost or 0.0, 6),
        }


@dataclass
class LlmResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    parsed: Any = None  # ответ по json_schema


def _call_id() -> str:
    return "call_" + uuid.uuid4().hex[:20]


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()

    def num(*keys: str) -> int:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, int | float):
                return int(value)
        return 0

    cost = raw.get("cost")  # OpenRouter сообщает стоимость сам
    return Usage(
        prompt_tokens=num("prompt_tokens", "input_tokens"),
        completion_tokens=num("completion_tokens", "output_tokens"),
        cost=float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None,
    )


def _content_text(content: Any) -> str:
    """content сообщения: строка или список частей [{type: text, text}]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
            elif isinstance(part, str):
                out.append(part)
        return "".join(out)
    return ""


# --- рассуждения <think> ------------------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
_OPEN, _CLOSE = "<think>", "</think>"


def strip_think(text: str) -> str:
    """Убрать рассуждения вида <think>…</think> (Qwen3, DeepSeek-R1 в Ollama и LM Studio)."""
    if "<think>" not in text and "</think>" not in text:
        return text
    out = _THINK_BLOCK.sub("", text)
    if "<think>" in out:  # ответ оборвался на рассуждении
        out = out.split("<think>", 1)[0]
    if "</think>" in out:  # открывающий тег был в шаблоне промпта
        out = out.rsplit("</think>", 1)[1]
    return out.lstrip()


def _partial_suffix(text: str, tag: str) -> int:
    """Сколько последних символов text могут быть началом tag (тег разрезан между кусками потока)."""
    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


class ThinkFilter:
    """Потоковый фильтр: не пропускает в интерфейс текст между <think> и </think>."""

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False
        self._after_close = False  # пробелы сразу после </think> не показываем

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while self._buf:
            if self._inside:
                index = self._buf.find(_CLOSE)
                if index < 0:
                    keep = _partial_suffix(self._buf, _CLOSE)
                    self._buf = self._buf[len(self._buf) - keep :] if keep else ""
                    break
                self._buf = self._buf[index + len(_CLOSE) :]
                self._inside = False
                self._after_close = True
            else:
                index = self._buf.find(_OPEN)
                if index < 0:
                    keep = _partial_suffix(self._buf, _OPEN)
                    out.append(self._buf[: len(self._buf) - keep])
                    self._buf = self._buf[len(self._buf) - keep :]
                    break
                out.append(self._buf[:index])
                self._buf = self._buf[index + len(_OPEN) :]
                self._inside = True
        visible = "".join(out)
        if self._after_close and visible:
            visible = visible.lstrip()
            if visible:
                self._after_close = False
        return visible

    def flush(self) -> str:
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


# --- поток SSE --------------------------------------------------------------------------------------


class SseDecoder:
    """Разбор text/event-stream по строкам: отдаёт данные (поле data:) каждого события.

    Обычно событие — одна строка «data: {json}» и пустая строка после неё; законченный JSON отдаётся сразу,
    не дожидаясь пустой строки (некоторые серверы её не шлют). Комментарии «: …» пропускаются.
    """

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> str | None:
        line = line.rstrip("\r\n")
        if not line:
            return self.flush()
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if not sep:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if name != "data":
            return None
        if not self._data and (value == "[DONE]" or (value.startswith("{") and value.endswith("}"))):
            return value
        self._data.append(value)
        return None

    def flush(self) -> str | None:
        if not self._data:
            return None
        data = "\n".join(self._data)
        self._data = []
        return data


@dataclass
class _ToolDraft:
    id: str = ""
    name: str = ""
    arguments: str = ""


class StreamAccumulator:
    """Собирает ответ из кусков потока: текст, вызовы инструментов (по index), причину остановки, расход."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.calls: dict[int, _ToolDraft] = {}
        self.finish_reason = ""
        self.usage: Usage | None = None
        self.model = ""

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def add(self, chunk: dict[str, Any]) -> str:
        """Учесть кусок; вернуть новый текст ответа (может быть пустым)."""
        if isinstance(chunk.get("model"), str) and chunk["model"]:
            self.model = chunk["model"]
        raw_usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
        if isinstance(raw_usage, dict) and raw_usage:
            self.usage = _usage(raw_usage)
        out: list[str] = []
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) not in (0, None):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            text = _content_text(delta.get("content"))
            if text:
                out.append(text)
            for position, call in enumerate(delta.get("tool_calls") or []):
                if isinstance(call, dict):
                    self._add_call(position, call)
            legacy = delta.get("function_call")
            if isinstance(legacy, dict):
                self._add_call(0, {"index": 0, "function": legacy})
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
        text = "".join(out)
        if text:
            self.parts.append(text)
        return text

    def _add_call(self, position: int, call: dict[str, Any]) -> None:
        index = call.get("index")
        call_id = str(call.get("id") or "")
        if not isinstance(index, int):
            # без index (бывает у совместимых серверов): по id, иначе продолжение последнего вызова
            found = next((i for i, d in self.calls.items() if call_id and d.id == call_id), None)
            if found is not None:
                index = found
            elif call_id or not self.calls:
                index = max(self.calls, default=-1) + 1
            else:
                index = max(self.calls)
        draft = self.calls.setdefault(index, _ToolDraft())
        if call_id and not draft.id:
            draft.id = call_id
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name")
        if isinstance(name, str) and name:
            if not draft.name:
                draft.name = name
            elif name != draft.name:
                # имя приходит целиком в первом куске; если сервер шлёт его частями — склеиваем
                draft.name = name if name.startswith(draft.name) else draft.name + name
        args = function.get("arguments")
        if isinstance(args, str):
            draft.arguments += args
        elif isinstance(args, dict | list):
            draft.arguments = json.dumps(args, ensure_ascii=False)

    def result(self, model: str) -> LlmResponse:
        calls = [
            ToolCall(d.id or _call_id(), d.name, d.arguments or "{}")
            for _, d in sorted(self.calls.items())
            if d.name
        ]
        return LlmResponse(
            content=strip_think(self.text).strip(),
            tool_calls=calls,
            finish_reason=self.finish_reason,
            usage=self.usage or Usage(),
            model=self.model or model,
        )


def _json_chunks(payload: str) -> Iterator[dict[str, Any]]:
    try:
        value = json.loads(payload)
    except ValueError:
        # несколько JSON в одном событии (сервер не разделил события пустой строкой)
        for line in payload.splitlines():
            line = line.strip()
            if not line or line == "[DONE]":
                continue
            try:
                value = json.loads(line)
            except ValueError:
                log.debug("кусок потока не JSON: %r", line[:200])
                continue
            if isinstance(value, dict):
                yield value
        return
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        yield from (v for v in value if isinstance(v, dict))


def _parse_completion(data: Any, model: str) -> LlmResponse:
    if not isinstance(data, dict):
        raise _ApiError(502, "неожиданный ответ провайдера")
    if data.get("error") and not data.get("choices"):
        raise _ApiError.from_payload(data)
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        args = function.get("arguments")
        if isinstance(args, dict | list):
            args = json.dumps(args, ensure_ascii=False)
        name = str(function.get("name") or "")
        if name:
            calls.append(ToolCall(str(call.get("id") or _call_id()), name, str(args or "{}")))
    legacy = message.get("function_call")
    if not calls and isinstance(legacy, dict) and legacy.get("name"):
        calls.append(ToolCall(_call_id(), str(legacy["name"]), str(legacy.get("arguments") or "{}")))
    return LlmResponse(
        content=strip_think(_content_text(message.get("content"))).strip(),
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason") or ""),
        usage=_usage(data.get("usage")),
        model=str(data.get("model") or model),
    )


# --- JSON в тексте -------------------------------------------------------------------------------------


def _balanced_blocks(text: str, open_ch: str, close_ch: str, limit: int = 5) -> Iterator[str]:
    """Сбалансированные блоки {…} или […] в тексте (с учётом строк и экранирования)."""
    start = text.find(open_ch)
    tried = 0
    while start != -1 and tried < limit:
        tried += 1
        depth, in_str, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break
        start = text.find(open_ch, start + 1)


def extract_json(text: str) -> Any:
    """JSON из ответа модели: весь текст, блок ```json```, первый объект {…} или массив […]. None — нет."""
    t = strip_think(text or "").strip()
    if not t:
        return None
    candidates: list[str] = [t]
    candidates += [m.group(1).strip() for m in re.finditer(r"```[a-zA-Z]*\s*\n?(.*?)```", t, re.S)]
    candidates += list(_balanced_blocks(t, "{", "}"))
    candidates += list(_balanced_blocks(t, "[", "]"))
    for candidate in candidates:
        with contextlib.suppress(ValueError):
            return json.loads(candidate)
    return None


def _schema_body(schema: dict[str, Any]) -> dict[str, Any]:
    inner = schema.get("schema")
    return inner if isinstance(inner, dict) else schema


def _schema_spec(schema: dict[str, Any]) -> dict[str, Any]:
    """json_schema для response_format: {name, schema, strict}."""
    if isinstance(schema.get("schema"), dict):
        spec = dict(schema)
        spec.setdefault("name", "answer")
        return spec
    return {"name": "answer", "schema": schema, "strict": False}


def _with_json_instruction(messages: list[dict[str, Any]], schema: dict[str, Any]) -> list[dict[str, Any]]:
    body = json.dumps(_schema_body(schema), ensure_ascii=False)
    instruction = (
        "Ответь только одним JSON-объектом — без пояснений, без markdown и без ```. "
        f"JSON должен соответствовать схеме: {body}"
    )
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        out[0]["content"] = out[0]["content"] + "\n\n" + instruction
    else:
        out.insert(0, {"role": "system", "content": instruction})
    return out


def _parse_json_answer(text: str, schema: dict[str, Any]) -> Any:
    value = extract_json(text)
    if value is None:
        return None
    if _schema_body(schema).get("type", "object") == "object" and not isinstance(value, dict):
        return None
    return value


# --- ошибки --------------------------------------------------------------------------------------------


class _ApiError(Exception):
    """Ответ провайдера с ошибкой (до перевода в текст для человека)."""

    def __init__(self, status: int, message: str, code: str = "", retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after

    @classmethod
    def from_payload(cls, data: dict[str, Any], status: int = 0) -> "_ApiError":
        message, code = _error_fields(data)
        if not status:
            raw = data.get("error")
            raw_code = raw.get("code") if isinstance(raw, dict) else None
            status = int(raw_code) if isinstance(raw_code, int) and 400 <= raw_code < 600 else 502
        return cls(status, message, code)

    @property
    def quota(self) -> bool:
        low = self.message.lower()
        return self.code == "insufficient_quota" or "current quota" in low or "billing" in low

    @property
    def retryable(self) -> bool:
        if self.status == 429:
            return not self.quota
        return self.status >= 500 or self.status in (408, 409)


def _error_fields(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    err = data.get("error")
    message: Any = ""
    code: Any = ""
    if isinstance(err, dict):
        message = err.get("message") or err.get("msg") or ""
        code = err.get("code") or err.get("type") or ""
    elif isinstance(err, str):
        message = err
    else:
        message = data.get("message") or data.get("detail") or ""
    if not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=False)
    return message.strip()[:500], str(code or "")


def _retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    with contextlib.suppress(TypeError, ValueError, IndexError):
        return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
    return None


def _api_error(status: int, body: bytes, headers: httpx.Headers) -> _ApiError:
    text = body.decode("utf-8", "replace")
    message, code = "", ""
    try:
        message, code = _error_fields(json.loads(text))
    except ValueError:
        message = re.sub(r"<[^>]+>", " ", text)  # HTML-страница ошибки (Cloudflare и т. п.)
        message = " ".join(message.split())[:300]
    return _ApiError(status, message, code, _retry_after(headers))


def _user_error(err: _ApiError, cfg: LlmConfig) -> LlmError:
    status, low, code = err.status, err.message.lower(), err.code.lower()
    region = any(w in low for w in ("region", "country", "territory", "location"))
    if status == 401:
        text = BAD_KEY
    elif status == 402:
        text = NO_CREDIT
    elif status in (403, 451):
        model_access = "model" in low and any(w in low for w in ("access", "permission", "not allowed"))
        text = (
            f"Нет доступа к модели «{cfg.model}» — выберите другую модель"
            if model_access and not region
            else REGION
        )
    elif status == 404:
        if cfg.provider == "ollama" and "model" in low:
            text = f"Модель «{cfg.model}» не скачана в Ollama — выполните команду «ollama pull {cfg.model}»"
        elif "model" in low or "model" in code:
            text = f"Модель «{cfg.model}» не найдена у провайдера — выберите другую в настройках Jarvis AI"
        else:
            text = "Адрес API провайдера не найден (404) — проверьте адрес в настройках Jarvis AI"
    elif status == 429:
        text = NO_CREDIT if err.quota else RATE_LIMIT
    elif status == 413 or "context_length" in code or "context length" in low or "maximum context" in low:
        text = TOO_LONG
    elif status >= 500:
        text = f"Провайдер ИИ сейчас не отвечает (ошибка {status}) — попробуйте позже"
    elif region and status == 400:
        text = REGION
    else:
        text = f"Провайдер ИИ отклонил запрос: {err.message or f'ошибка {status}'}"
    return LlmError(text, status=status, code=err.code)


def _network_error(e: Exception, cfg: LlmConfig) -> LlmError:
    if isinstance(e, httpx.TimeoutException) and not isinstance(e, httpx.ConnectTimeout | httpx.PoolTimeout):
        return LlmError(TIMEOUT)
    if isinstance(e, httpx.ProxyError):
        return LlmError(f"{NETWORK} — прокси не отвечает, проверьте его в настройках Jarvis AI")
    if cfg.local:
        name = cfg.preset.title.split(" (", 1)[0] if cfg.preset.local else "сервер ИИ"
        return LlmError(f"{NETWORK} — проверьте, что {name} запущен на этом компьютере")
    if cfg.proxy:
        return LlmError(f"{NETWORK} — проверьте прокси в настройках Jarvis AI")
    return LlmError(NETWORK)


_NO_TOOLS = re.compile(
    r"(does not|doesn't|do not) support tools|tools? (are|is) not supported|"
    r"tool[ _]?(use|calling|calls?|choice) (is |are )?not supported|unsupported.{0,20}tools?"
)
_TOOL_INDEX = re.compile(r"tools\[(\d+)\]")


def _quirk_for(message: str, body: dict[str, Any]) -> str | None:
    """Какой параметр убрать или заменить, судя по тексту ошибки 400 (провайдер его не понимает)."""
    m = message.lower()
    if "stream_options" in body and "stream_options" in m:
        return "no_stream_options"
    if "temperature" in body and "temperature" in m:
        return "no_temperature"
    if "max_tokens" in body and "max_completion_tokens" in m:
        return "use_max_completion_tokens"
    if "max_completion_tokens" in body and "max_completion_tokens" in m:
        return "use_max_tokens"
    if "response_format" in body and any(w in m for w in ("response_format", "json_schema", "json_object")):
        return "json_prompt"
    if "tool_choice" in body and "tool_choice" in m:
        return "no_tool_choice"
    if "tools" in body and _NO_TOOLS.search(m):
        return "no_tools"
    return None


def _is_reasoning_model(model: str) -> bool:
    """«Думающие» модели OpenAI (gpt-5*, o1/o3/o4…) не принимают temperature."""
    name = model.lower().rsplit("/", 1)[-1]
    return bool(re.match(r"(gpt-5|o\d)", name))


def _backoff(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, 20.0)
    return min(2.0**attempt, 8.0) + random.uniform(0.0, 0.4)


def _retryable_network(e: Exception, cfg: LlmConfig) -> bool:
    if isinstance(e, httpx.ProxyError):
        return False
    if cfg.local and isinstance(e, httpx.ConnectError):
        return False  # локальный сервер не запущен — повторять бессмысленно
    return isinstance(
        e,
        httpx.ConnectError
        | httpx.ConnectTimeout
        | httpx.ReadError
        | httpx.WriteError
        | httpx.RemoteProtocolError
        | httpx.PoolTimeout,
    )


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    total += 3000  # картинка — порядка тысячи токенов
                else:
                    total += len(str(part.get("text", "") if isinstance(part, dict) else part))
        else:
            total += len(str(content or ""))
        if message.get("tool_calls"):
            total += len(json.dumps(message["tool_calls"], ensure_ascii=False))
    return max(1, total // 4)


def month_start(ts: float | None = None) -> float:
    moment = datetime.fromtimestamp(ts if ts is not None else time.time())
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def user_content(text: str, images: list[str] | None, vision: bool) -> str | list[dict[str, Any]]:
    """Содержимое сообщения человека: текст и, если модель видит картинки, изображения (data URL)."""
    if not images:
        return text
    if not vision:
        note = (
            "(К сообщению приложены изображения, но выбранная модель их не видит — "
            "включите «Зрение» в настройках Jarvis AI.)"
        )
        return f"{text}\n\n{note}" if text else note
    parts: list[dict[str, Any]] = [{"type": "text", "text": text or "Что на изображении?"}]
    parts += [{"type": "image_url", "image_url": {"url": url}} for url in images]
    return parts


# --- клиент --------------------------------------------------------------------------------------------

DeltaFn = Callable[[str], None]


class LlmClient:
    """Запросы к модели выбранного провайдера. Настройки читаются при каждом запросе."""

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.transport: httpx.AsyncBaseTransport | None = None  # тесты подменяют сеть (httpx.MockTransport)
        self.sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
        self.max_retries = 2
        self._clients: dict[tuple[str, bool, int], httpx.AsyncClient] = {}
        self._quirks: dict[tuple[str, str], set[str]] = {}

    # --- настройки ------------------------------------------------------------------------------------
    def config(self, model: str | None = None) -> LlmConfig:
        return resolve_config(self.ctx.settings.value.ai, self.ctx.secrets, model)

    def problem(self, model: str | None = None) -> str | None:
        return self.config(model).problem()

    @property
    def configured(self) -> bool:
        try:
            return self.problem() is None
        except Exception:
            log.exception("настройки ИИ не читаются")
            return False

    def _client(self, cfg: LlmConfig) -> httpx.AsyncClient:
        key = (cfg.proxy, cfg.local, id(self.transport))
        client = self._clients.get(key)
        if client is not None and not client.is_closed:
            return client
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        elif cfg.proxy:
            kwargs["proxy"] = cfg.proxy
        if cfg.local:
            kwargs["trust_env"] = False  # системный прокси не для 127.0.0.1
        try:
            client = httpx.AsyncClient(**kwargs)
        except (ValueError, TypeError, httpx.InvalidURL) as e:
            raise LlmError(BAD_PROXY) from e
        self._clients[key] = client
        return client

    def _headers(self, cfg: LlmConfig, *, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": f"Jarvis/{__version__}",
        }
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        if cfg.provider == "openrouter":
            headers["X-Title"] = "Jarvis"
        return headers

    def _quirks_for(self, cfg: LlmConfig) -> set[str]:
        key = (cfg.base_url, cfg.model)
        quirks = self._quirks.get(key)
        if quirks is None:
            quirks = set()
            if not cfg.preset.send_temperature or _is_reasoning_model(cfg.model):
                quirks.add("no_temperature")
            if cfg.preset.json_mode == "prompt":
                quirks.add("json_prompt")
            elif cfg.preset.json_mode == "object":
                quirks.add("json_object")
            self._quirks[key] = quirks
        return quirks

    def _payload(
        self,
        cfg: LlmConfig,
        messages: list[dict[str, Any]],
        quirks: set[str],
        *,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        temperature: float | None,
        max_tokens: int | None,
        json_schema: dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": cfg.model, "messages": messages, "stream": stream}
        if stream and cfg.preset.stream_usage and "no_stream_options" not in quirks:
            body["stream_options"] = {"include_usage": True}
        if tools and "no_tools" not in quirks:
            body["tools"] = tools
            if tool_choice and "no_tool_choice" not in quirks:
                body["tool_choice"] = tool_choice
        temp = cfg.temperature if temperature is None else temperature
        if "no_temperature" not in quirks:
            body["temperature"] = temp
        if max_tokens:
            if "use_max_completion_tokens" in quirks:
                param = "max_completion_tokens"
            elif "use_max_tokens" in quirks:
                param = "max_tokens"
            else:
                param = cfg.preset.max_tokens_param
            body[param] = int(max_tokens)
        if json_schema is not None and "json_prompt" not in quirks:
            if "json_object" in quirks:
                body["response_format"] = {"type": "json_object"}
            else:
                body["response_format"] = {"type": "json_schema", "json_schema": _schema_spec(json_schema)}
        return body

    # --- запросы ------------------------------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
        stream: bool = False,
        on_delta: DeltaFn | None = None,
    ) -> LlmResponse:
        """Один ответ модели (с вызовами инструментов, если они есть). on_delta — куски текста по ходу.

        tool_choice="none" — инструменты описаны (история с их вызовами остаётся понятной), но вызывать
        их нельзя.
        """
        cfg = self.config(model)
        problem = cfg.problem()
        if problem:
            raise LlmError(problem)
        self.check_budget(cfg)
        quirks = self._quirks_for(cfg)
        msgs = _with_json_instruction(messages, json_schema) if json_schema is not None else messages
        options: dict[str, Any] = {
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "json_schema": json_schema,
            "stream": stream,
            "on_delta": on_delta,
        }
        response = await self._request(cfg, msgs, quirks, **options)
        self._record(cfg, response, msgs)
        if json_schema is None:
            return response
        parsed = _parse_json_answer(response.content, json_schema)
        if parsed is None and "json_prompt" not in quirks:
            # провайдер мог пропустить response_format мимо ушей — ещё раз, только с просьбой в промпте
            retry = await self._request(cfg, msgs, quirks | {"json_prompt"}, **options)
            self._record(cfg, retry, msgs)
            retry.usage.add(response.usage)
            response = retry
            parsed = _parse_json_answer(response.content, json_schema)
        if parsed is None:
            raise LlmFormatError(BAD_FORMAT)
        response.parsed = parsed
        return response

    async def _request(
        self,
        cfg: LlmConfig,
        messages: list[dict[str, Any]],
        quirks: set[str],
        *,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        temperature: float | None,
        max_tokens: int | None,
        json_schema: dict[str, Any] | None,
        stream: bool,
        on_delta: DeltaFn | None,
    ) -> LlmResponse:
        attempt = 0
        adapted: set[str] = set()
        active_tools = list(tools) if tools else None
        while True:
            body = self._payload(
                cfg,
                messages,
                quirks,
                tools=active_tools,
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens,
                json_schema=json_schema,
                stream=stream,
            )
            emitted = False

            def relay(text: str) -> None:
                nonlocal emitted
                emitted = True
                if on_delta is not None:
                    on_delta(text)

            try:
                if stream:
                    return await self._send_stream(cfg, body, relay)
                return await self._send(cfg, body)
            except _ApiError as e:
                if e.status in (400, 404, 422) and not emitted:
                    quirk = _quirk_for(e.message, body)
                    if quirk and quirk not in adapted:
                        log.info(
                            "провайдер %s не принял параметр (%s) — повторяю без него", cfg.provider, quirk
                        )
                        adapted.add(quirk)
                        quirks.add(quirk)
                        if quirk == "use_max_tokens":
                            quirks.discard("use_max_completion_tokens")
                        continue
                    bad_tool = _TOOL_INDEX.search(e.message)
                    if active_tools and bad_tool and int(bad_tool.group(1)) < len(active_tools):
                        dropped = active_tools.pop(int(bad_tool.group(1)))
                        log.warning(
                            "провайдер отверг описание инструмента %s: %s",
                            dropped.get("function", {}).get("name"),
                            e.message,
                        )
                        continue
                if e.retryable and attempt < self.max_retries and not emitted:
                    await self.sleep(_backoff(attempt, e.retry_after))
                    attempt += 1
                    continue
                log.warning("ИИ (%s, %s): %s", cfg.provider, cfg.model, e)
                raise _user_error(e, cfg) from None
            except httpx.TransportError as e:
                if not emitted and attempt < self.max_retries and _retryable_network(e, cfg):
                    await self.sleep(_backoff(attempt, None))
                    attempt += 1
                    continue
                log.warning("ИИ (%s): нет связи: %r", cfg.provider, e)
                raise _network_error(e, cfg) from None

    async def _send(self, cfg: LlmConfig, body: dict[str, Any]) -> LlmResponse:
        client = self._client(cfg)
        response = await client.post(cfg.chat_url, json=body, headers=self._headers(cfg, stream=False))
        if response.status_code >= 400:
            raise _api_error(response.status_code, response.content, response.headers)
        try:
            data = response.json()
        except ValueError:
            raise _ApiError(502, "провайдер вернул не JSON") from None
        return _parse_completion(data, cfg.model)

    async def _send_stream(self, cfg: LlmConfig, body: dict[str, Any], relay: DeltaFn) -> LlmResponse:
        client = self._client(cfg)
        acc = StreamAccumulator()
        think = ThinkFilter()

        def consume(payload: str) -> bool:
            if payload.strip() == "[DONE]":
                return True
            for chunk in _json_chunks(payload):
                if chunk.get("error") and not chunk.get("choices"):
                    raise _ApiError.from_payload(chunk)
                text = acc.add(chunk)
                if text:
                    visible = think.feed(text)
                    if visible:
                        relay(visible)
            return False

        headers = self._headers(cfg, stream=True)
        async with client.stream("POST", cfg.chat_url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                raise _api_error(response.status_code, await response.aread(), response.headers)
            if "event-stream" not in response.headers.get("content-type", "").lower():
                # сервер ответил целиком, без потока
                raw = await response.aread()
                try:
                    data = json.loads(raw)
                except ValueError:
                    raise _ApiError(502, "провайдер вернул не поток и не JSON") from None
                result = _parse_completion(data, cfg.model)
                if result.content:
                    relay(result.content)
                return result
            decoder = SseDecoder()
            finished = False
            async for line in response.aiter_lines():
                payload = decoder.feed(line)
                if payload is not None and consume(payload):
                    finished = True
                    break
            if not finished:
                rest = decoder.flush()
                if rest is not None:
                    consume(rest)
        tail = think.flush()
        if tail:
            relay(tail)
        return acc.result(cfg.model)

    # --- учёт расходов -------------------------------------------------------------------------------
    def _record(self, cfg: LlmConfig, response: LlmResponse, messages: list[dict[str, Any]]) -> None:
        usage = response.usage
        if not usage.prompt_tokens and not usage.completion_tokens:
            usage.prompt_tokens = _estimate_tokens(messages)
            usage.completion_tokens = max(1, (len(response.content) + len(str(response.tool_calls))) // 4)
            usage.estimated = True
        if usage.cost is None:
            model = cfg.model if price_for(cfg.model) else (response.model or cfg.model)
            usage.cost = estimate_cost(model, usage.prompt_tokens, usage.completion_tokens, local=cfg.local)
        try:
            self.ctx.db.insert(
                "usage",
                {
                    "ts": time.time(),
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cost": usage.cost,
                },
            )
        except Exception:
            log.exception("расход токенов не записан")

    def month_usage(self) -> dict[str, Any]:
        since = month_start()
        row = self.ctx.db.query_one(
            "SELECT COALESCE(SUM(prompt_tokens), 0) AS p, COALESCE(SUM(completion_tokens), 0) AS c, "
            "COALESCE(SUM(cost), 0) AS cost, COUNT(*) AS n FROM usage WHERE ts >= ?",
            (since,),
        ) or {"p": 0, "c": 0, "cost": 0.0, "n": 0}
        return {
            "prompt_tokens": int(row["p"]),
            "completion_tokens": int(row["c"]),
            "cost": round(float(row["cost"]), 4),
            "requests": int(row["n"]),
            "since": since,
        }

    def check_budget(self, cfg: LlmConfig | None = None) -> None:
        """Месячный лимит расходов исчерпан — LlmError (локальные модели бесплатны и не ограничены)."""
        budget = self.ctx.settings.value.ai.monthly_budget
        if budget <= 0 or (cfg is not None and cfg.local):
            return
        spent = self.month_usage()["cost"]
        if spent >= budget:
            raise LlmError(
                f"Достигнут месячный лимит расходов на ИИ (${spent:.2f} из ${budget:.2f}) — "
                "увеличьте его в настройках Jarvis AI",
                code="budget",
            )

    # --- сервисные запросы ------------------------------------------------------------------------------
    async def list_models(self) -> list[str]:
        """Модели провайдера (GET /models); при ошибке — список из пресета."""
        cfg = self.config()
        fallback = list(cfg.preset.models) or ([cfg.model] if cfg.model else [])
        if not cfg.base_url or (cfg.preset.needs_key and not cfg.api_key):
            return fallback
        try:
            client = self._client(cfg)
            response = await client.get(
                cfg.base_url + "/models", headers=self._headers(cfg, stream=False), timeout=15.0
            )
            if response.status_code >= 400:
                return fallback
            data = response.json()
        except (httpx.HTTPError, ValueError, LlmError) as e:
            log.info("список моделей не получен: %r", e)
            return fallback
        items = data.get("data") if isinstance(data, dict) else data
        if isinstance(data, dict) and not isinstance(items, list):
            items = data.get("models")
        ids: set[str] = set()
        for item in items if isinstance(items, list) else []:
            model_id = item.get("id") or item.get("name") if isinstance(item, dict) else item
            if isinstance(model_id, str) and model_id and not any(w in model_id.lower() for w in _NOT_CHAT):
                ids.add(model_id)
        return sorted(ids) or fallback

    async def test(self) -> dict[str, Any]:
        """Проверка связи: крошечный запрос. {ok, error?, latency_ms, model}."""
        cfg = self.config()
        started = time.perf_counter()
        try:
            await self.chat(
                [{"role": "user", "content": "Ответь одним словом: готов."}], max_tokens=64, stream=False
            )
        except LlmError as e:
            latency = int((time.perf_counter() - started) * 1000)
            return {"ok": False, "error": str(e), "latency_ms": latency, "model": cfg.model}
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000), "model": cfg.model}

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()
