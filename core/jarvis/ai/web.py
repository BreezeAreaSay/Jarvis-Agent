"""Интернет для Jarvis AI: поиск (DuckDuckGo, запасной — Bing RSS), новости, текст страниц, погода, курсы.

Всё, что приходит из сети, — данные, а не инструкции: инструменты ИИ передают их модели в обёртке «данные из
интернета». Страницы локальной сети и самого компьютера не открываются (защита от подмены ссылок).
"""

import contextlib
import ipaddress
import logging
import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

DDG_URL = "https://html.duckduckgo.com/html/"
BING_RSS_URL = "https://www.bing.com/search"
BING_NEWS_URL = "https://www.bing.com/news/search"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}
MAX_PAGE_BYTES = 2_000_000
PAGE_LIMIT = 6000

# погода: коды WMO → слова
WEATHER_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман с изморозью",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежная крупа",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}

_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "button",
    "template",
    "select",
    "input",
    "canvas",
    "dialog",
)


class WebError(UserFacingError):
    """Сеть или страница подвели; текст — для человека и модели."""


# --- проверка ссылок ----------------------------------------------------------------------------------


def check_public_url(url: str) -> str:
    """Ссылку можно открыть: http(s) и не адрес этого компьютера или локальной сети."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise WebError("Открывать можно только ссылки http и https")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise WebError("Неверная ссылка")
    if host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".internal", ".home.arpa")):
        raise WebError("Адреса этого компьютера и локальной сети открывать нельзя")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:  # имя без домена — компьютер в локальной сети
            raise WebError("Адреса этого компьютера и локальной сети открывать нельзя") from None
        return url.strip()
    if not ip.is_global:
        raise WebError("Адреса этого компьютера и локальной сети открывать нельзя")
    return url.strip()


# --- разбор выдачи ------------------------------------------------------------------------------------


def decode_ddg_url(href: str) -> str:
    """Ссылка из выдачи DuckDuckGo («//duckduckgo.com/l/?uddg=…») → настоящий адрес."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href


def decode_bing_url(href: str) -> str:
    """Ссылка-переходник Bing News (apiclick.aspx?…&url=…) → настоящий адрес."""
    parsed = urlparse(href)
    if parsed.netloc.endswith("bing.com"):
        target = parse_qs(parsed.query).get("url")
        if target:
            return target[0]
    return href


def _dedupe(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = item["url"].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def parse_ddg(html: str, limit: int = 10) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    for result in soup.select("div.result, div.web-result"):
        classes = result.get("class") or []
        if "result--ad" in classes or result.select_one(".badge--ad"):
            continue
        link = result.select_one("a.result__a")
        if link is None:
            continue
        url = decode_ddg_url(str(link.get("href") or ""))
        if not url.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in url:
            continue
        snippet = result.select_one(".result__snippet")
        items.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": url,
                "snippet": snippet.get_text(" ", strip=True) if snippet else "",
            }
        )
    return _dedupe(items, limit)


def _html_text(fragment: str) -> str:
    if "<" not in fragment:
        return " ".join(fragment.split())
    return BeautifulSoup(fragment, "html.parser").get_text(" ", strip=True)


def parse_rss(xml_text: str, limit: int = 10, *, news: bool = False) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise WebError("Поиск вернул непонятный ответ") from e
    items: list[dict[str, Any]] = []
    for node in root.iter("item"):
        url = (node.findtext("link") or "").strip()
        if news:
            url = decode_bing_url(url)
        if not url.startswith(("http://", "https://")):
            continue
        item: dict[str, Any] = {
            "title": " ".join((node.findtext("title") or "").split()),
            "url": url,
            "snippet": _html_text(node.findtext("description") or ""),
        }
        published = (node.findtext("pubDate") or "").strip()
        if published:
            item["published"] = published
        source = next((child.text for child in node if child.tag.endswith("Source") and child.text), "")
        if source:
            item["source"] = source.strip()
        items.append(item)
    return _dedupe(items, limit)


