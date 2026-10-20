"""Характер Джарвиса: ответы голосом, как у Джарвиса из «Железного человека», — коротко, учтиво, с обращением
из профиля («сэр» — только если оно задано) и в нескольких вариантах, чтобы не повторяться.

Включён, когда персона ИИ — «Джарвис» (settings.ai.persona); с персоной «Обычный» всё звучит как раньше.

- spoken(result) — что сказать после команды: свой ответ встроенной команды («Сворачиваю все окна, сэр.»,
  «Приятного просмотра, сэр.»), «Запускаю протокол игры» для игр, «Открываю <программа>, сэр.» для программ,
  обращение к ответам-сведениям («Сейчас 14 часов 5 минут, сэр.»). None — как раньше: своя фраза команды
  человека, ошибки, цепочки команд. Персонаж только переиначивает то, что и так прозвучало бы: с «Озвучивать
  итог команд: не озвучивать» подтверждения молчат и у него.
- with_ack(…) — команда думает дольше ACK_AFTER_S (погода из сети, музыка проверяет, заиграла ли) — сразу
  «Секунду, сэр.», итог — следом, не перебивая.
- reminder(…) — «Сэр, напоминаю: …», «Сэр, время вышло: таймер на 3 минуты.»
- greeting(…) — первая фраза сводки «С возвращением» и «Доброе утро» (jarvis.scenarios.briefing).

Вариант для ключа выбирается случайно, но не тот, что прозвучал в прошлый раз, а из вариантов, звук которых
уже готов (Tts.is_ready), — в первую очередь. Варианты без данных — частые ответы Tts (add_stock): звук
облачного голоса готовится заранее (самые частые — первыми) и хранится на диске — звучат сразу, без сети.
Фразы с названием программы или сайта — Tts.keep: после первого раза тоже с диска.
"""

import asyncio
import contextlib
import logging
import random
import re
from collections.abc import Awaitable, Iterable
from typing import TYPE_CHECKING, Any

from jarvis.commands import numbers
from jarvis.service_base import BaseService
from jarvis.services import ExecResult

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

ACK_AFTER_S = 0.35  # команда думает дольше — сразу «Секунду, сэр», чтобы ответ начался мгновенно
POLITE_MAX = 140  # к сведениям длиннее (ответ ИИ, сводка) обращение не добавляем

# --- фразы -------------------------------------------------------------------------------------------------
# {address} — обращение из профиля, {Address} — оно же с заглавной; без обращения запятые убираются сами

OK = (
    "Готово, {address}.",
    "Сделано, {address}.",
    "Выполнено, {address}.",
    "Есть, {address}.",
    "Будет исполнено, {address}.",
)
ACK = ("Секунду, {address}.", "Минуту, {address}.", "Сейчас, {address}.")
ACK_WEATHER = ("Уточняю, {address}.", "Секунду, {address}. Смотрю прогноз.")

