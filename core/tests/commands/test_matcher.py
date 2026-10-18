"""Разбор фраз: синонимы, слоты, слова-паразиты, ошибки распознавания, составные фразы и отказы (→ ИИ)."""

import time
from typing import Any

import pytest

from jarvis.commands.model import CommandIn
from jarvis.commands.service import Commands
from jarvis.context import AppContext
from jarvis.platform.base import AppInfo
from jarvis.services import DynamicCommand, ExecResult


def one(cmds: Commands, text: str) -> tuple[str, dict[str, Any]]:
    match = cmds.match(text)
    assert match is not None, f"не распознано: {text!r}"
    assert len(match.parts) == 1, f"{text!r} → {[p.command_id for p in match.parts]}"
    return match.parts[0].command_id, match.parts[0].slots


@pytest.mark.parametrize(
    ("text", "command_id", "slots"),
    [
        # медиа и громкость — синонимы, обращение, слова-паразиты
        ("включи музыку", "builtin.media.play", {}),
        ("Джарвис, включи музыку пожалуйста", "builtin.media.play", {}),
        ("ну давай включи музыку", "builtin.media.play", {}),
        ("вруби музыку", "builtin.media.play", {}),
        ("поставь на паузу", "builtin.media.pause", {}),
        ("паузу", "builtin.media.pause", {}),
        ("следующий трек", "builtin.media.next", {}),
        ("переключи песню", "builtin.media.next", {}),
        ("что сейчас играет", "builtin.media.now", {}),
        ("громче", "builtin.volume.up", {}),
        ("сделай погромче", "builtin.volume.up", {}),
        ("убавь звук", "builtin.volume.down", {}),
        ("громкость на пятьдесят", "builtin.volume.set", {"number": 50}),
        ("громкость 50%", "builtin.volume.set", {"number": 50}),
        ("громкость на двадцать пять процентов", "builtin.volume.set", {"number": 25}),
        ("поставь громкость на тридцать пять", "builtin.volume.set", {"number": 35}),
        ("громкость наполовину", "builtin.volume.set", {"number": 50}),
        ("сделай громче на 20", "builtin.volume.up_by", {"number": 20}),
        ("тише на десять", "builtin.volume.down_by", {"number": 10}),
        ("максимальная громкость", "builtin.volume.max", {}),
        ("выключи звук", "builtin.volume.mute", {}),
        ("какая громкость", "builtin.volume.info", {}),
        # окна, система, папки
        ("сверни все окна", "builtin.windows.minimize_all", {}),
        ("покажи рабочий стол", "builtin.windows.desktop", {}),
        ("закрой окно", "builtin.windows.close", {}),
        ("выключи компьютер", "builtin.system.shutdown", {}),
        ("выключи компьютер через 30 минут", "builtin.system.shutdown_in", {"number": 30}),
        ("перезагрузи компьютер", "builtin.system.restart", {}),
        ("сделай скриншот", "builtin.system.screenshot", {}),
        ("заблокируй компьютер", "builtin.system.lock", {}),
        ("яркость на 70", "builtin.system.brightness_set", {"number": 70}),
        ("очисти корзину", "builtin.system.empty_recycle_bin", {}),
        ("открой параметры", "builtin.system.settings", {}),
        ("открой загрузки", "builtin.folders.downloads", {}),
        ("открой документы", "builtin.folders.documents", {}),
        # клавиатура и браузер
        ("скопируй", "builtin.keyboard.copy", {}),
        ("нажми энтер", "builtin.keyboard.key_enter", {}),
        ("нажми f5", "builtin.keyboard.key_f5", {}),
        ("нажми контрол шифт эс", "builtin.keyboard.press", {"keys": "ctrl+shift+s"}),
        ("напечатай привет мир", "builtin.keyboard.type", {"text": "привет мир"}),
        ("новая вкладка", "builtin.browser.new_tab", {}),
        ("перейди на третью вкладку", "builtin.browser.tab_3", {}),
        ("обнови страницу", "builtin.browser.refresh", {}),
        ("режим инкогнито", "builtin.browser.incognito", {}),
        # программы: свои фразы и общие со слотом
        ("открой спотифай", "builtin.app.spotify.open", {}),
        ("запусти телегу", "builtin.app.telegram.open", {}),
        ("закрой хром", "builtin.app.chrome.close", {}),
        ("переключись на дискорд", "builtin.app.discord.focus", {}),
        ("открой вс код", "builtin.app.vscode.open", {}),
        ("открой фотошоп", "builtin.app.photoshop.open", {}),
        ("открой диспетчер задач", "builtin.app.task_manager.open", {}),
        ("открой программу калькулятор", "builtin.apps.open", {"app": "Калькулятор"}),
        # сайты и поиск
        ("открой ютуб", "builtin.site.youtube", {}),
        ("зайди в вк", "builtin.site.vk", {}),
        ("открой госуслуги", "builtin.site.gosuslugi", {}),
        ("открой vk.com", "builtin.sites.open", {"site": "vk.com", "url": "https://vk.com"}),
        ("найди в гугле как приготовить борщ", "builtin.search.google", {"query": "как приготовить борщ"}),
        ("найди на ютубе котиков", "builtin.search.youtube", {"query": "котиков"}),
        ("загугли погода в москве", "builtin.search.google", {"query": "погода в москве"}),
        ("найди в яндексе рецепт блинов", "builtin.search.yandex", {"query": "рецепт блинов"}),
        ("поищи новые фильмы", "builtin.search.default", {"query": "новые фильмы"}),
        # информация, джарвис, таймеры
        ("который час", "builtin.info.time", {}),
        ("какое сегодня число", "builtin.info.date", {}),
        ("какая погода", "builtin.info.weather", {}),
        ("какая погода в питере", "builtin.info.weather_city", {"text": "питере"}),
        ("тихий режим", "builtin.jarvis.silent_on", {}),
        ("открой окно джарвиса", "builtin.jarvis.show", {}),
        ("скрой окно", "builtin.jarvis.hide", {}),
        ("поставь таймер на 5 минут", "builtin.timers.minutes", {"number": 5}),
        ("таймер на одну минуту", "builtin.timers.minutes", {"number": 1}),
        ("засеки полчаса", "builtin.timers.half_hour", {}),
        (
            "напомни через 10 минут позвонить маме",
            "builtin.timers.remind_minutes",
            {"number": 10, "text": "позвонить маме"},
        ),
        (
            "напомни мне через двадцать минут выключить духовку",
            "builtin.timers.remind_minutes",
            {"number": 20, "text": "выключить духовку"},
        ),
    ],
)
def test_phrases(ctx: AppContext, text: str, command_id: str, slots: dict[str, Any]) -> None:
    got_id, got_slots = one(ctx.commands, text)  # type: ignore[arg-type]
    assert got_id == command_id
    for key, value in slots.items():
        assert got_slots.get(key) == value


