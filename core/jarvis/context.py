"""AppContext — всё, что нужно сервисам ядра: настройки, секреты, шина, база, платформа и сами сервисы.

Сервисы создаёт jarvis.app.build_context(); каждый сервис получает ctx и обращается к соседям через него
(ctx.commands, ctx.tts, ctx.ai…). Интерфейсы сервисов — в jarvis.services, описание — docs/CONTRACTS.md.
"""

import asyncio
import contextlib
import logging
import sys
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

from jarvis.bus import EventBus
from jarvis.db import Database
from jarvis.platform import Platform
from jarvis.settings import Secrets, SettingsStore

if TYPE_CHECKING:
    from jarvis.confirm import Confirmations
    from jarvis.scenarios.service import Scenarios
    from jarvis.services import (
        AgentService,
        AiService,
        CommandService,
        DeskService,
        Dispatcher,
        McpService,
        NotesService,
        PluginManager,
        RemoteService,
        TtsService,
        VoiceService,
        WidgetService,
    )

log = logging.getLogger(__name__)
T = TypeVar("T")


def _com_init() -> None:
    """Поток платформы: COM в режиме STA (pycaw, UI Automation, Shell)."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import pythoncom  # type: ignore[import-not-found]

            pythoncom.CoInitialize()


class AppContext:
    def __init__(
        self,
        settings: SettingsStore,
        secrets: Secrets,
        bus: EventBus,
        db: Database,
        platform: Platform,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self.bus = bus
        self.db = db
        self.platform = platform
        self.loop: asyncio.AbstractEventLoop | None = None
        # один поток для COM-операций платформы (порядок действий сохраняется) и пул для прочего
        self._platform_pool = ThreadPoolExecutor(1, thread_name_prefix="platform", initializer=_com_init)
        self._io_pool = ThreadPoolExecutor(4, thread_name_prefix="io", initializer=_com_init)
        self._tasks: set[asyncio.Task[Any]] = set()
        self.started = threading.Event()
        # адрес и токен HTTP API ядра (заполняет jarvis.server) — для сервера MCP Jarvis (jarvis.mcp.server)
        self.api_url = ""
        self.api_token = ""
        # штатный выход ядра (POST /api/shutdown от Electron) — задаёт jarvis.server
        self.request_exit: Callable[[], None] | None = None
        # сервисы — заполняет jarvis.app.build_context
        self.confirmations: Confirmations
        self.commands: CommandService
        self.voice: VoiceService
        self.tts: TtsService
        self.ai: AiService
        self.agent: AgentService
        self.notes: NotesService
        self.mcp: McpService
        self.plugins: PluginManager
        self.remote: RemoteService
        self.scenarios: Scenarios
        self.widgets: WidgetService
        self.desks: DeskService
        self.dispatcher: Dispatcher

    async def run_platform(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Выполнить вызов платформы в её потоке (COM STA), не блокируя цикл."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._platform_pool, lambda: fn(*args, **kwargs))

    async def run_io(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Блокирующая работа (файлы, тяжёлые вычисления) в пуле потоков."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._io_pool, lambda: fn(*args, **kwargs))

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str = "") -> "asyncio.Task[Any]":
        """Фоновая задача с журналированием ошибок; ссылка хранится, пока задача жива."""
        task = asyncio.ensure_future(coro)
        if name:
            task.set_name(name)
        self._tasks.add(task)

        def done(t: "asyncio.Task[Any]") -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("фоновая задача %s упала", t.get_name(), exc_info=t.exception())

        task.add_done_callback(done)
        return task

    def call_soon(self, coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        """Запустить корутину в цикле ядра из любого потока (голос, хуки клавиатуры)."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: self.spawn(coro_factory()))

    def run_coro_threadsafe(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Из чужого потока выполнить корутину в цикле ядра и дождаться результата."""
        if self.loop is None:
            raise RuntimeError("цикл ядра ещё не запущен")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def shutdown_pools(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._platform_pool.shutdown(wait=False, cancel_futures=True)
        self._io_pool.shutdown(wait=False, cancel_futures=True)