REPLIES: dict[str, tuple[str, ...]] = {
    # окна
    "builtin.windows.minimize_all": (
        "Сворачиваю все окна, {address}.",
        "Убираю окна, {address}.",
        "Окна свёрнуты, {address}.",
    ),
    "builtin.windows.desktop": ("Рабочий стол, {address}.", "Показываю рабочий стол, {address}."),
    "builtin.windows.maximize": ("Разворачиваю, {address}.", "Окно на весь экран, {address}."),
    "builtin.windows.minimize": ("Сворачиваю, {address}.", "Убрал окно, {address}."),
    "builtin.windows.close": ("Закрываю окно, {address}.", "Окно закрыто, {address}."),
    # сам Джарвис
    "builtin.jarvis.show": (
        "Я здесь, {address}.",
        "Развернул окно, {address}.",
        "К вашим услугам, {address}.",
    ),
    "builtin.jarvis.hide": (
        "Ухожу в тень, {address}.",
        "Скрываюсь, {address}. Позовите, если понадоблюсь.",
        "Как скажете, {address}.",
    ),
    "builtin.jarvis.thanks": (
        "Всегда к вашим услугам, {address}.",
        "Рад помочь, {address}.",
        "Обращайтесь, {address}.",
        "Не стоит благодарности, {address}.",
    ),
    "builtin.jarvis.silent_off": ("Снова на связи, {address}.", "Снова говорю вслух, {address}."),
    "builtin.jarvis.settings": ("Открываю настройки, {address}.",),
    "builtin.jarvis.commands": ("Вот что я умею, {address}.",),
    # медиа и звук
    "builtin.media.next": ("Следующий трек, {address}.", "Переключаю, {address}."),
    "builtin.media.prev": ("Предыдущий трек, {address}.", "Возвращаю трек, {address}."),
    "builtin.media.close": ("Сделано, {address}.", "Музыка закрыта, {address}."),
    "builtin.volume.up": ("Громче, {address}.", "Добавил громкости, {address}."),
    "builtin.volume.down": ("Тише, {address}.", "Убавил громкость, {address}."),
    "builtin.volume.max": ("Громкость на максимум, {address}.",),
    "builtin.volume.mute": ("Звук выключен, {address}.", "Тишина, {address}."),
    "builtin.volume.unmute": ("Звук включён, {address}.",),
    # система
    "builtin.system.lock": ("Блокирую компьютер, {address}.",),
    "builtin.system.sleep": ("Перевожу компьютер в сон, {address}.", "Спящий режим, {address}."),
    "builtin.system.shutdown": ("Выключаю компьютер, {address}.",),
    "builtin.system.restart": ("Перезагружаю компьютер, {address}.",),
    "builtin.system.cancel_shutdown": (
        "Выключение отменено, {address}.",
        "Отбой, {address}. Компьютер остаётся включённым.",
    ),
    "builtin.system.screenshot": ("Снимок экрана сделан, {address}.", "Сохранил снимок экрана, {address}."),
    "builtin.system.screenshot_window": ("Снимок окна сделан, {address}.",),
    # сайты и игры
    "builtin.site.youtube": ("Приятного просмотра, {address}.", "Открываю YouTube, {address}."),
    "builtin.app.steam.open": (
        "Библиотека игр будет доступна через мгновение, {address}.",
        "Открываю Steam, {address}.",
    ),
    "builtin.games.mode": (
        "Игровой режим активирован, {address}.",
        "Игровой режим включён, {address}. Окна свёрнуты, библиотека игр открыта.",
    ),
}
MEDIA: dict[str, tuple[str, ...]] = {
    # плеер пришлось запустить (≈4 с) — «Секунду, сэр» уже прозвучало, итог — что он открыт
    "launched": (
        "Приложение открыто, {address}. Музыка играет.",
        "Плеер запущен, {address}. Приятного прослушивания.",
    ),
    "playing": ("Включаю музыку, {address}.", "Музыка, {address}.", "Запускаю воспроизведение, {address}."),
    "already_playing": ("Музыка уже играет, {address}.",),
    "paused": ("Выполняю.", "Пауза, {address}.", "Ставлю на паузу, {address}."),
    "already_paused": ("Уже на паузе, {address}.",),
}
GAME = ("Запускаю протокол игры, {address}.", "Игра загружается, {address}. Удачной охоты.")
TIMER = ("Таймер установлен, {address}.", "Таймер запущен, {address}.", "Засекаю, {address}.")
REMIND_SET = ("Принято, {address}.", "Записал, {address}.", "Хорошо, {address}.")
SHUTDOWN_IN = ("Выключаю компьютер, {address}.", "Принято, {address}. Готовлю компьютер к выключению.")
RESTART_IN = ("Перезагружаю компьютер, {address}.",)
WEATHER = ("Температура за бортом", "За бортом", "На улице сейчас")
STATUS_OK = (
    "В полном порядке, {address}. Все системы функционируют в штатном режиме.",
    "Все системы в норме, {address}.",
    "Лучше не бывает, {address}. Системы работают штатно.",
)
STATUS_BUSY = ("Системы работают, {address}, но есть нюансы.", "Держимся, {address}, хотя нагрузка заметная.")