@pytest.mark.parametrize(
    ("text", "command_id"),
    [
        ("открой спортифай", "builtin.app.spotify.open"),  # ошибка распознавания
        ("громкасть на 50", "builtin.volume.set"),
        ("открой ю туб", "builtin.site.youtube"),  # слово разорвано на два
        ("окна все сверни", "builtin.windows.minimize_all"),  # другой порядок слов
        ("сверни окна", "builtin.windows.minimize_all"),
        ("паставь на паузу", "builtin.media.pause"),
    ],
)
def test_tolerates_recognition_errors(ctx: AppContext, text: str, command_id: str) -> None:
    assert one(ctx.commands, text)[0] == command_id  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "какая погода завтра в москве",
        "какая погода завтра в москве и что надеть",
        "какая будет погода на выходных",
        "привет как дела",
        "расскажи анекдот",
        "что такое квантовая физика",
        "сколько будет два плюс два",
        "кто написал войну и мир",
        "напиши письмо начальнику что я заболел",
        "переведи привет на английский",
        "почему небо голубое",
        "открой телефон",
        "включи свет на кухне",
        "выключи свет",
        "как дела у тебя сегодня",
        "что ты думаешь о погоде",
        "открой мне секрет",
        "спасибо",
    ],
)
def test_questions_go_to_ai(ctx: AppContext, text: str) -> None:
    assert ctx.commands.match(text) is None


