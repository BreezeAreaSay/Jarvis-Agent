"""Менеджер плагинов: реестр девяти плагинов, включение и настройки, действия, голосовые команды.

Включённость и настройки живут в settings.plugins[id] ({enabled, config}); секреты (токены) — в ctx.secrets.
При запуске ядра стартуют включённые плагины; падение одного плагина не мешает остальным — его ошибка
видна в статусе ({"error": "…"}). Голосовые команды работающих плагинов отдаются сервису команд через
провайдер "plugins"; изменения — событие plugins.changed {items} (контракт — docs/CONTRACTS.md §5).
"""

# метод list() (протокол PluginManager) в теле класса заслоняет встроенный list в аннотациях — откладываем их
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import httpx

from jarvis.platform.base import PlatformError
from jarvis.plugins.autolock import AutoLock
from jarvis.plugins.base import Plugin, error_text
from jarvis.plugins.dictation import Dictation
from jarvis.plugins.notify_voice import NotifyVoice
from jarvis.plugins.pet import Pet
from jarvis.plugins.reader import Reader
from jarvis.plugins.second_pc import SecondPc
from jarvis.plugins.smart_home import SmartHome
from jarvis.plugins.subtitles import Subtitles
from jarvis.plugins.summary import Summary
from jarvis.service_base import BaseService
from jarvis.services import DynamicCommand, UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

PLUGIN_CLASSES: tuple[type[Plugin], ...] = (
    SmartHome,
    Dictation,
    Subtitles,
    Summary,
    Reader,
    AutoLock,
    SecondPc,
    NotifyVoice,
    Pet,
)

START_TIMEOUT = 15.0
STOP_TIMEOUT = 8.0
CHANGED_DELAY = 0.25  # plugins.changed от фоновых изменений статуса — пачкой, не чаще
COMMANDS_DELAY = 0.5


class UnknownPlugin(UserFacingError):
    """Плагина с таким id нет (API отвечает 404)."""


