"""Поиск секретов в тексте — вторая линия защиты границы облака (ADR 0028).

Первая линия — классы данных по происхождению (зона секретов, `private_roots`): поиск по тексту их не
заменяет. Он ловит ключ, вставленный в запрос, токен в выводе процесса, пароль в строке подключения —
и маскирует тексты ошибок провайдеров. Это не DLP-система: ложное срабатывание оставляет вызов
локальным, и это осознанный выбор. Возвращаются только категории находок, без самих значений.
"""

import re

MASK = "[скрыто]"

# Категория → шаблон. Шаблоны намеренно широкие: лишняя находка безопаснее пропущенного ключа.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    "authorization_header": re.compile(
        r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*(?:bearer|basic|token|digest)?\s*[A-Za-z0-9._~+/=-]{8,}"
    ),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "api_key": re.compile(
        r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"  # ключи вида sk-…
        r"|AKIA[0-9A-Z]{16}"  # AWS access key
        r"|AIza[0-9A-Za-z_-]{35}"  # Google API key
        r"|gh[pousr]_[A-Za-z0-9]{30,}"  # GitHub
        r"|github_pat_[A-Za-z0-9_]{40,}"
        r"|xox[abpors]-[A-Za-z0-9-]{10,}"  # Slack
        r"|glpat-[A-Za-z0-9_-]{20,}"  # GitLab
        r"|hf_[A-Za-z0-9]{30,})"  # Hugging Face
    ),
    "secret_assignment": re.compile(
        # Имя переменной может быть частью другого: DB_PASSWORD, OPENAI_API_KEY.
        r"(?i)(?<![a-z])(?:api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token"
        r"|refresh[_-]?token|private[_-]?key|password|passwd|pwd|пароль)(?![a-z])[\"']?\s*[:=]\s*[\"']?"
        r"[^\s\"',;]{6,}"
    ),
    "connection_string": re.compile(
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@[^\s/]+"  # схема://логин:пароль@хост
        r"|\b(?:password|pwd)\s*=\s*[^;\s]{4,}\s*;"  # Server=…;Password=…;
    ),
}


def find_secrets(text: str) -> list[str]:
    """Категории найденного (без значений), в порядке объявления шаблонов."""
    return [name for name, pattern in _PATTERNS.items() if pattern.search(text)]


def mask_secrets(text: str, *known: str) -> str:
    """Текст без найденных секретов и без известных значений (например, ключа самого провайдера) —
    для сообщений об ошибках и журналов."""
    for value in known:
        if value:
            text = text.replace(value, MASK)
    for pattern in _PATTERNS.values():
        text = pattern.sub(MASK, text)
    return text
