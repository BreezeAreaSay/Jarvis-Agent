"""Разбор веб-адресов для прямых команд и политики: только http и https (ADR 0030).

Без urllib (ядро не импортирует сетевые модули): строгий разбор регулярным выражением. Адрес без схемы
принимается, только если это домен с известной доменной зоной, — иначе «README.md» (зона .md) стал бы
сайтом. Учётные данные в адресе (`user:pass@`), пробелы и управляющие символы — не адрес.
"""

import re

MAX_URL_CHARS = 2000

# Зоны, по которым адрес без схемы считается сайтом. Зоны, совпадающие с расширениями файлов (.md, .py,
# .sh, .zip …), сюда не входят.
BARE_TLDS = frozenset(
    {
        "com", "org", "net", "ru", "io", "dev", "app", "ai", "co", "me", "tv", "info", "su", "рф", "edu",
        "gov", "uk", "de", "fr", "eu", "us", "ua", "by", "kz", "cn", "jp", "tech", "site", "online",
    }
)  # fmt: skip

_LABEL = r"[^\W_](?:[\w-]{0,61}[^\W_])?"
_HOST = rf"(?:{_LABEL}\.)+{_LABEL}|localhost|\d{{1,3}}(?:\.\d{{1,3}}){{3}}|\[[0-9A-Fa-f:.]+\]"
_URL = re.compile(
    rf"(?P<scheme>https?)://(?P<host>{_HOST})(?::(?P<port>\d{{1,5}}))?(?P<rest>[/?#][^\s]*)?",
    re.IGNORECASE,
)
_BARE = re.compile(rf"(?P<host>(?:{_LABEL}\.)+(?P<tld>{_LABEL}))(?::(?P<port>\d{{1,5}}))?(?P<rest>/[^\s]*)?")


def normalize_web_url(raw: str, *, allow_bare: bool = True) -> str | None:
    """Нормализованный http(s)-адрес или None, если это не веб-адрес. Схема и хост — в нижнем регистре;
    адрес без схемы (если разрешён) получает https://."""
    text = raw.strip()
    if not text or len(text) > MAX_URL_CHARS or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    if any(ch.isspace() for ch in text):
        return None
    match = _URL.fullmatch(text)
    if match is None:
        if not allow_bare:
            return None
        bare = _BARE.fullmatch(text)
        if bare is None or bare.group("tld").casefold() not in BARE_TLDS:
            return None
        match = _URL.fullmatch(f"https://{text}")
        if match is None:
            return None
    port = match.group("port")
    if port is not None and not 0 < int(port) <= 65535:
        return None
    host = match.group("host")
    if re.fullmatch(r"[\d.]+", host) and any(int(part) > 255 for part in host.split(".") if part):
        return None
    # Учётных данных в адресе нет: хост идёт сразу за схемой, и «user:pass@» до пути не пройдёт.
    rest = match.group("rest") or ""
    netloc = host.casefold() + (f":{port}" if port else "")
    return f"{match.group('scheme').lower()}://{netloc}{rest}"


def is_web_url(value: str) -> bool:
    """Уже нормализованный http(s)-адрес (так его проверяет политика)."""
    return normalize_web_url(value, allow_bare=False) == value