class Plugins(BaseService):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.http_transport: httpx.AsyncBaseTransport | None = None  # тесты подменяют сеть всем плагинам
        self._plugins: dict[str, Plugin] = {}
        self._running: set[str] = set()
        self._start_errors: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._started = False
        self._changed_timer: asyncio.TimerHandle | None = None
        self._commands_timer: asyncio.TimerHandle | None = None
        for cls in PLUGIN_CLASSES:
            try:
                plugin = cls(ctx, self)
            except Exception:
                log.exception("плагин %s не создаётся — пропускаю", getattr(cls, "id", cls.__name__))
                continue
            self._plugins[plugin.id] = plugin

    # --- реестр ---------------------------------------------------------------------------------------
    def has(self, plugin_id: str) -> bool:
        return plugin_id in self._plugins

    def get(self, plugin_id: str) -> Plugin:
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise UnknownPlugin(f"Плагин «{plugin_id}» не найден")
        return plugin

    def ids(self) -> list[str]:
        return list(self._plugins)

    def is_enabled(self, plugin_id: str) -> bool:
        state = self.ctx.settings.value.plugins.get(plugin_id)
        return bool(state and state.enabled)

    def is_running(self, plugin_id: str) -> bool:
        return plugin_id in self._running

    def stored_config(self, plugin_id: str) -> dict[str, Any]:
        state = self.ctx.settings.value.plugins.get(plugin_id)
        return dict(state.config) if state else {}

    # --- описание для интерфейса ---------------------------------------------------------------------
    def info(self, plugin_id: str) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        status: dict[str, Any]
        error = ""
        try:
            status = dict(plugin.status())
        except Exception as e:
            log.exception("плагин %s: статус не собирается", plugin_id)
            status, error = {}, error_text(e)
        try:
            config = plugin.public_config()
        except Exception:
            log.exception("плагин %s: настройки не читаются", plugin_id)
            config = {}
        status["running"] = plugin_id in self._running
        error = self._start_errors.get(plugin_id) or plugin.error or error
        if error:
            status["error"] = error
        return {
            "id": plugin.id,
            "name": plugin.name,
            "description": plugin.description,
            "icon": plugin.icon,
            "color": list(plugin.color),
            "enabled": self.is_enabled(plugin_id),
            "config": config,
            "schema": plugin.schema(),
            "status": status,
            "actions": [a.to_json() for a in plugin.actions],
            "examples": list(plugin.examples),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.info(plugin_id) for plugin_id in self._plugins]

    # --- жизненный цикл -------------------------------------------------------------------------------
    async def start(self) -> None:
        self.ctx.commands.register_provider("plugins", self._provide_commands)
        self._started = True
        for plugin_id, plugin in self._plugins.items():
            if self.is_enabled(plugin_id):
                async with self._lock(plugin_id):
                    await self._start_plugin(plugin)
        self._refresh_commands()
        self._publish_changed()

    async def stop(self) -> None:
        self._started = False  # новые таймеры plugins.changed и команд больше не взводятся
        for plugin_id in list(self._running):
            await self._stop_plugin(self._plugins[plugin_id])
        for timer in (self._changed_timer, self._commands_timer):
            if timer is not None:
                timer.cancel()
        self._changed_timer = self._commands_timer = None

    async def _start_plugin(self, plugin: Plugin) -> bool:
        self._start_errors.pop(plugin.id, None)
        plugin.error = ""
        try:
            async with asyncio.timeout(START_TIMEOUT):
                await plugin.start()
        except Exception as e:
            message = "Плагин запускался слишком долго" if isinstance(e, TimeoutError) else error_text(e)
            if not isinstance(e, UserFacingError | PlatformError):
                log.exception("плагин %s не запустился", plugin.id)
            else:
                log.warning("плагин %s не запустился: %s", plugin.id, message)
            self._start_errors[plugin.id] = message
            with contextlib.suppress(Exception):
                await plugin.stop()
            await plugin.cancel_tasks()
            return False
        self._running.add(plugin.id)
        log.info("плагин %s запущен", plugin.id)
        return True

    async def _stop_plugin(self, plugin: Plugin) -> None:
        self._running.discard(plugin.id)
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await plugin.stop()
        except Exception:
            log.exception("плагин %s не остановился как следует", plugin.id)
        await plugin.cancel_tasks()
        plugin.error = ""
        self._start_errors.pop(plugin.id, None)
        log.info("плагин %s остановлен", plugin.id)

    def _lock(self, plugin_id: str) -> asyncio.Lock:
        lock = self._locks.get(plugin_id)
        if lock is None:
            lock = self._locks[plugin_id] = asyncio.Lock()
        return lock

    # --- включение и настройки ------------------------------------------------------------------------
    async def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        return await self.update(plugin_id, enabled=enabled)

    async def update(
        self, plugin_id: str, *, enabled: bool | None = None, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """PUT /api/plugins/{id}: сначала настройки, потом включённость. Вернёт Plugin."""
        plugin = self.get(plugin_id)
        async with self._lock(plugin_id):
            if config is not None:
                old, new, secrets_changed = self._save_config(plugin, config)
                if plugin_id in self._running and (new != old or secrets_changed):
                    try:
                        await plugin.on_config(new, old)
                    except Exception as e:
                        if not isinstance(e, UserFacingError | PlatformError):
                            log.exception("плагин %s: новые настройки не применились", plugin_id)
                        plugin.set_error(error_text(e))
            if enabled is not None and bool(enabled) != self.is_enabled(plugin_id):
                self.ctx.settings.patch({"plugins": {plugin_id: {"enabled": bool(enabled)}}})
            want = self.is_enabled(plugin_id)
            if self._started:
                if want and plugin_id not in self._running:
                    await self._start_plugin(plugin)  # включили — или снова пробуем после ошибки запуска
                elif not want and plugin_id in self._running:
                    await self._stop_plugin(plugin)
        self._refresh_commands()
        self._publish_changed()
        return self.info(plugin_id)

    def _save_config(
        self, plugin: Plugin, patch: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Проверить и сохранить настройки. Секреты — в ctx.secrets (None — удалить, "" — не менять).

        Вернёт (старые настройки, новые, менялись ли секреты). Неверное значение — UserFacingError, и тогда
        не меняется ничего."""
        if not isinstance(patch, dict):
            raise UserFacingError("Настройки плагина — объект {ключ: значение}")
        old = plugin.config
        new = dict(old)
        secrets: dict[str, str] = {}
        for f in plugin.fields:
            if f.key not in patch:
                continue
            value = patch[f.key]
            if f.kind == "secret":
                if value is None:
                    secrets[f.secret] = ""
                elif isinstance(value, str) and value.strip():
                    secrets[f.secret] = value.strip()
                continue
            try:
                new[f.key] = f.coerce(value)
            except ValueError as e:
                raise UserFacingError(str(e)) from None
        secrets_changed = False
        for name, value in secrets.items():
            if self.ctx.secrets.get(name) != value:
                self.ctx.secrets.set(name, value)
                secrets_changed = True
        new = plugin.prepare_config(new, old)
        if new != old or plugin.id not in self.ctx.settings.value.plugins:
            self.ctx.settings.patch({"plugins": {plugin.id: {"config": new}}})
        return old, plugin.config, secrets_changed

    # --- действия -------------------------------------------------------------------------------------
    async def action(self, plugin_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        if plugin_id not in self._running:
            if not self.is_enabled(plugin_id):
                raise UserFacingError(f"Плагин «{plugin.name}» выключен — включите его на вкладке «Плагины»")
            error = self._start_errors.get(plugin_id)
            raise UserFacingError(f"Плагин «{plugin.name}» не запущен" + (f": {error}" if error else ""))
        try:
            result = await plugin.handle(action, dict(args or {}))
        except (UserFacingError, PlatformError):
            raise
        except Exception as e:
            log.exception("плагин %s: действие %s упало", plugin_id, action)
            raise UserFacingError(f"Плагин «{plugin.name}»: {error_text(e)}") from e
        out: dict[str, Any] = {"ok": True}
        out.update(result or {})
        return out

    # --- голосовые команды ----------------------------------------------------------------------------
    def _provide_commands(self) -> list[DynamicCommand]:
        commands: list[DynamicCommand] = []
        for plugin_id, plugin in self._plugins.items():
            if plugin_id not in self._running:
                continue
            try:
                commands.extend(plugin.commands())
            except Exception:
                log.exception("плагин %s: голосовые команды не собираются", plugin_id)
        return commands

    def _refresh_commands(self) -> None:
        if self._commands_timer is not None:
            self._commands_timer.cancel()
            self._commands_timer = None
        try:
            self.ctx.commands.refresh_dynamic()
        except Exception:
            log.exception("сервис команд не обновил динамические команды")

    def notify_commands_changed(self) -> None:
        """Из плагина (любой поток): набор команд изменился — обновить с небольшой задержкой."""
        self._call_in_loop(self._arm_commands_timer, self._refresh_commands)

    def _arm_commands_timer(self) -> None:
        if self._commands_timer is None and self._started:
            loop = asyncio.get_running_loop()
            self._commands_timer = loop.call_later(COMMANDS_DELAY, self._refresh_commands)

    # --- plugins.changed ------------------------------------------------------------------------------
    def _publish_changed(self) -> None:
        if self._changed_timer is not None:
            self._changed_timer.cancel()
            self._changed_timer = None
        try:
            items = self.list()
        except Exception:
            log.exception("список плагинов не собирается")
            return
        self.ctx.bus.publish("plugins.changed", {"items": items})

    def notify_changed(self) -> None:
        """Из плагина (любой поток): статус изменился — разослать plugins.changed пачкой."""
        self._call_in_loop(self._arm_changed_timer, self._publish_changed)

    def _arm_changed_timer(self) -> None:
        if self._changed_timer is None and self._started:
            loop = asyncio.get_running_loop()
            self._changed_timer = loop.call_later(CHANGED_DELAY, self._publish_changed)

    def _call_in_loop(self, arm: Any, fallback: Any) -> None:
        loop = self.ctx.loop or self.ctx.bus.loop
        if loop is None or loop.is_closed():
            fallback()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            arm()
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(arm)
