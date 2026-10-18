"""Встроенные команды: время, дата, погода, тихий режим, окно Jarvis, столы, таймеры и напоминания."""

from jarvis.commands.library.base import Builtin, cmd
from jarvis.commands.library.base import variants as v

N = "{number}"
T = "{text}"
# погода «на завтра» и прогнозы — это вопрос к ИИ, а не «какая погода сейчас»
_FORECAST = (
    "завтр",
    "послезавтр",
    "выходн",
    "недел",
    "вечер",
    "утр",
    "ноч",
    "днем",
    "будет",
    "прогноз",
    "через",
    "надеть",
    "одеться",
    "зонт",
    "месяц",
)


def _info() -> list[Builtin]:
    c = "info"
    return [
        cmd(
            "info.time",
            "Который час",
            v(
                "который (сейчас |)час",
                "сколько (сейчас |)времени",
                "сколько время",
                "(текущее |)время",
                "какое (сейчас |)время",
                "скажи время",
                "подскажи время",
                "время сейчас",
            ),
            [("Info.Time", "")],
            category=c,
            icon="clock",
        ),
        cmd(
            "info.date",
            "Какое сегодня число",
            v(
                "какое (сегодня |)число",
                "какая (сегодня |)дата",
                "(сегодняшняя|текущая) дата",
                "какой (сегодня |)день",
                "какой (сегодня |)день недели",
                "число сегодня",
                "дата сегодня",
                "какое число сегодня",
            ),
            [("Info.Date", "")],
            category=c,
            icon="calendar",
        ),
        cmd(
            "info.weather",
            "Погода",
            v(
                "какая (сейчас |)погода",
                "погода",
                "погоду",
                "погода сейчас",
                "что с погодой",
                "как на улице",
                "сколько (сейчас |)градусов (на улице|)",
                "какая (сейчас |)температура (на улице|)",
                "какая погода на улице",
                "погода на улице",
                "холодно на улице",
                "тепло на улице",
            ),
            [("Info.Weather", "")],
            category=c,
            icon="weather",
            exclude=_FORECAST,
        ),
        cmd(
            "info.weather_city",
            "Погода в городе",
            v(
                f"какая (сейчас |)погода (в|во) {T}",
                f"погода (в|во) {T}",
                f"сколько градусов (в|во) {T}",
                f"какая температура (в|во) {T}",
                f"что с погодой (в|во) {T}",
            ),
            [("Info.Weather", T)],
            category=c,
            icon="weather",
            chain=False,
            exclude=_FORECAST,
        ),
    ]


def _jarvis() -> list[Builtin]:
    c = "jarvis"
    return [
        cmd(
            "jarvis.silent_on",
            "Включить тихий режим",
            v(
                "(включи |)тихий режим",
                "режим тишины",
                "не разговаривай",
                "молчи",
                "работай молча",
                "без ответов голосом",
            ),
            [("Jarvis.SilentMode", "on")],
            category=c,
            icon="mute",
        ),
        cmd(
            "jarvis.silent_off",
            "Выключить тихий режим",
            v(
                "(выключи|отключи|убери) тихий режим",
                "можешь говорить",
                "говори (вслух|снова|)",
                "режим разговора",
                "отвечай голосом",
            ),
            [("Jarvis.SilentMode", "off")],
            category=c,
            icon="volume",
        ),
        cmd(
            "jarvis.mic_off",
            "Перестать слушать",
            v(
                "не слушай( меня|)",
                "перестань( меня|) слушать",
                "хватит( меня|) слушать",
                "(выключи|отключи) (прослушивание|свой микрофон|микрофон джарвиса)",
                "не подслушивай",
            ),
            [("Jarvis.Microphone", "off")],
            category=c,
            icon="mic-off",
        ),
        cmd(
            "jarvis.mic_on",
            "Снова слушать",
            v(
                "(включи) (прослушивание|свой микрофон|микрофон джарвиса)",
                "слушай( меня|) снова",
                "снова слушай( меня|)",
            ),
            [("Jarvis.Microphone", "on")],
            category=c,
            icon="mic",
        ),
        cmd(
            "jarvis.show",
            "Показать окно Jarvis",
            v(
                "(открой|покажи|разверни) (окно джарвиса|главное окно|свое окно|джарвиса)",
                "покажи себя",
                "покажись",
                "открой приложение джарвис",
            ),
            [("Jarvis.ShowWindow", "show")],
            category=c,
            icon="home",
        ),
        cmd(
            "jarvis.hide",
            "Скрыть окно Jarvis",
            v(
                "(скрой|спрячь|сверни) (окно джарвиса|свое окно|себя)",
                "скрой окно",
                "спрячься",
                "скройся",
            ),
            [("Jarvis.ShowWindow", "hide")],
            category=c,
            icon="home",
        ),
        cmd(
            "jarvis.settings",
            "Настройки Jarvis",
            v(
                "(открой |)(настройки джарвиса|настройки ассистента|свои настройки)",
            ),
            [("Jarvis.ShowWindow", "settings")],
            category=c,
            icon="home",
        ),
        cmd(
            "jarvis.commands",
            "Список команд",
            v(
                "(открой |покажи |)(список команд|команды джарвиса|все команды)",
                "что ты умеешь",
                "какие (есть |у тебя |)команды",
                "покажи команды",
            ),
            [("Jarvis.ShowWindow", "commands")],
            category=c,
            icon="home",
        ),
        cmd(
            "jarvis.chat",
            "Открыть чат",
            v("(открой|покажи) (чат|чат с ии|переписку)"),
            [("Jarvis.ShowWindow", "chat")],
            category=c,
            icon="ai",
        ),
        cmd(
            "jarvis.desk",
            "Открыть стол",
            v(
                "(открой|покажи) стол {desk}",
                "(переключи|переключись) на стол {desk}",
                "стол {desk}",
            ),
            [("Jarvis.OpenDesk", "{desk}")],
            category=c,
            icon="desk",
        ),
        cmd(
            "jarvis.ask",
            "Спросить ИИ",
            v(
                f"спроси (у ии|ии|у нейросети|нейросеть|у чата) {T}",
                f"вопрос (для ии|нейросети) {T}",
            ),
            [("Jarvis.Ask", T)],
            category=c,
            icon="ai",
            chain=False,
        ),
    ]


