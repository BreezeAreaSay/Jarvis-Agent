"""Встроенные команды: сайты («открой ютуб») и поиск («найди на ютубе котиков», «загугли …»)."""

from jarvis.commands.library.base import Builtin, cmd
from jarvis.commands.library.base import variants as v
from jarvis.commands.sites import SITES
from jarvis.commands.textutil import normalize

Q = "{query}"

# дополнительные фразы поиска для отдельных сайтов
_EXTRA_SEARCH: dict[str, tuple[str, ...]] = {
    "google": (
        f"(загугли|погугли) {Q}",
        f"(найди|поищи|поиск) в интернете {Q}",
        f"поищи в сети {Q}",
        f"гугл поиск {Q}",
    ),
    "youtube": (f"(включи|поставь|открой) на ютубе {Q}", f"(найди|включи) видео {Q}", f"ютуб поиск {Q}"),
    "yandex": (f"яндекс поиск {Q}",),
    "wikipedia": (f"(что говорит|посмотри в) википеди(и|я) {Q}",),
    "translate": (f"переведи в переводчике {Q}",),
    "rutube": (f"(включи|поставь) на рутубе {Q}",),
    "yandex_maps": (f"(покажи|найди) на карте {Q}",),
}


def _site_commands() -> list[Builtin]:
    out: list[Builtin] = []
    for site in SITES:
        aliases = [normalize(a) for a in site.aliases[:5]]
        phrases = v(
            *(f"(открой|запусти) {a}" for a in aliases),
            *(f"(зайди|перейди) {site.prep} {a}" for a in aliases[:3]),
            f"открой сайт {aliases[0]}",
        )
        out.append(
            cmd(
                f"site.{site.key}",
                f"Открыть {site.name}",
                phrases,
                [("Launch.Url", site.url)],
                category="sites",
                icon=site.icon,
            )
        )
        if site.search and site.search_forms:
            forms = [normalize(f) for f in site.search_forms]
            search = v(
                *(f"(найди|поищи|ищи|поиск) {f} {Q}" for f in forms),
                *(f"{f} (найди|поищи) {Q}" for f in forms),
                *_EXTRA_SEARCH.get(site.key, ()),
            )
            out.append(
                cmd(
                    f"search.{site.key}",
                    f"Поиск {site.prep} {site.name}",
                    search,
                    [("Web.Search", f"{site.key}: {Q}")],
                    category="search",
                    icon=site.icon if site.icon != "site" else "search",
                )
            )
    return out


def _generic() -> list[Builtin]:
    return [
        cmd(
            "search.default",
            "Поиск в интернете",
            v(f"(найди|поищи|ищи|поиск) {Q}", f"(найди|поищи) мне {Q}"),
            [("Web.Search", Q)],
            category="search",
            icon="search",
        ),
        cmd(
            "search.images",
            "Найти картинки",
            v(
                f"(найди|покажи|поищи) (картинки|изображения|фото|фотографии) {Q}",
                f"картинки {Q}",
            ),
            [("Web.Search", f"images: {Q}")],
            category="search",
            icon="search",
        ),
        cmd(
            "search.bing",
            "Поиск: Bing",
            v(f"(найди|поищи) в (бинге|бинг|bing) {Q}"),
            [("Web.Search", f"bing: {Q}")],
            category="search",
            icon="search",
        ),
        cmd(
            "search.duckduckgo",
            "Поиск: DuckDuckGo",
            v(f"(найди|поищи) в (дак дак гоу|duckduckgo) {Q}"),
            [("Web.Search", f"duckduckgo: {Q}")],
            category="search",
            icon="search",
        ),
        cmd(
            "sites.open",
            "Открыть сайт",
            v(
                "(открой|запусти) {site}",
                "открой сайт {site}",
                "(зайди|перейди) (на|в) {site}",
                "(зайди|перейди) на сайт {site}",
            ),
            [("Launch.Url", "{site}")],
            category="sites",
            icon="site",
        ),
    ]


def commands() -> list[Builtin]:
    return [*_site_commands(), *_generic()]
