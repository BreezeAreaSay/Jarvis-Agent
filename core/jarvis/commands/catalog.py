"""Каталог действий: что умеет шаг команды, как его показать и настроить в редакторе
(GET /api/commands/actions).

Сверх списка из docs/CONTRACTS.md §2 есть Info.Volume, Info.Media, Info.Battery, Info.System, Jarvis.Timer и
Jarvis.Reminder — их используют встроенные команды («какая громкость», «поставь таймер на 5 минут»).
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Param:
    kind: str  # text | number | keys | path | app | url | select | coords
    label: str
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()
    name: str = "value"

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "kind": self.kind, "label": self.label}
        if self.placeholder:
            data["placeholder"] = self.placeholder
        if self.options:
            data["options"] = [{"value": v, "label": label} for v, label in self.options]
        return data


@dataclass(frozen=True)
class ActionType:
    type: str
    title: str
    category: str
    icon: str
    params: tuple[Param, ...] = ()
    description: str = ""
    step: str = ""  # заголовок шага в «Создать с помощью AI»: «Запустить программу — OBS Studio»
    required: bool = False  # значение обязательно
    user_only: bool = False  # только в своих командах (не в паках и не во встроенных)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "category": self.category,
            "icon": self.icon,
            "params": [{**p.to_json(), "required": self.required} for p in self.params],
            "description": self.description,
        }


def _a(
    type_: str,
    title: str,
    category: str,
    icon: str,
    param: Param | None = None,
    description: str = "",
    *,
    step: str = "",
    required: bool = False,
    user_only: bool = False,
) -> ActionType:
    return ActionType(
        type_,
        title,
        category,
        icon,
        (param,) if param else (),
        description,
        step or title,
        required,
        user_only,
    )


_COORDS = Param("coords", "Точка на экране", "960 540 (пусто — где сейчас курсор)")
_APP = "Программа"

ACTIONS: tuple[ActionType, ...] = (
    # запуск
    _a(
        "Launch.App",
        "Запустить программу",
        "Запуск",
        "apps",
        Param("app", _APP, "Spotify"),
        "Программа из меню «Пуск» по названию («Spotify», «OBS Studio») или путь к exe.",
        required=True,
    ),
    _a(
        "Launch.File",
        "Открыть файл",
        "Запуск",
        "folder",
        Param("path", "Файл", "C:\\Program Files\\…\\program.exe"),
        'Файл или программа по пути. Аргументы — после пути в кавычках: "C:\\…\\obs64.exe" --startrecording',
        required=True,
    ),
    _a(
        "Launch.Url",
        "Открыть сайт",
        "Запуск",
        "site",
        Param("url", "Адрес", "https://youtube.com"),
        "Адрес сайта, ссылка приложения (spotify:, steam://…) или название сайта («ютуб»).",
        required=True,
    ),
    _a(
        "Launch.Folder",
        "Открыть папку",
        "Запуск",
        "folder",
        Param("path", "Папка", "downloads или C:\\Users\\…"),
        "Путь или особая папка: downloads, documents, desktop, pictures, music, videos.",
        required=True,
    ),
    _a(
        "Launch.Shell",
        "Команда консоли",
        "Запуск",
        "keyboard",
        Param("text", "Команда", "shutdown /a"),
        "Строка для командной оболочки Windows. Только в своих командах.",
        step="Выполнить команду",
        required=True,
        user_only=True,
    ),
    # программы
    _a(
        "App.Close",
        "Закрыть программу",
        "Программы",
        "window",
        Param("app", _APP, "Telegram"),
        "Мягко закрыть окна программы (как крестиком).",
        required=True,
    ),
    _a(
        "App.Kill",
        "Завершить процесс",
        "Программы",
        "error",
        Param("app", "Процесс", "chrome.exe"),
        "Принудительно завершить процесс — несохранённое пропадёт.",
        required=True,
    ),
    _a(
        "App.Focus",
        "Переключиться на программу",
        "Программы",
        "window",
        Param("app", _APP, "Discord"),
        "Вывести окно программы на передний план (нужно перед горячими клавишами программы).",
        required=True,
    ),
    _a(
        "Ui.Click",
        "Нажать кнопку в окне",
        "Программы",
        "mouse",
        Param("text", "Кнопка, ссылка или пункт", "Отправить"),
        "Нажать элемент окна впереди по названию (как его называет Windows); пока его нет — подождать. "
        "Не нашёлся — дело передаётся агенту.",
        step="Нажать кнопку",
        required=True,
    ),
    _a(
        "Ui.Type",
        "Ввести текст в поле",
        "Программы",
        "keyboard",
        Param("text", "Поле | текст", "Кому | ivan@example.com"),
        "Название поля окна впереди и текст через «|»: содержимое поля заменится.",
        step="Ввести текст",
        required=True,
    ),
    # клавиатура
    _a(
        "Keys.Press",
        "Нажать клавиши",
        "Клавиатура",
        "keyboard",
        Param("keys", "Сочетание", "ctrl+shift+s"),
        "Сочетание или несколько через пробел: «ctrl+k ctrl+s», повтор — «down*3».",
        step="Нажать клавишу",
        required=True,
    ),
    _a(
        "Keys.Type",
        "Напечатать текст",
        "Клавиатура",
        "keyboard",
        Param("text", "Текст", "Что напечатать"),
        "Ввести текст посимвольно в активное окно.",
        required=True,
    ),
    _a(
        "Keys.Paste",
        "Вставить текст",
        "Клавиатура",
        "keyboard",
        Param("text", "Текст", "Что вставить"),
        "Вставить текст через буфер обмена (быстро, буфер потом восстанавливается).",
        required=True,
    ),
    # мышь
    _a("Mouse.ClickLeft", "Клик левой кнопкой", "Мышь", "mouse", _COORDS, "Координаты «x y» или пусто."),
    _a("Mouse.ClickRight", "Клик правой кнопкой", "Мышь", "mouse", _COORDS, "Координаты «x y» или пусто."),
    _a("Mouse.DoubleClick", "Двойной клик", "Мышь", "mouse", _COORDS, "Координаты «x y» или пусто."),
    _a(
        "Mouse.Move",
        "Переместить курсор",
        "Мышь",
        "mouse",
        Param("coords", "Точка на экране", "960 540"),
        "Переместить курсор в точку «x y».",
        required=True,
    ),
    _a(
        "Mouse.Scroll",
        "Прокрутить колесо",
        "Мышь",
        "mouse",
        Param("number", "Щелчков (минус — вниз)", "-5"),
        "Положительное число — вверх, отрицательное — вниз.",
    ),
    # звук
    _a(
        "System.VolumeSet",
        "Громкость",
        "Звук",
        "volume",
        Param("number", "Громкость, %", "35"),
        "Установить громкость 0–100.",
        required=True,
    ),
    _a(
        "System.VolumeUp",
        "Громче",
        "Звук",
        "volume",
        Param("number", "На сколько, %", "10"),
        "Прибавить громкость (по умолчанию на 10).",
    ),
    _a(
        "System.VolumeDown",
        "Тише",
        "Звук",
        "volume",
        Param("number", "На сколько, %", "10"),
        "Убавить громкость (по умолчанию на 10).",
    ),
    _a("System.Mute", "Выключить звук", "Звук", "mute"),
    _a("System.Unmute", "Включить звук", "Звук", "volume"),
    _a("System.MuteToggle", "Звук вкл/выкл", "Звук", "mute"),
    # медиа
    _a(
        "Media.PlayPause",
        "Пауза / продолжить",
        "Медиа",
        "media",
        Param(
            "select",
            "Что сделать",
            "",
            (("", "Переключить"), ("play", "Только включить"), ("pause", "Только пауза")),
        ),
        "Клавиша Play/Pause. «Только включить» запустит музыку, если ничего не играет.",
        step="Нажать клавишу",
    ),
    _a("Media.Next", "Следующий трек", "Медиа", "media", step="Нажать клавишу"),
    _a("Media.Prev", "Предыдущий трек", "Медиа", "media", step="Нажать клавишу"),
    _a("Media.Stop", "Остановить", "Медиа", "media", step="Нажать клавишу"),
    # окна
    _a("Window.MinimizeAll", "Свернуть все окна", "Окна", "window"),
    _a(
        "Window.Minimize",
        "Свернуть окно",
        "Окна",
        "window",
        Param("app", "Чьё окно", "пусто — активное"),
        "Пусто — активное окно, иначе — окно программы.",
    ),
    _a("Window.Maximize", "Развернуть окно", "Окна", "window", Param("app", "Чьё окно", "пусто — активное")),
    _a("Window.Restore", "Восстановить окно", "Окна", "window", Param("app", "Чьё окно", "пусто — активное")),
    _a("Window.Close", "Закрыть окно", "Окна", "window", Param("app", "Чьё окно", "пусто — активное")),
    # система
    _a(
        "System.BrightnessSet",
        "Яркость",
        "Система",
        "system",
        Param("number", "Яркость, %", "70"),
        "0–100; «+10» / «-10» — изменить относительно текущей.",
        required=True,
    ),
    _a("System.Lock", "Заблокировать компьютер", "Система", "lock"),
    _a("System.Sleep", "Спящий режим", "Система", "power"),
    _a(
        "System.Shutdown",
        "Выключить компьютер",
        "Система",
        "power",
        Param("number", "Через, секунд", "0"),
        "Задержка в секундах (или «5m» — минуты).",
    ),
    _a(
        "System.Restart", "Перезагрузить компьютер", "Система", "power", Param("number", "Через, секунд", "0")
    ),
    _a("System.CancelShutdown", "Отменить выключение", "Система", "power"),
    _a(
        "System.Screenshot",
        "Снимок экрана",
        "Система",
        "screenshot",
        Param("select", "Что снять", "", (("", "Весь экран"), ("window", "Активное окно"))),
        "Снимок сохраняется в «Изображения\\Screenshots».",
    ),
    _a("System.EmptyRecycleBin", "Очистить корзину", "Система", "system"),
    # сценарий
    _a(
        "Wait",
        "Пауза",
        "Сценарий",
        "timer",
        Param("number", "Секунд", "2"),
        "Подождать перед следующим шагом.",
        required=True,
    ),
    _a("Speak", "Сказать вслух", "Сценарий", "ai", Param("text", "Текст", "Готово, сэр"), required=True),
    _a(
        "Notify",
        "Уведомление",
        "Сценарий",
        "bell",
        Param("text", "Текст", "Текст уведомления"),
        required=True,
    ),
    _a(
        "Clipboard.Set",
        "Скопировать в буфер",
        "Сценарий",
        "note",
        Param("text", "Текст", "Что скопировать"),
        required=True,
    ),
    _a(
        "Web.Search",
        "Поиск в интернете",
        "Поиск",
        "search",
        Param("text", "Запрос", "google: {query}"),
        "Запрос с приставкой поисковика: google:, yandex:, youtube:, bing:, duckduckgo:, wikipedia:, "
        "images:, "
        "maps: или сайта (avito:, ozon:…). Без приставки — Google.",
        step="Найти",
        required=True,
    ),
    # информация
    _a("Info.Time", "Сказать время", "Информация", "clock"),
    _a("Info.Date", "Сказать дату", "Информация", "calendar"),
    _a("Info.Weather", "Сказать погоду", "Информация", "weather", Param("text", "Город", "как в профиле")),
    _a("Info.Volume", "Сказать громкость", "Информация", "volume"),
    _a("Info.Media", "Что сейчас играет", "Информация", "media"),
    _a("Info.Battery", "Заряд батареи", "Информация", "system"),
    _a("Info.System", "Загрузка компьютера", "Информация", "system"),
    # джарвис
    _a("Jarvis.OpenDesk", "Открыть стол", "Джарвис", "desk", Param("text", "Стол", "Игры"), required=True),
    _a("Jarvis.Ask", "Спросить Jarvis AI", "Джарвис", "ai", Param("text", "Вопрос", "{text}"), required=True),
    _a(
        "Jarvis.Command",
        "Выполнить команду",
        "Джарвис",
        "command",
        Param("text", "Команда", "builtin.media.play или «сверни все окна»"),
        "id другой команды или её фраза.",
        required=True,
    ),
    _a(
        "Jarvis.Plugin",
        "Действие плагина",
        "Джарвис",
        "plugin",
        Param("text", "Действие", "dictation:toggle"),
        "Плагин и действие через двоеточие: dictation:toggle, reader:read, summary:start.",
        required=True,
    ),
    _a(
        "Jarvis.SilentMode",
        "Тихий режим",
        "Джарвис",
        "mute",
        Param("select", "Режим", "", (("toggle", "Переключить"), ("on", "Включить"), ("off", "Выключить"))),
    ),
    _a(
        "Jarvis.Microphone",
        "Микрофон Jarvis",
        "Джарвис",
        "mic",
        Param("select", "Режим", "", (("toggle", "Переключить"), ("on", "Включить"), ("off", "Выключить"))),
        "Выключенный микрофон голосом обратно не включить — только в трее, хоткеем или в настройках.",
    ),
    _a(
        "Jarvis.ShowWindow",
        "Окно Jarvis",
        "Джарвис",
        "home",
        Param(
            "select",
            "Что сделать",
            "",
            (
                ("show", "Показать"),
                ("hide", "Скрыть"),
                ("settings", "Открыть настройки"),
                ("commands", "Открыть команды"),
                ("chat", "Открыть чат"),
            ),
        ),
    ),
    _a(
        "Jarvis.Timer",
        "Таймер",
        "Джарвис",
        "timer",
        Param("text", "Через", "5m"),
        "Длительность: «90» — секунды, «5m» — минуты, «1h» — часы.",
        required=True,
    ),
    _a(
        "Jarvis.Reminder",
        "Напоминание",
        "Джарвис",
        "note",
        Param("text", "Через и текст", "10m | Позвонить маме"),
        "Через сколько и о чём: «10m | Позвонить маме».",
        required=True,
    ),
    # сводка для сценариев «Доброе утро», «Конец рабочего дня», «С возвращением» (jarvis.scenarios.briefing)
    _a(
        "Jarvis.Briefing",
        "Сводка дня",
        "Джарвис",
        "weather",
        Param(
            "select",
            "Что рассказать",
            "",
            (
                ("", "Утренняя сводка: погода, дела, курсы, новости"),
                ("short", "Коротко: погода и дела"),
                ("tasks", "Незакрытые задачи"),
                ("missed", "Что пропустили, пока вас не было"),
            ),
        ),
        "Приветствие, погода в вашем городе, встречи и задачи на сегодня — голосом и в уведомлении. "
        "Работает без ИИ.",
    ),
)

BY_TYPE: dict[str, ActionType] = {a.type: a for a in ACTIONS}
TYPES: frozenset[str] = frozenset(BY_TYPE)


def get(type_: str) -> ActionType | None:
    return BY_TYPE.get(type_)


def catalog_json() -> list[dict[str, Any]]:
    return [a.to_json() for a in ACTIONS]
