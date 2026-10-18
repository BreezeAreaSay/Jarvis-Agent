"""Финансы: курсы ЦБ (cbr-xml-daily.ru), криптовалюта (CoinGecko) и акции Мосбиржи (MOEX ISS).

Здесь только адреса и разбор ответов — сеть и кэш в jarvis.widgets.service.
"""

import time
from typing import Any

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
MOEX_URL = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"

# --- курсы ЦБ ---------------------------------------------------------------------------------------

DEFAULT_CURRENCIES = ("USD", "EUR", "CNY", "GBP", "KZT", "BYN", "TRY", "JPY")

# ЦБ называет валюты в форме «за номинал» («Казахстанских тенге») — для виджета короче и в ед. числе
CURRENCY_NAMES = {
    "USD": "Доллар США",
    "EUR": "Евро",
    "CNY": "Юань",
    "GBP": "Фунт стерлингов",
    "KZT": "Тенге",
    "BYN": "Белорусский рубль",
    "TRY": "Турецкая лира",
    "JPY": "Иена",
    "CHF": "Швейцарский франк",
    "AED": "Дирхам ОАЭ",
    "UAH": "Гривна",
    "AMD": "Драм",
    "GEL": "Лари",
    "UZS": "Сум",
    "INR": "Рупия",
    "HKD": "Гонконгский доллар",
}
CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "CNY": "¥",
    "GBP": "£",
    "KZT": "₸",
    "BYN": "Br",
    "TRY": "₺",
    "JPY": "¥",
    "CHF": "₣",
    "UAH": "₴",
    "AMD": "֏",
    "GEL": "₾",
    "INR": "₹",
}


def parse_cbr(data: dict[str, Any]) -> dict[str, Any]:
    """daily_json.js → {date, currencies: {код: {...}}}; курс — за одну единицу валюты."""
    currencies: dict[str, dict[str, Any]] = {}
    for code, raw in (data.get("Valute") or {}).items():
        try:
            nominal = int(raw.get("Nominal") or 1) or 1
            value = float(raw["Value"]) / nominal
            previous = float(raw.get("Previous", raw["Value"])) / nominal
        except (KeyError, TypeError, ValueError):
            continue
        code = str(raw.get("CharCode") or code).upper()
        delta = value - previous
        currencies[code] = {
            "code": code,
            "name": CURRENCY_NAMES.get(code) or str(raw.get("Name") or code),
            "symbol": CURRENCY_SYMBOLS.get(code, ""),
            "value": round(value, 4),
            "previous": round(previous, 4),
            "delta": round(delta, 4),
            "delta_pct": round(delta / previous * 100, 2) if previous else 0.0,
            "nominal": nominal,
        }
    if not currencies:
        raise ValueError("в ответе ЦБ нет курсов")
    return {"date": str(data.get("Date") or "")[:10], "currencies": currencies}


def rates_view(parsed: dict[str, Any], codes: list[str] | None) -> dict[str, Any]:
    currencies: dict[str, dict[str, Any]] = parsed["currencies"]
    wanted = sorted(currencies) if codes == ["ALL"] else (codes or list(DEFAULT_CURRENCIES))
    items = [currencies[c] for c in wanted if c in currencies]
    return {"date": parsed["date"], "base": "RUB", "items": items}


# --- криптовалюта -------------------------------------------------------------------------------------

DEFAULT_COINS = ("bitcoin", "ethereum", "the-open-network", "solana")
COINS: dict[str, tuple[str, str]] = {  # id CoinGecko → (тикер, название)
    "bitcoin": ("BTC", "Bitcoin"),
    "ethereum": ("ETH", "Ethereum"),
    "the-open-network": ("TON", "Toncoin"),
    "solana": ("SOL", "Solana"),
    "tether": ("USDT", "Tether"),
    "binancecoin": ("BNB", "BNB"),
    "ripple": ("XRP", "XRP"),
    "dogecoin": ("DOGE", "Dogecoin"),
    "tron": ("TRX", "TRON"),
    "cardano": ("ADA", "Cardano"),
    "litecoin": ("LTC", "Litecoin"),
    "notcoin": ("NOT", "Notcoin"),
}
_COIN_BY_SYMBOL = {symbol.lower(): coin_id for coin_id, (symbol, _) in COINS.items()}


