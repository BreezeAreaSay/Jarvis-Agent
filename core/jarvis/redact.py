"""Маскировка секретов в тексте: номера карт, CVV, пароли, ключи API и токены.

redact() зовут перед записью в журналы (jarvis.log.RedactFilter) и в историю чатов, чтобы случайно
сказанный или найденный в окне секрет не остался на диске. Маска оставляет начало ключа и последние цифры
карты — по ним человек узнает, что это было, но воспользоваться уже нельзя.
"""

import re

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
_ASSIGN_RE = re.compile(
    r"(?i)((?<![A-Za-z0-9])[\"']?[A-Za-z0-9_-]*?(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|"
    r"secret|token|access[_-]?token|refresh[_-]?token|authorization|session[_-]?string|api[_-]?hash|password|"
    r"passwd|pwd)[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/=:-]{8,})"
)
_TOKEN_RES = (
    re.compile(r"\b(sk-(?:ant-|proj-)?)[A-Za-z0-9_-]{16,}"),  # OpenAI, Anthropic
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
)


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
    out = _CARD_RE.sub(_card, text)
    out = _CVV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _PASSWORD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    out = _PIN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    for pattern in _TOKEN_RES:
        out = pattern.sub(lambda m: f"{m.group(1)}{MASK}", out)
    return _ASSIGN_RE.sub(lambda m: f"{m.group(1)}{MASK}", out)


def has_secret(text: str) -> bool:
    """Есть ли в тексте то, что redact() замаскирует."""
    return bool(text) and redact(text) != text
