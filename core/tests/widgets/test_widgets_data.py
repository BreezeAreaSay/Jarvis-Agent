"""Виджеты: разбор ответов внешних сервисов (без сети — httpx.MockTransport), кэш, данные платформы, API."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.platform.base import MediaInfo
from jarvis.widgets import news, weather
from jarvis.widgets.media import media_action
from jarvis.widgets.service import Widgets
from tests.conftest import EventLog

GEO = {
    "results": [
        {
            "id": 524901,
            "name": "Москва",
            "latitude": 55.75222,
            "longitude": 37.61556,
            "country": "Россия",
            "admin1": "Москва",
            "timezone": "Europe/Moscow",
        }
    ],
    "generationtime_ms": 0.6,
}
FORECAST = {
    "latitude": 55.75,
    "longitude": 37.625,
    "timezone": "Europe/Moscow",
    "current": {
        "time": "2026-10-18T12:00",
        "temperature_2m": 14.4,
        "apparent_temperature": 12.6,
        "weather_code": 0,
        "wind_speed_10m": 3.24,
        "relative_humidity_2m": 63,
        "is_day": 1,
    },
    "daily": {
        "time": ["2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"],
        "weather_code": [0, 3, 61, 71, 95],
        "temperature_2m_max": [15.2, 13.0, 10.5, 2.4, 8.0],
        "temperature_2m_min": [6.1, 5.5, 4.4, -1.5, 3.0],
    },
}
CBR = {
    "Date": "2026-10-18T11:30:00+03:00",
    "PreviousDate": "2026-10-17T11:30:00+03:00",
    "Valute": {
        "USD": {"CharCode": "USD", "Nominal": 1, "Name": "Доллар США", "Value": 81.4215, "Previous": 81.1093},
        "EUR": {"CharCode": "EUR", "Nominal": 1, "Name": "Евро", "Value": 94.5, "Previous": 95.0},
        "CNY": {"CharCode": "CNY", "Nominal": 1, "Name": "Китайский юань", "Value": 11.2, "Previous": 11.2},
        "KZT": {
            "CharCode": "KZT",
            "Nominal": 100,
            "Name": "Казахстанских тенге",
            "Value": 15.9,
            "Previous": 15.8,
        },
        "GBP": {
            "CharCode": "GBP",
            "Nominal": 1,
            "Name": "Фунт стерлингов",
            "Value": 108.0,
            "Previous": 107.5,
        },
    },
}
COINGECKO = {
    "bitcoin": {"usd": 67187.33, "usd_24h_change": -1.234, "rub": 5470000.0, "rub_24h_change": -1.1},
    "ethereum": {"usd": 3500.5, "usd_24h_change": 2.5, "rub": 285000.0, "rub_24h_change": 2.6},
    "the-open-network": {"usd": 5.2, "usd_24h_change": 0.4, "rub": 423.0, "rub_24h_change": 0.5},
    "solana": {"usd": 150.0, "usd_24h_change": 3.0, "rub": 12200.0, "rub_24h_change": 3.1},
}
MOEX = {
    "securities": {
        "columns": ["SECID", "BOARDID", "SHORTNAME", "PREVPRICE", "STATUS", "PREVLEGALCLOSEPRICE"],
        "data": [
            ["SBER", "TQBR", "Сбербанк", 300.0, "A", 300.0],
            ["GAZP", "TQBR", "ГАЗПРОМ ао", 130.0, "A", 130.0],
            ["TCSG", "TQBR", "ТКС Холдинг", 2500.0, "N", 2500.0],
            ["YDEX", "TQBR", "Яндекс", 4000.0, "A", None],
        ],
    },
    "marketdata": {
        "columns": ["SECID", "BOARDID", "LAST", "LASTTOPREVPRICE", "TRADINGSTATUS"],
        "data": [
            ["SBER", "TQBR", 306.0, 2.0, "T"],
            ["GAZP", "TQBR", None, None, "N"],
            ["TCSG", "TQBR", None, None, "N"],
            ["YDEX", "TQBR", 3960.0, -1.0, "T"],
        ],
    },
}
GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Главное</title>
<item><title>Курс доллара упал - РИА Новости</title><link>https://news.google.com/rss/articles/a1</link>
<pubDate>Sat, 18 Oct 2026 08:15:00 GMT</pubDate><source url="https://ria.ru">РИА Новости</source></item>
<item><title>Запуск ракеты перенесли - ТАСС</title><link>https://news.google.com/rss/articles/a2</link>
<pubDate>Sat, 18 Oct 2026 09:15:00 GMT</pubDate><source url="https://tass.ru">ТАСС</source></item>
</channel></rss>"""
LENTA_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Lenta.ru : Новости</title>
<item><guid>https://lenta.ru/news/1/</guid><title>Новость Ленты &amp; подробности</title>
<link>https://lenta.ru/news/2026/10/18/one/</link><pubDate>Sat, 18 Oct 2026 11:22:00 +0300</pubDate>
<category>Мир</category></item>
</channel></rss>"""


Handler = Callable[[httpx.Request], httpx.Response]


class FakeWeb:
    """Поддельный интернет: (хост, путь) → ответ; запросы записываются."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Handler] = {}

    def json(self, host: str, path: str, data: Any, status: int = 200) -> None:
        self.routes[(host, path)] = lambda _r: httpx.Response(status, json=data)

    def text(self, host: str, path: str, body: str, status: int = 200) -> None:
        self.routes[(host, path)] = lambda _r: httpx.Response(status, text=body)

    def down(self, host: str, path: str) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("нет сети", request=request)

        self.routes[(host, path)] = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.url.host, request.url.path))
        if route is None:
            raise httpx.ConnectError("нет маршрута", request=request)
        return route(request)

    def count(self, host: str) -> int:
        return sum(1 for r in self.requests if r.url.host == host)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def web(ctx: AppContext) -> FakeWeb:
    fake = FakeWeb()
    widgets = ctx.widgets
    assert isinstance(widgets, Widgets)
    widgets.transport = httpx.MockTransport(fake.handler)
    return fake


