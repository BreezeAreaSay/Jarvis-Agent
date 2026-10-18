"""Новости из RSS: Google Новости (главное и поиск по теме), Lenta.ru — запасной источник.

Здесь только адреса и разбор лент — сеть и кэш в jarvis.widgets.service.
"""

import datetime as dt
import email.utils
import html
import re
import xml.etree.ElementTree as ET
from typing import Any

GOOGLE_TOP_URL = "https://news.google.com/rss"
GOOGLE_SEARCH_URL = "https://news.google.com/rss/search"
GOOGLE_PARAMS = {"hl": "ru", "gl": "RU", "ceid": "RU:ru"}
LENTA_URL = "https://lenta.ru/rss/news"

_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    plain = _TAGS.sub(" ", html.unescape(node.text))
    return _SPACES.sub(" ", plain).strip()


def _ts(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return email.utils.parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0.0


def parse_rss(
    text: str, default_source: str = "", *, strip_source_suffix: bool = False
) -> list[dict[str, Any]]:
    """Лента RSS 2.0 (или Atom) → [{title, url, source, ts}], новые первыми. Битая лента — ValueError."""
    if "<!ENTITY" in text[:4096]:
        raise ValueError("лента с объявлениями сущностей не принимается")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"лента не разбирается: {e}") from e
    items: list[dict[str, Any]] = []
    for node in root.iter("item"):
        title = _text(node.find("title"))
        url = _text(node.find("link"))
        source_node = node.find("source")
        source = _text(source_node) or default_source
        if strip_source_suffix and source_node is not None and title.endswith(f" - {source}"):
            # Google добавляет издание в заголовок: «Заголовок - РИА Новости»
            title = title[: -len(source) - 3].rstrip()
        if title and url:
            items.append(
                {"title": title, "url": url, "source": source, "ts": _ts(_text(node.find("pubDate")))}
            )
    if not items:
        for node in root.iter(f"{_ATOM}entry"):
            title = _text(node.find(f"{_ATOM}title"))
            link = node.find(f"{_ATOM}link")
            url = link.get("href", "") if link is not None else ""
            stamp = _text(node.find(f"{_ATOM}updated")) or _text(node.find(f"{_ATOM}published"))
            if title and url:
                items.append({"title": title, "url": url, "source": default_source, "ts": _iso_ts(stamp)})
    items.sort(key=lambda item: item["ts"], reverse=True)
    return _dedupe(items)


def _iso_ts(raw: str) -> float:
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = item["title"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
