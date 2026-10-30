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
from jarvis.ai.format import human_time, one_line, plural
from jarvis.ai.uia import find_by_name
from jarvis.commands import keys as keyops
from jarvis.commands import numbers
from jarvis.commands.matcher import display_title
from jarvis.commands.model import Action, Entry
from jarvis.commands.sites import looks_like_url, search_url, to_url
from jarvis.commands.textutil import normalize
from jarvis.commands.undo import UndoJournal
from jarvis.commands.when import parse_text
from jarvis.platform.base import PlatformError
from jarvis.services import ExecResult, NeedsAgent, Source, UserFacingError

if TYPE_CHECKING:
    from jarvis.commands.service import Commands

log = logging.getLogger(__name__)

MAX_DEPTH = 4  # Jarvis.Command внутри Jarvis.Command…
MAX_WAIT_S = 600.0
SLOT_NAMES = ("number", "app", "site", "desk", "text", "query", "keys", "when", "url", "app_id", "exe")
SLOT_TITLES = {
    "number": "число",
    "app": "программа",
    "site": "сайт",
    "desk": "стол",
    "text": "текст",
    "query": "запрос",
    "keys": "клавиши",
    "when": "когда",
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
# лаунчеры игр — программы, а не игры: «открой Steam» — не «запускаю протокол игры»
GAME_LAUNCHERS = frozenset({"steam", "epic", "battlenet", "ea_app", "ubisoft", "gog", "xbox"})
GAME_LINKS = ("steam://rungameid/", "com.epicgames.launcher://apps/", "goggalaxy://opengameview/")
# «Включи музыку»: заиграла ли — проверяем столько раз через столько секунд (≈2,4 с)
PLAY_CHECKS = 8
PLAY_CHECK_S = 0.3
# Launch.App, за которым ещё действия (сценарий): окно программы ждём не дольше LAUNCH_WAIT_S, новому окну
# даём догрузиться LAUNCH_SETTLE_S (кнопки, плеер), уже открытому — выйти вперёд FOCUS_SETTLE_S
LAUNCH_WAIT_S = 15.0
LAUNCH_POLL_S = 0.25
LAUNCH_SETTLE_S = 1.5
FOCUS_SETTLE_S = 0.3
# Ui.Click / Ui.Type: элемент окна впереди ждём, пока окно догружается; после нажатия — мгновение окну
UI_WAIT_S = 8.0
UI_POLL_S = 0.3
UI_SETTLE_S = 0.4
UI_LIMIT = 300
MUSIC_TASK = (
    "Включи музыку{where}: открой окно программы и нажми кнопку воспроизведения. Если трек не выбран — "
    "запусти подборку на главной (например, «Моя волна» в Яндекс Музыке) и проверь, что музыка заиграла."
)

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
    rest: int = 0  # действий после текущего: запуск программы перед ними ждёт её окна

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


def _window_is(window: Any, name: str) -> bool:
    """Окно программы name: тот же exe («Telegram.exe» или «Telegram») или name — в заголовке."""
    want = name.casefold().removesuffix(".exe")
    exe = str(window.exe or "").casefold().removesuffix(".exe")
    return bool(want) and (exe == want or want in str(window.title or "").casefold())


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
        self.undo = UndoJournal()  # «отмени» и «закрой его» (jarvis.commands.undo)
        self._handlers: dict[str, Callable[[str, RunState], Awaitable[None]]] = {
            "Launch.App": self._launch_app,
            "Launch.File": self._launch_file,
            "Launch.Url": self._launch_url,
            "Launch.Folder": self._launch_folder,
            "Launch.Shell": self._launch_shell,
            "App.Close": self._app_close,
            "App.CloseLast": self._app_close_last,
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
            "Media.Close": self._media_close,
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
            "Jarvis.Microphone": self._microphone,
            "Jarvis.ShowWindow": self._show_window,
            "Jarvis.Timer": self._timer,
            "Jarvis.Reminder": self._reminder,
            "Jarvis.ReminderAt": self._reminder_at,
            "Jarvis.Reminders": self._reminders,
            "Jarvis.ReminderCancel": self._reminder_cancel,
            "Jarvis.Undo": self._undo,
            "Jarvis.Briefing": self._briefing,
            "Ui.Click": self._ui_click,
            "Ui.Type": self._ui_type,
            "Mouse.MoveInWindow": self._mouse_in_window,
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
            for i, action in enumerate(cmd.actions):
                state.rest = len(cmd.actions) - i - 1
                await self.run_action(action, state)
            say = substitute(cmd.say, slots) if cmd.say else state.say
            if not cmd.actions:
                state.data.setdefault("silent", True)  # команда-ответ («спасибо»): только голос, без тоста
        except (UserFacingError, PlatformError) as e:
            data = {"agent_task": e.task} if isinstance(e, NeedsAgent) else {}
            return ExecResult(False, title, str(e), icon=icon, command_ids=[cmd.id], data=data)
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
            if _is_game("", value):  # steam://rungameid/730 — запуск игры ссылкой лаунчера
                st.data["game"] = True
            return
        if _DRIVE.match(value) or "\\" in value or "/" in value:
            await self._platform(platform.launch, value, "")
            return
        target, name, exe, key = value, value, "", ""
        if st.slots.get("app") == value and st.slots.get("app_id"):
            target = str(st.slots["app_id"])
        else:
            found = self.service.apps.resolve(value)
            if found is not None:
                target, name, exe, key = found.app_id, found.name, found.exe, found.key
        before = await self._windows() if st.rest else []
        if _URI.match(target) and not _DRIVE.match(target):
            await self._platform(platform.open_url, target)
        else:
            await self._platform(platform.launch, target, "")
        st.data["app"] = name
        self._remember_launch(value, name)
        if key:
            st.data["app_key"] = key
        if _is_game(key, target):
            st.data["game"] = True  # «Запускаю протокол игры» вместо «Открываю …»
        if st.rest:  # дальше — действия в этой программе: дождаться, пока она откроется
            await self._wait_window(name, exe or target, before)

    async def _windows(self) -> list[Any]:
        try:
            return list(await self._platform(self.ctx.platform.list_windows) or [])
        except PlatformError:
            return []

    async def _wait_window(self, name: str, exe: str, before: list[Any]) -> None:
        """Окно запущенной программы: уже было — дать ему выйти вперёд; нет — ждать, пока появится, и дать
        догрузиться. Не дождались за LAUNCH_WAIT_S — дальше всё равно: окна может и не быть, это не ошибка."""
        file = re.split(r"[\\/]", exe)[-1].casefold()
        stem = file[:-4] if file.endswith(".exe") else ""  # у программ из Магазина — AppUserModelID, не exe
        keys = [k for k in (name.casefold().strip(), stem) if len(k) >= 3]

        def mine(window: Any) -> bool:
            text = f"{window.title} {window.exe}".casefold()
            return any(k in text for k in keys)

        if any(mine(w) for w in before):
            await asyncio.sleep(FOCUS_SETTLE_S)
            return
        seen = {w.hwnd for w in before}
        polls = round(LAUNCH_WAIT_S / LAUNCH_POLL_S)
        for n in range(polls):
            await asyncio.sleep(LAUNCH_POLL_S)
            windows = await self._windows()
            fresh = [w for w in windows if w.hwnd not in seen]
            # своё окно узнаём по названию или процессу; любое новое — тоже, если прошла секунда
            if any(mine(w) for w in fresh) or (fresh and (n + 1) * LAUNCH_POLL_S >= 1.0):
                await asyncio.sleep(LAUNCH_SETTLE_S)
                return
        log.info("команды: окно «%s» не появилось за %.0f с — продолжаю", name, LAUNCH_WAIT_S)

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
        """Команда консоли: слоты без спецсимволов оболочки; ждём код выхода до SHELL_WAIT_S — ошибка видна
        в итоге команды, долгая программа считается запущенной."""
        from jarvis.commands import validate

        if st.entry.source != "user":
            raise UserFacingError("Команды консоли можно запускать только из своих команд")
        if not value:
            raise UserFacingError("Не указана команда консоли")
        unsafe = validate.shell_slot_error(value, st.slots)
        if unsafe:
            raise UserFacingError(unsafe)
        proc = await asyncio.create_subprocess_shell(
            value,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        st.data["pid"] = proc.pid

        async def read_errors() -> bytes:
            # канал читаем до конца (иначе болтливая программа встанет на полном канале), храним только начало
            kept = b""
            while proc.stderr is not None and (chunk := await proc.stderr.read(65536)):
                kept = (kept + chunk)[:4000]
            return kept

        errors = asyncio.ensure_future(read_errors())
        waiter = asyncio.ensure_future(proc.wait())

        async def finish() -> None:
            await asyncio.gather(errors, waiter, return_exceptions=True)

        try:
            done, _ = await asyncio.wait({waiter}, timeout=validate.SHELL_WAIT_S)
        except asyncio.CancelledError:
            self.ctx.spawn(finish(), "commands-shell")  # команду остановили — программа живёт сама по себе
            raise
        if not done:
            self.ctx.spawn(finish(), "commands-shell")
            st.info("Запущено")
            return
        raw = await errors
        code = waiter.result()
        if code:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp866" if sys.platform == "win32" else "latin-1", errors="replace")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            detail = f": {lines[-1][:200]}" if lines else ""
            raise UserFacingError(f"Команда консоли завершилась с ошибкой (код {code}){detail}")

    # --- программы ----------------------------------------------------------------------------------
    def _remember_launch(self, value: str, name: str) -> None:
        """Открытую программу — для «закрой его» и «отмени» (закрыть её)."""
        self.undo.remember_app(value, name)

        async def close() -> None:
            found = self.service.apps.resolve(value)
            await self._platform(self.ctx.platform.close_app, found.target if found is not None else value)
            self.undo.forget_app()

        self.undo.push(f"{name} закрыт", close)

    async def _app_close_last(self, value: str, st: RunState) -> None:
        app = self.undo.last_app()
        if app is None:
            raise UserFacingError("Не знаю, что закрыть: за последние пять минут я ничего не открывал")
        target, _ = self._app_target(app.target, st)
        closed = await self._platform(self.ctx.platform.close_app, target)
        self.undo.forget_app()
        if not closed:
            raise UserFacingError(f"{app.name} уже закрыт")
        st.info(f"Закрыл {app.name}", "Закрыл")
        st.data["closed"] = app.name

    async def _undo(self, value: str, st: RunState) -> None:
        """Вернуть последнее, что Jarvis сделал обратимо (не раньше пяти минут назад); не было — Ctrl+Z."""
        step = self.undo.pop()
        if step is None:
            await self._platform(self.ctx.platform.press_keys, "ctrl+z")
            st.info("Отменил последнее действие в окне (Ctrl+Z)", "Отменил")
            return
        await step.revert()
        st.info(f"Отменил: {step.title}", "Отменил")
        st.data["undone"] = step.title

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
        st.data["closed"] = name

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

    # --- элементы окна (шаги, записанные агентом) ---------------------------------------------------
    async def _ui_click(self, value: str, st: RunState) -> None:
        ui, element = await self._ui_element(value, st)
        await self._platform(ui.click, element.id)
        await asyncio.sleep(UI_SETTLE_S)

    async def _ui_type(self, value: str, st: RunState) -> None:
        name, sep, text = value.partition("|")
        if not sep or not name.strip():
            raise UserFacingError("«Ввести текст в поле»: нужно «поле | текст», например «Кому | Иван»")
        ui, element = await self._ui_element(name.strip(), st)
        await self._platform(ui.set_text, element.id, text.strip())
        await asyncio.sleep(UI_SETTLE_S)

    @staticmethod
    def _handoff(st: RunState, message: str) -> NeedsAgent:
        """Шаг, записанный агентом, не сработал — дело снова берёт агент: задача — сам сценарий (его
        название — это и есть просьба человека)."""
        why = message[:1].lower() + message[1:]
        return NeedsAgent(message, f"{st.entry.command.name}. (Готовый сценарий не справился: {why}.)")

    async def _ui_element(self, name: str, st: RunState) -> tuple[Any, Any]:
        """Элемент окна впереди по названию; пока его нет — ждём (окно догружается). Так и не появился —
        NeedsAgent: дело берёт агент."""
        if not name:
            raise UserFacingError("Не указано, какой элемент окна нужен")
        ui = getattr(getattr(self.ctx, "agent", None), "ui", None)
        if ui is None:
            raise self._handoff(st, "Элементы окон недоступны")
        deadline = time.monotonic() + UI_WAIT_S
        while True:
            try:
                fg = await self._platform(self.ctx.platform.foreground_window)
                snapshot = await self._platform(ui.snapshot, UI_LIMIT, fg.hwnd if fg is not None else None)
            except PlatformError as e:
                raise self._handoff(st, f"Окно не читается: {e}") from e
            element = find_by_name(snapshot.elements, name)
            if element is not None:
                return ui, element
            if time.monotonic() >= deadline:
                raise self._handoff(st, f"В окне «{snapshot.window}» нет «{name}»")
            await asyncio.sleep(UI_POLL_S)

    async def _mouse_in_window(self, value: str, st: RunState) -> None:
        """Курсор — в точку окна программы («Telegram.exe | 150 -40»: от левого и верхнего края, минус — от
        правого и нижнего); окно не впереди — вывести вперёд; окна нет — дело берёт агент."""
        name, sep, point = value.partition("|")
        m = _COORDS.match(point)
        if not sep or not name.strip() or m is None:
            raise UserFacingError("«Навести курсор в окно»: нужно «программа | x y» — «Telegram | 150 60»")
        name, x, y = name.strip(), int(m.group(1)), int(m.group(2))
        platform = self.ctx.platform
        windows = await self._windows()
        front = next((w for w in windows if w.foreground and _window_is(w, name)), None)
        window = front or next((w for w in windows if _window_is(w, name)), None)
        if window is None:
            raise self._handoff(st, f"Окна «{name}» нет")
        if front is None:
            await self._platform(platform.window_action, "focus", window.hwnd)
            await asyncio.sleep(FOCUS_SETTLE_S)
        left, top, right, bottom = window.rect
        px = min(max(left + x if x >= 0 else right + x, left), right - 1)
        py = min(max(top + y if y >= 0 else bottom + y, top), bottom - 1)
        await self._platform(platform.mouse_move, px, py)

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
    async def _volume_before(self) -> int | None:
        try:
            return int(await self._platform(self.ctx.platform.volume_get))
        except (PlatformError, UserFacingError, TypeError, ValueError):
            return None

    def _volume_undo(self, before: int | None, after: int) -> None:
        if before is None or before == after:
            return

        async def revert() -> None:
            await self._platform(self.ctx.platform.volume_set, before)

        self.undo.push(f"громкость снова {before}%", revert)

    async def _volume_set(self, value: str, st: RunState) -> None:
        platform = self.ctx.platform
        before = await self._volume_before()
        if value[:1] in "+-" and value[1:2].isdigit():
            level = await self._platform(platform.volume_change, _int(value, "громкости"))
        else:
            level = await self._platform(platform.volume_set, max(0, min(100, _int(value, "громкости"))))
        self._volume_undo(before, level)
        st.info(f"Громкость {level}%")

    async def _volume_up(self, value: str, st: RunState) -> None:
        before = await self._volume_before()
        level = await self._platform(self.ctx.platform.volume_change, abs(_int(value or "10", "шага")))
        self._volume_undo(before, level)
        st.info(f"Громкость {level}%")

    async def _volume_down(self, value: str, st: RunState) -> None:
        before = await self._volume_before()
        level = await self._platform(self.ctx.platform.volume_change, -abs(_int(value or "10", "шага")))
        self._volume_undo(before, level)
        st.info(f"Громкость {level}%")

    async def _muted(self) -> bool | None:
        try:
            return bool(await self._platform(self.ctx.platform.is_muted))
        except (PlatformError, UserFacingError, AttributeError):
            return None

    def _mute_undo(self, before: bool | None, after: bool) -> None:
        if before is None or before == after:
            return

        async def revert() -> None:
            await self._platform(self.ctx.platform.mute_set, before)

        self.undo.push("звук снова выключен" if before else "звук снова включён", revert)

    async def _mute(self, value: str, st: RunState) -> None:
        before = await self._muted()
        await self._platform(self.ctx.platform.mute_set, True)
        self._mute_undo(before, True)
        st.info("Звук выключен")

    async def _unmute(self, value: str, st: RunState) -> None:
        before = await self._muted()
        await self._platform(self.ctx.platform.mute_set, False)
        self._mute_undo(before, False)
        st.info("Звук включён")

    async def _mute_toggle(self, value: str, st: RunState) -> None:
        muted = await self._platform(self.ctx.platform.mute_set, None)
        self._mute_undo(not muted, bool(muted))
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
        st.data["delay"] = delay
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
        info = await self._platform(platform.now_playing)
        if mode not in ("play", "pause"):  # «Переключить»: играет — пауза, нет — включить (и проверить)
            mode = "pause" if info is not None and info.status == "playing" else "play"
        if mode == "pause":
            if info is not None and info.status in ("paused", "stopped"):
                st.info("Уже на паузе")
                st.data["media"] = "already_paused"
                return
            await self._platform(platform.media_key, "play_pause")
            st.data["media"] = "paused"
            return
        if info is not None and info.status == "playing":
            st.info("Музыка уже играет")
            st.data["media"] = "already_playing"
            return
        # программа — та, что играла, или только что запущенная сценарием; нет ни той, ни другой — своя
        app = info.app if info is not None else str(st.data.get("app") or "")
        st.data["media"] = "playing"
        if info is None and not app:
            app = await self._start_music_app(st)
            if app:
                st.data["media"] = "launched"  # «Открываю приложение» — программу пришлось запустить
        await self._platform(platform.media_key, "play_pause")
        if not await self._playing():
            # клавиша «Play» без выбранного трека ничего не включает («Моя волна» ждёт щелчка) — агент
            # откроет программу и нажмёт кнопку сам
            where = f" в «{app}»" if app else ""
            raise NeedsAgent("Музыка не заиграла", MUSIC_TASK.format(where=where))

    async def _playing(self) -> bool:
        """После «Play» музыка и правда заиграла (сеанс воспроизведения Windows)."""
        for n in range(PLAY_CHECKS + 1):
            if n:
                await asyncio.sleep(PLAY_CHECK_S)
            info = await self._platform(self.ctx.platform.now_playing)
            if info is not None and info.status == "playing":
                return True
        return False

    async def _start_music_app(self, st: RunState) -> str:
        """Ничего не играет и плеера нет — запустить установленную музыкальную программу (подождав, пока
        она откроется). Вернёт её название ("" — музыкальной программы нет)."""
        from jarvis.commands.apps import KNOWN_BY_KEY

        for key in MUSIC_APPS:
            installed = self.service.apps.find_installed(KNOWN_BY_KEY[key])
            if installed is None:
                continue
            await self._platform(self.ctx.platform.launch, installed.app_id, "")
            await asyncio.sleep(4.0)
            st.info(f"Запускаю {installed.name}")
            return str(installed.name)
        return ""

    async def _media_next(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.media_key, "next")

    async def _media_prev(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.media_key, "prev")

    async def _media_stop(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.media_key, "stop")

    async def _media_close(self, value: str, st: RunState) -> None:
        """«Закрой музыку»: закрыть программу, которая играет (сеанс воспроизведения Windows), а нет её —
        запущенную музыкальную программу (Spotify, Яндекс Музыка, AIMP…). Браузер не закрываем: в нём может
        быть что угодно — только пауза."""
        from jarvis.commands.apps import KNOWN_BY_KEY

        platform = self.ctx.platform
        info = await self._platform(platform.now_playing)
        candidates: list[tuple[str, str]] = []  # (чем назвать платформе, название для людей)
        if info is not None and info.app:
            found = self.service.apps.resolve(info.app)
            if found is not None and found.key and KNOWN_BY_KEY[found.key].kind == "browser":
                if info.status == "playing":
                    await self._platform(platform.media_key, "play_pause")
                st.info(f"Музыка в «{found.name}» на паузе — браузер не закрываю")
                st.data["media"] = "paused"
                return
            candidates.append((found.target, found.name) if found is not None else (info.app, info.app))
        for key in MUSIC_APPS:
            known = KNOWN_BY_KEY[key]
            if known.exe and all(known.exe != target for target, _ in candidates):
                candidates.append((known.exe, known.name))
        for target, name in candidates:
            if not await self._platform(platform.is_running, target):
                continue
            if await self._platform(platform.close_app, target):
                st.info(f"Закрыто: {name}")
                st.data["closed"] = name
                return
        if info is not None and info.status == "playing":  # плеер не узнали — хотя бы остановить
            await self._platform(platform.media_key, "stop")
            st.info("Музыка остановлена")
            st.data["media"] = "paused"
            return
        raise UserFacingError("Музыкальная программа не запущена")

    # --- окна ---------------------------------------------------------------------------------------
    async def _minimize_all(self, value: str, st: RunState) -> None:
        await self._platform(self.ctx.platform.window_action, "minimize_all", None)

        async def revert() -> None:
            await self._platform(self.ctx.platform.window_action, "undo_minimize_all", None)

        self.undo.push("окна развёрнуты обратно", revert)

    async def _window(self, action: str, value: str, st: RunState) -> None:
        target = self._app_target(value, st)[0] if value else None
        window = None
        if action in ("minimize", "maximize") and target is None:
            with contextlib.suppress(PlatformError, UserFacingError):
                window = await self._platform(self.ctx.platform.foreground_window)
        await self._platform(self.ctx.platform.window_action, action, target)
        if window is not None and not (action == "maximize" and window.maximized):
            hwnd, title = window.hwnd, window.title

            async def revert() -> None:
                await self._platform(self.ctx.platform.window_action, "restore", hwnd)

            self.undo.push(f"окно «{one_line(title, 40)}» как было", revert)

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
        st.data["weather"] = {"city": city, "temp": temp, "condition": condition, "feels": feels}

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
        """Загрузка компьютера; value «full» («как дела», «статус систем») — ещё диск, батарея и температура
        видеокарты, если известна (температуру процессора Windows без драйвера не отдаёт — не выдумываем)."""
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
        free = _first_number(s.extra, "disk_free_gb")
        temp = _first_number(s.extra, "gpu_temp_c")
        battery = round(s.battery) if s.battery is not None else None
        if value.casefold() == "full":
            if free is not None:
                gb = round(free)
                message += f", на диске свободно {gb} ГБ"
                spoken += f", на системном диске свободно {gb} {numbers.plural(gb, GIGABYTES)}"
            if battery is not None:
                message += f", заряд {battery}%" + (" (заряжается)" if s.charging else "")
                spoken += f", заряд батареи {battery} {numbers.plural(battery, numbers.PERCENT)}"
            if temp is not None:
                t = round(temp)
                message += f", видеокарта {t}°C"
                spoken += f", температура видеокарты {t} {numbers.plural(t, numbers.DEGREES)}"
        st.info(message, spoken)
        st.data["system"] = {
            "cpu": cpu,
            "ram": ram,
            "disk": round(s.disk),
            "disk_free_gb": round(free, 1) if free is not None else None,
            "battery": battery,
            "charging": s.charging,
            "gpu": round(s.gpu) if s.gpu is not None else None,
            "gpu_temp": round(temp) if temp is not None else None,
        }

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

    async def _microphone(self, value: str, st: RunState) -> None:
        """«Не слушай меня»: микрофон Jarvis выключен — голосом обратно не включить, только в трее, хоткеем
        или в настройках (об этом и говорим)."""
        current = self.ctx.settings.value.voice.enabled
        mode = (value or "toggle").casefold()
        new = {"on": True, "off": False, "1": True, "0": False}.get(mode, not current)
        if new != current:
            await self.ctx.voice.set_enabled(new)
        if new:
            st.info("Микрофон Jarvis включён", "Слушаю")
        else:
            hotkey = self.ctx.settings.value.system.hotkey_mic
            how = f"клавишами {hotkey} или «Микрофон» в трее" if hotkey else "«Микрофон» в трее"
            st.info(f"Микрофон Jarvis выключен. Включить — {how}", "Больше не слушаю")

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
        item = self.ctx.notes.reminder_add(f"Таймер на {duration}", at)
        self._reminder_undo(item, f"таймер на {duration} снят")
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
        item = self.ctx.notes.reminder_add(text[:1].upper() + text[1:], at)
        self._reminder_undo(item, f"напоминание «{one_line(text, 40)}» удалено")
        when = dt.datetime.fromtimestamp(at).strftime("%H:%M")
        st.info(
            f"Напомню в {when}: {text}", f"Напомню через {numbers.duration_text(seconds, accusative=True)}"
        )
        st.data.update({"at": at, "text": text, "seconds": seconds})

    def _reminder_undo(self, item: dict[str, Any], title: str) -> None:
        reminder_id = str(item.get("id") or "")
        if not reminder_id:
            return

        async def revert() -> None:
            self.ctx.notes.reminder_delete(reminder_id)

        self.undo.push(title, revert)

    async def _reminder_at(self, value: str, st: RunState) -> None:
        """«завтра в 9 | Позвонить маме» — напоминание на время, сказанное словами (jarvis.commands.when)."""
        when_text, sep, text = value.partition("|")
        text = text.strip()
        if not sep or not text:
            raise UserFacingError("Не понял, о чём напомнить")
        moment = parse_text(normalize(when_text))
        if moment is None:
            raise UserFacingError(f"Не понял, когда напомнить: «{when_text.strip()}»")
        at = moment.timestamp()
        item = self.ctx.notes.reminder_add(text[:1].upper() + text[1:], at)
        self._reminder_undo(item, f"напоминание «{one_line(text, 40)}» удалено")
        when = human_time(at)
        st.info(f"Напомню {when}: {text}", f"Напомню {when}")
        st.data.update({"at": at, "text": text})

    async def _reminders(self, value: str, st: RunState) -> None:
        items = [r for r in self.ctx.notes.reminders_list(include_done=False) if not r.get("done")]
        if not items:
            st.info("Напоминаний нет", "Напоминаний нет")
            return
        items.sort(key=lambda r: float(r.get("at") or 0))
        lines = [f"{human_time(float(r['at']))} — {r['text']}" for r in items]
        count = len(items)
        head = f"{count} {plural(count, ('напоминание', 'напоминания', 'напоминаний'))}"
        spoken = "; ".join(lines[:3]) + (f" и ещё {count - 3}" if count > 3 else "")
        st.info(f"{head}:\n" + "\n".join(lines[:10]), f"{head}: {spoken}")
        st.data["count"] = count

    async def _reminder_cancel(self, value: str, st: RunState) -> None:
        """Пусто — последнее поставленное; «все» — все; иначе — то, в тексте которого есть эти слова."""
        notes = self.ctx.notes
        pending = [r for r in notes.reminders_list(include_done=False) if not r.get("done")]
        if not pending:
            raise UserFacingError("Напоминаний нет")
        query = normalize(value or "")
        if query in ("все", "всё"):
            for r in pending:
                notes.reminder_delete(r["id"])
            count = len(pending)
            st.info(f"Удалил все напоминания: {count}", "Все напоминания удалены")
            return
        if query:
            words = [w for w in query.split() if len(w) > 2]
            scored = [
                (sum(1 for w in words if w[: max(4, len(w) - 2)] in normalize(r["text"])), r) for r in pending
            ]
            best = max(scored, key=lambda pair: pair[0])
            if best[0] == 0:
                raise UserFacingError(f"Не нашёл напоминание про «{value.strip()}»")
            target = best[1]
        else:
            target = max(pending, key=lambda r: float(r.get("created") or 0))
        notes.reminder_delete(target["id"])
        st.info(
            f"Удалил напоминание: {target['text']} ({human_time(float(target['at']))})", "Напоминание удалено"
        )
        st.data["removed"] = target["text"]

    async def _briefing(self, value: str, st: RunState) -> None:
        # текст собирает jarvis.scenarios.briefing (погода, календарь, задачи…); голосом — через say итога
        from jarvis.scenarios.briefing import build

        text, spoken = await build(self.ctx, value, st.slots)
        st.info(text, spoken)


GIGABYTES = ("гигабайт", "гигабайта", "гигабайт")


def _is_game(key: str, target: str) -> bool:
    """Запускается игра (не лаунчер): известная игра или ссылка запуска игры лаунчером."""
    from jarvis.commands.apps import KNOWN_BY_KEY

    known = KNOWN_BY_KEY.get(key) if key else None
    if known is not None and known.kind == "game" and key not in GAME_LAUNCHERS:
        return True
    return target.casefold().startswith(GAME_LINKS)


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
