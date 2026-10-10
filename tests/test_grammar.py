"""Грамматика (уровень 0): шаблоны, падежи, опечатки, «его/это», open_found, отрицательные случаи, скорость.

Таблицы фраз прогоняются дважды на общем инвентаре tests/fixtures/apps.json: с фейком resolve (rapidfuzz,
транслитерация, разговорные формы — грамматика не опирается на тонкости счёта) и с настоящим pc.apps.resolve.
Защита «не жадничай» проверяется отдельно resolve-ом, который узнаёт что угодно.
"""

import os
import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest
from rapidfuzz import fuzz

from jarvis import grammar
from jarvis.context import CUR, Context
from pc import apps
from pc.windows import WindowInfo

# --- фейк apps.resolve ---------------------------------------------------------------------------------

_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
        "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
        "ю": "yu", "я": "ya",
    }
)  # fmt: skip
# разговорные формы, которые настоящий resolve знает по спецификации S1 (здесь — минимальный набор)
COLLOQUIAL = {
    "телега": "Telegram", "телеграм": "Telegram", "телеграмм": "Telegram",
    "хром": "Google Chrome", "гугл хром": "Google Chrome",
    "ворд": "Word", "эксель": "Excel", "вскод": "Visual Studio Code", "вс код": "Visual Studio Code",
    "настройки": "Параметры", "дискорд": "Discord", "стим": "Steam", "обс": "OBS Studio",
    "спотифай": "Spotify", "зум": "Zoom Workplace",
}  # fmt: skip


def _norm(s: str) -> str:
    return " ".join(s.casefold().replace("ё", "е").split())


def make_fake_resolve(items: list[dict[str, str]]) -> Callable[[str], tuple[apps.App | None, float]]:
    """resolve(name) -> (App | None, score): разговорная форма → 100, иначе ratio (с транслитерацией)."""
    inventory = [(apps.App(i["name"], i["app_id"]), _norm(i["name"])) for i in items]
    by_name = {app.name: app for app, _ in inventory}

    def resolve(name: str) -> tuple[apps.App | None, float]:
        q = _norm(name)
        if q in COLLOQUIAL:
            return by_name[COLLOQUIAL[q]], 100.0
        best: tuple[apps.App | None, float] = (None, 0.0)
        for app, n in inventory:
            score = max(fuzz.ratio(q, n), fuzz.ratio(q.translate(_TRANSLIT), n))
            if score > best[1]:
                best = (app, score)
        return best if best[1] >= 85 else (None, best[1])

    return resolve


@pytest.fixture
def fake_apps(monkeypatch: pytest.MonkeyPatch, apps_fixture: list[dict[str, str]]) -> None:
    monkeypatch.setattr(apps, "resolve", make_fake_resolve(apps_fixture))
    monkeypatch.setattr(apps, "THRESHOLD", 85.0)


@pytest.fixture(params=["fake", "real"])
def any_apps(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, apps_fixture: list[dict[str, str]]
):
    """Фейк resolve или настоящий pc.apps.resolve на том же инвентаре."""
    if request.param == "fake":
        monkeypatch.setattr(apps, "resolve", make_fake_resolve(apps_fixture))
        monkeypatch.setattr(apps, "THRESHOLD", 85.0)
        yield
        return
    apps.set_inventory([apps.App(i["name"], i["app_id"]) for i in apps_fixture])
    yield
    apps.set_inventory(None)


# --- контексты -----------------------------------------------------------------------------------------

FOUND = [r"C:\Users\me\Documents\смета.xlsx", r"C:\Users\me\Desktop\смета-2.xlsx", r"C:\Users\me\смета.txt"]


def empty() -> Context:
    return Context()


def with_window() -> Context:
    return Context(active_window=WindowInfo(hwnd=1001, title="Telegram", pid=4242, exe="Telegram.exe"))


def with_found() -> Context:
    ctx = with_window()
    ctx.set_found(FOUND)
    return ctx


def check(text: str, ctx: Context, expected: tuple[str, dict[str, Any]] | None) -> None:
    hit = grammar.match(text, ctx)
    if expected is None:
        assert hit is None, f"{text!r}: ложное срабатывание {hit}"
        return
    assert hit is not None, f"{text!r}: грамматика не узнала"
    assert (hit.action, hit.args) == expected, text
    assert 0 < hit.confidence <= 1


# --- положительные случаи ------------------------------------------------------------------------------