def extract_readable(data: bytes, charset: str | None, content_type: str = "") -> tuple[str, str]:
    """HTML → (заголовок, читаемый текст без меню, скриптов и подвалов)."""
    if "html" not in content_type and "xml" not in content_type and content_type:
        text = data.decode(charset or "utf-8", "replace")
        return "", _tidy(text)
    soup = BeautifulSoup(data, "html.parser", from_encoding=charset or None)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for tag in soup.select(
        '[role="navigation"], [role="banner"], [role="contentinfo"], [aria-hidden="true"]'
    ):
        tag.decompose()
    candidates = [soup.find("article"), soup.find("main"), soup.find(attrs={"role": "main"})]
    root = None
    for candidate in candidates:
        if candidate is not None and len(candidate.get_text(" ", strip=True)) >= 300:
            root = candidate
            break
    if root is None:
        root = soup.body or soup
    return title, _tidy(root.get_text("\n", strip=True))


def _tidy(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)


def weather_description(code: Any) -> str:
    try:
        return WEATHER_CODES.get(int(code), "")
    except (TypeError, ValueError):
        return ""


# --- клиент -----------------------------------------------------------------------------------------


class WebClient:
    """HTTP-запросы инструментов ИИ. transport — подмена сети в тестах (httpx.MockTransport)."""

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.transport: httpx.AsyncBaseTransport | None = None
        self.timeout = 12.0

    def _client(self, *, follow_redirects: bool = True) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout, connect=8.0),
            "headers": BROWSER_HEADERS,
            "follow_redirects": follow_redirects,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    # --- поиск ------------------------------------------------------------------------------------------
    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Поиск в интернете: [{title, url, snippet}]. DuckDuckGo, при неудаче — Bing."""
        query = " ".join(query.split())
        if not query:
            raise WebError("Пустой поисковый запрос")
        limit = max(1, min(int(max_results or 5), 10))
        errors: list[str] = []
        async with self._client() as client:
            for engine in (self._ddg, self._bing):
                try:
                    results = await engine(client, query, limit)
                except (httpx.HTTPError, WebError) as e:
                    errors.append(f"{engine.__name__}: {e!r}")
                    continue
                if results:
                    return results
        if errors:
            log.info("поиск «%s» не удался: %s", query, "; ".join(errors))
            if len(errors) == 2:
                raise WebError("Поиск в интернете сейчас недоступен — нет связи с поисковиками")
        return []

    async def _ddg(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
        response = await client.get(DDG_URL, params={"q": query, "kl": "ru-ru"})
        if response.status_code != 200 or "anomaly" in response.text[:20000].lower():
            raise WebError(f"DuckDuckGo ответил {response.status_code}")
        return parse_ddg(response.text, limit)

    async def _bing(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, Any]]:
        response = await client.get(BING_RSS_URL, params={"format": "rss", "q": query, "setlang": "ru"})
        if response.status_code != 200:
            raise WebError(f"Bing ответил {response.status_code}")
        return parse_rss(response.text, limit)

    async def news(self, topic: str = "", limit: int = 8) -> list[dict[str, Any]]:
        """Свежие новости (Bing News RSS); при неудаче — обычный поиск «… новости»."""
        query = " ".join(topic.split()) or "главные новости"
        try:
            async with self._client() as client:
                response = await client.get(
                    BING_NEWS_URL, params={"format": "rss", "q": query, "setlang": "ru", "cc": "ru"}
                )
            if response.status_code == 200:
                items = parse_rss(response.text, limit, news=True)
                if items:
                    return items
        except (httpx.HTTPError, WebError) as e:
            log.info("новости через Bing не получены: %r", e)
        return await self.search(f"{query} новости", limit)

    # --- страницы ------------------------------------------------------------------------------------
    async def fetch_page(self, url: str, limit: int = PAGE_LIMIT) -> dict[str, Any]:
        """Текст страницы: {title, url, text, truncated}. Переходы проверяются так же, как исходная ссылка."""
        current = check_public_url(url)
        async with self._client(follow_redirects=False) as client:
            for _ in range(6):
                async with client.stream("GET", current) as response:
                    if response.is_redirect and response.headers.get("location"):
                        current = check_public_url(urljoin(current, response.headers["location"]))
                        continue
                    if response.status_code >= 400:
                        raise WebError(f"Страница ответила ошибкой {response.status_code}")
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not any(t in content_type for t in ("html", "text", "xml", "json")):
                        raise WebError(f"По ссылке не текст, а {content_type.split(';')[0]}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_PAGE_BYTES:
                            break
                    charset = response.charset_encoding
                    final_url = str(response.url)
                break
            else:
                raise WebError("Слишком много переадресаций")
        title, text = await self.ctx.run_io(extract_readable, bytes(body), charset, content_type)
        return {
            "title": title,
            "url": final_url,
            "text": text[:limit],
            "truncated": len(text) > limit,
        }

    # --- погода и курсы ------------------------------------------------------------------------------
    async def weather(self, city: str, days: int = 1) -> dict[str, Any]:
        """Погода Open-Meteo: сейчас и прогноз на days дней."""
        days = max(1, min(int(days or 1), 7))
        async with self._client() as client:
            geo = await client.get(
                GEOCODE_URL, params={"name": city, "count": 1, "language": "ru", "format": "json"}
            )
            geo.raise_for_status()
            places = geo.json().get("results") or []
            if not places:
                raise WebError(f"Город «{city}» не найден")
            place = places[0]
            forecast = await client.get(
                FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                    "wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": days,
                    "wind_speed_unit": "ms",
                },
            )
            forecast.raise_for_status()
            data = forecast.json()
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        out_days = []
        for i, day in enumerate(daily.get("time") or []):

            def pick(key: str, index: int = i) -> Any:
                values = daily.get(key) or []
                return values[index] if index < len(values) else None

            out_days.append(
                {
                    "date": day,
                    "description": weather_description(pick("weather_code")),
                    "temp_min": pick("temperature_2m_min"),
                    "temp_max": pick("temperature_2m_max"),
                    "precipitation_probability": pick("precipitation_probability_max"),
                }
            )
        name = ", ".join(p for p in (place.get("name"), place.get("country")) if p)
        return {
            "city": name,
            "current": {
                "temperature": current.get("temperature_2m"),
                "feels_like": current.get("apparent_temperature"),
                "description": weather_description(current.get("weather_code")),
                "humidity": current.get("relative_humidity_2m"),
                "wind_ms": current.get("wind_speed_10m"),
            },
            "days": out_days,
            "units": "°C, ветер — м/с",
            "source": "Open-Meteo",
        }

    async def rates(self) -> dict[str, Any]:
        """Курсы ЦБ РФ: доллар, евро, юань (рублей за единицу) и изменение за день."""
        async with self._client() as client:
            response = await client.get(CBR_URL)
            response.raise_for_status()
            data = response.json()
        valutes = data.get("Valute") or {}
        rates: dict[str, Any] = {}
        for code in ("USD", "EUR", "CNY", "GBP", "TRY", "KZT", "BYN"):
            item = valutes.get(code)
            if not isinstance(item, dict):
                continue
            nominal = item.get("Nominal") or 1
            value = item.get("Value")
            previous = item.get("Previous")
            if not isinstance(value, int | float):
                continue
            entry: dict[str, Any] = {"rub": round(value / nominal, 4)}
            if isinstance(previous, int | float):
                entry["change"] = round((value - previous) / nominal, 4)
            rates[code] = entry
        return {"date": str(data.get("Date") or "")[:10], "base": "RUB", "rates": rates, "source": "ЦБ РФ"}


def site_name(url: str) -> str:
    """«https://www.rbc.ru/news/…» → «rbc.ru» (подпись источника без заголовка)."""
    with contextlib.suppress(ValueError):
        return re.sub(r"^www\.", "", urlparse(url).hostname or "")
    return ""
