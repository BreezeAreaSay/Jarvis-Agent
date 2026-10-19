"""Прокси: адрес из поля «Прокси» Jarvis AI и системный прокси Windows, который включают VPN-программы.

Системный прокси httpx берёт из urllib.request.getproxies(): на Windows — из реестра (Internet Settings), куда
VPN-программы в режиме «системный прокси» пишут свой адрес. Запись «socks=127.0.0.1:10808» urllib превращает
в socks4://…, а httpx знает только http, https, socks5 и socks5h — и каждый клиент httpx в ядре падал при
создании с ValueError (ИИ отвечал «Неверный адрес прокси», хотя поле прокси пустое). install() подменяет
источник системных прокси для httpx на system_proxies(): SOCKS из реестра — как socks5:// (локальные
VPN-программы — Xray/v2rayN, sing-box, Clash — отвечают по SOCKS5), а то, чем httpx пользоваться не умеет,
пропускается: запрос идёт напрямую, а не падает.

normalize() приводит адрес из поля «Прокси» к виду, который понимают httpx, Codex и Claude Code:
«127.0.0.1:1080» → «http://127.0.0.1:1080», «socks://…» и «socks4://…» → «socks5://…».
"""

import logging
import urllib.request
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)

SCHEMES = ("http", "https", "socks5", "socks5h")  # что понимает httpx
# httpx не умеет SOCKS4 — локальные прокси VPN-программ отвечают и по SOCKS5
SOCKS_ALIASES = ("socks", "socks4", "socks4a")

# откуда берутся системные прокси (тесты подставляют свои)
source: Callable[[], dict[str, str]] = urllib.request.getproxies

_reported: set[str] = set()


def normalize(value: str) -> str:
    """Адрес прокси из настроек → вид со схемой: «127.0.0.1:1080» → «http://127.0.0.1:1080»."""
    text = value.strip()
    if not text:
        return ""
    scheme, sep, rest = text.partition("://")
    if not sep:
        return "http://" + text
    low = scheme.lower()
    if low in SOCKS_ALIASES:
        return "socks5://" + rest
    return f"{low}://{rest}" if low in SCHEMES else text


def usable(url: str) -> bool:
    """httpx может ходить через этот прокси."""
    try:
        httpx.Proxy(url)
    except (ValueError, TypeError, httpx.InvalidURL):
        return False
    return True


def is_socks(url: str) -> bool:
    return normalize(url).startswith(("socks5://", "socks5h://"))


def system_proxies() -> dict[str, str]:
    """Системные прокси (как urllib.request.getproxies()), но только те, через которые httpx может ходить."""
    try:
        raw = source()
    except Exception:
        log.exception("системный прокси не читается — запросы идут напрямую")
        return {}
    found: dict[str, str] = {}
    for key, value in raw.items():
        if key == "no":
            found[key] = value
            continue
        if key not in ("http", "https", "all") or not value:
            continue  # httpx берёт только http, https, all и no
        url = normalize(value)
        if usable(url):
            found[key] = url
        elif value not in _reported:
            _reported.add(value)
            log.warning("системный прокси %s=%s не поддерживается — запросы идут без него", key, value)
    return found


def system_https() -> str:
    """Системный прокси для https-запросов ("" — его нет)."""
    found = system_proxies()
    return found.get("https") or found.get("all") or ""


def install() -> None:
    """Клиенты httpx берут системный прокси через system_proxies().

    httpx читает системный прокси функцией getproxies, импортированной в httpx._utils; если её там нет (другая
    версия httpx), подмены нет — тест test_netproxy об этом скажет.
    """
    from httpx import _utils

    if not hasattr(_utils, "getproxies"):
        log.warning("httpx без _utils.getproxies — системный прокси не проверяется")
        return
    _utils.getproxies = system_proxies  # type: ignore[attr-defined]
