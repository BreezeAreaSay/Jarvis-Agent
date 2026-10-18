"""Выполнение команд: действия по очереди, слоты подставляются в значения («{number}» → «50»), вызовы ОС —
через поток платформы (ctx.run_platform). Ошибка шага останавливает команду; итог — ExecResult для тоста
и голоса.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis import paths
from jarvis.commands import keys as keyops
from jarvis.commands import numbers
from jarvis.commands.matcher import display_title
from jarvis.commands.model import Action, Entry
from jarvis.commands.sites import looks_like_url, search_url, to_url
from jarvis.platform.base import PlatformError
from jarvis.services import ExecResult, Source, UserFacingError

if TYPE_CHECKING:
    from jarvis.commands.service import Commands

log = logging.getLogger(__name__)

MAX_DEPTH = 4  # Jarvis.Command внутри Jarvis.Command…
MAX_WAIT_S = 600.0
SLOT_NAMES = ("number", "app", "site", "desk", "text", "query", "keys", "url", "app_id", "exe")
SLOT_TITLES = {
    "number": "число",
    "app": "программа",
    "site": "сайт",
    "desk": "стол",
    "text": "текст",
    "query": "запрос",
    "keys": "клавиши",
    "url": "адрес",
}
_SLOT_RE = re.compile(r"\{(" + "|".join(SLOT_NAMES) + r")\}")
_COORDS = re.compile(r"^\s*(-?\d+)\s*[ ,;x×]\s*(-?\d+)\s*$")
_URI = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)
_DRIVE = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)

FOLDER_NAMES = {
    "downloads": "downloads",
    "загрузки": "downloads",
    "documents": "documents",
    "документы": "documents",
    "desktop": "desktop",
    "рабочий стол": "desktop",
    "pictures": "pictures",
    "изображения": "pictures",
    "картинки": "pictures",
    "music": "music",
    "музыка": "music",
    "videos": "videos",
    "видео": "videos",
}
MUSIC_APPS = ("spotify", "yandex_music", "aimp", "foobar", "vlc")

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_ORD_UNITS = ("первое", "второе", "третье", "четвёртое", "пятое", "шестое", "седьмое", "восьмое", "девятое")
DAY_ORDINALS = (
    *_ORD_UNITS,
    "десятое",
    "одиннадцатое",
    "двенадцатое",
    "тринадцатое",
    "четырнадцатое",
    "пятнадцатое",
    "шестнадцатое",
    "семнадцатое",
    "восемнадцатое",
    "девятнадцатое",
    "двадцатое",
    *(f"двадцать {u}" for u in _ORD_UNITS),
    "тридцатое",
    "тридцать первое",
)

# «в москве» → «Москва»: город из фразы «какая погода в …» для поиска погоды
CITY_FORMS = {
    "москве": "Москва",
    "питере": "Санкт-Петербург",
    "петербурге": "Санкт-Петербург",
    "санкт-петербурге": "Санкт-Петербург",
    "санкт петербурге": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "казани": "Казань",
    "екатеринбурге": "Екатеринбург",
    "екб": "Екатеринбург",
    "новосибирске": "Новосибирск",
    "самаре": "Самара",
    "нижнем новгороде": "Нижний Новгород",
    "нижнем": "Нижний Новгород",
    "краснодаре": "Краснодар",
    "ростове": "Ростов-на-Дону",
    "ростове-на-дону": "Ростов-на-Дону",
    "уфе": "Уфа",
    "перми": "Пермь",
    "твери": "Тверь",
    "воронеже": "Воронеж",
    "волгограде": "Волгоград",
    "челябинске": "Челябинск",
    "омске": "Омск",
    "красноярске": "Красноярск",
    "саратове": "Саратов",
    "тюмени": "Тюмень",
    "ярославле": "Ярославль",
    "калининграде": "Калининград",
    "владивостоке": "Владивосток",
    "иркутске": "Иркутск",
    "хабаровске": "Хабаровск",
    "минске": "Минск",
    "киеве": "Киев",
    "астане": "Астана",
    "ташкенте": "Ташкент",
    "ереване": "Ереван",
    "лондоне": "Лондон",
    "париже": "Париж",
    "берлине": "Берлин",
    "нью-йорке": "Нью-Йорк",
    "нью йорке": "Нью-Йорк",
    "дубае": "Дубай",
    "стамбуле": "Стамбул",
    "анталии": "Анталья",
    "риме": "Рим",
    "праге": "Прага",
    "варшаве": "Варшава",
    "пекине": "Пекин",
    "туле": "Тула",
    "рязани": "Рязань",
}


@dataclass
class RunState:
    """Что накопилось за выполнение команды: сообщение, фраза для голоса, данные."""

    entry: Entry
    slots: dict[str, Any]
    source: Source
    depth: int = 0
    message: str = ""
    say: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def info(self, message: str, say: str = "") -> None:
        self.message = message
        if say:
            self.say = say


def substitute(value: str, slots: dict[str, Any]) -> str:
    """Подставить слоты: «{number}» → «50». Нужного слота нет — UserFacingError с подсказкой."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        value = slots.get(name)
        if value is None and name in ("text", "query"):
            value = slots.get("query" if name == "text" else "text")
        if value is None or value == "":
            raise UserFacingError(
                f"Не хватает значения «{SLOT_TITLES.get(name, name)}» — скажите его в команде"
            )
        if isinstance(value, float):
            return numbers.fmt_number(value)
        return str(value)

    return _SLOT_RE.sub(repl, value)


