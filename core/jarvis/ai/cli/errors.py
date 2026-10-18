"""Ошибки программ-провайдеров (Claude Code, Gemini CLI, Qwen Code) → понятный текст по-русски.

Источники: поле error сообщений Claude Code (authentication_failed, oauth_org_not_allowed, account_on_hold,
billing_error, rate_limit, overloaded, model_not_found, server_error…), событие rate_limit_event (status
rejected, resetsAt), код выхода Gemini CLI 41 (нет входа), тексты ошибок API в result и хвост stderr.
"""

import re
import time
from datetime import datetime
from typing import Any

NOT_INSTALLED = "{name} не установлен — установите его в настройках Jarvis AI"
NOT_LOGGED_IN = {
    "claude_code": "Войдите в Claude Code: откройте терминал, запустите claude и выполните /login",
    "gemini_cli": "Войдите в Gemini CLI: откройте терминал, запустите gemini и выберите способ входа",
    "qwen_code": "Настройте вход в Qwen Code: откройте терминал, запустите qwen и выполните /auth",
}
GEMINI_CONSUMER = (
    "Google закрыл Gemini CLI для личных аккаунтов Google (18 июня 2026). Выберите «Google Gemini» по ключу "
    "API — бесплатный ключ выдаёт Google AI Studio"
)
QWEN_OAUTH = (
    "Бесплатный вход Qwen OAuth закрыт с 15 апреля 2026 — в Qwen Code выберите тариф Coding Plan, Token Plan "
    "или ключ API (команда /auth)"
)
NO_PLAN = "Claude Code работает с подпиской Claude Pro или Max — бесплатного плана claude.ai для него мало"
ORG = "Организация вашего аккаунта Claude не разрешает этот вход — войдите в Claude Code другим аккаунтом"
HOLD = "Аккаунт Claude приостановлен — проверьте его на claude.ai"
BILLING = "Закончился баланс или кредиты {name} — проверьте тариф"
LIMIT = "Лимит {name} исчерпан{until}"
RATE = "Слишком много запросов к {name} — подождите минуту и попробуйте ещё раз"
REGION = "{name} недоступен из вашего региона — включите VPN или укажите прокси в настройках Jarvis AI"
NETWORK = "Нет связи с {name} — проверьте интернет, VPN или прокси в настройках Jarvis AI"
SERVER = "Серверы {name} сейчас перегружены — попробуйте чуть позже"
MODEL = "Модель «{model}» недоступна в {name} — выберите другую в настройках Jarvis AI"
TOO_LONG = "Разговор стал слишком длинным для модели — начните новый чат"
TOO_LONG_ANSWER = "Ответ получился слишком длинным — попросите короче"
HUNG = "{name} не ответил вовремя — попробуйте ещё раз"
START = "{name} не запустился: {detail}"
GENERIC = "Ошибка {name}: {detail}"
NO_DETAILS = "без подробностей"
NO_MCP = "Инструменты Jarvis для {name} недоступны: ядро запущено без HTTP-сервера"

GEMINI_CONSUMER_MARKERS = ("no longer supported for gemini code assist for individuals", "antigravity")
QWEN_OAUTH_MARKERS = (
    "qwen oauth",
    "qwen-oauth",
    "free tier was discontinued",
    "free tier has been discontinued",
)
AUTH_MARKERS = (
    "/login",
    "/auth",
    "invalid api key",
    "not logged in",
    "login expired",
    "oauth token",
    "token has expired",
    "please set an auth method",
    "no auth",
    "authentication",
    "unauthorized",
    "unauthenticated",
    "invalid_api_key",
    "incorrect api key",
    "invalidapikey",
    "api key not valid",
)
BILLING_MARKERS = (
    "credit balance",
    "credits_required",
    "billing",
    "insufficient_quota",
    "insufficient balance",
    "arrearage",
    "payment required",
)
LIMIT_MARKERS = (
    "usage limit",
    "limit reached",
    "hit your limit",
    "quota",
    "resource_exhausted",
    "exhausted your",
    "daily limit",
)
RATE_MARKERS = ("rate limit", "rate_limit", "too many requests")
REGION_MARKERS = (
    "unsupported_country",
    "not available in your country",
    "not supported in your country",
    "country, region, or territory",
    "user location is not supported",
    "location is not supported",
    "not available in your region",
    "request not allowed",
    "unsupported region",
)
MODEL_MARKERS = (
    "not found",
    "not_found",
    "does not exist",
    "not available",
    "not supported",
    "unknown model",
)
TOO_LONG_MARKERS = (
    "prompt is too long",
    "context window",
    "context_length",
    "maximum context",
    "too many tokens",
)
SERVER_MARKERS = (
    "overloaded",
    "529",
    "internal server error",
    "service unavailable",
    "bad gateway",
)
NETWORK_MARKERS = (
    "enotfound",
    "econnrefused",
    "econnreset",
    "etimedout",
    "eai_again",
    "fetch failed",
    "socket hang up",
    "getaddrinfo",
    "connection error",
    "unable to connect",
    "network",
    "certificate",
    "self signed",
    "self-signed",
    "proxy",
)


