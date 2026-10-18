"""Ошибки ChatGPT (Codex) → понятный текст по-русски.

Как Codex сообщает об ошибках (проверено на codex 0.163.0 с подставным сервером модели): событие `error`
и `turn/completed` со статусом failed несут TurnError {message, codexErrorInfo, additionalDetails};
codexErrorInfo — строка («usageLimitExceeded», «unauthorized», «contextWindowExceeded»…) или объект
{"httpConnectionFailed": {"httpStatusCode": 403}}. Ошибки запросов JSON-RPC (вход, старт треда) —
исключения SDK.
"""

import json
from typing import Any

NOT_INSTALLED = "Скачайте компонент ChatGPT в настройках Jarvis AI"
NOT_LOGGED_IN = "Войдите в ChatGPT в настройках Jarvis AI"
REGION = "ChatGPT недоступен из вашего региона — включите VPN или укажите прокси в настройках Jarvis AI"
QUOTA = "Лимит подписки ChatGPT исчерпан: {detail}"
RATE = "Слишком много запросов к ChatGPT — подождите минуту и попробуйте ещё раз"
NETWORK = "Нет связи с ChatGPT ({detail}) — проверьте интернет, VPN или прокси в настройках Jarvis AI"
SERVER = "Серверы ChatGPT сейчас перегружены — попробуйте чуть позже"
TOO_LONG = "Разговор стал слишком длинным для модели — начните новый чат"
MODEL = "Модель «{model}» недоступна в вашей подписке ChatGPT — выберите другую в настройках Jarvis AI"
POLICY = "ChatGPT отказался выполнять этот запрос"
SUBAGENT = "ChatGPT попытался запустить под-агента — ход прерван. Попробуйте сформулировать просьбу проще"
DEAD = "Компонент ChatGPT (Codex) неожиданно завершился — перезапущу его при следующем запросе"
START = "Компонент ChatGPT (Codex) не запустился: {detail}"
CONFIG = "Ошибка настроек Codex: {detail}"
GENERIC = "Ошибка ChatGPT: {detail}"
# прежний Jarvis (codex 0.160.1) на Windows не работал с CODEX_HOME, где есть кириллица (имя пользователя)
NON_ASCII_HINT = (
    ". В пути к данным Jarvis есть не-латинские буквы — если Codex так и не запустится, задайте переменную "
    "JARVIS_DATA_DIR (например C:\\JarvisData) и войдите в ChatGPT заново"
)
NO_DETAILS = "без подробностей"

REGION_MARKERS = (
    "unsupported_country_region_territory",
    "country, region, or territory not supported",
    "unsupported_country",
    "not available in your country",
    "restricted region",
)
QUOTA_CODES = {"usageLimitExceeded", "sessionBudgetExceeded"}
QUOTA_MARKERS = ("usage limit", "usage_limit", "hit your usage", "quota")
RATE_CODES = {"rateLimitExceeded"}
AUTH_CODES = {"unauthorized"}
AUTH_MARKERS = (
    "not logged in",
    "please try signing in",
    "sign in again",
    "log in again",
    "login again",
    "token_expired",
    "refresh_token",
    "authentication token",
    "no auth",
    "requires authentication",
)
SERVER_CODES = {"serverOverloaded", "internalServerError"}
NETWORK_MARKERS = (
    "error sending request",
    "stream disconnected",
    "connection refused",
    "connection reset",
    "dns error",
    "failed to lookup",
    "timed out",
    "timeout",
    "network",
    "tls",
    "certificate",
    "proxy",
    "reconnecting",
)


def short(text: Any, limit: int = 300) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def error_info(info: Any) -> tuple[str, int | None]:
    """codexErrorInfo (как пришёл в событии: строка или {вариант: {...}}) → (код, HTTP-статус)."""
    if info is None:
        return "", None
    if isinstance(info, str):
        return info, None
    if isinstance(info, dict):
        for code, body in info.items():
            status = body.get("httpStatusCode") if isinstance(body, dict) else None
            return str(code), status if isinstance(status, int) else None
    return str(info), None


def _http_status(message: str) -> int | None:
    """«unexpected status 403 Forbidden: …» → 403."""
    low = message.casefold()
    marker = "unexpected status "
    at = low.find(marker)
    if at < 0:
        return None
    digits = low[at + len(marker) : at + len(marker) + 3]
    return int(digits) if digits.isdigit() else None