CLOCK = [
    ("который час", ("clock", {"what": "time"})),
    ("Который час?", ("clock", {"what": "time"})),
    ("сколько сейчас времени", ("clock", {"what": "time"})),
    ("сколько щас время", ("clock", {"what": "time"})),
    ("какое сегодня число", ("clock", {"what": "date"})),
    ("Какое сегодня число?", ("clock", {"what": "date"})),
    ("какой сегодня день недели", ("clock", {"what": "date"})),
    ("какая сегодня дата", ("clock", {"what": "date"})),
]
LOCK = [(t, ("lock", {})) for t in ("заблокируй компьютер", "Заблокируй ПК", "заблокируй экран", "заблокируй",
                                    "заблакируй комп", "Джарвис, заблокируй компьютер!")]  # fmt: skip
VOLUME = [
    ("громкость 30", ("vol", {"set": 30})),
    ("Громкость на 75 процентов", ("vol", {"set": 75})),
    ("звук на 40%", ("vol", {"set": 40})),
    ("поставь громкость на 50", ("vol", {"set": 50})),
    ("громкасть 20", ("vol", {"set": 20})),
    ("громкость тридцать пять", ("vol", {"set": 35})),
    ("громкость сто", ("vol", {"set": 100})),
    ("громкость 0", ("vol", {"set": 0})),
    ("на 30 процентов", ("vol", {"set": 30})),
    ("звук на максимум", ("vol", {"set": 100})),
    ("громкость на минимум", ("vol", {"set": 0})),
    ("громче", ("vol", {"delta": 10})),
    ("погромче", ("vol", {"delta": 10})),
    ("сделай погромче, пожалуйста", ("vol", {"delta": 10})),
    ("громче на 20", ("vol", {"delta": 20})),
    ("прибавь звук", ("vol", {"delta": 10})),
    ("увеличь громкость на 15", ("vol", {"delta": 15})),
    ("тише", ("vol", {"delta": -10})),
    ("Потише!", ("vol", {"delta": -10})),
    ("сделай чуть тише", ("vol", {"delta": -10})),
    ("тише на 5", ("vol", {"delta": -5})),
    ("убавь звук", ("vol", {"delta": -10})),
    ("выключи звук", ("vol", {"mute": True})),
    ("отключи звук", ("vol", {"mute": True})),
    ("без звука", ("vol", {"mute": True})),
    ("включи звук", ("vol", {"mute": False})),
    ("верни звук", ("vol", {"mute": False})),
]
MEDIA = [
    ("пауза", ("media", {"action": "play_pause"})),
    ("поставь на паузу", ("media", {"action": "play_pause"})),
    ("стоп музыка", ("media", {"action": "play_pause"})),
    ("продолжи", ("media", {"action": "play_pause"})),
    ("сними с паузы", ("media", {"action": "play_pause"})),
    ("дальше", ("media", {"action": "next"})),
    ("следующий трек", ("media", {"action": "next"})),
    ("следущий трек", ("media", {"action": "next"})),
    ("следующая песня", ("media", {"action": "next"})),
    ("включи следующую песню", ("media", {"action": "next"})),
    ("переключи на следующий", ("media", {"action": "next"})),
    ("предыдущий трек", ("media", {"action": "prev"})),
    ("прошлый трек", ("media", {"action": "prev"})),
    ("предыдущая песня", ("media", {"action": "prev"})),
    ("верни прошлую песню", ("media", {"action": "prev"})),
]
FOLDERS = [
    ("открой загрузки", ("open", {"target": "загрузки", "kind": "folder"})),
    ("открой папку загрузок", ("open", {"target": "загрузки", "kind": "folder"})),
    ("открой документы", ("open", {"target": "документы", "kind": "folder"})),
    ("открой мои документы", ("open", {"target": "документы", "kind": "folder"})),
    ("открой рабочий стол", ("open", {"target": "рабочий стол", "kind": "folder"})),
    ("открой картинки", ("open", {"target": "изображения", "kind": "folder"})),
    ("открой изображения", ("open", {"target": "изображения", "kind": "folder"})),
    ("открой видео", ("open", {"target": "видео", "kind": "folder"})),
    ("открой музыку", ("open", {"target": "музыка", "kind": "folder"})),
    ("открой папку с музыкой", ("open", {"target": "музыка", "kind": "folder"})),
    ("покажи папку загрузки", ("open", {"target": "загрузки", "kind": "folder"})),
    ("перейди в документы", ("open", {"target": "документы", "kind": "folder"})),
]
APPS = [
    ("открой телегу", ("open", {"target": "Telegram", "kind": "app"})),
    ("запусти стим", ("open", {"target": "Steam", "kind": "app"})),
    ("открой калькулятор", ("open", {"target": "Калькулятор", "kind": "app"})),
    ("отрой проводник", ("open", {"target": "Проводник", "kind": "app"})),
    ("открой ворд", ("open", {"target": "Word", "kind": "app"})),
    ("открой настройки", ("open", {"target": "Параметры", "kind": "app"})),
    ("запусти вскод", ("open", {"target": "Visual Studio Code", "kind": "app"})),
    ("открой яндекс музыку", ("open", {"target": "Яндекс Музыка", "kind": "app"})),
    ("открой-ка хром", ("open", {"target": "Google Chrome", "kind": "app"})),
    ("Джарвис, открой Telegram, пожалуйста", ("open", {"target": "Telegram", "kind": "app"})),
    ("открой командную строку", ("open", {"target": "Командная строка", "kind": "app"})),
    ("включи спотифай", ("open", {"target": "Spotify", "kind": "app"})),
    ("открой мне калькулятр", ("open", {"target": "Калькулятор", "kind": "app"})),
    ("переключись на телеграм", ("focus", {"target": "Telegram"})),
    ("перейди в дискорд", ("focus", {"target": "Discord"})),
    ("покажи блокнот", ("focus", {"target": "Блокнот"})),
    ("переключится на хром", ("focus", {"target": "Google Chrome"})),
    ("вернись в ворд", ("focus", {"target": "Word"})),
    ("закрой блокнот", ("close", {"target": "Блокнот"})),
    ("закрой окно хрома", ("close", {"target": "Google Chrome"})),
    ("закрои дискорд", ("close", {"target": "Discord"})),
    ("закрой телегу", ("close", {"target": "Telegram"})),
    ("сверни телегу", ("win", {"action": "minimize", "target": "Telegram"})),
    ("сверни окно дискорда", ("win", {"action": "minimize", "target": "Discord"})),
    ("разверни хром на весь экран", ("win", {"action": "maximize", "target": "Google Chrome"})),
    ("восстанови блокнот", ("win", {"action": "restore", "target": "Блокнот"})),
]
MINIMIZE_ALL = ("сверни все окна", "сверни всё", "Сверни все", "покажи рабочий стол")
WINDOW_ALL = [(t, ("win", {"action": "minimize_all"})) for t in MINIMIZE_ALL]
CUR_WINDOW = [
    ("закрой его", ("close", {"target": CUR})),
    ("закрой это", ("close", {"target": CUR})),
    ("закрой окно", ("close", {"target": CUR})),
    ("закрой это окно", ("close", {"target": CUR})),
    ("закрой текущее окно", ("close", {"target": CUR})),
    ("сверни его", ("win", {"action": "minimize", "target": CUR})),
    ("сверни окно", ("win", {"action": "minimize", "target": CUR})),
    ("сверни", ("win", {"action": "minimize", "target": CUR})),
    ("разверни", ("win", {"action": "maximize", "target": CUR})),
    ("разверни окно на весь экран", ("win", {"action": "maximize", "target": CUR})),
    ("на весь экран", ("win", {"action": "maximize", "target": CUR})),
    ("восстанови окно", ("win", {"action": "restore", "target": CUR})),
]
FIND = [
    ("найди файл смета", ("find", {"query": "смета", "kind": "file"})),
    ("найди папку проекты", ("find", {"query": "проекты", "kind": "folder"})),
    ("поищи файл договор аренды", ("find", {"query": "договор аренды", "kind": "file"})),
    ("найди файл «Отчёт за май.docx»", ("find", {"query": "Отчёт за май.docx", "kind": "file"})),
    ("найди файл с названием бюджет", ("find", {"query": "бюджет", "kind": "file"})),
    ("найди мне файл про отпуск", ("find", {"query": "отпуск", "kind": "file"})),
]
OPEN_FOUND = [
    ("открой второй", ("open_found", {"index": 2})),
    ("открой последний", ("open_found", {"index": -1})),
    ("открой 3", ("open_found", {"index": 3})),
    ("открой третий файл", ("open_found", {"index": 3})),
    ("открой файл номер 1", ("open_found", {"index": 1})),
    ("открой первый результат", ("open_found", {"index": 1})),
    ("открой 2-й", ("open_found", {"index": 2})),
]

