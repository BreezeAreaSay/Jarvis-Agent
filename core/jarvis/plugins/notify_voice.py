"""«Озвучка уведомлений»: Jarvis читает вслух уведомления Windows — «Telegram: Аня. Привет, как дела?».

Источник — UserNotificationListener (WinRT): плагин просит доступ к уведомлениям и раз в ~2 с читает список
всплывающих уведомлений; новые (по id) озвучиваются ctx.tts.say(f"{программа}: {заголовок}. {текст}").
Молчит в тихом режиме, во время игр и видео во весь экран (настройка) и фильтрует программы
(config.apps_allow — только эти, config.apps_block — кроме этих). Служебный шум не читает никогда (noise):
«Ножницы» и другие снимки экрана показывают уведомление после каждого снимка («Снимок экрана скопирован в
буфер обмена»). Без доступа статус объясняет, где его включить. Не в Windows — статус unsupported.
"""

import asyncio
import collections
import contextlib
import logging
import re
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jarvis.plugins import ru
from jarvis.plugins.base import ConfigField, Plugin, PluginAction, error_text
from jarvis.services import DynamicCommand, ExecResult

log = logging.getLogger(__name__)

POLL_S = 2.0
ACCESS_RETRY_S = 10.0
MAX_PER_ROUND = 3
SEEN_LIMIT = 500
ACCESS_HINT = (
    "Разрешите доступ к уведомлениям: Параметры Windows → Конфиденциальность и защита → Уведомления → "
    "включите «Доступ к уведомлениям» (и для Jarvis в списке приложений), затем нажмите «Запросить доступ»."
)
UNSUPPORTED_HINT = "Озвучка уведомлений работает только в Windows 10 и 11"
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# служебный шум: программы снимков экрана и их «снимок скопирован / сохранён» — после каждого снимка
NOISE_APPS = re.compile(
    r"ножниц|snipping|набросок на фрагменте|фрагмент и набросок|snip\s*&\s*sketch|screen\s*sketch|screenclip",
    re.IGNORECASE,
)
NOISE_TEXT = re.compile(
    r"(?:снимок экрана|скриншот|фрагмент)\w*\s+(?:скопирован|сохран)|"
    r"(?:screenshot|snip|screen capture)\w*\s+(?:copied|saved)",
    re.IGNORECASE,
)


def noise(toast: "Toast") -> bool:
    """Служебное уведомление, которое никому не нужно слышать: «Ножницы: снимок экрана скопирован…»."""
    return bool(NOISE_APPS.search(toast.app) or NOISE_TEXT.search(f"{toast.title} {toast.body}"))


@dataclass
class Toast:
    """Уведомление: id, программа, заголовок и текст."""

    id: int | str
    app: str
    title: str
    body: str = ""
    created: float = 0.0


class NotificationSource(Protocol):
    async def access(self, request: bool = False) -> str:
        """allowed | denied | unspecified | unsupported. request — показать запрос Windows."""
        ...

    async def fetch(self) -> list[Toast]: ...

    async def close(self) -> None: ...