def short(text: Any, limit: int = 240) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def until_text(resets_at: Any) -> str:
    """«— обновится в 15:30» по времени сброса лимита (unix-секунды)."""
    if not isinstance(resets_at, int | float) or isinstance(resets_at, bool) or resets_at <= 0:
        return " — попробуйте позже"
    moment = datetime.fromtimestamp(float(resets_at))
    if float(resets_at) - time.time() > 20 * 3600:
        return f" — обновится {moment.strftime('%d.%m в %H:%M')}"
    return f" — обновится в {moment.strftime('%H:%M')}"


_STATUS = re.compile(r"\b(401|402|403|429|500|502|503|529)\b")


def _has(low: str, markers: tuple[str, ...]) -> bool:
    return any(m in low for m in markers)


def _statuses(low: str) -> set[str]:
    """HTTP-статусы в тексте ошибки («429 Too Many Requests», «status 401»)."""
    return set(_STATUS.findall(low))


def classify(
    provider: str,
    name: str,
    text: str = "",
    *,
    code: str = "",
    model: str = "",
    exit_code: int | None = None,
    stderr: str = "",
    resets_at: Any = None,
) -> tuple[str, str]:
    """Ошибка хода → (reason, текст для человека).

    reason: auth | gemini_consumer | qwen_oauth | plan | org | hold | billing | limit | rate | region |
    model | too_long | server | network | error.
    """
    low = f"{text}\n{stderr}".casefold()
    statuses = _statuses(low)
    code = (code or "").strip()
    login = NOT_LOGGED_IN.get(provider, GENERIC.format(name=name, detail="нужен вход в программу"))
    if _has(low, GEMINI_CONSUMER_MARKERS) and provider == "gemini_cli":
        return "gemini_consumer", GEMINI_CONSUMER
    if _has(low, QWEN_OAUTH_MARKERS) and provider == "qwen_code" and ("discontinu" in low or "free" in low):
        return "qwen_oauth", QWEN_OAUTH
    if code == "oauth_org_not_allowed":
        return "org", ORG
    if code == "account_on_hold":
        return "hold", HOLD
    if code == "authentication_failed" or (provider == "gemini_cli" and exit_code == 41):
        return "auth", login
    if code in ("billing_error", "credits_required") or _has(low, BILLING_MARKERS) or "402" in statuses:
        if provider == "claude_code" and "claude code" in low and "pro" in low and "max" in low:
            return "plan", NO_PLAN
        return "billing", BILLING.format(name=name)
    if code == "rate_limit" or _has(low, LIMIT_MARKERS) or _has(low, RATE_MARKERS) or "429" in statuses:
        if code == "rate_limit" or _has(low, LIMIT_MARKERS) or resets_at:
            return "limit", LIMIT.format(name=name, until=until_text(resets_at))
        return "rate", RATE.format(name=name)
    if _has(low, REGION_MARKERS) or ("403" in statuses and "forbidden" in low and "model" not in low):
        return "region", REGION.format(name=name)
    if _has(low, AUTH_MARKERS) or "401" in statuses:
        return "auth", login
    if code == "model_not_found" or ("model" in low and _has(low, MODEL_MARKERS)):
        return "model", MODEL.format(model=model or "?", name=name)
    if _has(low, TOO_LONG_MARKERS):
        return "too_long", TOO_LONG
    if code == "max_output_tokens":
        return "too_long", TOO_LONG_ANSWER
    if (
        code in ("overloaded", "server_error")
        or _has(low, SERVER_MARKERS)
        or statuses & {"500", "502", "503", "529"}
    ):
        return "server", SERVER.format(name=name)
    if _has(low, NETWORK_MARKERS):
        return "network", NETWORK.format(name=name)
    detail = short(text) or short(stderr.strip().splitlines()[-1] if stderr.strip() else "") or NO_DETAILS
    return "error", GENERIC.format(name=name, detail=detail)
