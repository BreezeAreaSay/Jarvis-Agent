"""Сборка ядра: AppContext со всеми сервисами, запуск и остановка в правильном порядке."""

import logging
from typing import Any

from jarvis import crashguard, netproxy, paths
from jarvis.bus import EventBus
from jarvis.confirm import Confirmations
from jarvis.context import AppContext
from jarvis.db import Database
from jarvis.platform import create_platform
from jarvis.settings import Secrets, SettingsStore

log = logging.getLogger(__name__)


def build_context(platform_name: str = "auto") -> AppContext:
    netproxy.install()  # системный прокси от VPN (socks= в реестре) не ломает клиенты httpx
    bus = EventBus()
    data = paths.data_dir()
    settings = SettingsStore(data / "settings.json", bus)
    secrets = Secrets(data / "secrets.dat", bus)
    db = Database(data / "jarvis.db")
    platform = create_platform(platform_name)
    ctx = AppContext(settings, secrets, bus, db, platform)
    ctx.confirmations = Confirmations(ctx)

    from jarvis.ai.agent import Agent
    from jarvis.ai.browser.service import Browser
    from jarvis.ai.images import Images
    from jarvis.ai.service import Ai
    from jarvis.background.service import Background
    from jarvis.commands.service import Commands
    from jarvis.desks.service import Desks
    from jarvis.dispatcher import MainDispatcher
    from jarvis.mcp.service import Mcp
    from jarvis.notes.service import Notes
    from jarvis.plugins.manager import Plugins
    from jarvis.remote.service import Remote
    from jarvis.retention import Retention
    from jarvis.scenarios.service import Scenarios
    from jarvis.voice.character import Character
    from jarvis.voice.presence import Presence
    from jarvis.voice.service import Voice
    from jarvis.voice.tts import Tts
    from jarvis.widgets.service import Widgets
    from jarvis.widgets.sysops import SysOps

    ctx.notes = Notes(ctx)
    ctx.commands = Commands(ctx)
    ctx.desks = Desks(ctx)
    ctx.widgets = Widgets(ctx)
    ctx.sysops = SysOps(ctx)  # «Системные операции»: микшер, температура, фраза «открой системные операции»
    ctx.tts = Tts(ctx)
    ctx.voice = Voice(ctx)
    ctx.ai = Ai(ctx)
    ctx.images = Images(ctx)  # картинки Jarvis AI: «нарисуй…» в чате (OpenAI gpt-image-1)
    ctx.mcp = Mcp(ctx)
    ctx.browser = Browser(ctx)  # браузер Jarvis для агента (до агента: агент берёт его из ctx)
    ctx.agent = Agent(ctx)
    ctx.background = Background(ctx)  # фоновые задачи: агент работает в фоне, итог — в заметки (Obsidian)
    ctx.plugins = Plugins(ctx)
    ctx.remote = Remote(ctx)
    ctx.scenarios = Scenarios(ctx)  # после команд: регистрирует в них свои фразы
    ctx.dispatcher = MainDispatcher(ctx)
    # после диспетчера: его частые ответы готовятся первыми, ответы характера — следом
    ctx.character = Character(ctx)
    ctx.presence = Presence(ctx)
    ctx.retention = Retention(ctx)
    return ctx


# порядок запуска: хранилища → команды → голос → ИИ → плагины → пульт; остановка — в обратном
START_ORDER = (
    "notes",
    "commands",
    "desks",
    "widgets",
    "sysops",
    "tts",
    "ai",
    "images",
    "mcp",
    "browser",
    "agent",
    "background",
    "plugins",
    "voice",
    "remote",
    "scenarios",  # последним: триггеры начинают работать, когда всё остальное готово
    "character",
    "presence",  # приветствие по возвращении (выключено по умолчанию)
    "retention",  # очистка по срокам хранения — через минуту после запуска и раз в сутки
)


async def start_services(ctx: AppContext) -> None:
    for name in START_ORDER:
        service: Any = getattr(ctx, name)
        try:
            await service.start()
        except Exception:
            log.exception("сервис %s не запустился", name)
    ctx.started.set()
    ctx.bus.publish("core.ready", {})


async def stop_services(ctx: AppContext) -> None:
    for name in reversed(START_ORDER):
        service: Any = getattr(ctx, name, None)
        if service is None:
            continue
        try:
            await service.stop()
        except Exception:
            log.exception("сервис %s не остановился", name)
    await ctx.shutdown_pools()
    ctx.db.close()
    crashguard.forget_process()  # штатный выход — оборванные им загрузки моделей не считаются вылетом
