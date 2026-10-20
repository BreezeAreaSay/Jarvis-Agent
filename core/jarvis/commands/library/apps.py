"""Встроенные команды программ: для каждой известной программы — открыть, закрыть, переключиться, свернуть;
и общие «открой {app}», «закрой {app}» для всего, что есть в меню «Пуск».
"""

from jarvis.commands.apps import KNOWN_APPS, KnownApp
from jarvis.commands.library.base import Builtin, cmd
from jarvis.commands.library.base import variants as v
from jarvis.commands.textutil import normalize

_MAX_ALIASES = 5
_MAX_GAME_ALIASES = 9  # игры зовут по-разному: «кс», «контра», «каэс»…
# игру не только открывают: «поставь игру CS2», «давай поиграем в доту»
GAME_VERBS = (
    "(открой|запусти|включи|поставь|запусти игру|включи игру|поставь игру|давай поиграем в|давай сыграем в|"
    "хочу поиграть в|хочу сыграть в)"
)


def _aliases(app: KnownApp) -> list[str]:
    skip = {normalize(a) for a in app.no_phrases}
    out = [normalize(a) for a in app.aliases if normalize(a) not in skip]
    return list(dict.fromkeys(out))[: _MAX_GAME_ALIASES if app.kind == "game" else _MAX_ALIASES]


def _app_commands(app: KnownApp) -> list[Builtin]:
    names = _aliases(app)
    target = app.exe or app.name
    open_verbs = (
        "(открой|запусти)" if app.kind in ("system", "office", "dev", "tool") else "(открой|запусти|включи)"
    )
    if app.kind == "game" and app.launch.startswith(("steam://rungameid/", "com.epicgames.launcher://apps/")):
        open_verbs = GAME_VERBS
    c = "apps"
    return [
        cmd(
            f"app.{app.key}.open",
            f"Открыть {app.name}",
            v(*(f"{open_verbs} {a}" for a in names)),
            [("Launch.App", app.name)],
            category=c,
            icon=app.icon,
        ),
        cmd(
            f"app.{app.key}.close",
            f"Закрыть {app.name}",
            v(*(f"(закрой|выключи) {a}" for a in names)),
            [("App.Close", target)],
            category=c,
            icon=app.icon,
        ),
        cmd(
            f"app.{app.key}.focus",
            f"Переключиться на {app.name}",
            v(
                *(f"(переключись на|перейди в|покажи|разверни) {a}" for a in names),
            ),
            [("App.Focus", target)],
            category=c,
            icon=app.icon,
        ),
        cmd(
            f"app.{app.key}.minimize",
            f"Свернуть {app.name}",
            v(*(f"сверни {a}" for a in names)),
            [("Window.Minimize", target)],
            category=c,
            icon=app.icon,
        ),
    ]


def _generic() -> list[Builtin]:
    c = "apps"
    return [
        # «запусти игровой режим» (не «открой игровой режим» — это страница параметров Windows): окна
        # свёрнуты, Steam — библиотека; любимую игру сразу — свой сценарий с Launch.App «CS2»
        cmd(
            "games.mode",
            "Игровой режим",
            v(
                "(запусти|включи|активируй) игровой режим",
                "игровой режим (включи|запусти|активируй)",
                "(запусти|включи) протокол игры",
            ),
            [("Window.MinimizeAll", ""), ("Launch.Url", "steam://open/games")],
            category=c,
            icon="steam",
            say="Игровой режим: окна свёрнуты, библиотека Steam открыта.",
        ),
        cmd(
            "apps.open",
            "Открыть программу",
            v(
                "(открой|запусти) {app}",
                "(открой|запусти) (программу|приложение|игру) {app}",
                "(поставь|включи) игру {app}",
                "(давай|хочу) (поиграем|сыграем|поиграть|сыграть) в {app}",
            ),
            [("Launch.App", "{app}")],
            category=c,
            icon="apps",
        ),
        cmd(
            "apps.close",
            "Закрыть программу",
            v(
                "(закрой|выключи) {app}",
                "(закрой|выключи) (программу|приложение|игру) {app}",
                "выйди из {app}",
            ),
            [("App.Close", "{app}")],
            category=c,
            icon="apps",
        ),
        cmd(
            "apps.focus",
            "Переключиться на программу",
            v(
                "(переключись|переключи) на {app}",
                "перейди в {app}",
                "покажи (окно |программу |){app}",
            ),
            [("App.Focus", "{app}")],
            category=c,
            icon="apps",
        ),
        cmd(
            "apps.minimize",
            "Свернуть программу",
            v("сверни {app}", "сверни (окно|программу) {app}"),
            [("Window.Minimize", "{app}")],
            category=c,
            icon="apps",
        ),
        cmd(
            "apps.kill",
            "Завершить процесс программы",
            v(
                "(убей|заверши|прибей) (процесс |программу |){app}",
                "принудительно закрой {app}",
                "заверши процесс {app}",
            ),
            [("App.Kill", "{app}")],
            category=c,
            icon="apps",
            confirm=True,
        ),
        cmd(
            "apps.restart",
            "Перезапустить программу",
            v("(перезапусти|перезагрузи) (программу |){app}"),
            [("App.Close", "{app}"), ("Wait", "2"), ("Launch.App", "{app}")],
            category=c,
            icon="apps",
        ),
    ]


def commands() -> list[Builtin]:
    out: list[Builtin] = []
    for app in KNOWN_APPS:
        out.extend(_app_commands(app))
    return [*out, *_generic()]
