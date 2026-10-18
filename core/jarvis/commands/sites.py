"""Сайты: популярные сайты с русскими названиями («ютуб», «вк», «госуслуги»), поиск на них и поисковики.

Web.Search принимает «<поисковик>: запрос» — google, yandex, youtube, bing, duckduckgo, wikipedia, images,
maps или ключ сайта с поиском (avito, ozon, wildberries…).
"""

import re
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

from rapidfuzz import fuzz, process

from jarvis.commands.textutil import normalize


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    url: str
    aliases: tuple[str, ...]  # как называют: «ютуб», «вк»; первые — для фраз встроенных команд
    icon: str = "site"
    search: str = ""  # адрес поиска с {q}
    search_forms: tuple[str, ...] = ()  # «на ютубе», «в гугле» — для фраз «найди на ютубе …»
    prep: str = "на"  # «зайди на ютуб» / «зайди в вк»


def _s(
    key: str,
    name: str,
    url: str,
    aliases: str,
    *,
    icon: str = "site",
    search: str = "",
    forms: str = "",
    prep: str = "на",
) -> Site:
    return Site(
        key,
        name,
        url,
        tuple(a.strip() for a in aliases.split("|") if a.strip()),
        icon,
        search,
        tuple(f.strip() for f in forms.split("|") if f.strip()),
        prep,
    )