def test_compound_phrases(ctx: AppContext) -> None:
    def ids(text: str) -> list[str]:
        match = ctx.commands.match(text)
        assert match is not None, text
        return [p.command_id for p in match.parts]

    assert ids("сверни все окна и включи музыку") == ["builtin.windows.minimize_all", "builtin.media.play"]
    assert ids("сверни все окна, включи музыку и поставь громкость на 30") == [
        "builtin.windows.minimize_all",
        "builtin.media.play",
        "builtin.volume.set",
    ]
    assert ids("открой ютуб, а потом сделай громче") == ["builtin.site.youtube", "builtin.volume.up"]
    assert ids("открой ютуб, затем сделай громче") == ["builtin.site.youtube", "builtin.volume.up"]
    # глагол переносится на следующую часть: «закрой хром и (закрой) телеграм»
    assert ids("закрой хром и телеграм") == ["builtin.app.chrome.close", "builtin.app.telegram.close"]
    assert ids("найди котиков и включи музыку") == ["builtin.search.default", "builtin.media.play"]
    # «и» внутри свободного текста не делит фразу
    match = ctx.commands.match("найди в гугле кошки и собаки")
    assert match is not None and len(match.parts) == 1
    assert match.parts[0].slots["query"] == "кошки и собаки"
    match = ctx.commands.match("напомни через 5 минут купить хлеб и молоко")
    assert match is not None and match.parts[0].slots["text"] == "купить хлеб и молоко"


def test_compound_requires_chainable_parts(ctx: AppContext) -> None:
    cmds: Commands = ctx.commands  # type: ignore[assignment]
    cmds.create(
        CommandIn(
            name="Утренний режим",
            phrases=["утренний режим"],
            chain=False,
            actions=[{"type": "Media.PlayPause", "value": "play"}],
        )
    )  # type: ignore[list-item]
    assert cmds.match("утренний режим") is not None
    assert cmds.match("утренний режим и сверни все окна") is None


def test_installed_apps_resolve_by_start_menu_name(ctx: AppContext) -> None:
    ctx.platform.list_apps = lambda refresh=False: [  # type: ignore[method-assign]
        AppInfo("Krita", "krita.exe", r"C:\Program Files\Krita\bin\krita.exe"),
        AppInfo("Adobe Photoshop 2026", "Photoshop.exe", ""),
    ]
    cmds: Commands = ctx.commands  # type: ignore[assignment]
    cmds.apps.set_apps(ctx.platform.list_apps(), time.time())
    command_id, slots = one(cmds, "открой крита")
    assert command_id == "builtin.apps.open"
    assert slots["app"] == "Krita" and slots["app_id"] == "krita.exe"
    assert one(cmds, "закрой криту")[1]["app"] == "Krita"
    assert cmds.apps.resolve("фотошоп").name == "Adobe Photoshop 2026"  # type: ignore[union-attr]


def test_user_command_wins_over_builtin(ctx: AppContext) -> None:
    cmds: Commands = ctx.commands  # type: ignore[assignment]
    info = cmds.create(
        CommandIn(
            name="Моя музыка", phrases=["включи музыку"], actions=[{"type": "Launch.App", "value": "Spotify"}]
        )
    )  # type: ignore[list-item]
    assert one(cmds, "включи музыку")[0] == info["id"]


def test_optional_words(ctx: AppContext) -> None:
    cmds: Commands = ctx.commands  # type: ignore[assignment]
    info = cmds.create(
        CommandIn(
            name="Режим стрима",
            phrases=["режим стрима"],
            optional=["мой", "любимый"],
            actions=[{"type": "Launch.App", "value": "OBS Studio"}],
        )
    )  # type: ignore[list-item]
    assert one(cmds, "режим стрима")[0] == info["id"]
    assert one(cmds, "мой любимый режим стрима")[0] == info["id"]
    assert one(cmds, "режим стрима мой")[0] == info["id"]
    # чужие необязательные слова не помогают другим командам
    assert cmds.match("мой любимый новая вкладка") is None


