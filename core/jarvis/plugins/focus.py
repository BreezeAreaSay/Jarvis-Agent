"""«Фокус»: сессии глубокой работы по помодоро — фокус и перерывы, тишина, защита от отвлечений, статистика.

Цикл: фокус config.focus_min минут → перерыв config.break_min; после каждого config.long_every-го фокуса —
длинный перерыв config.long_break_min. Конец фокуса — сигнал, «Время перерыва, сэр. Вы сосредоточенно
работали 25 минут.» и карточка в оверлее (plugin.focus.alert); конец перерыва — «Перерыв окончен —
возвращаемся?» (config.auto_continue — следующий фокус начинается сам). Хоткей config.hotkey → toggle.

На время фокуса Jarvis в тихом режиме (settings.voice.silent_mode; после фокуса прежнее значение
возвращается, а если человек переключил режим сам — остаётся его выбор) и, если включено, «Озвучка
уведомлений» на паузе. Раз в ~3 с плагин смотрит на активное окно: отвлекающая программа (config.apps) —
карточка «Telegram подождёт до перерыва, сэр» или, в режиме «Закрывать», её окна закрываются; вкладка
браузера с отвлекающим сайтом (config.sites, по заголовку окна) — мягкое напоминание. Файл hosts и права
администратора не нужны. Нет ввода дольше config.idle_pause_min минут — фокус на паузе (время без вас не
считается), а когда вы вернётесь, Jarvis об этом скажет.

Статистика — минуты фокуса, сессии и отвлечения по дням в kv «focus:days»: сегодня, 7 дней, серия дней
подряд хотя бы с одной сессией, лучший день. Сессия — фокус, доведённый до конца (или прерванный после
5 минут работы). Событие plugin.focus.state — при каждом изменении и раз в минуту, пока идёт таймер;
секунды интерфейс отсчитывает сам от left_s (или ends_at).
"""

import asyncio
import contextlib
import logging
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from jarvis.commands import numbers
from jarvis.platform.base import WindowInfo
from jarvis.plugins.base import ConfigField, Option, Plugin, error_text, unique
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

Phase = Literal["idle", "focus", "break", "paused"]

TICK_S = 1.0
GUARD_S = 3.0  # активное окно — отвлекающая программа или сайт
IDLE_CHECK_S = 5.0  # бездействие во время фокуса
BACK_CHECK_S = 2.0  # вернулся ли человек после автопаузы
BACK_IDLE_S = 10.0  # ввод не старше — человек снова за компьютером
HEARTBEAT_S = 60.0  # состояние раз в минуту, пока идёт таймер (растут минуты за сегодня)
REMIND_AGAIN_S = 45.0  # снова открыл отвлекающее — напомнить, если с прошлого раза прошло столько
REMIND_STAY_S = 300.0  # сидит в отвлекающем — напоминать раз в 5 минут
MIN_SESSION_S = 300.0  # прерванный фокус засчитывается сессией, если в нём было хотя бы 5 минут работы
SERIES_GAP_S = 3600.0  # после перерыва прошло больше часа — следующий фокус начинает новую серию
MAX_MINUTES = 240
KEEP_DAYS = 400
DAYS_KEY = "focus:days"
RESTORE_KEY = "focus:restore"  # тихий режим, который вернуть, если ядро упало во время фокуса
DEFAULT_HOTKEY = "Ctrl+Alt+P"
NOTIFY_ID = "notify_voice"

# браузеры (имя файла без .exe): у них по заголовку окна ищем отвлекающие сайты
BROWSERS = (
    "chrome",
    "msedge",
    "firefox",
    "browser",  # Яндекс Браузер
    "opera",
    "brave",
    "vivaldi",
    "yandex",
    "chromium",
    "arc",
    "iexplore",
    "waterfox",
    "librewolf",
    "floorp",
    "thorium",
    "zen",
)
# как программы называют по-русски → имя файла
APP_ALIASES = {
    "телеграм": "telegram",
    "телеграмм": "telegram",
    "тг": "telegram",
    "дискорд": "discord",
    "стим": "steam",
    "вк": "vk",
    "вконтакте": "vk",
    "vkontakte": "vk",
    "ватсап": "whatsapp",
    "вотсап": "whatsapp",
}
# слова в заголовке вкладки, кроме самого адреса: «Новости | ВКонтакте — Яндекс Браузер»
SITE_ALIASES = {
    "vk": ("вконтакте", "vk"),
    "ok": ("одноклассники",),
    "dzen": ("дзен",),
    "pikabu": ("пикабу",),
    "x": ("twitter",),
}
NAMES = {
    "telegram": "Telegram",
    "discord": "Discord",
    "steam": "Steam",
    "vk": "VK",
    "whatsapp": "WhatsApp",
    "youtube": "YouTube",
    "twitch": "Twitch",
    "tiktok": "TikTok",
    "reddit": "Reddit",
    "instagram": "Instagram",
    "rutube": "Rutube",
    "pikabu": "Пикабу",
    "dzen": "Дзен",
    "ok": "Одноклассники",
}
SESSIONS = ("сессия", "сессии", "сессий")
DAYS = ("день", "дня", "дней")


# --- отвлечения ------------------------------------------------------------------------------------------
def split_list(value: Any) -> list[str]:
    """«Telegram, Discord; Steam» → ["Telegram", "Discord", "Steam"] (без пустых и повторов)."""
    return unique(part.strip() for part in re.split(r"[,;\n]", str(value or "")))


def _stem(name: str) -> str:
    """Имя файла программы без пути и .exe, в нижнем регистре: «C:\\…\\Telegram.exe» → «telegram»."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].casefold().strip()
    return base.removesuffix(".exe").strip()


def app_key(item: str) -> str:
    """Программа из списка → имя файла: «Телеграм» → «telegram», «Discord.exe» → «discord»."""
    key = _stem(item)
    return APP_ALIASES.get(key, key)


def app_matches(key: str, exe: str) -> bool:
    """Окно этой программы? «steam» — и steam.exe, и steamwebhelper.exe; короткое «vk» — только целиком."""
    stem = _stem(exe)
    if not key or not stem:
        return False
    if stem == key or stem.startswith(key + " "):
        return True
    if len(key) >= 4 and stem.startswith(key):
        return True
    first = key.split()[0]  # «Telegram Desktop» → telegram.exe
    return first != key and len(first) >= 4 and stem.startswith(first)


def is_browser(exe: str) -> bool:
    stem = _stem(exe)
    return any(stem == b or (len(b) >= 5 and stem.startswith(b)) for b in BROWSERS)


def site_key(item: str) -> str:
    """«https://www.youtube.com/feed» → «youtube.com»."""
    text = re.sub(r"^[a-z]+://", "", item.casefold().strip())
    return text.removeprefix("www.").split("/", 1)[0]


