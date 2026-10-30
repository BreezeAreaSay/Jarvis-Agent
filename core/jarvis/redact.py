"""Маскировка секретов в тексте: номера карт, CVV, пароли, ключи API, токены, закрытые ключи и пароли в
адресах подключения.

redact() зовут перед записью в журналы (jarvis.log.RedactFilter) и в историю чатов, чтобы случайно
сказанный или найденный в окне секрет не остался на диске, и перед отправкой облачной модели
(redact_messages — в клиенте API, подписки ChatGPT и программ вроде Claude Code; settings.ai.mask_secrets).
Маска оставляет начало ключа и последние цифры карты — по ним человек узнает, что это было, но
воспользоваться уже нельзя.
"""

import re
from typing import Any

MASK = "•••"

# номер карты: 13–19 цифр подряд или группами через пробел/дефис; первая цифра — как у платёжных систем
# (2 — «Мир» и Mastercard, 3 — Amex и JCB, 4 — Visa, 5 — Mastercard, 6 — UnionPay), проверка Луна отсекает
# случайные числа и метки времени
_CARD_RE = re.compile(r"(?<![\d-])[2-6](?:[ -]?\d){12,18}(?![\d-])")
_CVV_RE = re.compile(
    r"(?i)\b(cvv2?|cvc2?|cvn|cid|код безопасности|код с обратной стороны)(\s*[:=]?\s*|\s+[—-]\s+)(\d{3,4})\b"
)
_SEP = r"(\s*[:=]\s*|\s+[—-]\s+|\s+(?:это|is)\s+)"
_PASSWORD_RE = re.compile(
    r"(?i)\b(парол[ьяюе]|password|passwd|pwd|пин-?код|pin-?code|pin|кодовое слово|секретное слово)"
    + _SEP
    + r"(\"[^\"]+\"|'[^']+'|«[^»]+»|\S+)"
)
# «пин-код 1234» — и без двоеточия, если дальше одни цифры
_PIN_RE = re.compile(r"(?i)\b(пин-?код|pin-?code|pin)(\s+)(\d{4,8})\b")
# ключ=значение: api_key=…, "token": "…", Authorization: …, TELEGRAM_SESSION_STRING=…
# имя ключа — до 40 знаков перед словом-признаком и не с середины слова: иначе на длинных «ab-ab-ab-…»
# (адреса, base64) поиск квадратичный и замирает на секунды
_ASSIGN_RE = re.compile(
    r"(?i)((?<![A-Za-z0-9_-])[\"']?[A-Za-z0-9_-]{0,40}?(?:api[_-]?key|access[_-]?key|secret[_-]?key|"
    r"client[_-]?secret|"
    r"secret|token|access[_-]?token|refresh[_-]?token|authorization|session[_-]?string|api[_-]?hash|password|"
    r"passwd|pwd)[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/=:-]{8,})"
)
_TOKEN_RES = (
    re.compile(r"\b(sk-(?:ant-|proj-)?)[A-Za-z0-9_-]{16,}"),  # OpenAI, Anthropic
    re.compile(r"\b(sk_)[A-Za-z0-9]{32,}"),  # ElevenLabs
    re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"\b(github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(xox[abposr]-)[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\b(AIza)[0-9A-Za-z_-]{30,}"),  # Google
    re.compile(r"\b(AKIA)[0-9A-Z]{16}\b"),  # AWS
    re.compile(r"\b(y0_)[A-Za-z0-9_-]{30,}"),  # Яндекс OAuth
    re.compile(r"\b(\d{8,10}:)[A-Za-z0-9_-]{35}\b"),  # бот Telegram
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(basic\s+)[A-Za-z0-9+/]{12,}={0,2}"),
    re.compile(r"\b(eyJ)[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b(glpat-)[A-Za-z0-9_-]{20,}"),  # GitLab
    re.compile(r"\b(hf_)[A-Za-z0-9]{30,}"),  # Hugging Face
    re.compile(r"\b((?:sk|rk|pk)_(?:live|test)_)[A-Za-z0-9]{16,}"),  # Stripe
    re.compile(r"\b(AQVN)[A-Za-z0-9_-]{30,}"),  # ключ API Yandex Cloud
    re.compile(r"\b(t1\.)[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{40,}"),  # IAM-токен Yandex Cloud
    re.compile(r"\b(ya29\.)[A-Za-z0-9_-]{20,}"),  # Google OAuth
)
# закрытый ключ целиком: -----BEGIN RSA PRIVATE KEY----- … -----END RSA PRIVATE KEY-----
_PEM_RE = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY)-----.*?-----END \1-----", re.S)
# пароль в адресе подключения: postgres://user:пароль@host, mongodb+srv://…, ftp://…
_URL_CREDS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20}://[^\s:/@\"']+:)([^\s@/\"']{3,})(@)")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if not 13 <= len(digits) <= 19 or not luhn_ok(digits):
        return m.group(0)
    return f"•••• {digits[-4:]}"


def redact(text: str) -> str:
    """Текст с замаскированными секретами (без секретов — тот же текст)."""
    if not text:
        return text
    out = _PEM_RE.sub(lambda m: f"-----BEGIN {m.group(1)}----- {MASK} -----END {m.group(1)}-----", text)
    out = _URL_CREDS_RE.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", out)
    out = _CARD_RE.sub(_card, out)
    out = _CVV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _PIN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    for pattern in _TOKEN_RES:
        out = pattern.sub(lambda m: f"{m.group(1)}{MASK}", out)
    return _ASSIGN_RE.sub(_assign, out)


# «токены» страниц и синхронизации (nextPageToken, syncToken, cursor) — не секреты: без них ломается выдача
# частями
_PAGING_KEY = re.compile(r"(?i)(page|next|prev|sync|cursor|continuation|resume)[_-]?token")


def _assign(m: re.Match[str]) -> str:
    return m.group(0) if _PAGING_KEY.search(m.group(1)) else f"{m.group(1)}{MASK}"


def has_secret(text: str) -> bool:
    """Есть ли в тексте то, что redact() замаскирует."""
    return bool(text) and redact(text) != text


# маскировать ли текст для облачных моделей подписки (ChatGPT, Claude Code…) — settings.ai.mask_secrets;
# ставит сервис Jarvis AI при запуске и смене настроек
_OUTGOING = {"on": True}


def set_outgoing(enabled: bool) -> None:
    _OUTGOING["on"] = bool(enabled)


def outgoing(text: str) -> str:
    """Текст для облачной модели: секреты — маской (если это не выключено в настройках)."""
    return redact(text) if _OUTGOING["on"] and text else text


def redact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сообщения для модели (формат /chat/completions) с замаскированными секретами в тексте: и строкой, и
    частями {"type": "text"}; картинки и вызовы инструментов — как есть. Исходный список не меняется."""
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            masked: Any = redact(content)
        elif isinstance(content, list):
            masked = [
                {**part, "text": redact(part["text"])}
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                else part
                for part in content
            ]
        else:
            out.append(message)
            continue
        out.append(message if masked == content else {**message, "content": masked})
    return out