def test_disabled_command_is_not_matched(ctx: AppContext) -> None:
    cmds: Commands = ctx.commands  # type: ignore[assignment]
    cmds.set_enabled("builtin.media.next", False)
    match = cmds.match("следующий трек")
    assert match is None or match.parts[0].command_id != "builtin.media.next"
    cmds.set_enabled("builtin.media.next", True)
    assert one(cmds, "следующий трек")[0] == "builtin.media.next"


def test_dynamic_literal_beats_builtin_template(ctx: AppContext) -> None:
    """Фраза умного дома «какая температура в спальне» важнее шаблона погоды «какая температура в {text}»."""

    async def handler(slots: dict[str, Any]) -> ExecResult:
        return ExecResult(True, "Температура в спальне", "22°")

    cmds: Commands = ctx.commands  # type: ignore[assignment]
    cmds.register_provider(
        "smart_home_test",
        lambda: [
            DynamicCommand(
                "plugin.smart_home:temp_bedroom",
                "Температура в спальне",
                ["какая температура в спальне"],
                handler,
                category="smarthome",
            ),
        ],
    )
    cmds.refresh_dynamic()
    assert one(cmds, "какая температура в спальне")[0] == "plugin.smart_home:temp_bedroom"
    assert one(cmds, "какая температура в спальни")[0] == "plugin.smart_home:temp_bedroom"  # чуть неточно
    assert one(cmds, "какая температура в казани")[0] == "builtin.info.weather_city"


def test_literal_phrase_wins_over_free_text_template(ctx: AppContext) -> None:
    # «напечатай документ» — печать (Ctrl+P), а не набор слова «документ»
    assert one(ctx.commands, "напечатай документ")[0] == "builtin.keyboard.print"  # type: ignore[arg-type]


def test_match_text_and_titles(ctx: AppContext) -> None:
    match = ctx.commands.match("Громкость на 50%!")
    assert match is not None
    assert match.text == "громкость на 50%"
    part = match.parts[0]
    assert part.title == "Громкость на 50" and part.score == 100.0


def test_matching_is_fast_with_all_packs(ctx: AppContext) -> None:
    cmds: Commands = ctx.commands  # type: ignore[assignment]
    ctx.settings.patch({"packs": [p.id for p in cmds.packs.all()]})
    assert len(cmds._index.entries) > 1000
    phrases = [
        "включи музыку",
        "громкость на двадцать пять процентов",
        "открой спортифай",
        "сверни все окна и включи музыку",
        "найди на ютубе котиков",
        "какая погода завтра в москве и что надеть",
        "напомни через 10 минут позвонить маме",
        "закрой хром и телеграм",
        "нажми контрол шифт эс",
        "что такое квантовая физика",
        "открой телефон",
        "спотифай следующий трек",
        "перейди на третью вкладку",
        "выключи компьютер через 30 минут",
    ]
    for p in phrases:  # прогрев кэшей
        cmds.match(p)
    start = time.process_time()
    rounds = 5
    for _ in range(rounds):
        for p in phrases:
            cmds.match(p)
    per_phrase_ms = (time.process_time() - start) * 1000 / (rounds * len(phrases))
    assert per_phrase_ms < 20, f"{per_phrase_ms:.1f} мс на фразу"


def test_opposites_are_never_confused(ctx: AppContext) -> None:
    """«включи» и «выключи» различаются одной приставкой — нечёткое сравнение не должно их путать."""
    cmds: Commands = ctx.commands  # type: ignore[assignment]
    cmds.set_enabled("builtin.media.play", False)
    match = cmds.match("включи музыку")
    assert match is None or match.parts[0].command_id != "builtin.media.pause"
    assert cmds.match("включи компьютер") is None
    match = cmds.match("разблокируй компьютер")
    assert match is None or match.parts[0].command_id != "builtin.system.lock"
    match = cmds.match("отключи звук")
    assert match is not None and match.parts[0].command_id == "builtin.volume.mute"
