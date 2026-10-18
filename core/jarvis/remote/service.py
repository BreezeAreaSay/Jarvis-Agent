"""Пульт в телефоне: страница в браузере по Wi-Fi и Telegram-бот «PC Control» (управление из любой точки).

Оба пульта включаются в настройках (settings.remote) и перезапускаются сами при смене настроек, порта,
прокси или токена бота. Состояние — info() (GET /api/remote), изменения — событие remote.changed.
Токен страницы — секрет remote_token (создаётся сам); владелец бота привязывается кодом из pair().
"""

import asyncio
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.bus import Event
from jarvis.remote.actions import RemoteActions
from jarvis.remote.lan import LanServer
from jarvis.remote.net import lan_ips, qr_svg
from jarvis.remote.telegram import TelegramBot
from jarvis.service_base import BaseService
from jarvis.settings import Settings

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

TOKEN_SECRET = "remote_token"
TELEGRAM_SECRET = "telegram_token"
PAIRING_TTL_S = 600.0
PAIRING_ATTEMPTS = 5


@dataclass
class Pairing:
    code: str
    expires: float  # unix-время
    attempts: int = 0


class Remote(BaseService):
    def __init__(self, ctx: "AppContext") -> None:
        super().__init__(ctx)
        self.actions = RemoteActions(ctx)
        self.lan = LanServer(self)
        self.telegram = TelegramBot(self)
        self._pairing: Pairing | None = None
        self._unsubscribe: list[Any] = []
        self._apply_lock = asyncio.Lock()
        self._started = False

    # --- жизненный цикл ---------------------------------------------------------------------------
    async def start(self) -> None:
        self.token()  # ссылка и QR-код нужны сразу
        self._unsubscribe = [
            self.ctx.settings.on_change(self._on_settings),
            self.ctx.bus.on("secrets.changed", self._on_secret),
        ]
        self._started = True
        await self.apply()

    async def stop(self) -> None:
        self._started = False
        for off in self._unsubscribe:
            off()
        self._unsubscribe = []
        await self.lan.stop()
        await self.telegram.stop()

    async def apply(self) -> None:
        """Привести оба пульта к текущим настройкам."""
        async with self._apply_lock:
            settings = self.ctx.settings.value
            remote = settings.remote
            await self.lan.apply(remote.lan_enabled, remote.lan_port)
            await self.telegram.apply(
                remote.telegram_enabled, self.ctx.secrets.get(TELEGRAM_SECRET), settings.ai.proxy
            )
        self.publish()

    def _schedule_apply(self) -> None:
        if self._started:
            self.ctx.call_soon(self.apply)

    def _on_settings(self, old: Settings, new: Settings) -> None:
        before, after = old.remote, new.remote
        restart = (before.lan_enabled, before.lan_port, before.telegram_enabled) != (
            after.lan_enabled,
            after.lan_port,
            after.telegram_enabled,
        )
        if restart or (old.ai.proxy != new.ai.proxy and after.telegram_enabled):
            self._schedule_apply()
        elif before != after:
            self.publish()  # привязан владелец, сменилось избранное

    def _on_secret(self, event: Event) -> None:
        name = event.data.get("name")
        if name == TELEGRAM_SECRET:
            self._schedule_apply()
        elif name == TOKEN_SECRET:
            self.publish()  # ссылки и QR-код теперь с новым токеном

    # --- токен страницы ---------------------------------------------------------------------------
    def token(self) -> str:
        value = self.ctx.secrets.get(TOKEN_SECRET)
        if not value:
            value = secrets.token_urlsafe(18)
            self.ctx.secrets.set(TOKEN_SECRET, value)
        return value

    def regenerate_token(self) -> str:
        """Новый токен: старые ссылки и открытые на телефонах страницы перестают работать."""
        value = secrets.token_urlsafe(18)
        self.ctx.secrets.set(TOKEN_SECRET, value)
        return value

    def check_token(self, given: str) -> bool:
        expected = self.ctx.secrets.get(TOKEN_SECRET)
        if not given or not expected:
            return False
        return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))

    # --- привязка Telegram ------------------------------------------------------------------------
    def pair(self) -> dict[str, Any]:
        """Код привязки владельца: 6 цифр, живёт 10 минут, 5 неверных попыток — и он сгорает."""
        code = f"{secrets.randbelow(10**6):06d}"
        self._pairing = Pairing(code, time.time() + PAIRING_TTL_S)
        self.publish()
        return {"pairing_code": code, "expires": self._pairing.expires, **self._bot_links(code)}

    def _active_pairing(self) -> Pairing | None:
        pairing = self._pairing
        if pairing is not None and time.time() > pairing.expires:
            self._pairing = pairing = None
        return pairing

    def consume_pairing(self, code: str) -> bool:
        pairing = self._active_pairing()
        if pairing is None:
            return False
        if hmac.compare_digest(code.strip().encode("utf-8"), pairing.code.encode("utf-8")):
            self._pairing = None
            self.publish()
            return True
        pairing.attempts += 1
        if pairing.attempts >= PAIRING_ATTEMPTS:
            log.warning("telegram: код привязки сгорел после %s неверных попыток", pairing.attempts)
            self._pairing = None
            self.publish()
        return False

    def _bot_links(self, code: str) -> dict[str, str]:
        username = self.telegram.username
        if not username:
            return {"bot": "", "link": ""}
        return {"bot": username, "link": f"https://t.me/{username}?start={code}"}

    # --- состояние --------------------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        settings = self.ctx.settings.value.remote
        token = self.ctx.secrets.get(TOKEN_SECRET)
        port = settings.lan_port
        urls = [f"http://{ip}:{port}/?t={token}" for ip in lan_ips()] if token else []
        pairing = self._active_pairing()
        return {
            "lan": {
                "enabled": settings.lan_enabled,
                "port": port,
                "urls": urls,
                "qr_svg": qr_svg(urls[0]) if urls else "",
                "status": self.lan.status,
                "error": self.lan.error,
            },
            "telegram": {
                "enabled": settings.telegram_enabled,
                "status": self.telegram.status,
                "bot": self.telegram.username,
                "owner_id": settings.telegram_owner_id,
                "pairing_code": pairing.code if pairing else "",
                "pairing_expires": pairing.expires if pairing else None,
                "pairing_link": self._bot_links(pairing.code)["link"] if pairing else "",
                "token_set": bool(self.ctx.secrets.get(TELEGRAM_SECRET)),
                "error": self.telegram.error,
            },
        }

    def publish(self) -> None:
        try:
            data = self.info()
        except Exception:
            log.exception("пульт: состояние не собралось")
            return
        self.ctx.bus.publish("remote.changed", data)