class WinNotificationSource:
    """UserNotificationListener (WinRT) в своём потоке с собственным циклом asyncio."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener: Any = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        ready = threading.Event()
        holder: dict[str, asyncio.AbstractEventLoop] = {}

        def run() -> None:
            loop = asyncio.new_event_loop()
            holder["loop"] = loop
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        threading.Thread(target=run, name="plugin-notifications", daemon=True).start()
        ready.wait(5.0)
        self._loop = holder["loop"]
        return self._loop

    async def _call(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        async def runner() -> Any:
            return await factory()

        loop = self._ensure_loop()
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(runner(), loop))

    async def access(self, request: bool = False) -> str:
        return await self._call(lambda: self._access(request))

    async def _access(self, request: bool) -> str:
        from winrt.windows.ui.notifications.management import (  # type: ignore[import-not-found]
            UserNotificationListener,
            UserNotificationListenerAccessStatus,
        )

        listener = UserNotificationListener.current
        status = listener.get_access_status()
        if request and status == UserNotificationListenerAccessStatus.UNSPECIFIED:
            try:
                status = await listener.request_access_async()
            except Exception as e:  # без «окна» приложения Windows может не показать запрос
                log.info("уведомления: запрос доступа не показан (%s)", e)
        self._listener = listener
        if status == UserNotificationListenerAccessStatus.ALLOWED:
            return "allowed"
        if status == UserNotificationListenerAccessStatus.DENIED:
            return "denied"
        return "unspecified"

    async def fetch(self) -> list[Toast]:
        return await self._call(self._fetch)

    async def _fetch(self) -> list[Toast]:
        from winrt.windows.ui.notifications import (  # type: ignore[import-not-found]
            KnownNotificationBindings,
            NotificationKinds,
        )
        from winrt.windows.ui.notifications.management import (  # type: ignore[import-not-found]
            UserNotificationListener,
        )

        listener = self._listener or UserNotificationListener.current
        toasts: list[Toast] = []
        for item in await listener.get_notifications_async(NotificationKinds.TOAST):
            app = ""
            texts: list[str] = []
            created = 0.0
            with contextlib.suppress(Exception):
                app = str(item.app_info.display_info.display_name or "")
            with contextlib.suppress(Exception):
                binding = item.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
                if binding is not None:
                    texts = [str(t.text) for t in binding.get_text_elements() if t.text]
            with contextlib.suppress(Exception):
                created = float(item.creation_time.timestamp())
            toasts.append(Toast(item.id, app, texts[0] if texts else "", " ".join(texts[1:]), created))
        return toasts

    async def close(self) -> None:
        loop, self._loop = self._loop, None
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)


def _apps(value: str) -> list[str]:
    return [a.strip().casefold() for a in value.split(",") if a.strip()]


def _shorten(text: str, limit: int) -> str:
    text = " ".join(_URL_RE.sub("ссылка", text).split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip(" ,.;:") + "…"


class NotifyVoice(Plugin):
    id = "notify_voice"
    name = "Озвучка уведомлений"
    description = "Jarvis читает вслух новые уведомления Windows: мессенджеры, почту, напоминания."
    icon = "notification"
    color = ("#FCD34D", "#F59E0B")
    fields = (
        ConfigField(
            "apps_allow",
            "text",
            "Читать только эти программы",
            default="",
            placeholder="Telegram, Почта",
            hint="Через запятую; пусто — все программы",
        ),
        ConfigField("apps_block", "text", "Не читать программы", default="", placeholder="Discord, Steam"),
        ConfigField("read_body", "bool", "Читать текст уведомления", default=True),
        ConfigField("quiet_fullscreen", "bool", "Молчать во время игр и видео во весь экран", default=True),
        ConfigField(
            "max_chars",
            "number",
            "Самая длинная фраза, символов",
            default=200,
            min=40,
            max=1000,
            integer=True,
        ),
    )
    actions = (PluginAction("test", "Проверить голосом"), PluginAction("request_access", "Запросить доступ"))
    examples = ("не читай уведомления", "читай уведомления")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        # тесты подставляют свой источник уведомлений
        self.source_factory: Callable[[], NotificationSource] | None = None
        self._source: NotificationSource | None = None
        self._access = "unknown"
        self._seen: collections.OrderedDict[Any, None] = collections.OrderedDict()
        self._primed = False
        self._muted = False
        self._count = 0
        self._last: dict[str, Any] = {}

    @property
    def supported(self) -> bool:
        return self.source_factory is not None or sys.platform == "win32"

    async def start(self) -> None:
        self._primed = False
        self._seen.clear()
        if not self.supported:
            self._access = "unsupported"
            return
        self._source = self.source_factory() if self.source_factory is not None else WinNotificationSource()
        self.spawn(self._loop(), "poll")

    async def stop(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            try:
                await source.close()
            except Exception:
                log.debug("уведомления: источник не закрылся", exc_info=True)

    def status(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "supported": self.supported,
            "access": self._access,
            "muted": self._muted,
            "read": self._count,
            "last": self._last,
        }
        if not self.supported:
            data["hint"] = UNSUPPORTED_HINT
        elif self._access in ("denied", "unspecified"):
            data["hint"] = ACCESS_HINT
        return data

    async def check_access(self, request: bool = False) -> str:
        if self._source is None:
            return self._access
        try:
            access = await self._source.access(request)
        except Exception as e:
            log.info("уведомления: доступ не проверить (%s)", error_text(e))
            access = "unspecified"
            self.set_error(f"Уведомления недоступны: {error_text(e)}")
        if access != self._access:
            self._access = access
            self.changed()
        return access

    async def _loop(self) -> None:
        await self.check_access(request=True)
        while True:
            if self._access != "allowed":
                await asyncio.sleep(ACCESS_RETRY_S)
                await self.check_access()
                continue
            assert self._source is not None
            try:
                toasts = await self._source.fetch()
            except Exception as e:
                self.set_error(f"Уведомления не читаются: {error_text(e)}")
                await asyncio.sleep(ACCESS_RETRY_S)
                continue
            if self.error:
                self.set_error("")
            await self.process(toasts)
            await asyncio.sleep(POLL_S)

    # --- что и как читать ---------------------------------------------------------------------------
    def _allowed(self, toast: Toast) -> bool:
        app = toast.app.casefold()
        if "jarvis" in app or noise(toast):
            return False
        config = self.config
        allow = _apps(str(config.get("apps_allow") or ""))
        if allow and not any(a in app for a in allow):
            return False
        return not any(b in app for b in _apps(str(config.get("apps_block") or "")))

    def phrase(self, toast: Toast) -> str:
        config = self.config
        app = " ".join(toast.app.split())
        title = " ".join(toast.title.split())
        body = " ".join(toast.body.split()) if config.get("read_body") else ""
        if title.casefold() == app.casefold():
            title = ""
        core = ". ".join(part.rstrip(".") for part in (title, body) if part)
        text = f"{app}: {core}" if app and core else (app or core)
        return _shorten(text, int(config.get("max_chars") or 200))

    async def process(self, toasts: list[Toast]) -> list[str]:
        """Новые уведомления → фразы для озвучки (первый проход после запуска только запоминает старые)."""
        fresh = [t for t in toasts if t.id not in self._seen]
        for toast in fresh:
            self._seen[toast.id] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        if not self._primed:
            self._primed = True
            return []
        fresh.sort(key=lambda t: t.created)
        readable = [(t, text) for t in fresh if self._allowed(t) and (text := self.phrase(t))]
        phrases = [text for _, text in readable]
        if not phrases or self._muted or self.ctx.settings.value.voice.silent_mode:
            return []
        if self.config.get("quiet_fullscreen") and await self._fullscreen():
            return []
        spoken = phrases[:MAX_PER_ROUND]
        extra = len(phrases) - len(spoken)
        if extra:
            spoken.append(f"И ещё {extra} {ru.plural(extra, 'уведомление', 'уведомления', 'уведомлений')}.")
        for text in spoken:
            try:
                await self.ctx.tts.say(text)
            except Exception as e:
                log.info("уведомления: не озвучить (%s)", error_text(e))
                break
        self._count += len(phrases)
        last = readable[-1][0]
        self._last = {"app": last.app, "title": last.title, "at": time.time()}
        self.publish("read", {"items": spoken})
        self.changed()
        return spoken

    async def _fullscreen(self) -> bool:
        try:
            return bool(await self.ctx.run_platform(self.ctx.platform.is_fullscreen_app))
        except Exception:
            return False

    # --- действия и команды ---------------------------------------------------------------------------
    async def act_test(self, args: dict[str, Any]) -> dict[str, Any]:
        sample = Toast("test", "Telegram", "Аня", "Привет! Как дела?")
        text = self.phrase(sample)
        await self.ctx.tts.say(f"Вот так я читаю уведомления. {text}")
        return {"text": text}

    async def act_request_access(self, args: dict[str, Any]) -> dict[str, Any]:
        access = await self.check_access(request=True)
        return {"access": access, "hint": "" if access == "allowed" else ACCESS_HINT}

    async def act_mute(self, args: dict[str, Any]) -> dict[str, Any]:
        self._muted = True
        self.changed()
        return {"muted": True}

    async def act_unmute(self, args: dict[str, Any]) -> dict[str, Any]:
        self._muted = False
        self.changed()
        return {"muted": False}

    def commands(self) -> list[DynamicCommand]:
        return [
            self.command(
                "mute",
                "Не читать уведомления",
                ["не читай уведомления", "выключи озвучку уведомлений", "не озвучивай уведомления"],
                self._cmd_mute,
            ),
            self.command(
                "unmute",
                "Читать уведомления",
                ["читай уведомления", "включи озвучку уведомлений", "озвучивай уведомления"],
                self._cmd_unmute,
            ),
        ]

    async def _cmd_mute(self, slots: dict[str, Any]) -> ExecResult:
        await self.act_mute({})
        return ExecResult(True, "Озвучка уведомлений", "Не читаю уведомления", icon=self.icon)

    async def _cmd_unmute(self, slots: dict[str, Any]) -> ExecResult:
        await self.act_unmute({})
        return ExecResult(True, "Озвучка уведомлений", "Снова читаю уведомления", icon=self.icon)
