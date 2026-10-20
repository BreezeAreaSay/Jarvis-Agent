"""Приветствие по возвращении: человек отошёл от компьютера (без ввода voice.presence_away_min минут или
компьютер спал) и снова взялся за мышь или клавиатуру — Jarvis здоровается и говорит, что важно сейчас:
напоминания, сработавшие без него, ближайшие напоминания и встречи, задачи на сегодня (сводка
jarvis.scenarios.briefing, режим welcome, — та же, что на фразу «Джарвис, я вернулся»).

voice.presence_greeting: off (по умолчанию) — не здороваться; news — только если есть что сказать
(напоминания); always — всегда. Выключено по умолчанию: Jarvis заговорит сам, без просьбы, и прочтёт
напоминания вслух — а рядом могут быть другие люди; на «я вернулся» он отвечает и без этой настройки.

Сам не заговорит никогда: в тихом режиме; над игрой или видео во весь экран; во время звонка (микрофон пишет
другая программа — platform.microphone_users); пока Jarvis говорит, слушает, ждёт «да / нет», работает агент;
пока идёт «Фокус» (он сам скажет «С возвращением»); когда у человека есть свой включённый сценарий с триггером
«Вернулся после отсутствия» (здороваться дважды незачем). Здоровается раз на возвращение — а им считается
только отсутствие не короче presence_away_min минут. Промолчал — событие presence.skipped {reason};
поздоровался — тост «С возвращением» и presence.greeted {away_min, news}.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jarvis.service_base import BaseService

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

POLL_S = 5.0
RETURN_IDLE_S = 10.0  # ввод за последние 10 секунд — человек вернулся (как у сценариев)
SLEEP_GAP_S = 30.0  # между проверками прошло больше — компьютер спал: человек тоже был не здесь
SAY_TIMEOUT_S = 30.0

# почему промолчал (presence.skipped)
QUIET_REASONS = {
    "silent": "тихий режим",
    "fullscreen": "игра или видео во весь экран",
    "call": "идёт звонок",
    "busy": "Jarvis занят",
    "focus": "идёт «Фокус»",
    "scenario": "есть свой сценарий «С возвращением»",
    "nothing": "нечего сказать",
}


class Presence(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self.clock: Callable[[], float] = time.time  # тесты подменяют время
        self.poll_s = POLL_S
        self._task: asyncio.Task[None] | None = None
        self._away_since: float | None = None  # человек отошёл (пока не вернулся)
        self._last_away: float | None = None  # последний завершённый уход — для сводки «я вернулся»
        self._last_tick: float | None = None

    async def start(self) -> None:
        self._task = self.ctx.spawn(self._loop(), "presence")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("приветствие по возвращении: проверка упала")
            await asyncio.sleep(self.poll_s)

    def away_since(self) -> float | None:
        """Когда человек отошёл в последний раз (или отошёл сейчас); None — неизвестно."""
        return self._away_since if self._away_since is not None else self._last_away

    # --- проверка -------------------------------------------------------------------------------------------
    async def tick(self) -> str:
        """Одна проверка. Вернёт, что случилось: "" — ничего, "away" — человек отошёл, "greeted" —
        поздоровался, иначе причина молчания (QUIET_REASONS)."""
        settings = self.ctx.settings.value.voice
        now = self.clock()
        prev, self._last_tick = self._last_tick, now
        if settings.presence_greeting == "off":
            self._away_since = None
            return ""
        try:
            idle = float(await self.ctx.run_platform(self.ctx.platform.idle_seconds))
        except Exception:
            return ""
        need = settings.presence_away_min * 60.0
        if self._away_since is None:
            if idle >= need:
                self._away_since = now - idle
                return "away"
            if prev is not None and now - prev >= max(need, SLEEP_GAP_S):  # компьютер спал
                self._away_since = prev
            else:
                return ""
        if idle >= RETURN_IDLE_S:
            return ""
        since, self._away_since = self._away_since, None
        self._last_away = since
        return await self._returned(since, now - idle, settings.presence_greeting)

    async def _returned(self, since: float, back_at: float, mode: str) -> str:
        reason = await self.quiet_reason()
        if not reason:
            from jarvis.scenarios.briefing import welcome

            found = await welcome(self.ctx, "back", now=back_at, since=since, weather=False)
            if mode == "news" and not found.news:
                reason = "nothing"
            else:
                return await self._greet(found.shown, found.spoken, since, back_at, found.news)
        log.info("приветствие по возвращении: промолчал — %s", QUIET_REASONS.get(reason, reason))
        self.ctx.bus.publish("presence.skipped", {"reason": reason})
        return reason

    async def _greet(self, shown: str, spoken: str, since: float, back_at: float, news: bool) -> str:
        self.ctx.bus.publish(
            "toast",
            {
                "title": "С возвращением",
                "message": shown,
                "status": "info",
                "icon": "home",
                "ts": time.time(),
            },
        )
        away_min = max(1, round((back_at - since) / 60))
        self.ctx.bus.publish("presence.greeted", {"away_min": away_min, "news": news})
        try:
            await asyncio.wait_for(self.ctx.tts.say(spoken), SAY_TIMEOUT_S)
        except Exception:
            log.exception("приветствие по возвращении не озвучено")
        return "greeted"

    async def quiet_reason(self) -> str:
        """Почему сейчас нельзя заговорить самому ("" — можно)."""
        ctx = self.ctx
        if ctx.settings.value.voice.silent_mode:
            return "silent"
        if self._busy():
            return "busy"
        if self._focus_running():
            return "focus"
        if self._own_scenario():
            return "scenario"
        platform = ctx.platform
        try:
            if await ctx.run_platform(platform.is_fullscreen_app):
                return "fullscreen"
        except Exception:
            log.debug("приветствие: полноэкранное окно не проверить", exc_info=True)
        try:
            users = await ctx.run_platform(platform.microphone_users)
        except Exception:
            users = []
        if users:
            log.info("приветствие: микрофон пишет %s — звонок", ", ".join(users))
            return "call"
        return ""

    def _busy(self) -> bool:
        ctx: Any = self.ctx
        if getattr(getattr(ctx, "tts", None), "speaking", False):
            return True
        if getattr(getattr(ctx, "agent", None), "busy", False):
            return True
        waiting = getattr(getattr(ctx, "confirmations", None), "waiting", None)
        if callable(waiting) and waiting():
            return True
        return getattr(getattr(ctx, "voice", None), "state", "") in ("listening", "processing")

    def _focus_running(self) -> bool:
        plugins: Any = getattr(self.ctx, "plugins", None)
        try:
            focus = plugins.get("focus") if plugins is not None else None
        except Exception:
            return False
        if focus is None or not getattr(focus, "enabled", False):
            return False
        try:
            return str(focus.status().get("phase") or "idle") != "idle"
        except Exception:
            return False

    def _own_scenario(self) -> bool:
        scenarios: Any = getattr(self.ctx, "scenarios", None)
        try:
            items = scenarios.list_all() if scenarios is not None else []
        except Exception:
            return False
        return any(
            item.get("enabled") and any(t.get("type") == "idle_return" for t in item.get("triggers") or [])
            for item in items
        )