def coin_ids(raw: str | None) -> list[str]:
    """«btc, ton, solana» → id CoinGecko; пусто — монеты по умолчанию."""
    ids: list[str] = []
    for part in (raw or "").replace(";", ",").split(","):
        token = part.strip().lower()
        if not token:
            continue
        coin_id = _COIN_BY_SYMBOL.get(token, token)
        if coin_id not in ids:
            ids.append(coin_id)
    return ids or list(DEFAULT_COINS)


def coingecko_params(ids: list[str]) -> dict[str, Any]:
    return {"ids": ",".join(ids), "vs_currencies": "usd,rub", "include_24hr_change": "true"}


def parse_coingecko(data: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    items = []
    for coin_id in ids:
        raw = data.get(coin_id)
        if not isinstance(raw, dict) or raw.get("usd") is None:
            continue
        symbol, name = COINS.get(coin_id, (coin_id.upper()[:6], coin_id.replace("-", " ").title()))
        items.append(
            {
                "id": coin_id,
                "symbol": symbol,
                "name": name,
                "usd": float(raw["usd"]),
                "rub": _float_or_none(raw.get("rub")),
                "change_24h": _round2(raw.get("usd_24h_change")),
                "change_24h_rub": _round2(raw.get("rub_24h_change")),
            }
        )
    if not items and ids:
        raise ValueError("в ответе CoinGecko нет нужных монет")
    return {"items": items, "updated": time.time()}


# --- акции Мосбиржи -----------------------------------------------------------------------------------

# Т-Технологии торгуются под тикером T (раньше TCSG); в запросе оба — вернётся тот, что торгуется
DEFAULT_TICKERS = ("SBER", "GAZP", "LKOH", "YDEX", "T", "TCSG", "VTBR")


def tickers(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in (raw or "").replace(";", ",").split(","):
        token = part.strip().upper()
        if token and token not in out:
            out.append(token)
    return out or list(DEFAULT_TICKERS)


def moex_params(secids: list[str]) -> dict[str, Any]:
    return {"securities": ",".join(secids), "iss.meta": "off", "iss.only": "marketdata,securities"}


def _table(block: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Таблица ISS {columns, data} → {SECID: {колонка: значение}}."""
    if not block:
        return {}
    columns = block.get("columns") or []
    out: dict[str, dict[str, Any]] = {}
    for row in block.get("data") or []:
        record = dict(zip(columns, row, strict=False))
        secid = record.get("SECID")
        if secid:
            out[str(secid)] = record
    return out


def parse_moex(data: dict[str, Any], secids: list[str]) -> dict[str, Any]:
    if "securities" not in data and "marketdata" not in data:
        raise ValueError("в ответе Мосбиржи нет таблиц")
    securities = _table(data.get("securities"))
    market = _table(data.get("marketdata"))
    items = []
    for secid in secids:
        sec = securities.get(secid, {})
        md = market.get(secid, {})
        if not sec and not md:
            continue
        if sec.get("STATUS") not in (None, "A") and not md.get("LAST"):
            continue  # бумага не торгуется (например, старый тикер после смены)
        prev = _first_number(sec.get("PREVLEGALCLOSEPRICE"), sec.get("PREVPRICE"))
        last = _first_number(md.get("LAST"), md.get("LCURRENTPRICE"), md.get("MARKETPRICE"), prev)
        if last is None:
            continue
        if prev:
            change = last - prev
            change_pct = change / prev * 100
        else:
            change = _first_number(md.get("CHANGE")) or 0.0
            change_pct = _first_number(md.get("LASTTOPREVPRICE")) or 0.0
        items.append(
            {
                "ticker": secid,
                "name": str(sec.get("SHORTNAME") or sec.get("SECNAME") or secid),
                "last": last,
                "prev": prev,
                "change": round(change, 4),
                "change_pct": round(change_pct, 2),
                "trading": md.get("TRADINGSTATUS") == "T",
            }
        )
    return {"items": items, "currency": "RUB", "updated": time.time()}


def _first_number(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number:
            return number
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _round2(value: Any) -> float | None:
    number = _float_or_none(value)
    return None if number is None else round(number, 2)
