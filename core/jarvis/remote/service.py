"""Пульт в телефоне: Telegram-бот и страница в браузере по Wi-Fi (заглушка — см. docs/CONTRACTS.md)."""

from typing import Any

from jarvis.service_base import BaseService


class Remote(BaseService):
    def info(self) -> dict[str, Any]:
        return {}