GREET_BACK = (
    "С возвращением, {address}.",
    "Добро пожаловать обратно, {address}. Без вас здесь было подозрительно тихо.",
    "Рад вас видеть, {address}. Я присмотрел за всем, пока вас не было.",
    "С возвращением, {address}. С вами работа куда интереснее.",
)
GREET_TIME: dict[str, tuple[str, ...]] = {
    "morning": ("Доброе утро, {address}.", "Доброе утро, {address}. Надеюсь, вы выспались."),
    "day": ("Добрый день, {address}.", "Добрый день, {address}. Рад вас слышать."),
    "evening": ("Добрый вечер, {address}.", "Добрый вечер, {address}. Как прошёл день?"),
    "night": ("Доброй ночи, {address}.", "Доброй ночи, {address}. Работаем допоздна?"),
}
GREET_PLAIN = {
    "back": "С возвращением, {address}.",
    "morning": "Доброе утро, {address}.",
    "day": "Добрый день, {address}.",
    "evening": "Добрый вечер, {address}.",
    "night": "Доброй ночи, {address}.",
}
ALL_CLEAR = (
    "Новых напоминаний нет, всё под контролем.",
    "Срочных дел нет.",
    "Всё спокойно, срочного ничего.",
)
# «озвучь задачи на сегодня»: первая фраза — готовая (звучит сразу), список — следом
TODAY_LEAD = ("Сверяюсь с расписанием, {address}.", "Ваши планы на сегодня, {address}.")
TODAY_UNDATED = ("{Address}, на сегодня ничего не назначено.",)
TODAY_EMPTY = (
    "{Address}, на сегодня дел нет. Можно выдохнуть.",
    "Сегодня ничего не запланировано, {address}.",
)

REMIND = (
    "{Address}, напоминаю: {text}.",
    "Позвольте напомнить, {address}: {text}.",
    "{Address}, вы просили напомнить: {text}.",
)
REMIND_TIMER = ("{Address}, время вышло: {text}.", "Таймер сработал, {address}: {text}.")
REMIND_EVENT = ("{Address}, напоминаю: {text}.", "{Address}, по календарю: {text}.")
REMIND_MISSED = ("{Address}, пока вас не было, сработало напоминание: {text}.",)
REMIND_PLAIN = "Напоминаю, {address}: {text}"

# команды, которые бывают долгими (сеть, проверка, что музыка заиграла): им — «Секунду, сэр» сразу
ACK_COMMANDS: dict[str, tuple[str, ...]] = {
    "builtin.info.weather": ACK_WEATHER,
    "builtin.info.weather_city": ACK_WEATHER,
    "builtin.media.play": ACK,
    "builtin.media.resume": ACK,
    "builtin.media.toggle": ACK,
}
_TIMERS = ("builtin.timers.minutes", "builtin.timers.seconds", "builtin.timers.hours", "builtin.timers.hour")
_TIMERS += ("builtin.timers.half_hour", "builtin.timers.minute")
_SHUTDOWN_IN = (
    "builtin.system.shutdown_in",
    "builtin.system.shutdown_in_hours",
    "builtin.system.shutdown_hour",
)

_STOCK_GROUPS: tuple[tuple[str, ...], ...] = (
    OK,
    ACK,
    ACK_WEATHER,
    *REPLIES.values(),
    *MEDIA.values(),
    GAME,
    TIMER,
    REMIND_SET,
    SHUTDOWN_IN,
    STATUS_OK,
    STATUS_BUSY,
    GREET_BACK,
    *GREET_TIME.values(),
    ALL_CLEAR,
    TODAY_LEAD,
    TODAY_UNDATED,
    TODAY_EMPTY,
)
_SPACE_COMMA = re.compile(r"\s*,\s*(?=[.!?:,;—])")
_LEAD_COMMA = re.compile(r"^\s*,\s*")


def with_address(phrase: str, address: str) -> str:
    """«Готово, {address}.» → «Готово, сэр.»; без обращения — «Готово.», «{Address}, напоминаю: …» —
    «Напоминаю: …»."""
    address = (address or "").strip()
    out = phrase.replace("{Address}", address[:1].upper() + address[1:]).replace("{address}", address)
    if not address:
        out = _LEAD_COMMA.sub("", _SPACE_COMMA.sub("", out)).strip()
        out = out[:1].upper() + out[1:]
    return out


def lower_first(text: str) -> str:
    """«Позвонить маме» → «позвонить маме»; аббревиатуры («ДР Ани», «CS2») не трогаем."""
    if len(text) > 1 and text[0].isupper() and not text[1].isupper() and not text[1].isdigit():
        return text[0].lower() + text[1:]
    return text