def site_words(item: str) -> list[str]:
    """Что искать в заголовке вкладки: «vk.com» → [«vk.com», «вконтакте», «vk»], «youtube» → [«youtube»]."""
    key = site_key(item)
    stem = key.split(".", 1)[0]
    words = [key, *SITE_ALIASES.get(stem, ())]
    if len(stem) >= 4:
        words.append(stem)
    return unique(w for w in words if w)


def title_has(title: str, word: str) -> bool:
    """Слово в заголовке (короткое — только отдельным словом: «vk», но не «vkusvill»)."""
    if len(word) <= 3:
        return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", title) is not None
    return word in title


@dataclass(frozen=True)
class Distraction:
    key: str  # app:telegram | site:youtube.com — по нему «Нужно по работе» и паузы между напоминаниями
    name: str  # для людей: «Telegram», «YouTube»
    icon: str  # ключ значка интерфейса: telegram, youtube…
    kind: Literal["app", "site"]


def find_distraction(window: WindowInfo, apps: Iterable[str], sites: Iterable[str]) -> Distraction | None:
    """Активное окно — отвлекающая программа или вкладка с отвлекающим сайтом? Иначе None."""
    for item in apps:
        key = app_key(item)
        if app_matches(key, window.exe):
            return Distraction(f"app:{key}", NAMES.get(key) or item, key, "app")
    if not is_browser(window.exe):
        return None
    title = window.title.casefold()
    for item in sites:
        if any(title_has(title, word) for word in site_words(item)):
            key = site_key(item)
            stem = key.split(".", 1)[0]
            return Distraction(f"site:{key}", NAMES.get(stem) or item, stem, "site")
    return None


# --- слова ---------------------------------------------------------------------------------------------
def left_text(seconds: float) -> str:
    """Сколько осталось (именительный): «12 минут», «1 час 5 минут», «меньше минуты»."""
    if seconds < 60:
        return "меньше минуты"
    minutes = math.ceil(seconds / 60 - 1e-6)
    hours, rest = divmod(minutes, 60)
    parts = [numbers.count_text(hours, numbers.HOURS)] if hours else []
    if rest:
        parts.append(numbers.count_text(rest, numbers.MINUTES))
    return " ".join(parts)


def span_text(minutes: float, *, accusative: bool = True) -> str:
    """Длительность в минутах: «25 минут», «21 минуту» (винительный), «1 час 15 минут»."""
    total = max(0, round(minutes))
    hours, rest = divmod(total, 60)
    parts = [numbers.count_text(hours, numbers.HOURS)] if hours else []
    if rest or not parts:
        parts.append(numbers.count_text(rest, numbers.MINUTES_ACC if accusative else numbers.MINUTES))
    return " ".join(parts)