ALWAYS = CLOCK + LOCK + VOLUME + MEDIA + FOLDERS + APPS + WINDOW_ALL + FIND


@pytest.mark.usefixtures("any_apps")
@pytest.mark.parametrize(("text", "expected"), ALWAYS)
def test_patterns_without_context(text: str, expected: tuple[str, dict[str, Any]]) -> None:
    check(text, empty(), expected)


@pytest.mark.usefixtures("any_apps")
@pytest.mark.parametrize(("text", "expected"), CUR_WINDOW)
def test_cur_target_needs_something_to_point_at(text: str, expected: tuple[str, dict[str, Any]]) -> None:
    check(text, with_window(), expected)
    check(text, empty(), None)  # «закрой его» без окна и без последнего объекта — рукам


@pytest.mark.usefixtures("any_apps")
def test_cur_target_from_last_object() -> None:
    ctx = Context()
    ctx.remember("Telegram", "app")
    check("закрой его", ctx, ("close", {"target": CUR}))


@pytest.mark.usefixtures("any_apps")
@pytest.mark.parametrize(("text", "expected"), OPEN_FOUND)
def test_open_found(text: str, expected: tuple[str, dict[str, Any]]) -> None:
    check(text, with_found(), expected)
    check(text, with_window(), None)  # результатов find нет — рукам