def degrees(value: float) -> str:
    v = round(value)
    sign = "плюс " if v > 0 else "минус " if v < 0 else ""
    return f"{sign}{abs(v)} {numbers.plural(abs(v), numbers.DEGREES)}"


def day_part(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "day"
    if 18 <= hour < 23:
        return "evening"
    return "night"


class Character(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self.random = random.Random()
        self.ack_after_s = ACK_AFTER_S
        self._last: dict[str, str] = {}  # ключ → прозвучавший в прошлый раз вариант
        self._site_names: dict[str, str] | None = None
        add_stock = getattr(getattr(ctx, "tts", None), "add_stock", None)
        if callable(add_stock):
            add_stock(self.stock_phrases)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    # --- общее --------------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """Персона «Джарвис» — отвечаем в его характере."""
        return self.ctx.settings.value.ai.persona == "jarvis"

    @property
    def address(self) -> str:
        return (self.ctx.settings.value.profile.address or "").strip()

    def fmt(self, phrase: str, **values: str) -> str:
        out = with_address(phrase, self.address)
        for name, value in values.items():
            out = out.replace("{" + name + "}", value)
        return out

    def stock_phrases(self) -> list[str]:
        """Варианты без данных — частые ответы Tts (звук готовится заранее): сначала первые варианты всех
        ключей, потом вторые и так далее — подготовка с ограничением по объёму успевает самое частое."""
        if not self.active:
            return []
        out: list[str] = []
        depth = max(len(g) for g in _STOCK_GROUPS)
        for i in range(depth):
            for group in _STOCK_GROUPS:
                if i < len(group):
                    text = self.fmt(group[i])
                    if "{" not in text:
                        out.append(text)
        return list(dict.fromkeys(out))

    def pick(self, key: str, variants: Iterable[str], **values: str) -> str:
        """Вариант фразы: не тот, что в прошлый раз, и — если есть — из тех, чей звук уже готов."""
        texts = list(dict.fromkeys(self.fmt(v, **values) for v in variants))
        if not texts:
            return ""
        last = self._last.get(key)
        pool = [t for t in texts if t != last] or texts
        ready = [t for t in pool if self._ready(t)]
        choice = self.random.choice(ready or pool)
        self._last[key] = choice
        return choice

    def _ready(self, text: str) -> bool:
        probe = getattr(getattr(self.ctx, "tts", None), "is_ready", None)
        if not callable(probe):
            return False
        try:
            return bool(probe(text))
        except Exception:
            return False

    def _keep(self, text: str) -> str:
        """В фразе нет личного (название программы или сайта): звук можно хранить на диске."""
        keep = getattr(getattr(self.ctx, "tts", None), "keep", None)
        if callable(keep):
            with contextlib.suppress(Exception):
                keep(text)
        return text

    def polite(self, text: str) -> str:
        """Ответ-сведение с обращением: «Сейчас 14 часов» → «Сейчас 14 часов, сэр.»; длинное, вопрос, уже с
        обращением — как есть."""
        text = text.strip()
        address = self.address
        if not text or not address or address.casefold() in text.casefold() or len(text) > POLITE_MAX:
            return text
        body = text.rstrip(" .!")
        if not body or body.endswith(("?", "…", ":")) or re.search(r"[.!?]\s", body):
            return text
        return f"{body}, {address}."

    # --- ответ на команду ---------------------------------------------------------------------------------
    def spoken(self, result: ExecResult, *, confirmations: bool = True) -> str | None:
        """Что сказать после удачной команды в характере Джарвиса; None — как без персонажа."""
        if not self.active or not result.ok or len(result.command_ids) != 1:
            return None
        if not result.say and not confirmations:
            return None  # подтверждения выключены, а сведений нет — молчим, как и без персонажа
        command_id = result.command_ids[0]
        data = result.data or {}
        if not command_id.startswith("builtin."):
            # своя команда, пак, сценарий: их фраза — их; без фразы — программа или общее «Готово, сэр»
            if result.say:
                return None
            return self._app_reply(data) or self.pick("ok", OK)
        reply = self._builtin(command_id, result, data)
        if reply:
            return reply
        if result.say:
            return self.polite(result.say)
        return self._app_reply(data) or self.pick("ok", OK)

    def _builtin(self, command_id: str, result: ExecResult, data: dict[str, Any]) -> str:
        if command_id == "builtin.jarvis.status":
            return self.status(data.get("system") or {}) or self.polite(result.say)
        if command_id == "builtin.info.weather":
            return self._weather(data.get("weather") or {}) or self.polite(result.say)
        if command_id in _TIMERS and data.get("seconds"):
            when = numbers.duration_text(float(data["seconds"]), accusative=True)
            return f"{self.pick('timer', TIMER)} Сработает через {when}."
        if command_id.startswith("builtin.timers.remind") and data.get("seconds"):
            when = numbers.duration_text(float(data["seconds"]), accusative=True)
            return f"{self.pick('remind_set', REMIND_SET)} Напомню через {when}."
        if command_id in _SHUTDOWN_IN and data.get("delay"):
            when = numbers.duration_text(float(data["delay"]))
            return f"{self.pick('shutdown_in', SHUTDOWN_IN)} Отключение через {when}."
        if command_id == "builtin.system.restart" and data.get("delay"):
            when = numbers.duration_text(float(data["delay"]))
            return f"{self.pick('restart_in', RESTART_IN)} Через {when}."
        media = str(data.get("media") or "")
        if media in MEDIA and command_id.startswith("builtin.media."):
            return self.pick(f"media.{media}", MEDIA[media])
        if command_id in REPLIES:
            return self.pick(command_id, REPLIES[command_id])
        if command_id.startswith("builtin.site."):
            name = self._site_name(command_id.removeprefix("builtin.site."))
            if name:
                return self._keep(self.fmt("Открываю {site}, {address}.", site=name))
        closed = str(data.get("closed") or "")
        if closed:
            return self._keep(self.fmt("Закрываю {app}, {address}.", app=closed))
        return ""

    def _app_reply(self, data: dict[str, Any]) -> str:
        """Запущена программа: игра — «Запускаю протокол игры», Steam — библиотека, иное — «Открываю …»."""
        if data.get("game"):
            return self.pick("game", GAME)
        app = str(data.get("app") or "")
        if not app:
            return ""
        if data.get("app_key") == "steam":
            return self.pick("builtin.app.steam.open", REPLIES["builtin.app.steam.open"])
        return self._keep(self.fmt("Открываю {app}, {address}.", app=app))

    def _site_name(self, key: str) -> str:
        if self._site_names is None:
            from jarvis.commands.sites import SITES

            self._site_names = {site.key: site.name for site in SITES}
        return self._site_names.get(key, "")

    def _weather(self, weather: dict[str, Any]) -> str:
        temp = weather.get("temp")
        if not isinstance(temp, int | float):
            return ""
        condition = str(weather.get("condition") or "").strip()
        text = f"{self.pick('weather', WEATHER)} {degrees(temp)}"
        if condition:
            text += f", {condition}"
        feels = weather.get("feels")
        if isinstance(feels, int | float) and abs(feels - temp) >= 4:
            text += f", ощущается как {degrees(feels)}"
        return self.polite(text + ".")

    def status(self, system: dict[str, Any]) -> str:
        """«Как дела?» — состояние систем: всё в норме — «В полном порядке, сэр…» и цифры; есть что-то
        тревожное (нагрузка, место, заряд, жар видеокарты) — об этом. Температуру процессора не выдумываем."""
        cpu, ram = system.get("cpu"), system.get("ram")
        if not isinstance(cpu, int | float) or not isinstance(ram, int | float):
            return ""
        cpu, ram = round(cpu), round(ram)
        free, battery = system.get("disk_free_gb"), system.get("battery")
        temp = system.get("gpu_temp")
        issues: list[str] = []
        if cpu >= 85:
            issues.append(f"процессор загружен на {cpu} {numbers.plural(cpu, numbers.PERCENT)}")
        if ram >= 90:
            issues.append(f"память занята на {ram} {numbers.plural(ram, numbers.PERCENT)}")
        if isinstance(free, int | float) and free < 10:
            gb = max(0, round(free))
            issues.append(f"на системном диске осталось {gb} {numbers.plural(gb, ('гигабайт',) * 3)}")
        if isinstance(battery, int | float) and battery <= 15 and not system.get("charging"):
            b = round(battery)
            issues.append(f"заряд батареи — {b} {numbers.plural(b, numbers.PERCENT)}")
        if isinstance(temp, int | float) and temp >= 85:
            issues.append(f"видеокарта нагрелась до {degrees(temp).removeprefix('плюс ')}")
        if issues:
            return f"{self.pick('status_busy', STATUS_BUSY)} Обратите внимание: {'; '.join(issues)}."
        text = (
            f"{self.pick('status_ok', STATUS_OK)} Процессор загружен на {cpu} "
            f"{numbers.plural(cpu, numbers.PERCENT)}, память — на {ram}."
        )
        if isinstance(temp, int | float):
            t = round(temp)
            text += f" Температура видеокарты {t} {numbers.plural(t, numbers.DEGREES)} — в норме."
        if isinstance(battery, int | float):
            b = round(battery)
            text += f" Заряд батареи {b} {numbers.plural(b, numbers.PERCENT)}."
        return text

    # --- «Секунду, сэр» -----------------------------------------------------------------------------------
    def ack_phrases(self, match: Any) -> tuple[str, ...]:
        """Команда из долгих (погода, музыка) и одна во фразе — фразы «Секунду, сэр»; иначе пусто."""
        if not self.active:
            return ()
        parts = getattr(match, "parts", None) or []
        if len(parts) != 1:
            return ()
        return ACK_COMMANDS.get(str(getattr(parts[0], "command_id", "")), ())

    def _can_ack(self) -> bool:
        voice = self.ctx.settings.value.voice
        if voice.silent_mode or voice.speak_confirmations != "short":
            return False
        waiting = getattr(getattr(self.ctx, "confirmations", None), "waiting", None)
        return not (callable(waiting) and waiting())

    async def with_ack(
        self, work: Awaitable[ExecResult], phrases: tuple[str, ...]
    ) -> tuple[ExecResult, bool]:
        """Выполнить команду; не успела за ack_after_s — сразу короткое «Секунду, сэр» (звук готов заранее).
        Вернёт (итог, прозвучало ли «секунду»): итог тогда говорится следом, не перебивая."""
        task = asyncio.ensure_future(work)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.ack_after_s)
            if done or not phrases or not self._can_ack():
                return await task, False
            try:
                await self.ctx.tts.say(self.pick("ack", phrases), interrupt=True)
            except Exception:
                log.exception("характер: «секунду» не озвучено")
                return await task, False
            return await task, True
        except asyncio.CancelledError:
            task.cancel()
            raise

    # --- напоминания и приветствия ------------------------------------------------------------------------
    def reminder(self, item: dict[str, Any], message: str, *, missed: bool) -> str:
        """Напоминание голосом. С персоной «Джарвис» — «Сэр, напоминаю: позвонить маме.», таймер — «Сэр,
        время вышло: таймер на 3 минуты.»; иначе — как раньше: «Напоминаю, сэр: …»."""
        text = " ".join(str(message or "").split()).rstrip(" .!")
        if not self.active:
            return with_address(REMIND_PLAIN, self.address).replace("{text}", text)
        body = lower_first(text)
        if missed:
            return self.pick("remind_missed", REMIND_MISSED, text=body)
        if item.get("kind") == "event":
            return self.pick("remind_event", REMIND_EVENT, text=body)
        if text.casefold().startswith("таймер на"):
            return self.pick("remind_timer", REMIND_TIMER, text=body)
        return self.pick("remind", REMIND, text=body)

    def greeting(self, kind: str, hour: int) -> str:
        """Первая фраза приветствия: kind «back» — «С возвращением», «hello» — по времени суток."""
        part = "back" if kind == "back" else day_part(hour)
        if not self.active:
            return with_address(GREET_PLAIN[part], self.address)
        variants = GREET_BACK if part == "back" else GREET_TIME[part]
        return self.pick(f"greet.{part}", variants)

    def all_clear(self) -> str:
        if not self.active:
            return "Новых напоминаний нет."
        return self.pick("all_clear", ALL_CLEAR)

    def today(self, kind: str) -> str:
        """Готовая первая фраза «дел на сегодня» (kind: lead — перед списком, undated — ничего не назначено,
        empty — дел нет); "" — персона не «Джарвис» (тогда текст сводки как раньше)."""
        if not self.active:
            return ""
        groups = {"lead": TODAY_LEAD, "undated": TODAY_UNDATED, "empty": TODAY_EMPTY}
        return self.pick(f"today.{kind}", groups[kind])