def _num(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _minutes_arg(value: Any) -> float | None:
    """Длина фокуса из действия или фразы; None — по настройкам. Неверное число — UserFacingError."""
    if value is None or value == "":
        return None
    try:
        minutes = float(str(value).replace(",", "."))
    except ValueError:
        raise UserFacingError("Сколько минут фокуса — нужно число") from None
    if not math.isfinite(minutes) or not 1 <= minutes <= MAX_MINUTES:
        raise UserFacingError(f"Фокус — от 1 до {MAX_MINUTES} минут")
    return minutes


class Focus(Plugin):
    id = "focus"
    name = "Фокус"
    description = "Глубокая работа по помодоро: тишина, защита от отвлечений и статистика по дням."
    icon = "focus"
    color = ("#6EE7B7", "#059669")
    fields = (
        ConfigField("focus_min", "number", "Фокус, минут", default=25, min=5, max=180, step=5, integer=True),
        ConfigField("break_min", "number", "Перерыв, минут", default=5, min=1, max=30, step=1, integer=True),
        ConfigField(
            "long_break_min",
            "number",
            "Длинный перерыв, минут",
            default=15,
            min=5,
            max=60,
            step=5,
            integer=True,
        ),
        ConfigField(
            "long_every",
            "number",
            "Длинный перерыв после каждого N-го фокуса",
            default=4,
            min=2,
            max=8,
            step=1,
            integer=True,
        ),
        ConfigField("auto_continue", "bool", "Сразу начинать фокус после перерыва", default=False),
        ConfigField(
            "silent",
            "bool",
            "Тихий режим во время фокуса",
            default=True,
            hint="Jarvis не отвечает голосом, пока вы работаете; после фокуса всё как было",
        ),
        ConfigField(
            "mute_notifications",
            "bool",
            "Не читать уведомления во время фокуса",
            default=True,
            hint="Ставит на паузу плагин «Озвучка уведомлений»",
        ),
        ConfigField("announce", "bool", "Говорить, когда пора отдохнуть и вернуться", default=True),
        ConfigField(
            "guard",
            "select",
            "Отвлекающие программы",
            default="remind",
            options=(
                Option("remind", "Только напоминать"),
                Option("close", "Закрывать"),
                Option("off", "Не следить"),
            ),
        ),
        ConfigField(
            "apps",
            "text",
            "Список отвлекающих программ",
            default="Telegram, Discord, Steam, VK",
            placeholder="Telegram, Discord, Steam",
            hint="Через запятую — название программы или её файла",
        ),
        ConfigField(
            "sites",
            "text",
            "Отвлекающие сайты",
            default="youtube, vk.com, twitch, tiktok",
            placeholder="youtube, vk.com",
            hint="Напомню, если заголовок вкладки браузера содержит слово из списка",
        ),
        ConfigField(
            "idle_pause_min",
            "number",
            "Пауза, если вас нет дольше, минут",
            default=5,
            min=0,
            max=30,
            step=1,
            integer=True,
            hint="0 — не ставить на паузу",
        ),
        ConfigField("hotkey", "hotkey", "Горячая клавиша: начать или пауза", default=DEFAULT_HOTKEY),
    )
    examples = ("начни фокус на 50 минут", "сколько осталось", "как я сегодня поработал", "стоп фокус")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        # тесты подставляют свои часы: clock — для таймера, wall — для дней статистики
        self.clock: Callable[[], float] = time.monotonic
        self.wall: Callable[[], float] = time.time
        self.tick_s = TICK_S
        self._lock = asyncio.Lock()
        self._settings_off: Callable[[], Any] | None = None
        self._applying = False  # тихий режим переключаем мы сами — это не выбор человека
        self._silent_held = False  # тихий режим включил «Фокус» и вернёт его после
        self._notify_held = False  # «Озвучку уведомлений» поставил на паузу «Фокус»
        self._reset()

    def _reset(self) -> None:
        self._phase: Phase = "idle"
        self._paused_from: Phase = "idle"
        self._reason = ""  # user | idle — почему пауза
        self._welcomed = True  # после автопаузы уже сказали «с возвращением»
        self._long = False
        self._total = 0.0  # длина интервала, с
        self._elapsed = 0.0  # отработано в интервале до текущего отрезка, с
        self._run_at = 0.0  # по self.clock: когда пошёл текущий отрезок
        self._series = 0  # законченных фокусов в серии
        self._cycle = 0  # номер текущего (или последнего) фокуса в серии
        self._waiting_since: float | None = None  # перерыв кончился, ждём следующий фокус
        self._allowed: set[str] = set()  # «Нужно по работе» — до конца серии
        self._nagged: dict[str, float] = {}
        self._fg_key = ""  # отвлечение в активном окне на прошлой проверке
        self._next_guard = 0.0
        self._next_idle = 0.0
        self._next_beat = 0.0
        self._day = ""

    # --- жизненный цикл ------------------------------------------------------------------------------
    async def start(self) -> None:
        self._reset()
        self._recover()
        self._ensure_hotkey()
        self._settings_off = self.ctx.settings.on_change(self._on_settings)
        self._day = self._today().isoformat()
        self.spawn(self._loop(), "timer")
        self._publish()

    async def stop(self) -> None:
        async with self._lock:
            if self._focusing():
                elapsed = self._elapsed_at(self.clock())
                self._record(elapsed, session=elapsed >= MIN_SESSION_S)
            await self._leave_env()
            was = self._phase
            self._reset()
        if self._settings_off is not None:
            self._settings_off()
            self._settings_off = None
        if was != "idle":
            self._publish()  # оверлей убирает таймер

    async def on_config(self, config: dict[str, Any], old: dict[str, Any]) -> None:
        async with self._lock:
            if self._focusing():
                # выключили тишину или паузу уведомлений посреди фокуса — вернуть сразу, включили — применить
                if not config.get("silent") and self._silent_held:
                    self._release_silent()
                if not config.get("mute_notifications") and self._notify_held:
                    self._notify_held = False
                    await self._mute_notifications(False)
                await self._enter_env()
            self._publish()

    def _ensure_hotkey(self) -> None:
        """Хоткей по умолчанию — в сохранённые настройки: Electron регистрирует только сохранённый."""
        if "hotkey" in self.manager.stored_config(self.id):
            return
        try:
            self.ctx.settings.patch({"plugins": {self.id: {"config": {"hotkey": DEFAULT_HOTKEY}}}})
        except Exception:
            log.exception("фокус: хоткей по умолчанию не сохранился")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("фокус: проверка таймера упала")
            await asyncio.sleep(self.tick_s)

    # --- время --------------------------------------------------------------------------------------
    def _running(self) -> bool:
        return self._phase in ("focus", "break")

    def _focusing(self) -> bool:
        return self._phase == "focus" or (self._phase == "paused" and self._paused_from == "focus")

    def _on_break(self) -> bool:
        return self._phase == "break" or (self._phase == "paused" and self._paused_from == "break")

    def _elapsed_at(self, now: float) -> float:
        if self._running():
            return self._elapsed + max(0.0, now - self._run_at)
        return self._elapsed

    def _left_at(self, now: float) -> float:
        if self._phase == "idle":
            return 0.0
        return max(0.0, self._total - self._elapsed_at(now))

    def _today(self) -> date:
        return datetime.fromtimestamp(self.wall()).date()

    def _begin(self, phase: Literal["focus", "break"], seconds: float, *, long: bool = False) -> None:
        now = self.clock()
        self._phase = phase
        self._paused_from = "idle"
        self._reason = ""
        self._welcomed = True
        self._long = long and phase == "break"
        self._total = float(seconds)
        self._elapsed = 0.0
        self._run_at = now
        self._waiting_since = None
        self._next_guard = now  # активное окно — сразу на первой проверке
        self._next_idle = now + IDLE_CHECK_S
        if phase == "focus":
            self._cycle = self._series + 1
            self._fg_key = ""

    def _pause(self, now: float, reason: str, give_back: float = 0.0) -> None:
        """Пауза; give_back — сколько последних секунд не считать (человек уже давно отошёл)."""
        elapsed = self._elapsed_at(now)
        if give_back > 0:
            elapsed = max(self._elapsed, elapsed - give_back)
        self._paused_from = self._phase
        self._phase = "paused"
        self._elapsed = elapsed
        self._reason = reason
        self._welcomed = reason != "idle"
        self._next_idle = now + BACK_CHECK_S

    def _resume(self, now: float) -> None:
        self._phase = self._paused_from
        self._paused_from = "idle"
        self._reason = ""
        self._welcomed = True
        self._run_at = now
        self._next_guard = now
        self._next_idle = now + IDLE_CHECK_S

    def _idle_all(self) -> None:
        """Ни фокуса, ни перерыва: серия закончилась."""
        self._phase = "idle"
        self._paused_from = "idle"
        self._reason = ""
        self._welcomed = True
        self._long = False
        self._total = self._elapsed = 0.0
        self._series = self._cycle = 0
        self._waiting_since = None
        self._allowed.clear()
        self._nagged.clear()
        self._fg_key = ""

    # --- переходы (под self._lock) ----------------------------------------------------------------------
    async def _start_focus(self, minutes: float | None) -> str:
        """started — новый фокус, running — уже идёт, resumed — снят с паузы, changed — новая длина
        идущего, done — с новой длиной он и кончился."""
        now = self.clock()
        if self._focusing():
            paused = self._phase == "paused"
            if paused:
                self._resume(now)
            if minutes is None:
                return "resumed" if paused else "running"
            self._total = minutes * 60  # отработанное остаётся, меняется только длина
            if self._left_at(now) <= 0:
                await self._complete(now)
                return "done"
            return "changed"
        if self._phase == "idle":
            waiting = self._waiting_since
            if waiting is None or now - waiting > SERIES_GAP_S:
                self._idle_all()  # новая серия
        # идущий перерыв обрывается — серия продолжается
        self._begin("focus", (minutes or float(self.config["focus_min"])) * 60)
        await self._enter_env()
        return "started"

    async def _complete(self, now: float) -> None:
        """Интервал кончился: фокус → перерыв (каждый N-й — длинный), перерыв → ждём фокус или он сам."""
        config = self.config
        a = self._a()
        if self._phase == "focus":
            worked = self._total / 60
            self._record(self._total, session=True)
            self._series += 1
            long = self._series % int(config["long_every"]) == 0
            await self._leave_env()
            rest = float(config["long_break_min"] if long else config["break_min"])
            self._begin("break", rest * 60, long=long)
            if long:
                title = f"Время длинного перерыва{a}"
                say = (
                    f"Время длинного перерыва{a}. Вы сосредоточенно работали {span_text(worked)} — "
                    f"отдохните {span_text(rest)}."
                )
            else:
                title = f"Время перерыва{a}"
                say = f"Время перерыва{a}. Вы сосредоточенно работали {span_text(worked)}."
            text = f"Вы сосредоточенно работали {span_text(worked)}. Отдохните {span_text(rest)}."
            self._alert("focus_done", title, text, minutes=round(worked), break_min=round(rest), long=long)
            await self._announce(say)
        elif self._phase == "break":
            minutes = float(config["focus_min"])
            if config.get("auto_continue"):
                self._begin("focus", minutes * 60)
                await self._enter_env()
                text = f"Возвращаемся к работе: фокус на {span_text(minutes)}."
                self._alert("break_done", f"Перерыв окончен{a}", text, auto=True)
                await self._announce(f"Перерыв окончен{a}. {text}")
            else:
                self._phase = "idle"
                self._long = False
                self._total = self._elapsed = 0.0
                self._waiting_since = now
                text = f"Следующий фокус — {span_text(minutes, accusative=False)}."
                self._alert("break_done", "Перерыв окончен — возвращаемся?", text, auto=False)
                await self._announce(f"Перерыв окончен — возвращаемся{a}?")
        self._publish()
        self.changed()

    async def _take_break(self) -> str:
        """«Перерыв» сейчас: фокус засчитывается (сессией — если в нём хотя бы 5 минут работы)."""
        now = self.clock()
        if self._on_break():
            if self._phase == "paused":
                self._resume(now)
                return "resumed"
            return "already"
        config = self.config
        long = False
        if self._focusing():
            worked = self._elapsed_at(now)
            session = worked >= MIN_SESSION_S
            self._record(worked, session=session)
            if session:
                self._series += 1
                long = self._series % int(config["long_every"]) == 0
            await self._leave_env()
        rest = float(config["long_break_min"] if long else config["break_min"])
        self._begin("break", rest * 60, long=long)
        return "started"

    async def _stop(self) -> float:
        """Остановить фокус или перерыв и закончить серию. Вернёт засчитанные секунды фокуса."""
        worked = 0.0
        if self._focusing():
            worked = self._elapsed_at(self.clock())
            self._record(worked, session=worked >= MIN_SESSION_S)
        await self._leave_env()
        self._idle_all()
        return worked

    # --- тишина на время фокуса ----------------------------------------------------------------------------
    async def _enter_env(self) -> None:
        config = self.config
        if config.get("silent") and not self._silent_held and not self.ctx.settings.value.voice.silent_mode:
            self._set_silent(True)
            self._silent_held = True
            with contextlib.suppress(Exception):
                self.ctx.db.kv_set(RESTORE_KEY, {"silent_mode": False})
        if config.get("mute_notifications") and not self._notify_held:
            self._notify_held = await self._mute_notifications(True)

    async def _leave_env(self) -> None:
        if self._silent_held:
            self._release_silent()
        if self._notify_held:
            self._notify_held = False
            await self._mute_notifications(False)

    def _release_silent(self) -> None:
        self._silent_held = False
        self._forget_restore()
        if self.ctx.settings.value.voice.silent_mode:
            self._set_silent(False)

    def _set_silent(self, value: bool) -> None:
        self._applying = True
        try:
            self.ctx.settings.patch({"voice": {"silent_mode": value}})
        except Exception:
            log.exception("фокус: тихий режим не переключился")
        finally:
            self._applying = False

    def _forget_restore(self) -> None:
        with contextlib.suppress(Exception):
            self.ctx.db.delete("kv", RESTORE_KEY, key="key")

    def _on_settings(self, old: Any, new: Any) -> None:
        if self._applying or not self._silent_held:
            return
        if old.voice.silent_mode != new.voice.silent_mode:
            self._silent_held = False  # человек сам переключил тихий режим — дальше его выбор
            self._forget_restore()

    def _recover(self) -> None:
        """Ядро остановилось посреди фокуса — вернуть тихий режим, каким он был до фокуса."""
        try:
            saved = self.ctx.db.kv_get(RESTORE_KEY)
        except Exception:
            return
        if not isinstance(saved, dict):
            return
        self._forget_restore()
        if saved.get("silent_mode") is False and self.ctx.settings.value.voice.silent_mode:
            log.info("фокус: прошлый фокус не закончился как следует — выключаю тихий режим")
            self._set_silent(False)

    async def _mute_notifications(self, mute: bool) -> bool:
        """Поставить «Озвучку уведомлений» на паузу или снять. Вернёт True, если на паузу поставили мы."""
        manager = self.manager
        if not manager.has(NOTIFY_ID) or not manager.is_running(NOTIFY_ID):
            return False
        try:
            if mute:
                if manager.get(NOTIFY_ID).status().get("muted"):
                    return False  # уже молчит по желанию человека — снимать будет он сам
                await manager.action(NOTIFY_ID, "mute", {})
            else:
                await manager.action(NOTIFY_ID, "unmute", {})
        except Exception as e:
            log.info("фокус: озвучка уведомлений не переключилась (%s)", error_text(e))
            return False
        return True

    # --- проверки во время фокуса ----------------------------------------------------------------------
    async def tick(self) -> None:
        """Одна проверка: конец интервала, бездействие, отвлечения, новый день, состояние раз в минуту."""
        async with self._lock:
            now = self.clock()
            # сначала бездействие: человек ушёл (или компьютер спал) — пауза, а не «отработанный» фокус
            if self._phase == "focus" and now >= self._next_idle:
                self._next_idle = now + IDLE_CHECK_S
                await self.check_idle()
            if self._running() and self._left_at(now) <= 0:
                await self._complete(now)
                now = self.clock()
            if self._phase == "focus":
                if now >= self._next_guard:
                    self._next_guard = now + GUARD_S
                    await self.guard()
            elif self._phase == "paused" and not self._welcomed and now >= self._next_idle:
                self._next_idle = now + BACK_CHECK_S
                await self.check_back()
            day = self._today().isoformat()
            if day != self._day:
                self._day = day
                self._publish()  # новый день — «сегодня» с нуля
                self.changed()
            elif self._running() and now >= self._next_beat:
                self._publish()

    async def _idle_seconds(self) -> float | None:
        try:
            return float(await self.ctx.run_platform(self.ctx.platform.idle_seconds))
        except Exception:
            return None

    async def check_idle(self) -> bool:
        """Нет ввода дольше config.idle_pause_min — пауза; время без человека фокусом не считается."""
        if self._phase != "focus":
            return False
        limit = _num(self.config.get("idle_pause_min")) * 60
        if limit <= 0:
            return False
        idle = await self._idle_seconds()
        if idle is None or idle < limit or self._phase != "focus":
            return False
        self._pause(self.clock(), "idle", give_back=idle)
        log.info("фокус: ввода нет %.0f с — пауза", idle)
        self._publish()
        self.changed()
        return True

    async def check_back(self) -> bool:
        """После автопаузы: человек снова за компьютером — сказать, что фокус на паузе."""
        if self._phase != "paused" or self._reason != "idle" or self._welcomed:
            return False
        idle = await self._idle_seconds()
        if idle is None or idle > BACK_IDLE_S or self._welcomed:
            return False
        self._welcomed = True
        a = self._a()
        left = left_text(self._left_at(self.clock()))
        self._alert(
            "back", f"С возвращением{a}", f"Пока вас не было, фокус стоял на паузе. До перерыва — {left}."
        )
        await self._announce(f"С возвращением{a}. Пока вас не было, я поставил фокус на паузу. Продолжим?")
        self._publish()
        return True

    async def guard(self) -> dict[str, Any] | None:
        """Активное окно — отвлекающая программа или сайт? Напомнить (или закрыть). Вернёт событие alert."""
        config = self.config
        mode = str(config.get("guard") or "remind")
        if mode == "off" or self._phase != "focus":
            return None
        try:
            window = await self.ctx.run_platform(self.ctx.platform.foreground_window)
        except Exception as e:
            log.debug("фокус: активное окно не узнать (%s)", error_text(e))
            return None
        if window is None or self._phase != "focus":
            self._fg_key = ""
            return None
        hit = find_distraction(window, split_list(config.get("apps")), split_list(config.get("sites")))
        if hit is None or hit.key in self._allowed:
            self._fg_key = ""
            return None
        now = self.clock()
        fresh = hit.key != self._fg_key
        self._fg_key = hit.key
        closed = False
        if hit.kind == "app" and mode == "close":
            try:
                closed = (
                    int(await self.ctx.run_platform(self.ctx.platform.close_app, window.exe or hit.icon)) > 0
                )
            except Exception as e:
                log.info("фокус: %s не закрылся (%s)", hit.name, error_text(e))
            if closed:
                self._fg_key = ""  # окна больше нет — следующее открытие снова «новое»
        last = self._nagged.get(hit.key)
        due = (
            closed or last is None or (fresh and now - last >= REMIND_AGAIN_S) or now - last >= REMIND_STAY_S
        )
        if not due:
            return None
        self._nagged[hit.key] = now
        self._count_distraction()
        left = left_text(self._left_at(now))
        if closed:
            text = f"Я закрыл {hit.name}. До перерыва — {left}."
        else:
            text = f"До перерыва — {left}. Вы справитесь."
        return self._alert(
            "distraction",
            f"{hit.name} подождёт до перерыва{self._a()}",
            text,
            key=hit.key,
            name=hit.name,
            icon=hit.icon,
            site=hit.kind == "site",
            closed=closed,
        )

    # --- голос и события ---------------------------------------------------------------------------------
    def _a(self) -> str:
        """«, сэр» — обращение из профиля (пусто — без него)."""
        address = (self.ctx.settings.value.profile.address or "").strip()
        return f", {address}" if address else ""

    def _earcon(self, name: str) -> None:
        if not self.ctx.settings.value.voice.earcons:
            return
        play = getattr(self.ctx.tts, "play_earcon", None)
        if play is not None:
            with contextlib.suppress(Exception):
                play(name)

    async def _speak(self, text: str, *, interrupt: bool = False) -> None:
        try:
            await self.ctx.tts.say(text, interrupt=interrupt)
        except Exception as e:
            log.info("фокус: не озвучить (%s)", error_text(e))

    async def _announce(self, text: str) -> None:
        """Конец интервала: сигнал и фраза. Тихий режим «Фокуса» не мешает, тихий режим человека — да."""
        self._earcon("done")
        if not self.config.get("announce"):
            return
        if not self._silent_held and self.ctx.settings.value.voice.silent_mode:
            return
        await self._speak(text)

    def _alert(self, kind: str, title: str, text: str, **extra: Any) -> dict[str, Any]:
        data = {
            "kind": kind,
            "title": title,
            "text": text,
            "phase": self._phase,
            "left_s": round(self._left_at(self.clock()), 1),
            "ts": self.wall(),
            **extra,
        }
        self.publish("alert", data)
        return data

    def _publish(self) -> dict[str, Any]:
        state = self.snapshot()
        self._next_beat = self.clock() + HEARTBEAT_S
        self.publish("state", state)
        return state

    def _next(self) -> str:
        if self._focusing():
            return "long_break" if (self._series + 1) % int(self.config["long_every"]) == 0 else "break"
        if self._on_break() or self._waiting_since is not None:
            return "focus"
        return ""

    def snapshot(self, stats: dict[str, Any] | None = None) -> dict[str, Any]:
        """Состояние для оверлея, виджета и панели (plugin.focus.state)."""
        stats = stats if stats is not None else self.stats()
        config = self.config
        now = self.clock()
        left = self._left_at(now)
        paused = self._phase == "paused"
        return {
            "phase": self._phase,
            "paused_from": self._paused_from if paused else "",
            "reason": self._reason if paused else "",
            "long_break": self._long,
            "left_s": round(left, 1),
            "total_s": round(self._total, 1),
            "ends_at": round(self.wall() + left, 1) if self._running() else None,
            "cycle": self._cycle,
            "long_every": int(config["long_every"]),
            "next": self._next(),
            "waiting": self._waiting_since is not None,
            "today_min": stats["today"]["min"],
            "today_sessions": stats["today"]["sessions"],
            "streak": stats["streak"],
            "focus_min": int(config["focus_min"]),
            "break_min": int(config["break_min"]),
            "long_break_min": int(config["long_break_min"]),
            "guard": str(config.get("guard") or "remind"),
            "allowed": sorted(self._allowed),
        }

    def status(self) -> dict[str, Any]:
        stats = self.stats()
        return {
            **self.snapshot(stats),
            "week": stats["week"],
            "best": stats["best"],
            "distractions": stats["today"]["distractions"],
            "total_min": stats["total_min"],
            "total_sessions": stats["total_sessions"],
        }

    # --- статистика ----------------------------------------------------------------------------------------
    def _load_days(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.ctx.db.kv_get(DAYS_KEY, {})
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save_days(self, days: dict[str, dict[str, Any]]) -> None:
        if len(days) > KEEP_DAYS:
            days = dict(sorted(days.items())[-KEEP_DAYS:])
        try:
            self.ctx.db.kv_set(DAYS_KEY, days)
        except Exception:
            log.exception("фокус: статистика не сохранилась")

    def _record(self, seconds: float, *, session: bool) -> None:
        """Минуты фокуса (и сессию) — в сегодняшний день."""
        if seconds < 1 and not session:
            return
        days = self._load_days()
        key = self._today().isoformat()
        rec = dict(days.get(key) or {})
        rec["min"] = round(_num(rec.get("min")) + seconds / 60, 2)
        rec["sessions"] = int(_num(rec.get("sessions"))) + (1 if session else 0)
        days[key] = rec
        self._save_days(days)

    def _count_distraction(self) -> None:
        days = self._load_days()
        key = self._today().isoformat()
        rec = dict(days.get(key) or {})
        rec["distractions"] = int(_num(rec.get("distractions"))) + 1
        days[key] = rec
        self._save_days(days)

    def stats(self) -> dict[str, Any]:
        """Сегодня, 7 дней (старые первыми), серия дней подряд с сессией, лучший день, всего."""
        days = self._load_days()
        today = self._today()
        live = self._elapsed_at(self.clock()) / 60 if self._focusing() else 0.0

        def day(d: date) -> tuple[float, int, int]:
            rec = days.get(d.isoformat()) or {}
            minutes = _num(rec.get("min")) + (live if d == today else 0.0)
            return minutes, int(_num(rec.get("sessions"))), int(_num(rec.get("distractions")))

        week = []
        for back in range(6, -1, -1):
            d = today - timedelta(days=back)
            minutes, sessions, _ = day(d)
            week.append({"date": d.isoformat(), "min": round(minutes), "sessions": sessions})
        minutes, sessions, distractions = day(today)
        # серия жива весь сегодняшний день: пока сессии сегодня нет, считаем со вчера
        start = today if sessions else today - timedelta(days=1)
        streak = 0
        while streak <= KEEP_DAYS and day(start - timedelta(days=streak))[1] > 0:
            streak += 1
        best: dict[str, Any] | None = None
        total_min = 0.0
        total_sessions = 0
        for key in sorted({*days, today.isoformat()}):
            try:
                m, s, _ = day(date.fromisoformat(key))
            except ValueError:
                continue
            total_min += m
            total_sessions += s
            if m > 0 and (best is None or m > best["min"]):
                best = {"date": key, "min": m}
        if best is not None:
            best["min"] = round(best["min"])
        return {
            "today": {
                "date": today.isoformat(),
                "min": round(minutes),
                "sessions": sessions,
                "distractions": distractions,
            },
            "week": week,
            "streak": streak,
            "best": best,
            "total_min": round(total_min),
            "total_sessions": total_sessions,
        }

    # --- действия --------------------------------------------------------------------------------------
    def _changed_state(self) -> dict[str, Any]:
        state = self._publish()
        self.changed()
        return state

    async def act_state(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"state": self.snapshot()}

    async def act_stats(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.stats()

    async def act_start(self, args: dict[str, Any]) -> dict[str, Any]:
        minutes = _minutes_arg(args.get("minutes"))
        async with self._lock:
            result = await self._start_focus(minutes)
            return {"result": result, "state": self._changed_state()}

    async def act_pause(self, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._phase == "idle":
                raise UserFacingError("Фокус не запущен")
            if self._running():
                self._pause(self.clock(), "user")
            return {"state": self._changed_state()}

    async def act_resume(self, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._phase == "paused":
                self._resume(self.clock())
            elif self._phase == "idle":
                await self._start_focus(None)  # после перерыва — следующий фокус серии
            return {"state": self._changed_state()}

    async def act_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        """Хоткей: ничего не идёт — начать фокус, идёт — пауза, на паузе — продолжить."""
        async with self._lock:
            if self._running():
                self._pause(self.clock(), "user")
            elif self._phase == "paused":
                self._resume(self.clock())
            else:
                await self._start_focus(None)
            return {"state": self._changed_state()}

    async def act_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._phase == "idle" and self._waiting_since is None:
                raise UserFacingError("Фокус и так не запущен")
            worked = await self._stop()
            return {"worked_min": round(worked / 60, 1), "state": self._changed_state()}

    async def act_break(self, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            result = await self._take_break()
            return {"result": result, "state": self._changed_state()}

    async def act_skip_break(self, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if not self._on_break() and self._waiting_since is None:
                raise UserFacingError("Сейчас не перерыв")
            if self._on_break():
                self._phase = "idle"  # перерыв обрывается, серия продолжается
                self._waiting_since = self.clock()
            await self._start_focus(None)
            return {"state": self._changed_state()}

    async def act_allow(self, args: dict[str, Any]) -> dict[str, Any]:
        """«Нужно по работе»: не напоминать об этой программе или сайте до конца серии."""
        key = str(args.get("key") or "").strip()
        if not key:
            raise UserFacingError("Что разрешить? Нужен key из карточки")
        self._allowed.add(key[:120])
        self._fg_key = ""
        return {"state": self._changed_state()}

    # --- голосовые команды -----------------------------------------------------------------------------
    def commands(self) -> list[DynamicCommand]:
        start = ("начни", "начать", "включи", "запусти")
        return [
            self.command(
                "start",
                "Начать фокус",
                [
                    *(f"{v} фокус" for v in start),
                    "включи режим фокуса",
                    "режим фокуса",
                    "начни помодоро",
                    "запусти помодоро",
                    "начни сессию фокуса",
                ],
                self._cmd_start,
            ),
            self.command(
                "start_minutes",
                "Фокус на N минут",
                [
                    *(f"{v} фокус на {{number}} минут" for v in start),
                    "фокус на {number} минут",
                    "начни помодоро на {number} минут",
                ],
                self._cmd_start,
            ),
            self.command(
                "start_hour",
                "Фокус на час",
                ["начни фокус на час", "включи фокус на час", "фокус на час"],
                lambda slots: self._cmd_start({**slots, "number": 60}),
            ),
            self.command(
                "start_half_hour",
                "Фокус на полчаса",
                ["начни фокус на полчаса", "включи фокус на полчаса", "фокус на полчаса"],
                lambda slots: self._cmd_start({**slots, "number": 30}),
            ),
            self.command(
                "pause",
                "Пауза фокуса",
                ["пауза фокуса", "поставь фокус на паузу", "приостанови фокус", "пауза помодоро"],
                self._cmd_pause,
            ),
            self.command(
                "resume",
                "Продолжить фокус",
                [
                    "продолжи фокус",
                    "продолжай фокус",
                    "продолжим фокус",
                    "возобнови фокус",
                    "сними фокус с паузы",
                    "возвращаемся к работе",
                    "вернемся к работе",
                ],
                self._cmd_resume,
            ),
            self.command(
                "stop",
                "Остановить фокус",
                [
                    "стоп фокус",
                    "останови фокус",
                    "выключи фокус",
                    "закончи фокус",
                    "заверши фокус",
                    "выключи режим фокуса",
                    "стоп помодоро",
                ],
                self._cmd_stop,
            ),
            self.command(
                "break",
                "Перерыв",
                ["перерыв", "сделай перерыв", "сделаем перерыв", "хочу перерыв", "начни перерыв"],
                self._cmd_break,
            ),
            self.command(
                "skip_break",
                "Пропустить перерыв",
                ["пропусти перерыв", "закончи перерыв", "без перерыва", "хватит отдыхать"],
                self._cmd_skip_break,
            ),
            self.command(
                "left",
                "Сколько осталось",
                [
                    "сколько осталось",
                    "сколько осталось фокуса",
                    "сколько осталось до перерыва",
                    "сколько до перерыва",
                    "сколько еще работать",
                    "сколько осталось перерыва",
                    "сколько еще отдыхать",
                ],
                self._cmd_left,
                icon="timer",
            ),
            self.command(
                "stats",
                "Статистика фокуса",
                [
                    "как я сегодня поработал",
                    "как я сегодня работал",
                    "сколько я сегодня работал",
                    "сколько я сегодня поработал",
                    "статистика фокуса",
                ],
                self._cmd_stats,
            ),
        ]

    async def _reply(self, slots: dict[str, Any], title: str, message: str, say: str) -> ExecResult:
        """Ответ на голосовую команду. Тихий режим включил сам «Фокус» — диспетчер промолчит, а на вопрос
        человека ответить голосом всё же стоит: тогда говорим сами."""
        if say and slots.get("_source") == "voice" and self._silent_held:
            await self._speak(say, interrupt=True)
        return ExecResult(True, title, message, icon=self.icon, say=say)

    def _until(self, seconds: float) -> str:
        return time.strftime("%H:%M", time.localtime(self.wall() + seconds))

    async def _cmd_start(self, slots: dict[str, Any]) -> ExecResult:
        minutes = _minutes_arg(slots.get("number"))
        a = self._a()
        async with self._lock:
            result = await self._start_focus(minutes)
            self._changed_state()
            left = self._left_at(self.clock())
            total = self._total / 60
        if result == "running":
            say = f"Фокус уже идёт{a}. До перерыва {left_text(left)}."
            return await self._reply(slots, "Фокус", f"До перерыва {left_text(left)}", say)
        if result == "resumed":
            say = f"Продолжаем{a}. До перерыва {left_text(left)}."
            return await self._reply(slots, "Фокус", f"До перерыва {left_text(left)}", say)
        if result == "changed":
            length = span_text(total, accusative=False)
            say = f"Хорошо{a}. Фокус теперь {length}, до перерыва {left_text(left)}."
            return await self._reply(slots, "Фокус", f"До перерыва {left_text(left)}", say)
        if result == "done":
            return ExecResult(True, "Фокус", "Фокус закончен — время перерыва", icon=self.icon, say="")
        quiet = " Не буду отвлекать." if self._silent_held else ""
        say = f"Начинаю фокус на {span_text(total)}{a}.{quiet}"
        message = f"{span_text(total, accusative=False)} — до {self._until(left)}"
        return await self._reply(slots, "Фокус", message, say)

    async def _cmd_pause(self, slots: dict[str, Any]) -> ExecResult:
        a = self._a()
        async with self._lock:
            if self._phase == "idle":
                return await self._reply(slots, "Пауза фокуса", "Фокус не запущен", f"Фокус не запущен{a}.")
            already = self._phase == "paused"
            if not already:
                self._pause(self.clock(), "user")
                self._changed_state()
            left = left_text(self._left_at(self.clock()))
        say = f"Фокус и так на паузе{a}." if already else f"Ставлю на паузу{a}. Осталось — {left}."
        return await self._reply(slots, "Пауза фокуса", f"Осталось — {left}", say)

    async def _cmd_resume(self, slots: dict[str, Any]) -> ExecResult:
        a = self._a()
        async with self._lock:
            phase = self._phase
            waiting = self._waiting_since is not None
            if phase == "paused":
                self._resume(self.clock())
            elif phase == "idle":
                await self._start_focus(None)
            self._changed_state()
            left = left_text(self._left_at(self.clock()))
            on_break = self._on_break()
        if phase == "idle":
            verb = "Продолжаем" if waiting else "Начинаем"
            say = f"{verb}{a}. Фокус на {span_text(self._total / 60)}."
        elif on_break:
            say = f"Продолжаем перерыв{a}. Осталось — {left}."
        elif phase == "paused":
            say = f"Продолжаем{a}. До перерыва {left}."
        else:
            say = f"Фокус и так идёт{a}. До перерыва {left}."
        return await self._reply(slots, "Продолжить фокус", f"Осталось — {left}", say)

    async def _cmd_stop(self, slots: dict[str, Any]) -> ExecResult:
        a = self._a()
        async with self._lock:
            if self._phase == "idle" and self._waiting_since is None:
                return await self._reply(
                    slots, "Остановить фокус", "Фокус и так не запущен", f"Фокус и так не запущен{a}."
                )
            await self._stop()
            self._changed_state()
        today = self.stats()["today"]["min"]
        if today:
            say = (
                f"Фокус остановлен{a}. Сегодня — {span_text(today, accusative=False)} сосредоточенной работы."
            )
            message = f"Сегодня — {span_text(today, accusative=False)}"
        else:
            say, message = f"Фокус остановлен{a}.", "Остановлен"
        return await self._reply(slots, "Остановить фокус", message, say)

    async def _cmd_break(self, slots: dict[str, Any]) -> ExecResult:
        a = self._a()
        async with self._lock:
            result = await self._take_break()
            self._changed_state()
            left = left_text(self._left_at(self.clock()))
            long = self._long
        if result == "already":
            say = f"Вы и так на перерыве{a}. Осталось — {left}."
        elif result == "resumed":
            say = f"Продолжаем перерыв{a}. Осталось — {left}."
        else:
            kind = "Длинный перерыв" if long else "Перерыв"
            say = f"{kind} {left}{a}. Отдохните."
        return await self._reply(slots, "Перерыв", f"Осталось — {left}", say)

    async def _cmd_skip_break(self, slots: dict[str, Any]) -> ExecResult:
        a = self._a()
        try:
            await self.act_skip_break({})
        except UserFacingError:
            return await self._reply(
                slots, "Пропустить перерыв", "Сейчас не перерыв", f"Сейчас не перерыв{a}."
            )
        total = span_text(self._total / 60)
        return await self._reply(
            slots,
            "Пропустить перерыв",
            f"Фокус на {total}",
            f"Без перерыва так без перерыва{a}. Фокус на {total}.",
        )

    async def _cmd_left(self, slots: dict[str, Any]) -> ExecResult:
        a = self._a()
        left_s = self._left_at(self.clock())
        left = left_text(left_s)
        if self._phase == "focus":
            until = "длинного перерыва" if self._next() == "long_break" else "перерыва"
            say, message = f"До {until} {left}{a}.", f"До {until} — {left}"
        elif self._phase == "break":
            say, message = f"Перерыв ещё {left}{a}.", f"Перерыв — ещё {left}"
        elif self._phase == "paused" and self._paused_from == "focus":
            say, message = f"Фокус на паузе{a}: до перерыва — {left}.", f"Пауза: до перерыва — {left}"
        elif self._phase == "paused":
            say, message = f"Перерыв на паузе{a}: осталось — {left}.", f"Пауза: перерыв — ещё {left}"
        elif self._waiting_since is not None:
            say, message = f"Перерыв окончен{a}. Скажите «продолжи фокус».", "Перерыв окончен"
        else:
            say, message = f"Фокус сейчас не идёт{a}. Скажите «начни фокус».", "Фокус не запущен"
        return await self._reply(slots, "Сколько осталось", message, say)

    async def _cmd_stats(self, slots: dict[str, Any]) -> ExecResult:
        a = self._a()
        stats = self.stats()
        today = stats["today"]
        if not today["min"]:
            say = f"Сегодня фокуса ещё не было{a}. Начнём?"
            return await self._reply(slots, "Статистика фокуса", "Сегодня — пока ноль минут", say)
        worked = span_text(today["min"])
        parts = [f"Сегодня вы сосредоточенно работали {worked}{a}"]
        if today["sessions"]:
            parts[0] += f": {numbers.count_text(today['sessions'], SESSIONS)}"
        parts[0] += "."
        if stats["streak"] >= 2:
            parts.append(f"Серия — {numbers.count_text(stats['streak'], DAYS)} подряд.")
        best = stats["best"]
        if best and best["date"] == today["date"] and stats["total_min"] > today["min"]:
            parts.append("Это ваш лучший день!")  # лучший среди нескольких, а не первый
        message = (
            f"{span_text(today['min'], accusative=False)} · {numbers.count_text(today['sessions'], SESSIONS)}"
        )
        return await self._reply(slots, "Статистика фокуса", message, " ".join(parts))