@pytest.mark.usefixtures("any_apps")
def test_open_found_out_of_range_or_stale() -> None:
    check("открой пятый", with_found(), None)
    check("открой 0", with_found(), None)
    stale = Context()
    stale.set_found(FOUND, now=time.time() - 600)
    check("открой второй", stale, None)


@pytest.mark.usefixtures("any_apps")
def test_find_keeps_original_case_and_yo() -> None:
    hit = grammar.match("Найди файл Ёлка Новогодняя", empty())
    assert hit is not None and hit.args == {"query": "Ёлка Новогодняя", "kind": "file"}


@pytest.mark.usefixtures("fake_apps")
def test_app_confidence_is_resolve_score() -> None:
    hit = grammar.match("открой мне калькулятр", empty())
    assert hit is not None and 0.85 <= hit.confidence < 1


# --- отрицательные случаи ------------------------------------------------------------------------------

NEGATIVE = [
    "",
    "   ",
    "открой то, что я вчера качал",
    "открой то что я вчера скачивал",
    "закрой процесс стима",
    "закрой процесс телеграма",
    "найди и открой последний pdf",
    "найди файл который я вчера скачал",
    "найди презентацию про бюджет",
    "открой фотошоп",
    "открой хром и телегу",
    "открой хром в режиме инкогнито",
    "открой его",
    "включи музыку",
    "открой файл отчёт",
    "открой новую вкладку",
    "открой хром?",
    "громкость 30?",
    "закрой все окна",
    "закрой вкладку",
    "покажи загрузки",
    "покажи погоду",
    "громкость 150",
    "громкость",
    "не открывай хром",
    "почему не работает звук",
    "что такое громкость",
    "сколько будет два плюс два",
    "привет",
    "заблокируй его",
    "открой второй",
    "стоп",
    "убей хром",
    "закрой",
    "открой " + "очень " * 30 + "длинную команду",
]


@pytest.mark.usefixtures("any_apps")
@pytest.mark.parametrize("text", NEGATIVE)
def test_negative(text: str) -> None:
    check(text, with_window(), None)


GREEDY = [
    "закрой процесс стима",
    "открой то что я вчера качал",
    "открой хром и телегу",
    "открой хром в режиме инкогнито",
    "запусти игру",
    "открой его",
    "переключись на него",
    "включи музыку",
    "открой браузер",
    "сверни все окна кроме хрома",
    "закрой ту программу которую я открыл",
    "открой папку с проектами на диске д",
    "открой сайт авито",
    "открой не хром",
    "открой очень длинное название из пяти слов",
]


@pytest.mark.parametrize("text", GREEDY)
def test_not_greedy_even_if_resolve_matches_anything(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Служебные слова, «процесс», несколько шагов, общие слова — None, даже если resolve узнаёт всё."""
    monkeypatch.setattr(apps, "resolve", lambda name: (apps.App("Steam", "steam"), 100.0))
    check(text, with_window(), None)


# --- скорость ------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("any_apps")
def test_match_is_fast() -> None:
    """Медиана match после прогрева ≤2 мс (при переменной окружения CI порог ×3)."""
    cases = [(t, with_found()) for t, _ in ALWAYS + CUR_WINDOW + OPEN_FOUND] + [
        (t, empty()) for t in NEGATIVE
    ]
    for text, ctx in cases:
        grammar.match(text, ctx)
    times = []
    for text, ctx in cases:
        t0 = time.perf_counter()
        grammar.match(text, ctx)
        times.append(time.perf_counter() - t0)
    limit = 0.002 * (3 if os.environ.get("CI") else 1)
    median = statistics.median(times)
    assert median <= limit, f"медиана match {median * 1000:.3f} мс > {limit * 1000:.0f} мс"