@pytest.fixture
def clock(ctx: AppContext) -> Clock:
    fake = Clock()
    widgets = ctx.widgets
    assert isinstance(widgets, Widgets)
    widgets.clock = fake
    return fake


GEO_HOST = "geocoding-api.open-meteo.com"
METEO_HOST = "api.open-meteo.com"


def _weather_routes(web: FakeWeb) -> None:
    web.json(GEO_HOST, "/v1/search", GEO)
    web.json(METEO_HOST, "/v1/forecast", FORECAST)


# --- погода -------------------------------------------------------------------------------------------


async def test_weather_parses_and_caches(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    _weather_routes(web)
    data = await ctx.widgets.data("weather", {})
    assert data["city"] == "Москва"
    assert data["temp"] == 14 and data["feels"] == 13
    assert data["condition"] == "ясно" and data["icon"] == "sun" and data["is_day"] is True
    assert data["humidity"] == 63 and data["wind"] == 3.2
    assert len(data["daily"]) == 5
    assert data["daily"][2] == {
        "date": "2026-10-20",
        "weekday": "вт",
        "min": 4,
        "max": 11,
        "code": 61,
        "condition": "небольшой дождь",
        "icon": "rain",
    }
    assert data["daily"][3]["min"] == -1 and data["daily"][3]["icon"] == "snow"
    assert data["daily"][4]["icon"] == "storm"
    assert "stale" not in data
    forecast_request = next(r for r in web.requests if r.url.host == METEO_HOST)
    assert forecast_request.url.params["current"].startswith("temperature_2m,apparent_temperature")
    assert forecast_request.url.params["forecast_days"] == "5"
    geocode_request = next(r for r in web.requests if r.url.host == GEO_HOST)
    assert geocode_request.url.params["name"] == "Москва" and geocode_request.url.params["language"] == "ru"

    # повтор — из кэша, без запросов
    await ctx.widgets.data("weather", {})
    assert web.count(METEO_HOST) == 1 and web.count(GEO_HOST) == 1
    # через 10 минут прогноз обновляется, а координаты города уже известны
    clock.now += 601
    await ctx.widgets.data("weather", {})
    assert web.count(METEO_HOST) == 2 and web.count(GEO_HOST) == 1


async def test_weather_uses_profile_city_and_param(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    _weather_routes(web)
    ctx.settings.patch({"profile": {"city": "Казань"}})
    await ctx.widgets.data("weather", {})
    assert web.requests[0].url.params["name"] == "Казань"
    await ctx.widgets.data("weather", {"city": "Сочи"})
    geocoded = [r.url.params["name"] for r in web.requests if r.url.host == GEO_HOST]
    assert geocoded == ["Казань", "Сочи"]


async def test_weather_city_not_found(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.json(GEO_HOST, "/v1/search", {"generationtime_ms": 0.4})
    data = await ctx.widgets.data("weather", {"city": "Неизвестноград"})
    assert data == {"error": "Город «Неизвестноград» не найден"}


async def test_weather_offline_returns_error_then_stale(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.down(GEO_HOST, "/v1/search")
    data = await ctx.widgets.data("weather", {})
    assert "error" in data and "Погода" in data["error"]

    _weather_routes(web)
    clock.now += 60  # ошибка помнится 30 с
    fresh = await ctx.widgets.data("weather", {})
    assert fresh["temp"] == 14

    web.down(METEO_HOST, "/v1/forecast")
    clock.now += 601
    stale = await ctx.widgets.data("weather", {})
    assert stale["temp"] == 14 and stale["stale"] is True


def test_wmo_codes_cover_all_documented() -> None:
    documented = {
        0,
        1,
        2,
        3,
        45,
        48,
        51,
        53,
        55,
        56,
        57,
        61,
        63,
        65,
        66,
        67,
        71,
        73,
        75,
        77,
        80,
        81,
        82,
        85,
        86,
    }
    documented |= {95, 96, 99}
    assert documented <= set(weather.WMO)
    icons = {icon for _, icon in weather.WMO.values()}
    assert icons == {"sun", "partly", "cloud", "fog", "rain", "snow", "storm"}
    assert weather.describe(None) == ("", "cloud")
    assert weather.round_half_up(14.5) == 15 and weather.round_half_up(-0.4) == 0


# --- финансы ------------------------------------------------------------------------------------------


async def test_rates_per_unit_and_delta(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.json("www.cbr-xml-daily.ru", "/daily_json.js", CBR)
    data = await ctx.widgets.data("rates", {})
    codes = [item["code"] for item in data["items"]]
    assert codes == ["USD", "EUR", "CNY", "GBP", "KZT"]
    usd = data["items"][0]
    assert usd["value"] == 81.4215 and usd["delta"] == pytest.approx(0.3122) and usd["symbol"] == "$"
    assert usd["delta_pct"] == pytest.approx(0.38)
    kzt = data["items"][-1]
    assert kzt["value"] == pytest.approx(0.159) and kzt["nominal"] == 100 and kzt["name"] == "Тенге"
    assert data["date"] == "2026-10-18" and data["base"] == "RUB"

    only = await ctx.widgets.data("rates", {"codes": "kzt,usd,xxx"})
    assert [item["code"] for item in only["items"]] == ["KZT", "USD"]
    everything = await ctx.widgets.data("rates", {"codes": "all"})
    assert len(everything["items"]) == 5
    assert web.count("www.cbr-xml-daily.ru") == 1  # курсы ЦБ кэшируются на час
    clock.now += 3601
    await ctx.widgets.data("rates", {})
    assert web.count("www.cbr-xml-daily.ru") == 2


async def test_crypto(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.json("api.coingecko.com", "/api/v3/simple/price", COINGECKO)
    data = await ctx.widgets.data("crypto", {})
    assert [c["symbol"] for c in data["items"]] == ["BTC", "ETH", "TON", "SOL"]
    btc = data["items"][0]
    assert btc["usd"] == 67187.33 and btc["rub"] == 5470000.0 and btc["change_24h"] == -1.23
    request = web.requests[0]
    assert request.url.params["vs_currencies"] == "usd,rub"
    assert request.url.params["include_24hr_change"] == "true"

    picked = await ctx.widgets.data("crypto", {"ids": "btc, ton"})
    assert [c["id"] for c in picked["items"]] == ["bitcoin", "the-open-network"]
    assert web.requests[-1].url.params["ids"] == "bitcoin,the-open-network"
    clock.now += 60
    await ctx.widgets.data("crypto", {})
    assert web.count("api.coingecko.com") == 2  # TTL 2 минуты


async def test_crypto_rate_limited_is_error(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.json("api.coingecko.com", "/api/v3/simple/price", {"status": {"error_code": 429}}, status=429)
    data = await ctx.widgets.data("crypto", {})
    assert "слишком много запросов" in data["error"]


async def test_stocks(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.json("iss.moex.com", "/iss/engines/stock/markets/shares/boards/TQBR/securities.json", MOEX)
    data = await ctx.widgets.data("stocks", {"tickers": "SBER,GAZP,TCSG,YDEX"})
    by_ticker = {item["ticker"]: item for item in data["items"]}
    assert set(by_ticker) == {"SBER", "GAZP", "YDEX"}  # TCSG не торгуется
    assert by_ticker["SBER"]["last"] == 306.0 and by_ticker["SBER"]["change_pct"] == 2.0
    assert by_ticker["SBER"]["trading"] is True and by_ticker["SBER"]["name"] == "Сбербанк"
    assert by_ticker["GAZP"]["last"] == 130.0 and by_ticker["GAZP"]["change_pct"] == 0.0
    assert by_ticker["YDEX"]["change_pct"] == -1.0
    request = web.requests[0]
    assert request.url.params["securities"] == "SBER,GAZP,TCSG,YDEX"
    assert request.url.params["iss.only"] == "marketdata,securities"


# --- новости ------------------------------------------------------------------------------------------


def test_parse_rss_google_strips_source() -> None:
    items = news.parse_rss(GOOGLE_RSS, "", strip_source_suffix=True)
    assert [i["title"] for i in items] == ["Запуск ракеты перенесли", "Курс доллара упал"]
    assert items[0]["source"] == "ТАСС" and items[0]["url"].endswith("/a2")
    assert items[0]["ts"] > items[1]["ts"] > 0


def test_parse_rss_rejects_entities_and_garbage() -> None:
    with pytest.raises(ValueError):
        news.parse_rss('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>')
    with pytest.raises(ValueError):
        news.parse_rss("<rss><channel>")


async def test_news_google_then_lenta_fallback(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.text("news.google.com", "/rss", "Service Unavailable", status=503)
    web.text("lenta.ru", "/rss/news", LENTA_RSS)
    data = await ctx.widgets.data("news", {"limit": "5"})
    assert data["provider"] == "lenta"
    assert data["items"][0]["title"] == "Новость Ленты & подробности"
    assert data["items"][0]["source"] == "Lenta.ru"

    # Google был недоступен — следующий раз сначала Lenta.ru (запрос к Google не повторяется)
    clock.now += 901
    google_before = web.count("news.google.com")
    await ctx.widgets.data("news", {})
    assert web.count("news.google.com") == google_before


async def test_news_topic_uses_google_search(ctx: AppContext, web: FakeWeb, clock: Clock) -> None:
    web.text("news.google.com", "/rss/search", GOOGLE_RSS)
    data = await ctx.widgets.data("news", {"topic": "экономика", "limit": "1"})
    assert data["provider"] == "google" and data["topic"] == "экономика"
    assert len(data["items"]) == 1 and data["items"][0]["title"] == "Запуск ракеты перенесли"
    request = web.requests[-1]
    assert request.url.params["q"] == "экономика" and request.url.params["hl"] == "ru"

    web.text(
        "news.google.com", "/rss/search", '<rss version="2.0"><channel><title>Пусто</title></channel></rss>'
    )
    empty = await ctx.widgets.data("news", {"topic": "квазары на марсе"})
    assert empty["items"] == [] and "error" not in empty


# --- компьютер ----------------------------------------------------------------------------------------


async def test_platform_widgets(ctx: AppContext) -> None:
    system = await ctx.widgets.data("system", {})
    assert {"cpu", "ram", "gpu", "updated"} <= set(system)

    media = await ctx.widgets.data("media", {})
    assert media["title"] == "Night Drive" and media["artist"] == "Neon Coast"
    assert media["active"] is True and media["volume"] == 40 and media["muted"] is False

    assert await ctx.widgets.data("volume", {}) == {"volume": 40, "muted": False}

    recent = await ctx.widgets.data("recent", {"limit": "2"})
    assert [f["name"] for f in recent["items"]] == ["Отчёт.docx", "Бюджет.xlsx"]

    apps = await ctx.widgets.data("apps", {})
    pinned = [a["name"] for a in apps["pinned"]]
    assert pinned[:3] == ["Google Chrome", "Яндекс Браузер", "Telegram"]
    assert apps["pinned"][0]["icon"] == "chrome"
    assert apps["total"] == len(apps["items"]) > 10

    chosen = await ctx.widgets.data("apps", {"pinned": "steam, obs studio,Нет такой"})
    assert [a["name"] for a in chosen["pinned"]] == ["Steam", "OBS Studio", "Нет такой"]
    assert chosen["pinned"][1]["icon"] == "obs" and chosen["pinned"][2]["missing"] is True


async def test_history_widget(ctx: AppContext) -> None:
    for i in range(3):
        ctx.db.insert(
            "history",
            {"ts": float(i), "source": "voice", "text": f"фраза {i}", "kind": "command", "status": "ok"},
        )
    data = await ctx.widgets.data("history", {"limit": "2"})
    assert [row["text"] for row in data["items"]] == ["фраза 2", "фраза 1"]


async def test_smarthome_without_plugin_is_error(ctx: AppContext) -> None:
    data = await ctx.widgets.data("smarthome", {})
    assert set(data) == {"error"} and data["error"]


async def test_smarthome_delegates_to_plugin(ctx: AppContext) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def action(plugin_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((plugin_id, action, args))
        return {"ok": True, "devices": [{"id": "light.kitchen", "name": "Свет на кухне", "on": True}]}

    ctx.plugins.action = action  # type: ignore[method-assign]
    data = await ctx.widgets.data("smarthome", {})
    assert data["devices"][0]["name"] == "Свет на кухне"
    assert calls == [("smart_home", "states", {})]


async def test_unknown_kind_raises(ctx: AppContext) -> None:
    from jarvis.services import UserFacingError

    with pytest.raises(UserFacingError):
        await ctx.widgets.data("nope", {})


# --- медиа --------------------------------------------------------------------------------------------


async def test_media_actions(ctx: AppContext) -> None:
    platform: Any = ctx.platform
    assert await media_action(ctx, "next") == {"ok": True, "action": "next"}
    assert ("media_key", ("next",)) in platform.calls
    up = await media_action(ctx, "volume_up")
    assert up["volume"] == 50 and up["muted"] is False
    muted = await media_action(ctx, "mute")
    assert muted["muted"] is True
    back = await media_action(ctx, "volume_up", step=5)
    assert back["volume"] == 55 and back["muted"] is False  # громче — значит со звуком
    assert (await media_action(ctx, "volume", level=12))["volume"] == 12
    with pytest.raises(Exception, match="Неизвестное"):
        await media_action(ctx, "explode")


async def test_media_watcher_publishes_changes(ctx: AppContext, events: EventLog) -> None:
    widgets = ctx.widgets
    assert isinstance(widgets, Widgets)
    watcher = widgets.watcher
    platform: Any = ctx.platform

    await watcher.poll_once()
    assert events.of("media.changed")[-1]["title"] == "Night Drive"
    await watcher.poll_once()
    assert len(events.of("media.changed")) == 1  # ничего не изменилось — тихо

    platform.media.position_s += 1  # позиция идёт сама — не событие
    await watcher.poll_once()
    assert len(events.of("media.changed")) == 1

    platform.media.position_s = 150  # перемотка — событие
    await watcher.poll_once()
    assert len(events.of("media.changed")) == 2

    platform.media = MediaInfo(title="Starlight", artist="Muse", status="playing", duration_s=240)
    await watcher.poll_once()
    assert events.of("media.changed")[-1]["title"] == "Starlight"

    platform.media = None
    platform.now_playing = lambda: None
    await watcher.poll_once()
    assert events.of("media.changed")[-1]["status"] == "stopped"

    platform.is_fullscreen_app = lambda: True
    await watcher.poll_once()
    await watcher.poll_once()
    assert events.of("fullscreen.changed") == [{"fullscreen": True}]
    platform.is_fullscreen_app = lambda: False
    await watcher.poll_once()
    assert events.of("fullscreen.changed")[-1] == {"fullscreen": False}


async def test_media_watcher_runs_in_background(running_ctx: AppContext) -> None:
    seen: list[dict[str, Any]] = []
    running_ctx.bus.on("media.changed", lambda e: seen.append(e.data))
    widgets = running_ctx.widgets
    assert isinstance(widgets, Widgets)
    widgets.watcher._signature = None  # заставим опубликовать текущее состояние
    widgets.refresh_media_soon()
    for _ in range(40):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert seen and seen[-1]["title"] == "Night Drive"


# --- API ----------------------------------------------------------------------------------------------


def test_api_widgets_and_media(client: TestClient) -> None:
    ctx: AppContext = client.ctx  # type: ignore[attr-defined]
    platform: Any = ctx.platform
    system = client.get("/api/widgets/system")
    assert system.status_code == 200 and "cpu" in system.json()
    assert client.get("/api/widgets/recent?limit=1").json()["items"][0]["name"] == "Отчёт.docx"
    assert client.get("/api/widgets/nope").status_code == 404

    assert client.post("/api/media/next").json() == {"ok": True, "action": "next"}
    assert ("media_key", ("next",)) in platform.calls
    assert client.post("/api/media/volume", json={"level": 30}).json()["volume"] == 30
    assert client.post("/api/media/volume").status_code == 400
    assert client.post("/api/media/dance").status_code == 404

    launched = client.post("/api/widgets/apps/launch", json={"app_id": "Spotify.exe"})
    assert launched.json() == {"ok": True} and ("launch", ("Spotify.exe", "")) in platform.calls
    opened = client.post("/api/widgets/quick/run", json={"url": "https://www.youtube.com"})
    assert opened.json() == {"ok": True} and ("open_url", ("https://www.youtube.com",)) in platform.calls
    assert client.post("/api/widgets/quick/run", json={"url": "file:///etc/passwd"}).status_code == 400
    assert client.post("/api/widgets/weather/explode", json={}).status_code == 400


def test_api_weather_with_mock_transport(client: TestClient) -> None:
    ctx: AppContext = client.ctx  # type: ignore[attr-defined]
    web = FakeWeb()
    _weather_routes(web)
    widgets = ctx.widgets
    assert isinstance(widgets, Widgets)
    widgets.transport = httpx.MockTransport(web.handler)
    data = client.get("/api/widgets/weather", params={"city": "Москва"}).json()
    assert data["temp"] == 14 and data["city"] == "Москва"