SITES: tuple[Site, ...] = (
    _s(
        "youtube",
        "YouTube",
        "https://www.youtube.com",
        "ютуб|ютюб|ютубчик|youtube|ю туб",
        icon="youtube",
        search="https://www.youtube.com/results?search_query={q}",
        forms="на ютубе|в ютубе|на youtube|в youtube",
    ),
    _s(
        "vk",
        "ВКонтакте",
        "https://vk.com",
        "вк|вконтакте|в контакте|контакт|vk|вэка",
        icon="vk",
        search="https://vk.com/search?c%5Bq%5D={q}",
        forms="в вк|в контакте|во вконтакте|в vk",
        prep="в",
    ),
    _s(
        "google",
        "Google",
        "https://www.google.com",
        "гугл|google|гугол",
        icon="search",
        search="https://www.google.com/search?q={q}",
        forms="в гугле|в google|в гугл",
        prep="в",
    ),
    _s(
        "yandex",
        "Яндекс",
        "https://ya.ru",
        "яндекс|yandex|я ру|ya ru",
        icon="yandex",
        search="https://yandex.ru/search/?text={q}",
        forms="в яндексе|в yandex|в яндекс",
        prep="в",
    ),
    _s(
        "kinopoisk",
        "Кинопоиск",
        "https://www.kinopoisk.ru",
        "кинопоиск|кино поиск",
        search="https://www.kinopoisk.ru/index.php?kp_query={q}",
        forms="на кинопоиске",
    ),
    _s(
        "avito",
        "Авито",
        "https://www.avito.ru",
        "авито|avito",
        search="https://www.avito.ru/rossiya?q={q}",
        forms="на авито",
    ),
    _s(
        "ozon",
        "Ozon",
        "https://www.ozon.ru",
        "озон|ozon",
        search="https://www.ozon.ru/search/?text={q}",
        forms="на озоне|в озоне|на озон",
    ),
    _s(
        "wildberries",
        "Wildberries",
        "https://www.wildberries.ru",
        "вайлдберриз|вайлдбериз|валберис|валдберис|вб|wildberries",
        search="https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
        forms="на вайлдберриз|на валберис|на вб|в вб|на wildberries",
    ),
    _s(
        "github",
        "GitHub",
        "https://github.com",
        "гитхаб|гит хаб|github",
        search="https://github.com/search?q={q}",
        forms="на гитхабе|в гитхабе",
    ),
    _s(
        "twitch",
        "Twitch",
        "https://www.twitch.tv",
        "твич|twitch",
        search="https://www.twitch.tv/search?term={q}",
        forms="на твиче|в твиче",
    ),
    _s("gosuslugi", "Госуслуги", "https://www.gosuslugi.ru", "госуслуги|гос услуги|госуслугах|gosuslugi"),
    _s(
        "yandex_mail",
        "Яндекс Почта",
        "https://mail.yandex.ru",
        "почта|почту|яндекс почта|яндекс почту",
        icon="yandex",
        prep="в",
    ),
    _s("gmail", "Gmail", "https://mail.google.com", "джимейл|гмейл|gmail|гугл почта|гугл почту", prep="в"),
    _s(
        "mail_ru",
        "Почта Mail.ru",
        "https://e.mail.ru",
        "мейл ру|мэйл ру|mail ru|почта мейл|почту мейл",
        prep="в",
    ),
    _s(
        "yandex_maps",
        "Яндекс Карты",
        "https://yandex.ru/maps",
        "карты|яндекс карты|карту",
        icon="yandex",
        search="https://yandex.ru/maps/?text={q}",
        forms="на картах|на карте|в картах|на яндекс картах",
        prep="в",
    ),
    _s(
        "google_maps",
        "Google Карты",
        "https://www.google.com/maps",
        "гугл карты|google maps|гугл мапс",
        search="https://www.google.com/maps/search/{q}",
        forms="на гугл картах|в гугл картах",
        prep="в",
    ),
    _s(
        "translate",
        "Яндекс Переводчик",
        "https://translate.yandex.ru",
        "переводчик|яндекс переводчик",
        icon="yandex",
        search="https://translate.yandex.ru/?text={q}",
        forms="в переводчике",
        prep="в",
    ),
    _s(
        "google_translate",
        "Google Переводчик",
        "https://translate.google.com",
        "гугл переводчик|гугл транслейт|google translate",
        search="https://translate.google.com/?sl=auto&tl=ru&text={q}",
        forms="в гугл переводчике",
        prep="в",
    ),
    _s(
        "habr",
        "Хабр",
        "https://habr.com",
        "хабр|хабрахабр|habr",
        search="https://habr.com/ru/search/?q={q}",
        forms="на хабре",
    ),
    _s(
        "wikipedia",
        "Википедия",
        "https://ru.wikipedia.org",
        "википедия|википедию|вики|wikipedia",
        search="https://ru.wikipedia.org/w/index.php?search={q}",
        forms="в википедии|на википедии|в вики",
        prep="в",
    ),
    _s(
        "chatgpt",
        "ChatGPT",
        "https://chatgpt.com",
        "чат гпт|чатгпт|чат джипити|chatgpt|chat gpt",
        icon="ai",
        prep="в",
    ),
    _s("claude", "Claude", "https://claude.ai", "клод|claude", icon="ai", prep="в"),
    _s("deepseek", "DeepSeek", "https://chat.deepseek.com", "дипсик|deepseek|дип сик", icon="ai", prep="в"),
    _s("gigachat", "GigaChat", "https://giga.chat", "гигачат|гига чат|gigachat", icon="ai", prep="в"),
    _s("dzen", "Дзен", "https://dzen.ru", "дзен|яндекс дзен", prep="в"),
    _s(
        "rutube",
        "Rutube",
        "https://rutube.ru",
        "рутуб|rutube",
        search="https://rutube.ru/search/?query={q}",
        forms="на рутубе",
    ),
    _s(
        "tiktok",
        "TikTok",
        "https://www.tiktok.com",
        "тикток|тик ток|tiktok",
        search="https://www.tiktok.com/search?q={q}",
        forms="в тиктоке|в тик токе",
        prep="в",
    ),
    _s(
        "instagram",
        "Instagram",
        "https://www.instagram.com",
        "инстаграм|инстаграмм|инста|инсту|instagram",
        prep="в",
    ),
    _s(
        "megamarket",
        "Мегамаркет",
        "https://megamarket.ru",
        "мегамаркет|мега маркет",
        search="https://megamarket.ru/catalog/?q={q}",
        forms="на мегамаркете",
    ),
    _s(
        "yandex_market",
        "Яндекс Маркет",
        "https://market.yandex.ru",
        "маркет|яндекс маркет",
        icon="yandex",
        search="https://market.yandex.ru/search?text={q}",
        forms="на маркете|на яндекс маркете|в маркете",
    ),
    _s(
        "sber",
        "СберБанк Онлайн",
        "https://online.sberbank.ru",
        "сбер|сбербанк|сбербанк онлайн|сбер онлайн",
        prep="в",
    ),
    _s("tbank", "Т-Банк", "https://www.tbank.ru", "тинькофф|тинькоф|т банк|тбанк|т-банк|тиньков", prep="в"),
    _s(
        "hh",
        "hh.ru",
        "https://hh.ru",
        "хх|хэдхантер|хедхантер|хх ру|аш аш|hh",
        search="https://hh.ru/search/vacancy?text={q}",
        forms="на хх|на хедхантере|на хэдхантере",
    ),
    _s(
        "steam_store",
        "Магазин Steam",
        "https://store.steampowered.com",
        "стим магазин|магазин стим|сайт стима",
        icon="steam",
        search="https://store.steampowered.com/search/?term={q}",
        forms="в стиме|в магазине стим",
        prep="в",
    ),
    _s(
        "ivi",
        "Иви",
        "https://www.ivi.ru",
        "иви|ivi",
        search="https://www.ivi.ru/search/?q={q}",
        forms="на иви",
    ),
    _s("okko", "Okko", "https://okko.tv", "окко|okko", prep="в"),
    _s("netflix", "Netflix", "https://www.netflix.com", "нетфликс|netflix"),
    _s(
        "telegram_web",
        "Telegram Web",
        "https://web.telegram.org",
        "телеграм веб|веб телеграм",
        icon="telegram",
        prep="в",
    ),
    _s(
        "whatsapp_web",
        "WhatsApp Web",
        "https://web.whatsapp.com",
        "ватсап веб|вотсап веб|веб ватсап",
        prep="в",
    ),
    _s("ok", "Одноклассники", "https://ok.ru", "одноклассники|одноклассниках|ок ру", prep="в"),
    _s("twitter", "X (Twitter)", "https://x.com", "твиттер|твитер|twitter", prep="в"),
    _s(
        "reddit",
        "Reddit",
        "https://www.reddit.com",
        "реддит|редит|reddit",
        search="https://www.reddit.com/search/?q={q}",
        forms="на реддите",
    ),
    _s(
        "pinterest",
        "Pinterest",
        "https://www.pinterest.com",
        "пинтерест|pinterest",
        search="https://www.pinterest.com/search/pins/?q={q}",
        forms="на пинтересте|в пинтересте",
        prep="в",
    ),
    _s(
        "stackoverflow",
        "Stack Overflow",
        "https://stackoverflow.com",
        "стек оверфлоу|стековерфлоу|stack overflow",
        search="https://stackoverflow.com/search?q={q}",
        forms="на стек оверфлоу|на стековерфлоу",
    ),
    _s("gismeteo", "Gismeteo", "https://www.gismeteo.ru", "гисметео|gismeteo", icon="weather"),
    _s(
        "yandex_weather",
        "Яндекс Погода",
        "https://yandex.ru/pogoda",
        "яндекс погода|яндекс погоду",
        icon="weather",
        prep="в",
    ),
    _s("google_drive", "Google Диск", "https://drive.google.com", "гугл диск|google drive", prep="в"),
    _s("yandex_disk", "Яндекс Диск", "https://disk.yandex.ru", "яндекс диск", icon="yandex", prep="в"),
    _s(
        "google_docs",
        "Google Документы",
        "https://docs.google.com",
        "гугл документы|гугл докс|google docs",
        prep="в",
    ),
    _s(
        "google_sheets",
        "Google Таблицы",
        "https://docs.google.com/spreadsheets",
        "гугл таблицы|google sheets",
        prep="в",
    ),
    _s(
        "google_calendar",
        "Google Календарь",
        "https://calendar.google.com",
        "гугл календарь|google calendar",
        icon="calendar",
        prep="в",
    ),
    _s(
        "youtube_music",
        "YouTube Music",
        "https://music.youtube.com",
        "ютуб музыка|ютуб мьюзик|youtube music",
        icon="youtube",
        prep="в",
    ),
    _s(
        "soundcloud",
        "SoundCloud",
        "https://soundcloud.com",
        "саундклауд|soundcloud",
        search="https://soundcloud.com/search?q={q}",
        forms="на саундклауде",
    ),
    _s(
        "aliexpress",
        "AliExpress",
        "https://aliexpress.ru",
        "алиэкспресс|али экспресс|алик|aliexpress",
        search="https://aliexpress.ru/wholesale?SearchText={q}",
        forms="на алиэкспрессе|на али",
    ),
    _s(
        "amazon",
        "Amazon",
        "https://www.amazon.com",
        "амазон|amazon",
        search="https://www.amazon.com/s?k={q}",
        forms="на амазоне",
    ),
    _s(
        "lamoda",
        "Lamoda",
        "https://www.lamoda.ru",
        "ламода|lamoda",
        search="https://www.lamoda.ru/catalogsearch/result/?q={q}",
        forms="на ламоде",
    ),
    _s(
        "dns",
        "DNS",
        "https://www.dns-shop.ru",
        "днс|днс шоп|dns",
        search="https://www.dns-shop.ru/search/?q={q}",
        forms="в днс|на днс",
        prep="в",
    ),
    _s(
        "mvideo",
        "М.Видео",
        "https://www.mvideo.ru",
        "мвидео|м видео|эм видео",
        search="https://www.mvideo.ru/product-list-page?q={q}",
        forms="в мвидео|на мвидео",
        prep="в",
    ),
    _s(
        "citilink",
        "Ситилинк",
        "https://www.citilink.ru",
        "ситилинк",
        search="https://www.citilink.ru/search/?text={q}",
        forms="в ситилинке|на ситилинке",
        prep="в",
    ),
    _s(
        "2gis",
        "2ГИС",
        "https://2gis.ru",
        "два гис|дубль гис|2 гис|2гис",
        search="https://2gis.ru/search/{q}",
        forms="в два гис|в 2гис|в дубль гис",
        prep="в",
    ),
    _s("drom", "Дром", "https://www.drom.ru", "дром|drom"),
    _s("auto_ru", "Авто.ру", "https://auto.ru", "авто ру|авто.ру"),
    _s("cian", "Циан", "https://www.cian.ru", "циан|cian"),
    _s(
        "pikabu",
        "Пикабу",
        "https://pikabu.ru",
        "пикабу|pikabu",
        search="https://pikabu.ru/search?q={q}",
        forms="на пикабу",
    ),
    _s("news", "Новости", "https://dzen.ru/news", "новости|яндекс новости", icon="note", prep="в"),
    _s("deepl", "DeepL", "https://www.deepl.com/translator", "дипл|deepl", prep="в"),
    _s("rzd", "РЖД", "https://www.rzd.ru", "ржд|рж д", prep="в"),
    _s("aviasales", "Авиасейлс", "https://www.aviasales.ru", "авиасейлс|авиасейлз|aviasales"),
    _s(
        "music_yandex_web",
        "Яндекс Музыка (сайт)",
        "https://music.yandex.ru",
        "сайт яндекс музыки",
        icon="yandex",
    ),
)

