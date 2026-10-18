"""Данные виджетов главного экрана: погода, курсы, крипта, акции, новости, система, медиа, программы…

data(kind, params) не бросает исключений из-за сети: при сбое вернёт устаревшие данные с "stale": true или
{"error": "текст для человека"}. Внешние сервисы кэшируются (TTL на каждый вид), система и медиа — нет.
В фоне MediaWatcher публикует media.changed и fullscreen.changed (см. jarvis.widgets.media).
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
from rapidfuzz import fuzz

from jarvis import __version__
from jarvis.platform.base import AppInfo, MediaInfo, PlatformError
from jarvis.service_base import BaseService
from jarvis.services import UserFacingError
from jarvis.text import normalize
from jarvis.widgets import demo, finance, news, weather
from jarvis.widgets.cache import TtlCache, WidgetError
from jarvis.widgets.media import MediaWatcher, media_action

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

KINDS = (
    "weather",
    "rates",
    "crypto",
    "stocks",
    "news",
    "system",
    "media",
    "volume",
    "recent",
    "apps",
    "history",
    "smarthome",
)

TTL = {
    "weather": 600.0,
    "rates": 3600.0,
    "crypto": 120.0,
    "stocks": 120.0,
    "news": 900.0,
}
GOOGLE_RETRY_S = 1800.0  # Google Новости не открылись — полчаса сначала спрашиваем Lenta.ru
USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jarvis/{__version__}"

# Программы на виджете, если человек не выбрал свои (берутся те, что установлены)
DEFAULT_PINNED_APPS = (
    "Google Chrome",
    "Яндекс Браузер",
    "Telegram",
    "Spotify",
    "Steam",
    "Discord",
    "Visual Studio Code",
    "Проводник",
)
APP_ICONS = (  # (часть имени или exe, ключ иконки)
    ("spotify", "spotify"),
    ("chrome", "chrome"),
    ("telegram", "telegram"),
    ("discord", "discord"),
    ("obs studio", "obs"),
    ("obs64", "obs"),
    ("steam", "steam"),
    ("youtube", "youtube"),
    ("яндекс", "yandex"),
    ("yandex", "yandex"),
    ("browser.exe", "yandex"),
    ("photoshop", "photoshop"),
    ("visual studio code", "vscode"),
    ("code.exe", "vscode"),
    ("minecraft", "minecraft"),
    ("проводник", "explorer"),
    ("explorer.exe", "explorer"),
    ("vk", "vk"),
)


def app_icon(app: AppInfo) -> str:
    haystack = f"{app.name} {app.app_id}".casefold()
    for needle, icon in APP_ICONS:
        if needle == "vk":
            if "vk " in f"{haystack} " or haystack.startswith("вконтакте"):
                return icon
            continue
        if needle in haystack:
            return icon
    return ""


def _split(raw: Any) -> list[str]:
    if isinstance(raw, list | tuple):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [part.strip() for part in str(raw or "").replace(";", ",").split(",") if part.strip()]


def _int_param(params: dict[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(params.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _truthy(value: Any) -> bool:
    return str(value).strip().casefold() in ("1", "true", "yes", "да", "on")


class Widgets(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self.cache = TtlCache()
        self.watcher = MediaWatcher(ctx)
        self._transport: httpx.AsyncBaseTransport | None = None
        self._client: httpx.AsyncClient | None = None
        self._google_failed_at: float | None = None
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "weather": self._weather,
            "rates": self._rates,
            "crypto": self._crypto,
            "stocks": self._stocks,
            "news": self._news,
            "system": self._system,
            "media": self._media,
            "volume": self._volume,
            "recent": self._recent,
            "apps": self._apps,
            "history": self._history,
            "smarthome": self._smarthome,
        }

    # --- жизненный цикл ---------------------------------------------------------------------------
    async def start(self) -> None:
        self.watcher.start()

    async def stop(self) -> None:
        await self.watcher.stop()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @property
    def clock(self) -> Callable[[], float]:
        return self.cache.clock

    @clock.setter
    def clock(self, value: Callable[[], float]) -> None:
        """Часы кэша (тесты подставляют свои, чтобы проверить TTL)."""
        self.cache.clock = value

    @property
    def transport(self) -> httpx.AsyncBaseTransport | None:
        return self._transport

    @transport.setter
    def transport(self, value: httpx.AsyncBaseTransport | None) -> None:
        """Транспорт HTTP (в тестах — httpx.MockTransport); клиент пересоздаётся при следующем запросе."""
        self._transport = value
        self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(10.0, connect=6.0),
                headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"},
                follow_redirects=True,
            )
        return self._client

    def refresh_media_soon(self) -> None:
        self.watcher.poke()

    # --- главный вход -----------------------------------------------------------------------------
    async def data(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(kind)
        if handler is None:
            raise UserFacingError(f"Неизвестный виджет: {kind}")
        if demo.enabled() and kind in demo.DEMO:
            return demo.DEMO[kind](dict(params or {}))
        try:
            return await handler(dict(params or {}))
        except WidgetError as e:
            return {"error": str(e)}
        except (PlatformError, UserFacingError) as e:
            return {"error": str(e)}
        except Exception:
            log.exception("виджет %s: данные не получены", kind)
            return {"error": "Не удалось получить данные"}

    async def action(self, kind: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """Действие из виджета: запустить программу, открыть файл, быструю команду, кнопку умного дома."""
        ctx = self.ctx
        if kind == "media":
            return await media_action(ctx, action, level=args.get("level"), step=args.get("step") or 10)
        if kind == "apps" and action == "launch":
            target = str(args.get("app_id") or args.get("name") or "").strip()
            if not target:
                raise UserFacingError("Не указано, какую программу запустить")
            await ctx.run_platform(ctx.platform.launch, target)
            return {"ok": True}
        if kind == "recent" and action == "open":
            path = str(args.get("path") or "").strip()
            if not path:
                raise UserFacingError("Не указан файл")
            await ctx.run_platform(ctx.platform.open_path, path)
            return {"ok": True}
        if kind == "quick" and action == "run":
            url = str(args.get("url") or "").strip()
            if url:
                if not url.startswith(("http://", "https://")):
                    raise UserFacingError("Ссылка должна начинаться с http:// или https://")
                await ctx.run_platform(ctx.platform.open_url, url)
                return {"ok": True}
            text = str(args.get("text") or "").strip()
            if not text:
                raise UserFacingError("Пустая команда")
            return await ctx.dispatcher.handle(text, "ui")
        if kind == "smarthome":
            return await ctx.plugins.action("smart_home", action, dict(args))
        raise UserFacingError(f"Виджет «{kind}» не умеет «{action}»")

    # --- сеть -------------------------------------------------------------------------------------
    async def _get(self, url: str, params: dict[str, Any] | None, service: str) -> httpx.Response:
        try:
            response = await self._http().get(url, params=params)
        except httpx.TimeoutException as e:
            raise WidgetError(f"{service} не отвечает") from e
        except httpx.HTTPError as e:
            raise WidgetError(f"Нет связи с сервисом «{service}»") from e
        if response.status_code == 429:
            raise WidgetError(f"{service}: слишком много запросов, данные обновятся позже")
        if response.status_code >= 400:
            raise WidgetError(f"{service}: ошибка {response.status_code}")
        return response

    async def _get_json(self, url: str, params: dict[str, Any] | None, service: str) -> Any:
        response = await self._get(url, params, service)
        try:
            return response.json()
        except ValueError as e:
            raise WidgetError(f"{service} вернул непонятный ответ") from e

    @staticmethod
    def _with_stale(value: dict[str, Any], stale: bool) -> dict[str, Any]:
        out = dict(value)
        if stale:
            out["stale"] = True
        return out

    # --- погода -----------------------------------------------------------------------------------
    async def _geocode(self, city: str) -> dict[str, Any]:
        key = normalize(city)
        known: dict[str, Any] = self.ctx.db.kv_get("widgets:geo", {}) or {}
        if key in known:
            return known[key]

        async def fetch() -> dict[str, Any]:
            data = await self._get_json(weather.GEOCODE_URL, weather.geocode_params(city), "Погода")
            try:
                place = weather.parse_geocode(data)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise WidgetError("Сервис погоды вернул непонятный ответ") from e
            if place is None:
                raise WidgetError(f"Город «{city}» не найден")
            return place

        place, _ = await self.cache.get(f"geo:{key}", 30 * 86400.0, fetch)
        known = self.ctx.db.kv_get("widgets:geo", {}) or {}
        known[key] = place
        if len(known) > 50:  # не копим сотни городов
            known = dict(list(known.items())[-50:])
        self.ctx.db.kv_set("widgets:geo", known)
        return place

    async def _weather(self, params: dict[str, Any]) -> dict[str, Any]:
        city = str(params.get("city") or "").strip() or self.ctx.settings.value.profile.city or "Москва"
        lat, lon = params.get("lat"), params.get("lon")
        if lat not in (None, "") and lon not in (None, ""):
            try:
                place = {"name": city, "lat": float(lat), "lon": float(lon)}  # type: ignore[arg-type]
            except (TypeError, ValueError) as e:
                raise WidgetError("Неверные координаты") from e
        else:
            place = await self._geocode(city)

        async def fetch() -> dict[str, Any]:
            data = await self._get_json(
                weather.FORECAST_URL, weather.forecast_params(place["lat"], place["lon"]), "Погода"
            )
            try:
                return weather.parse_forecast(data, place["name"] or city)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise WidgetError("Сервис погоды вернул непонятный ответ") from e

        key = f"weather:{place['lat']:.3f},{place['lon']:.3f}"
        value, stale = await self.cache.get(key, TTL["weather"], fetch)
        out = self._with_stale(value, stale)
        out["city"] = place.get("name") or city
        out["country"] = place.get("country", "")
        return out

    # --- финансы ----------------------------------------------------------------------------------
    async def _rates(self, params: dict[str, Any]) -> dict[str, Any]:
        async def fetch() -> dict[str, Any]:
            data = await self._get_json(finance.CBR_URL, None, "Курсы ЦБ")
            try:
                parsed = finance.parse_cbr(data)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise WidgetError("Курсы ЦБ: непонятный ответ") from e
            parsed["updated"] = time.time()
            return parsed

        parsed, stale = await self.cache.get("rates", TTL["rates"], fetch)
        raw_codes = _split(params.get("codes"))
        codes = ["ALL"] if [c.upper() for c in raw_codes] == ["ALL"] else [c.upper() for c in raw_codes]
        out = finance.rates_view(parsed, codes)
        out["updated"] = parsed["updated"]
        return self._with_stale(out, stale)

    async def _crypto(self, params: dict[str, Any]) -> dict[str, Any]:
        ids = finance.coin_ids(",".join(_split(params.get("ids"))))[:20]

        async def fetch() -> dict[str, Any]:
            data = await self._get_json(finance.COINGECKO_URL, finance.coingecko_params(ids), "CoinGecko")
            try:
                return finance.parse_coingecko(data, ids)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise WidgetError("CoinGecko: непонятный ответ") from e

        value, stale = await self.cache.get("crypto:" + ",".join(sorted(ids)), TTL["crypto"], fetch)
        return self._with_stale(value, stale)

    async def _stocks(self, params: dict[str, Any]) -> dict[str, Any]:
        secids = finance.tickers(",".join(_split(params.get("tickers"))))[:20]

        async def fetch() -> dict[str, Any]:
            data = await self._get_json(finance.MOEX_URL, finance.moex_params(secids), "Мосбиржа")
            try:
                return finance.parse_moex(data, secids)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise WidgetError("Мосбиржа: непонятный ответ") from e

        value, stale = await self.cache.get("stocks:" + ",".join(sorted(secids)), TTL["stocks"], fetch)
        return self._with_stale(value, stale)

    # --- новости ----------------------------------------------------------------------------------
    async def _rss(
        self, url: str, params: dict[str, Any] | None, service: str, source: str, *, allow_empty: bool = False
    ) -> list[dict[str, Any]]:
        response = await self._get(url, params, service)
        try:
            items = news.parse_rss(response.text, source, strip_source_suffix=service == "Google Новости")
        except ValueError as e:
            raise WidgetError(f"{service}: лента не читается") from e
        if not items and not allow_empty:
            raise WidgetError(f"{service}: лента пуста")
        return items

    async def _news(self, params: dict[str, Any]) -> dict[str, Any]:
        topic = " ".join(str(params.get("topic") or params.get("q") or "").split())[:100]
        limit = _int_param(params, "limit", 8, 1, 30)

        async def google() -> dict[str, Any]:
            if topic:
                query = {"q": topic, **news.GOOGLE_PARAMS}
                # по редкой теме новостей может не быть — это ответ, а не ошибка
                items = await self._rss(news.GOOGLE_SEARCH_URL, query, "Google Новости", "", allow_empty=True)
            else:
                items = await self._rss(news.GOOGLE_TOP_URL, dict(news.GOOGLE_PARAMS), "Google Новости", "")
            return {"items": items, "provider": "google", "updated": time.time()}

        async def lenta() -> dict[str, Any]:
            items = await self._rss(news.LENTA_URL, None, "Lenta.ru", "Lenta.ru")
            return {"items": items, "provider": "lenta", "updated": time.time()}

        async def fetch() -> dict[str, Any]:
            if topic:
                return await google()  # поиск по теме умеет только Google
            google_down = (
                self._google_failed_at is not None and self.clock() - self._google_failed_at < GOOGLE_RETRY_S
            )
            order = (lenta, google) if google_down else (google, lenta)
            error: WidgetError | None = None
            for source in order:
                try:
                    result = await source()
                except WidgetError as e:
                    if source is google:
                        self._google_failed_at = self.clock()
                    error = error or e
                    continue
                if source is google:
                    self._google_failed_at = None
                return result
            raise WidgetError("Новости недоступны: нет связи") from error

        value, stale = await self.cache.get(f"news:{normalize(topic)}", TTL["news"], fetch)
        return self._with_stale(
            {
                "items": value["items"][:limit],
                "topic": topic,
                "provider": value["provider"],
                "updated": value["updated"],
            },
            stale,
        )

    # --- компьютер --------------------------------------------------------------------------------
    async def _system(self, params: dict[str, Any]) -> dict[str, Any]:
        stats = await self.ctx.run_platform(self.ctx.platform.system_stats)
        out = stats.to_json()
        out["updated"] = time.time()
        return out

    async def _media(self, params: dict[str, Any]) -> dict[str, Any]:
        platform = self.ctx.platform

        def probe() -> tuple[MediaInfo | None, int, bool]:
            return platform.now_playing(), platform.volume_get(), platform.is_muted()

        info, volume, muted = await self.ctx.run_platform(probe)
        out = (info or MediaInfo(status="stopped")).to_json()
        out.update({"active": bool(info and info.title), "volume": volume, "muted": muted})
        return out

    async def _volume(self, params: dict[str, Any]) -> dict[str, Any]:
        platform = self.ctx.platform
        volume, muted = await self.ctx.run_platform(lambda: (platform.volume_get(), platform.is_muted()))
        return {"volume": volume, "muted": muted}

    async def _recent(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = _int_param(params, "limit", 8, 1, 50)
        files = await self.ctx.run_platform(self.ctx.platform.recent_files, limit)
        return {"items": [f.to_json() for f in files]}

    async def _apps(self, params: dict[str, Any]) -> dict[str, Any]:
        refresh = _truthy(params.get("refresh", ""))
        apps = await self.ctx.run_platform(self.ctx.platform.list_apps, refresh)
        apps = sorted(apps, key=lambda a: a.name.casefold())
        wanted = _split(params.get("pinned"))
        explicit = bool(wanted)
        if not wanted:
            wanted = list(DEFAULT_PINNED_APPS)
        pinned: list[dict[str, Any]] = []
        used: set[str] = set()
        for name in wanted:
            app = _find_app(apps, name)
            if app is None:
                if explicit:
                    pinned.append(
                        {"name": name, "app_id": "", "path": "", "source": "", "icon": "", "missing": True}
                    )
                continue
            if app.app_id in used:
                continue
            used.add(app.app_id)
            pinned.append({**app.to_json(), "icon": app_icon(app)})
        limit = _int_param(params, "limit", 500, 1, 2000)
        return {
            "pinned": pinned[:24],
            "items": [{**a.to_json(), "icon": app_icon(a)} for a in apps[:limit]],
            "total": len(apps),
        }

    async def _history(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = _int_param(params, "limit", 10, 1, 100)
        rows = self.ctx.db.query("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))
        return {"items": rows}

    async def _smarthome(self, params: dict[str, Any]) -> dict[str, Any]:
        plugins = getattr(self.ctx, "plugins", None)
        if plugins is None:
            return {"error": "Умный дом не подключён"}
        try:
            result = await plugins.action("smart_home", "states", {})
        except UserFacingError as e:
            return {"error": str(e) or "Умный дом не подключён"}
        except Exception:
            log.exception("умный дом: состояния не получены")
            return {"error": "Умный дом недоступен"}
        return result if isinstance(result, dict) else {"items": result}


def _find_app(apps: list[AppInfo], wanted: str) -> AppInfo | None:
    """Программа по имени или app_id: точное совпадение → начало имени → похожее имя."""
    target = wanted.casefold().strip()
    if not target:
        return None
    for app in apps:
        if app.name.casefold() == target or app.app_id.casefold() == target:
            return app
    for app in apps:
        if app.name.casefold().startswith(target):
            return app
    best: tuple[float, AppInfo | None] = (0.0, None)
    for app in apps:
        score = fuzz.ratio(target, app.name.casefold())
        if score > best[0]:
            best = (score, app)
    return best[1] if best[0] >= 85 else None
