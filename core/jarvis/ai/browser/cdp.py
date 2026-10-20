"""Клиент Chrome DevTools Protocol (CDP): одно WebSocket-подключение к браузеру и сессии вкладок (flatten).

Команда — send(method, params, session=…) → result; ошибка браузера — CdpError, нет ответа вовремя —
TimeoutError, связь оборвалась — CdpClosed. События — on(handler): handler(method, params, session_id),
синхронно, в цикле asyncio. Ответ, которого уже никто не ждёт (команду оборвали «Стопом» или тайм-аутом,
нажатие ждёт, пока закроют окно сайта), отбрасывается. Подключение — только к 127.0.0.1 и без прокси:
системный прокси VPN не должен перехватывать адрес отладки.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
MAX_MESSAGE = 256 * 2**20  # дерево доступности большой страницы — десятки мегабайт
BIG_MESSAGE = 2**20  # сообщение больше — разбирается в потоке, не в цикле событий

Handler = Callable[[str, dict[str, Any], str], None]


class CdpError(RuntimeError):
    """Браузер ответил ошибкой на команду."""

    def __init__(self, method: str, message: str, code: int = 0) -> None:
        super().__init__(f"{method}: {message}")
        self.method = method
        self.message = message
        self.code = code


class CdpClosed(CdpError):
    """Связь с браузером оборвалась (его закрыли)."""


class CdpClient:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: list[Handler] = []
        self.closed = False
        self.on_close: Callable[[], None] | None = None
        self._reader = asyncio.ensure_future(self._read())

    @classmethod
    async def connect(cls, url: str, timeout: float = 5.0) -> "CdpClient":
        from websockets.asyncio.client import connect

        ws = await asyncio.wait_for(
            connect(
                url,
                proxy=None,
                max_size=MAX_MESSAGE,
                ping_interval=None,  # браузер не обязан отвечать на ping — связь рвётся по закрытию сокета
                open_timeout=timeout,
                close_timeout=1.0,
            ),
            timeout + 1.0,
        )
        return cls(ws)

    def on(self, handler: Handler) -> Callable[[], None]:
        self._handlers.append(handler)

        def off() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return off

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        if self.closed:
            raise CdpClosed(method, "браузер закрыт")
        self._next += 1
        message_id = self._next
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        message: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            await self._ws.send(json.dumps(message, ensure_ascii=False))
            reply = await asyncio.wait_for(future, timeout)
        except CdpError:
            raise
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as e:  # сокет закрылся посреди отправки
            raise CdpClosed(method, str(e) or "связь оборвалась") from e
        finally:
            self._pending.pop(message_id, None)
        error = reply.get("error")
        if error:
            raise CdpError(method, str(error.get("message") or error), int(error.get("code") or 0))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    # ответ на снимок страницы — до десятков мегабайт: разбирать не в цикле событий
                    message = (
                        await asyncio.to_thread(json.loads, raw)
                        if len(raw) > BIG_MESSAGE
                        else json.loads(raw)
                    )
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if message_id is not None:
                    future = self._pending.pop(message_id, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                method = message.get("method")
                if not method:
                    continue
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                for handler in list(self._handlers):
                    try:
                        handler(str(method), params, str(message.get("sessionId") or ""))
                    except Exception:
                        log.exception("обработчик события CDP %s упал", method)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("связь с браузером оборвалась: %s", e)
        finally:
            self._finish()

    def _finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CdpClosed("CDP", "браузер закрыт"))
                future.exception()  # никто не ждёт — без «exception was never retrieved»
        self._pending.clear()
        callback, self.on_close = self.on_close, None
        if callback is not None:
            with contextlib.suppress(Exception):
                callback()

    async def close(self) -> None:
        if self.closed:
            return
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._reader.cancel()
        with contextlib.suppress(BaseException):
            await self._reader
        self._finish()