def _readable(message: str) -> str:
    """Текст ошибки сервера без JSON-обёртки и адреса: {"error": {"message": "…"}} → «…»."""
    text = message.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                text = err["message"]
            elif isinstance(data.get("detail"), str):
                text = data["detail"]
    for cut in (", url: ", " url: "):
        if cut in text:
            text = text.split(cut, 1)[0]
    return short(text, 240).rstrip(".")


def classify(message: str, info: Any = None, details: str = "", *, model: str = "") -> tuple[str, str]:
    """Ошибка хода → (reason, текст для человека).

    reason: region | quota | rate | auth | network | server | too_long | model | policy | error.
    """
    message = str(message or "")
    low = f"{message}\n{details}".casefold()
    code, status = error_info(info)
    status = status or _http_status(message)
    if status in (403, 451) or any(m in low for m in REGION_MARKERS):
        if "model" in low and any(w in low for w in ("not supported", "not available", "access")):
            return "model", MODEL.format(model=model or "?")
        return "region", REGION
    if code in QUOTA_CODES or any(m in low for m in QUOTA_MARKERS):
        return "quota", QUOTA.format(detail=_readable(message) or NO_DETAILS)
    if code in RATE_CODES or status == 429 or "rate limit" in low:
        return "rate", RATE
    if code in AUTH_CODES or status == 401 or any(m in low for m in AUTH_MARKERS):
        return "auth", NOT_LOGGED_IN
    if code == "contextWindowExceeded" or "context window" in low or "context_length" in low:
        return "too_long", TOO_LONG
    if code in ("cyberPolicy", "misalignmentPolicyViolation"):
        return "policy", POLICY
    if "model" in low and any(
        w in low for w in ("not supported", "does not exist", "not found", "unknown model")
    ):
        return "model", MODEL.format(model=model or "?")
    if code in SERVER_CODES or (status is not None and status >= 500):
        return "server", SERVER
    if code == "httpConnectionFailed" and status is None:
        return "network", NETWORK.format(detail=_readable(message) or NO_DETAILS)
    if code in ("responseStreamConnectionFailed", "responseStreamDisconnected") or any(
        m in low for m in NETWORK_MARKERS
    ):
        return "network", NETWORK.format(detail=_readable(message) or NO_DETAILS)
    return "error", GENERIC.format(detail=_readable(message) or NO_DETAILS)


def classify_turn_error(error: Any, *, model: str = "") -> tuple[str, str]:
    """TurnError из события (dict {message, codexErrorInfo, additionalDetails}) → (reason, текст)."""
    if not isinstance(error, dict):
        return "error", GENERIC.format(detail=NO_DETAILS)
    return classify(
        str(error.get("message") or ""),
        error.get("codexErrorInfo"),
        str(error.get("additionalDetails") or ""),
        model=model,
    )


def _stderr_error(text: str) -> str:
    """Строка «Error: …» с продолжением из хвоста stderr codex (так он сообщает о плохом override)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "Error:" in line:
            out = [line.split("Error:", 1)[1].strip()]
            for more in lines[i + 1 :]:
                if not more.strip() or more.startswith("Stack backtrace"):
                    break
                out.append(more.strip())
            return " ".join(out)
    return ""


def explain_exception(exc: BaseException, *, starting: bool = False, model: str = "") -> tuple[str, str]:
    """Исключение SDK или запуска → (reason, текст). reason: dead | start | config | not_installed | …"""
    name = type(exc).__name__
    message = str(getattr(exc, "message", "") or exc)
    if isinstance(exc, FileNotFoundError):
        return "not_installed", NOT_INSTALLED
    if name in ("TransportClosedError", "RuntimeGone"):
        if starting:
            detail = _stderr_error(message) or short(message, 200)
            if "configuration" in detail.casefold() or " in `" in detail:
                return "config", CONFIG.format(detail=detail)
            return "start", START.format(detail=detail)
        return "dead", DEAD
    if "failed to load configuration" in message.casefold() or "error loading config" in message.casefold():
        return "config", CONFIG.format(detail=short(message, 240))
    if isinstance(exc, OSError) and starting:
        return "start", START.format(detail=short(message, 200))
    reason, text = classify(message, None, str(getattr(exc, "data", "") or ""), model=model)
    if starting and reason == "error":
        return "start", START.format(detail=short(message, 200))
    return reason, text