def _signed(n: float) -> str:
    v = round(n)
    return f"+{v}" if v > 0 else str(v)


def _degrees_words(n: float) -> str:
    v = round(n)
    sign = "плюс " if v > 0 else "минус " if v < 0 else ""
    return f"{sign}{abs(v)} {numbers.plural(abs(v), numbers.DEGREES)}"


def city_variants(text: str) -> list[str]:
    """«питере» → [«Санкт-Петербург»], «туле» → [«туле», «Тула», …] — варианты для поиска погоды."""
    t = " ".join(text.casefold().replace("ё", "е").split())
    if t in CITY_FORMS:
        return [CITY_FORMS[t]]
    out = [t]
    if t.endswith("е") and len(t) > 3:
        out += [t[:-1] + "а", t[:-1]]
    elif t.endswith("и") and len(t) > 3:
        out += [t[:-1] + "ь", t[:-1] + "а"]
    return [" ".join(w.capitalize() for w in v.split()) for v in dict.fromkeys(out)]


class Executor:
    def __init__(self, service: "Commands") -> None:
        self.service = service
        self.ctx = service.ctx
        self._handlers: dict[str, Callable[[str, RunState], Awaitable[None]]] = {
            "Launch.App": self._launch_app,
            "Launch.File": self._launch_file,
            "Launch.Url": self._launch_url,
            "Launch.Folder": self._launch_folder,
            "Launch.Shell": self._launch_shell,
            "App.Close": self._app_close,
            "App.Kill": self._app_kill,
            "App.Focus": self._app_focus,
            "Keys.Press": self._keys_press,
            "Keys.Type": self._keys_type,
            "Keys.Paste": self._keys_paste,
            "Mouse.ClickLeft": self._click_left,
            "Mouse.ClickRight": self._click_right,
            "Mouse.DoubleClick": self._double_click,
            "Mouse.Move": self._mouse_move,
            "Mouse.Scroll": self._mouse_scroll,
            "System.VolumeSet": self._volume_set,
            "System.VolumeUp": self._volume_up,
            "System.VolumeDown": self._volume_down,
            "System.Mute": self._mute,
            "System.Unmute": self._unmute,
            "System.MuteToggle": self._mute_toggle,
            "System.BrightnessSet": self._brightness,
            "System.Lock": self._lock,
            "System.Sleep": self._sleep,
            "System.Shutdown": self._shutdown,
            "System.Restart": self._restart,
            "System.CancelShutdown": self._cancel_shutdown,
            "System.Screenshot": self._screenshot,
            "System.EmptyRecycleBin": self._empty_recycle_bin,
            "Media.PlayPause": self._play_pause,
            "Media.Next": self._media_next,
            "Media.Prev": self._media_prev,
            "Media.Stop": self._media_stop,
            "Window.MinimizeAll": self._minimize_all,
            "Window.Minimize": self._window_minimize,
            "Window.Maximize": self._window_maximize,
            "Window.Restore": self._window_restore,
            "Window.Close": self._window_close,
            "Wait": self._wait,
            "Speak": self._speak,
            "Notify": self._notify,
            "Clipboard.Set": self._clipboard,
            "Web.Search": self._web_search,
            "Info.Time": self._info_time,
            "Info.Date": self._info_date,
            "Info.Weather": self._info_weather,
            "Info.Volume": self._info_volume,
            "Info.Media": self._info_media,
            "Info.Battery": self._info_battery,
            "Info.System": self._info_system,
            "Jarvis.OpenDesk": self._open_desk,
            "Jarvis.Ask": self._ask,
            "Jarvis.Command": self._run_other,
            "Jarvis.Plugin": self._plugin,
            "Jarvis.SilentMode": self._silent_mode,
            "Jarvis.ShowWindow": self._show_window,
            "Jarvis.Timer": self._timer,
            "Jarvis.Reminder": self._reminder,
        }

    # --- вход ---------------------------------------------------------------------------------------
    async def run_entry(
        self, entry: Entry, slots: dict[str, Any], source: Source, depth: int = 0
    ) -> ExecResult:
        """Выполнить одну команду (подтверждение спрашивает вызывающий)."""
        cmd = entry.command
        title = display_title(entry, slots)
        icon = cmd.icon
        if entry.handler is not None:
            return await self._run_handler(entry, slots, source, title)
        state = RunState(entry, dict(slots), source, depth)
        try:
            for action in cmd.actions:
                await self.run_action(action, state)
            say = substitute(cmd.say, slots) if cmd.say else state.say
        except (UserFacingError, PlatformError) as e:
            return ExecResult(False, title, str(e), icon=icon, command_ids=[cmd.id])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("команда %s упала", cmd.id)
            return ExecResult(False, title, f"Ошибка: {e}", icon="error", command_ids=[cmd.id])
        return ExecResult(
            True,
            title,
            state.message or "Выполнено",
            icon=icon,
            say=say,
            command_ids=[cmd.id],
            data=state.data,
        )

    async def _run_handler(
        self, entry: Entry, slots: dict[str, Any], source: Source, title: str
    ) -> ExecResult:
        assert entry.handler is not None
        cmd = entry.command
        try:
            result = await entry.handler({**slots, "_source": source})
        except (UserFacingError, PlatformError) as e:
            return ExecResult(False, title, str(e), icon=cmd.icon, command_ids=[cmd.id])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("динамическая команда %s упала", cmd.id)
            return ExecResult(False, title, f"Ошибка: {e}", icon="error", command_ids=[cmd.id])
        if not isinstance(result, ExecResult):
            return ExecResult(True, title, str(result or "Выполнено"), icon=cmd.icon, command_ids=[cmd.id])
        if not result.command_ids:
            result.command_ids = [cmd.id]
        if not result.icon:
            result.icon = cmd.icon
        return result

    async def run_action(self, action: Action, state: RunState) -> None:
        handler = self._handlers.get(action.type)
        if handler is None:
            raise UserFacingError(f"Неизвестное действие «{action.type}»")
        value = substitute(action.value, state.slots).strip()
        await handler(value, state)

    async def _platform(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await self.ctx.run_platform(fn, *args)

    # --- запуск -------------------------------------------------------------------------------------
    async def _launch_app(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Не указано, какую программу запустить")
        platform = self.ctx.platform
        if _URI.match(value) and not _DRIVE.match(value):
            await self._platform(platform.open_url, value)
            return
        if _DRIVE.match(value) or "\\" in value or "/" in value:
            await self._platform(platform.launch, value, "")
            return
        target, name = value, value
        if st.slots.get("app") == value and st.slots.get("app_id"):
            target = str(st.slots["app_id"])
        else:
            found = self.service.apps.resolve(value)
            if found is not None:
                target, name = found.app_id, found.name
        if _URI.match(target) and not _DRIVE.match(target):
            await self._platform(platform.open_url, target)
        else:
            await self._platform(platform.launch, target, "")
        st.data["app"] = name

    async def _launch_file(self, value: str, st: RunState) -> None:
        path, args = _split_path_args(value)
        if not path:
            raise UserFacingError("Не указан файл")
        await self._platform(self.ctx.platform.launch, path, args)

    async def _launch_url(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Не указан адрес сайта")
        if looks_like_url(value):
            url = to_url(value)
        else:
            found = self.service.sites.resolve(value)
            if found is None:
                raise UserFacingError(f"Не знаю сайт «{value}»")
            url = found.site.url
        await self._platform(self.ctx.platform.open_url, url)
        st.data["url"] = url

    async def _launch_folder(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Не указана папка")
        path = await self._folder_path(value)
        await self._platform(self.ctx.platform.open_path, path)

    async def _folder_path(self, value: str) -> str:
        key = value.strip().casefold().replace("ё", "е")
        platform = self.ctx.platform
        if key == "screenshots":
            pictures = await self._platform(platform.known_folder, "pictures")
            if not pictures:
                raise UserFacingError("Папка «Изображения» не найдена")
            return _join(pictures, "Screenshots")
        if key in FOLDER_NAMES:
            found = await self._platform(platform.known_folder, FOLDER_NAMES[key])
            if not found:
                raise UserFacingError(f"Папка «{value}» не найдена")
            return str(found)
        if sys.platform == "win32" and "%" in value:
            return os.path.expandvars(value)
        return value

    async def _launch_shell(self, value: str, st: RunState) -> None:
        if st.entry.source != "user":
            raise UserFacingError("Команды консоли можно запускать только из своих команд")
        if not value:
            raise UserFacingError("Не указана команда консоли")
        proc = await asyncio.create_subprocess_shell(
            value,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        st.data["pid"] = proc.pid
        self.ctx.spawn(proc.wait(), "commands-shell")

    # --- программы ----------------------------------------------------------------------------------
    def _app_target(self, value: str, st: RunState) -> tuple[str, str]:
        """Программа из значения → (что передать платформе, название для людей)."""
        if st.slots.get("app") == value and st.slots.get("exe"):
            return str(st.slots["exe"]), value
        if _DRIVE.match(value) or "\\" in value or "/" in value:
            name = re.split(r"[\\/]", value)[-1]
            return name, name
        found = self.service.apps.resolve(value)
        if found is not None:
            return found.target, found.name
        return value, value

    async def _app_close(self, value: str, st: RunState) -> None:
        target, name = self._app_target(value, st)
        closed = await self._platform(self.ctx.platform.close_app, target)
        if not closed:
            raise UserFacingError(f"{name} не запущен")

    async def _app_kill(self, value: str, st: RunState) -> None:
        target, name = self._app_target(value, st)
        killed = await self._platform(self.ctx.platform.kill_process, target)
        if not killed:
            raise UserFacingError(f"Процесс «{name}» не найден")

    async def _app_focus(self, value: str, st: RunState) -> None:
        target, name = self._app_target(value, st)
        platform = self.ctx.platform
        try:
            await self._platform(platform.window_action, "focus", target)
        except PlatformError:
            running = await self._platform(platform.is_running, target)
            if not running:
                raise UserFacingError(f"{name} не запущен") from None
            raise
        await asyncio.sleep(0.15)  # окну нужно мгновение, чтобы выйти на передний план

    # --- клавиатура и мышь --------------------------------------------------------------------------
    async def _keys_press(self, value: str, st: RunState) -> None:
        try:
            seq = keyops.parse_sequence(value)
        except ValueError as e:
            raise UserFacingError(str(e)) from None
        platform = self.ctx.platform

        def press_all() -> None:
            first = True
            for combo, repeat in seq:
                for _ in range(repeat):
                    if not first:
                        time.sleep(0.04)
                    platform.press_keys(combo)
                    first = False

        await self._platform(press_all)

    async def _keys_type(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Нечего печатать")
        await self._platform(self.ctx.platform.type_text, value)

    async def _keys_paste(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Нечего вставлять")
        await self._platform(self.ctx.platform.paste_text, value)

    async def _coords(self, value: str, required: bool = False) -> tuple[int | None, int | None]:
        if not value:
            if required:
                raise UserFacingError("Не указана точка на экране: «x y»")
            return None, None
        if value.casefold() in ("center", "центр"):
            w, h = await self._platform(self.ctx.platform.screen_size)
            return w // 2, h // 2
        m = _COORDS.match(value)
        if not m:
            raise UserFacingError(f"Не понял координаты «{value}» — нужно «x y», например «960 540»")
        return int(m.group(1)), int(m.group(2))

    async def _click(self, value: str, button: str, double: bool) -> None:
        x, y = await self._coords(value)
        await self._platform(self.ctx.platform.mouse_click, x, y, button, double)

    async def _click_left(self, value: str, st: RunState) -> None:
        await self._click(value, "left", False)

    async def _click_right(self, value: str, st: RunState) -> None:
        await self._click(value, "right", False)

    async def _double_click(self, value: str, st: RunState) -> None:
        await self._click(value, "left", True)

    async def _mouse_move(self, value: str, st: RunState) -> None:
        x, y = await self._coords(value, required=True)
        await self._platform(self.ctx.platform.mouse_move, x, y)

    async def _mouse_scroll(self, value: str, st: RunState) -> None:
        amount = _int(value or "-5", "прокрутки")
        await self._platform(self.ctx.platform.mouse_scroll, max(-100, min(100, amount)))

    # --- звук ---------------------------------------------------------------------------------------
    async def _volume_set(self, value: str, st: RunState) -> None:
        platform = self.ctx.platform
        if value[:1] in "+-" and value[1:2].isdigit():
            level = await self._platform(platform.volume_change, _int(value, "громкости"))
        else:
            level = await self._platform(platform.volume_set, max(0, min(100, _int(value, "громкости"))))
        st.info(f"Громкость {level}%")

    async def _volume_up(self, value: str, st: RunState) -> None:
        level = await self._platform(self.ctx.platform.volume_change, abs(_int(value or "10", "шага")))
        st.info(f"Громкость {level}%")

    async def _volume_down(self, value: str, st: RunState) -> None:
        level = await self._platform(self.ctx.platform.volume_change, -abs(_int(value or "10", "шага")))
        st.info(f"Громкость {level}%")

    async def _mute(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.mute_set, True)
        st.info("Звук выключен")

    async def _unmute(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.mute_set, False)
        st.info("Звук включён")

    async def _mute_toggle(self, value: str, st: RunState) -> None:
        muted = await self._platform(self.ctx.platform.mute_set, None)
        st.info("Звук выключен" if muted else "Звук включён")

    # --- система ------------------------------------------------------------------------------------
    async def _brightness(self, value: str, st: RunState) -> None:
        platform = self.ctx.platform
        if value[:1] in "+-":
            current = await self._platform(platform.brightness_get)
            if current is None:
                raise UserFacingError("Яркость этого экрана не меняется программно")
            level = current + _int(value, "яркости")
        else:
            level = _int(value, "яркости")
        level = max(0, min(100, level))
        await self._platform(platform.brightness_set, level)
        st.info(f"Яркость {level}%")

    async def _lock(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.power, "lock", 0)

    async def _sleep(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.power, "sleep", 0)

    async def _power_delayed(self, action: str, value: str, st: RunState, what: str) -> None:
        delay = numbers.parse_duration(value or "0")
        if delay is None or delay < 0 or delay > 7 * 86400:
            raise UserFacingError(f"Не понял задержку «{value}»")
        await self._platform(self.ctx.platform.power, action, int(delay))
        if delay >= 1:
            text = f"{what} через {numbers.duration_text(delay)}"
            st.info(text, text)

    async def _shutdown(self, value: str, st: RunState) -> None:
        await self._power_delayed("shutdown", value, st, "Выключение")

    async def _restart(self, value: str, st: RunState) -> None:
        await self._power_delayed("restart", value, st, "Перезагрузка")

    async def _cancel_shutdown(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.power, "cancel", 0)
        st.info("Выключение отменено")

    async def _screenshot(self, value: str, st: RunState) -> None:
        platform = self.ctx.platform
        hwnd = None
        if value.casefold() == "window":
            window = await self._platform(platform.foreground_window)
            hwnd = window.hwnd if window is not None else None
        png = await self._platform(platform.screenshot, hwnd)
        if not png:
            raise UserFacingError("Не удалось сделать снимок экрана")
        folder = await self._screenshot_dir()
        name = "Снимок " + dt.datetime.now().strftime("%Y-%m-%d %H-%M-%S") + ".png"
        path = folder / name
        await self.ctx.run_io(path.write_bytes, png)
        st.info(f"Сохранён: {name}")
        st.data["path"] = str(path)

    async def _screenshot_dir(self) -> Path:
        if sys.platform == "win32":
            pictures = await self._platform(self.ctx.platform.known_folder, "pictures")
            if pictures:
                folder = Path(pictures) / "Screenshots"
                with contextlib.suppress(OSError):
                    folder.mkdir(parents=True, exist_ok=True)
                    return folder
        return paths.sub_dir("screenshots")

    async def _empty_recycle_bin(self, value: str, st: RunState) -> None:
        platform = self.ctx.platform
        count = await self._platform(platform.recycle_bin_count)
        if not count:
            st.info("Корзина уже пуста")
            return
        await self._platform(platform.empty_recycle_bin)
        st.info(f"Корзина очищена: {numbers.count_text(count, ('файл', 'файла', 'файлов'))}")

    # --- медиа --------------------------------------------------------------------------------------
    async def _play_pause(self, value: str, st: RunState) -> None:
        platform = self.ctx.platform
        mode = value.casefold()
        if mode in ("play", "pause"):
            info = await self._platform(platform.now_playing)
            if mode == "pause" and info is not None and info.status in ("paused", "stopped"):
                st.info("Уже на паузе")
                return
            if mode == "play" and info is not None and info.status == "playing":
                st.info("Музыка уже играет")
                return
            if mode == "play" and info is None and await self._start_music_app(st):
                return
        await self._platform(platform.media_key, "play_pause")

    async def _start_music_app(self, st: RunState) -> bool:
        """Ничего не играет и плеера нет — запустить установленную музыкальную программу и включить музыку."""
        from jarvis.commands.apps import KNOWN_BY_KEY

        for key in MUSIC_APPS:
            installed = self.service.apps.find_installed(KNOWN_BY_KEY[key])
            if installed is None:
                continue
            platform = self.ctx.platform
            await self._platform(platform.launch, installed.app_id, "")
            await asyncio.sleep(4.0)
            await self._platform(platform.media_key, "play_pause")
            st.info(f"Запускаю {installed.name}")
            return True
        return False

    async def _media_next(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.media_key, "next")

    async def _media_prev(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.media_key, "prev")

    async def _media_stop(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.media_key, "stop")

    # --- окна ---------------------------------------------------------------------------------------
    async def _minimize_all(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.window_action, "minimize_all", None)

    async def _window(self, action: str, value: str, st: RunState) -> None:
        target = self._app_target(value, st)[0] if value else None
        await self._platform(self.ctx.platform.window_action, action, target)

    async def _window_minimize(self, value: str, st: RunState) -> None:
        await self._window("minimize", value, st)

    async def _window_maximize(self, value: str, st: RunState) -> None:
        await self._window("maximize", value, st)

    async def _window_restore(self, value: str, st: RunState) -> None:
        await self._window("restore", value, st)

    async def _window_close(self, value: str, st: RunState) -> None:
        await self._window("close", value, st)

    # --- сценарий -----------------------------------------------------------------------------------
    async def _wait(self, value: str, st: RunState) -> None:
        seconds = numbers.parse_duration(value or "1")
        if seconds is None:
            raise UserFacingError(f"Не понял паузу «{value}» — нужно число секунд")
        await asyncio.sleep(max(0.0, min(MAX_WAIT_S, seconds)))

    async def _speak(self, value: str, st: RunState) -> None:
        if not value:
            return
        if self.ctx.settings.value.voice.silent_mode:
            await self._notify(value, st)
            return
        await self.ctx.tts.say(value)

    async def _notify(self, value: str, st: RunState) -> None:
        self.ctx.bus.publish(
            "toast",
            {
                "title": st.entry.command.name,
                "message": value,
                "status": "info",
                "icon": st.entry.command.icon,
                "ts": time.time(),
            },
        )

    async def _clipboard(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.clipboard_set, value)
        st.info("Скопировано в буфер обмена")

    async def _web_search(self, value: str, st: RunState) -> None:
        url, engine, query = search_url(value)
        if not query:
            raise UserFacingError("Не сказано, что искать")
        await self._platform(self.ctx.platform.open_url, url)
        st.info(f"Ищу: {query}")
        st.data.update({"url": url, "engine": engine, "query": query})

    # --- информация ---------------------------------------------------------------------------------
    async def _info_time(self, value: str, st: RunState) -> None:
        now = dt.datetime.now()
        h, m = now.hour, now.minute
        spoken = f"Сейчас {h} {numbers.plural(h, numbers.HOURS)}"
        spoken += f" {m} {numbers.plural(m, numbers.MINUTES)}" if m else " ровно"
        st.info(f"{h:02d}:{m:02d}", spoken)

    async def _info_date(self, value: str, st: RunState) -> None:
        today = dt.date.today()
        weekday = WEEKDAYS[today.weekday()]
        month = MONTHS_GEN[today.month - 1]
        st.info(
            f"{weekday.capitalize()}, {today.day} {month} {today.year}",
            f"Сегодня {weekday}, {DAY_ORDINALS[today.day - 1]} {month}",
        )

    async def _info_weather(self, value: str, st: RunState) -> None:
        data: dict[str, Any] = {}
        for city in city_variants(value) if value else [""]:
            data = await self.ctx.widgets.data("weather", {"city": city} if city else {}) or {}
            if not data.get("error") or "не найден" not in str(data.get("error")):
                break
        if not data or data.get("error"):
            raise UserFacingError(str(data.get("error") or "Нет данных о погоде — проверьте интернет"))
        current = data.get("current") if isinstance(data.get("current"), dict) else data
        temp = _first_number(current, "temp", "temperature", "temp_c")
        if temp is None:
            raise UserFacingError("Сервис погоды не прислал температуру")
        condition = str(current.get("condition") or current.get("description") or current.get("text") or "")
        feels = _first_number(current, "feels", "feels_like", "apparent")
        wind = _first_number(current, "wind", "wind_speed")
        city = str(data.get("city") or value or "")
        parts = [f"{_signed(temp)}°"]
        if condition:
            parts.append(condition)
        if feels is not None and abs(feels - temp) >= 2:
            parts.append(f"ощущается как {_signed(feels)}°")
        if wind is not None:
            parts.append(f"ветер {numbers.fmt_number(round(wind, 1))} м/с")
        message = (f"{city}: " if city else "") + ", ".join(parts)
        spoken = f"Сейчас {_degrees_words(temp)}" + (f", {condition}" if condition else "")
        if feels is not None and abs(feels - temp) >= 2:
            spoken += f", ощущается как {_degrees_words(feels)}"
        if value and city:
            spoken = f"{city}: {spoken[0].lower()}{spoken[1:]}"
        st.info(message, spoken)
        st.data["weather"] = {"city": city, "temp": temp, "condition": condition}

    async def _info_volume(self, value: str, st: RunState) -> None:
        platform = self.ctx.platform
        level = await self._platform(platform.volume_get)
        muted = await self._platform(platform.is_muted)
        message = f"Громкость {level}%" + (", звук выключен" if muted else "")
        spoken = f"Громкость {level} {numbers.plural(level, numbers.PERCENT)}" + (
            ", звук выключен" if muted else ""
        )
        st.info(message, spoken)

    async def _info_media(self, value: str, st: RunState) -> None:
        info = await self._platform(self.ctx.platform.now_playing)
        if info is None or not info.title:
            st.info("Сейчас ничего не играет", "Сейчас ничего не играет")
            return
        who = f"{info.artist} — " if info.artist else ""
        state = "" if info.status == "playing" else " (пауза)" if info.status == "paused" else ""
        message = f"{who}{info.title}{state}" + (f" · {info.app}" if info.app else "")
        spoken = f"Сейчас играет {info.title}" + (f", {info.artist}" if info.artist else "")
        st.info(message, spoken)

    async def _info_battery(self, value: str, st: RunState) -> None:
        stats = await self._platform(self.ctx.platform.system_stats)
        if stats.battery is None:
            st.info("Батареи нет — компьютер работает от сети", "Батареи нет, компьютер работает от сети")
            return
        level = round(stats.battery)
        charging = ", заряжается" if stats.charging else ""
        st.info(
            f"Заряд {level}%{charging}", f"Заряд {level} {numbers.plural(level, numbers.PERCENT)}{charging}"
        )

    async def _info_system(self, value: str, st: RunState) -> None:
        s = await self._platform(self.ctx.platform.system_stats)
        cpu, ram = round(s.cpu), round(s.ram)
        message = f"Процессор {cpu}%, память {ram}%"
        if s.ram_total_gb:
            used = numbers.fmt_number(round(s.ram_used_gb, 1))
            message += f" ({used} из {numbers.fmt_number(round(s.ram_total_gb))} ГБ)"
        if s.gpu is not None:
            message += f", видеокарта {round(s.gpu)}%"
        spoken = (
            f"Процессор загружен на {cpu} {numbers.plural(cpu, numbers.PERCENT)}, "
            f"память на {ram} {numbers.plural(ram, numbers.PERCENT)}"
        )
        st.info(message, spoken)

    # --- джарвис ------------------------------------------------------------------------------------
    async def _open_desk(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Не указан стол")
        desk = self.ctx.desks.open(value)
        if desk is None:
            raise UserFacingError(f"Стол «{value}» не найден")
        st.info(f"Открыт стол «{desk.get('name', value)}»")

    async def _ask(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Не сказано, что спросить")
        if not self.ctx.ai.configured:
            raise UserFacingError("ИИ не настроен — добавьте ключ в настройках")
        result = await self.ctx.ai.ask(value, source=st.source)
        if result.error:
            raise UserFacingError(result.error)
        text = " ".join(result.text.split())
        st.info(text if len(text) <= 160 else text[:159] + "…", (result.spoken or result.text).strip())
        st.data["chat_id"] = result.chat_id

    async def _run_other(self, value: str, st: RunState) -> None:
        if not value:
            raise UserFacingError("Не указана команда")
        if st.depth >= MAX_DEPTH:
            raise UserFacingError("Слишком длинная цепочка команд")
        result = await self.service.run_nested(value, st.slots, st.source, st.depth + 1)
        if not result.ok:
            raise UserFacingError(result.message or f"Команда «{value}» не выполнена")
        if result.message and result.message != "Выполнено":
            st.info(result.message, result.say)
        elif result.say:
            st.say = result.say

    async def _plugin(self, value: str, st: RunState) -> None:
        head, _, rest = value.partition("|")
        plugin_id, sep, action = head.strip().partition(":")
        if not sep:
            plugin_id, sep, action = head.strip().partition(".")
        if not plugin_id or not action:
            raise UserFacingError(
                "Действие плагина пишется так: «плагин:действие», например dictation:toggle"
            )
        args: dict[str, Any] = {}
        if rest.strip():
            import json

            try:
                parsed = json.loads(rest)
            except ValueError:
                parsed = {"text": rest.strip()}
            args = parsed if isinstance(parsed, dict) else {"value": parsed}
        result = await self.ctx.plugins.action(plugin_id.strip(), action.strip(), args)
        if isinstance(result, dict) and result.get("ok") is False:
            raise UserFacingError(
                str(result.get("error") or result.get("message") or "Плагин не выполнил действие")
            )
        if isinstance(result, dict) and result.get("message"):
            st.info(str(result["message"]))

    async def _silent_mode(self, value: str, st: RunState) -> None:
        current = self.ctx.settings.value.voice.silent_mode
        mode = (value or "toggle").casefold()
        new = {"on": True, "off": False, "1": True, "0": False}.get(mode, not current)
        if new != current:
            self.ctx.settings.patch({"voice": {"silent_mode": new}})
        if new:
            st.info("Тихий режим включён")
        else:
            st.info("Тихий режим выключен", "Тихий режим выключен")

    async def _show_window(self, value: str, st: RunState) -> None:
        mode = (value or "show").casefold()
        if mode == "hide":
            self.ctx.bus.publish("ui.hide", {})
        elif mode == "show":
            self.ctx.bus.publish("ui.show", {})
        else:
            self.ctx.bus.publish("ui.show", {"page": mode})

    async def _timer(self, value: str, st: RunState) -> None:
        seconds = numbers.parse_duration(value)
        if seconds is None or seconds < 1 or seconds > 7 * 86400:
            raise UserFacingError(f"Не понял длительность таймера «{value}»")
        at = time.time() + seconds
        duration = numbers.duration_text(seconds, accusative=True)
        self.ctx.notes.reminder_add(f"Таймер на {duration}", at)
        when = dt.datetime.fromtimestamp(at).strftime("%H:%M")
        st.info(f"Таймер на {duration} — сработает в {when}", f"Поставил таймер на {duration}")
        st.data.update({"at": at, "seconds": seconds})

    async def _reminder(self, value: str, st: RunState) -> None:
        delay, sep, text = value.partition("|")
        text = text.strip()
        if not sep or not text:
            raise UserFacingError("Не понял, о чём напомнить")
        seconds = numbers.parse_duration(delay.strip(), default_unit="m")
        if seconds is None or seconds < 1 or seconds > 366 * 86400:
            raise UserFacingError(f"Не понял, через сколько напомнить: «{delay.strip()}»")
        at = time.time() + seconds
        self.ctx.notes.reminder_add(text[:1].upper() + text[1:], at)
        when = dt.datetime.fromtimestamp(at).strftime("%H:%M")
        st.info(
            f"Напомню в {when}: {text}", f"Напомню через {numbers.duration_text(seconds, accusative=True)}"
        )
        st.data.update({"at": at, "text": text})


def _int(value: str, what: str) -> int:
    try:
        return round(float(value.replace(",", ".").rstrip("%").strip()))
    except ValueError:
        raise UserFacingError(f"Не понял значение {what}: «{value}»") from None


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            return float(raw)
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                return float(raw.replace(",", ".").replace("°", "").strip())
    return None


def _join(folder: str, name: str) -> str:
    sep = "\\" if ("\\" in folder or _DRIVE.match(folder)) else "/"
    return folder.rstrip("\\/") + sep + name


def _split_path_args(value: str) -> tuple[str, str]:
    """«"C:\\Program Files\\x.exe" --flag» → (путь, аргументы); без кавычек — всё значение путь."""
    v = value.strip()
    if v.startswith('"'):
        end = v.find('"', 1)
        if end > 0:
            return v[1:end], v[end + 1 :].strip()
    return v, ""