def _timers() -> list[Builtin]:
    c = "timers"
    put = "(поставь |запусти |заведи |установи |включи |)таймер"
    remind = "(напомни|поставь напоминание)"
    return [
        cmd(
            "timers.minutes",
            "Таймер на N минут",
            v(
                f"{put} на {N} (минут|мин)",
                f"засеки {N} минут",
                f"таймер {N} минут",
            ),
            [("Jarvis.Timer", f"{N}m")],
            category=c,
            icon="timer",
        ),
        cmd(
            "timers.seconds",
            "Таймер на N секунд",
            v(
                f"{put} на {N} (секунд|сек)",
                f"засеки {N} секунд",
                f"таймер {N} секунд",
            ),
            [("Jarvis.Timer", f"{N}s")],
            category=c,
            icon="timer",
        ),
        cmd(
            "timers.hours",
            "Таймер на N часов",
            v(f"{put} на {N} часов", f"засеки {N} часов"),
            [("Jarvis.Timer", f"{N}h")],
            category=c,
            icon="timer",
        ),
        cmd(
            "timers.hour",
            "Таймер на час",
            v(f"{put} на час", "засеки час"),
            [("Jarvis.Timer", "1h")],
            category=c,
            icon="timer",
        ),
        cmd(
            "timers.half_hour",
            "Таймер на полчаса",
            v(f"{put} на (полчаса|пол часа)", "засеки полчаса"),
            [("Jarvis.Timer", "30m")],
            category=c,
            icon="timer",
        ),
        cmd(
            "timers.minute",
            "Таймер на минуту",
            v(f"{put} на минуту", "засеки минуту"),
            [("Jarvis.Timer", "1m")],
            category=c,
            icon="timer",
        ),
        cmd(
            "timers.remind_minutes",
            "Напомнить через N минут",
            v(
                f"{remind} через {N} (минут|мин) {T}",
                f"напомни {T} через {N} минут",
            ),
            [("Jarvis.Reminder", f"{N}m|{T}")],
            category=c,
            icon="note",
            chain=False,
            optional=("мне",),
        ),
        cmd(
            "timers.remind_hours",
            "Напомнить через N часов",
            v(
                f"{remind} через {N} часов {T}",
                f"напомни {T} через {N} часов",
            ),
            [("Jarvis.Reminder", f"{N}h|{T}")],
            category=c,
            icon="note",
            chain=False,
            optional=("мне",),
        ),
        cmd(
            "timers.remind_hour",
            "Напомнить через час",
            v(f"{remind} через час {T}", f"напомни {T} через час"),
            [("Jarvis.Reminder", f"1h|{T}")],
            category=c,
            icon="note",
            chain=False,
            optional=("мне",),
        ),
        cmd(
            "timers.remind_half_hour",
            "Напомнить через полчаса",
            v(
                f"{remind} через (полчаса|пол часа) {T}",
                f"напомни {T} через полчаса",
            ),
            [("Jarvis.Reminder", f"30m|{T}")],
            category=c,
            icon="note",
            chain=False,
            optional=("мне",),
        ),
        cmd(
            "timers.remind_minute",
            "Напомнить через минуту",
            v(
                f"{remind} через минуту {T}",
                f"напомни {T} через минуту",
            ),
            [("Jarvis.Reminder", f"1m|{T}")],
            category=c,
            icon="note",
            chain=False,
            optional=("мне",),
        ),
    ]


def commands() -> list[Builtin]:
    return [*_info(), *_jarvis(), *_timers()]