SITES_BY_KEY: dict[str, Site] = {s.key: s for s in SITES}

# поисковики Web.Search сверх сайтов
ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "yandex": "https://yandex.ru/search/?text={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "wikipedia": "https://ru.wikipedia.org/w/index.php?search={q}",
    "images": "https://yandex.ru/images/search?text={q}",
    "maps": "https://yandex.ru/maps/?text={q}",
    **{s.key: s.search for s in SITES if s.search},
}

_DOMAIN = re.compile(r"^(?:https?://)?((?:[a-z0-9\-а-я]+\.)+[a-zа-я]{2,})(/\S*)?$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def search_url(value: str, default_engine: str = "google") -> tuple[str, str, str]:
    """«youtube: котики» → (url, поисковик, запрос). Без приставки — поисковик по умолчанию."""
    engine, query = default_engine, value.strip()
    head, sep, tail = query.partition(":")
    if sep and head.strip().casefold() in ENGINES:
        engine, query = head.strip().casefold(), tail.strip()
    template = ENGINES.get(engine) or ENGINES["google"]
    encoded = quote(query, safe="") if "/search/{q}" in template else quote_plus(query)
    return template.replace("{q}", encoded), engine, query


def looks_like_url(value: str) -> bool:
    v = value.strip()
    return (bool(_SCHEME.match(v)) and not re.match(r"^[a-z]:\\", v, re.IGNORECASE)) or bool(_DOMAIN.match(v))


def to_url(value: str) -> str:
    """«vk.com» → «https://vk.com»; ссылка со схемой (https:, spotify:, steam://) — как есть."""
    v = value.strip()
    if _SCHEME.match(v) and not re.match(r"^[a-z]:\\", v, re.IGNORECASE):
        return v
    return "https://" + v


@dataclass
class ResolvedSite:
    site: Site
    score: float

    def slots(self) -> dict[str, str]:
        return {"site": self.site.name, "url": self.site.url}


class SiteResolver:
    def __init__(self) -> None:
        alias_map: dict[str, Site] = {}
        for site in SITES:
            for alias in (*site.aliases, site.name):
                alias_map.setdefault(normalize(alias), site)
        self._alias_map = alias_map
        self._alias_list = list(alias_map)

    def resolve(self, text: str) -> ResolvedSite | None:
        """«ютуб» → YouTube, «vk.com» → сайт по адресу; ничего похожего — None."""
        words = normalize(text).split()
        if words and words[0] in ("сайт", "сайта", "страницу"):
            words = words[1:]
        query = " ".join(words)
        if not query:
            return None
        site = self._alias_map.get(query)
        if site is not None:
            return ResolvedSite(site, 100.0)
        m = _DOMAIN.match(query.replace(" точка ", "."))
        if m:
            domain = m.group(1) + (m.group(2) or "")
            return ResolvedSite(Site(domain, domain, "https://" + domain, (domain,)), 95.0)
        if len(query) <= 3:
            return None
        need = 86.0 if len(query) <= 5 else 82.0
        hit = process.extractOne(query, self._alias_list, scorer=fuzz.ratio, score_cutoff=need)
        if hit is None:
            return None
        return ResolvedSite(self._alias_map[hit[0]], float(hit[1]))
