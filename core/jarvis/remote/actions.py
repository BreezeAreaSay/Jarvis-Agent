"""Действия пульта — общие для страницы в браузере и Telegram-бота: медиа, громкость, питание, скриншот,
избранные команды и фразы для Jarvis.
"""

import base64
import binascii
import hashlib
import io
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis import __version__
from jarvis.platform.base import MediaInfo
from jarvis.remote.net import pc_name
from jarvis.services import Source, UserFacingError
from jarvis.widgets.media import media_action

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

MEDIA_TITLES = {
    "play_pause": "Пауза",
    "next": "Следующий трек",
    "prev": "Предыдущий трек",
    "stop": "Стоп",
    "volume_up": "Громче",
    "volume_down": "Тише",
    "mute": "Звук",
}
POWER_TITLES = {
    "lock": "Компьютер заблокирован",
    "sleep": "Компьютер уходит в сон",
    "shutdown": "Компьютер выключается",
    "restart": "Компьютер перезагружается",
    "cancel": "Выключение отменено",
}
SCREENSHOT_MAX_SIDE = 1920

# фразы на главном экране пульта, кроме команд, выбранных в настройках (settings.remote.favorites)
DEFAULT_FAVORITES: tuple[dict[str, str], ...] = (
    {"id": "say:time", "title": "Который час", "icon": "timer", "text": "который час"},
    {"id": "say:weather", "title": "Погода", "icon": "weather", "text": "какая погода"},
    {"id": "say:music", "title": "Включи музыку", "icon": "media", "text": "включи музыку"},
    {"id": "say:minimize", "title": "Сверни все окна", "icon": "window", "text": "сверни все окна"},
    {"id": "say:youtube", "title": "Открой YouTube", "icon": "youtube", "text": "открой ютуб"},
)


@dataclass
class Outcome:
    """Итог нажатия кнопки пульта: «Выполнено: Пауза»."""

    ok: bool
    title: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "title": self.title, "message": self.message, **self.data}


def favorite_key(favorite_id: str) -> str:
    """Короткий стабильный ключ избранного для callback_data Telegram (там не больше 64 байт)."""
    return hashlib.sha1(favorite_id.encode("utf-8")).hexdigest()[:10]


def decode_data_url(url: str) -> tuple[bytes, str] | None:
    """data:image/png;base64,… → (байты, тип) или None."""
    if not url.startswith("data:") or "," not in url:
        return None
    header, payload = url[5:].split(",", 1)
    mime = header.split(";", 1)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        return None
    try:
        data = base64.b64decode(payload) if ";base64" in header else payload.encode("utf-8")
    except (binascii.Error, ValueError):
        return None
    return data, mime


def shrink_image(png: bytes, max_side: int = SCREENSHOT_MAX_SIDE, fmt: str = "png") -> tuple[bytes, str]:
    """Уменьшить снимок экрана до max_side по длинной стороне; fmt="jpeg" — сжать для Telegram."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            image.load()
            resized = max(image.size) > max_side
            if not resized and fmt == "png":
                return png, "image/png"
            frame = image.copy()
        if resized:
            frame.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        if fmt == "jpeg":
            frame.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
            return buf.getvalue(), "image/jpeg"
        frame.save(buf, "PNG")
        return buf.getvalue(), "image/png"
    except Exception as e:
        log.warning("снимок экрана не обработан, отдаю как есть: %s", e)
        return png, "image/png"


class RemoteActions:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    # --- состояние --------------------------------------------------------------------------------
    async def state(self) -> dict[str, Any]:
        platform = self.ctx.platform

        def probe() -> tuple[MediaInfo | None, int, bool]:
            return platform.now_playing(), platform.volume_get(), platform.is_muted()

        info, volume, muted = await self.ctx.run_platform(probe)
        media = (info or MediaInfo(status="stopped")).to_json()
        thumbnail = media.pop("thumbnail", "") or ""
        media["thumb"] = hashlib.sha1(thumbnail.encode("utf-8")).hexdigest()[:12] if thumbnail else ""
        settings = self.ctx.settings.value
        voice = getattr(self.ctx, "voice", None)
        ai = getattr(self.ctx, "ai", None)
        return {
            "pc_name": pc_name(),
            "assistant": settings.profile.assistant_name,
            "version": __version__,
            "volume": volume,
            "muted": muted,
            "media": media,
            "voice": {"state": getattr(voice, "state", "off")},
            "ai": {"configured": bool(getattr(ai, "configured", False))},
        }

    async def thumbnail(self) -> tuple[bytes, str] | None:
        info = await self.ctx.run_platform(self.ctx.platform.now_playing)
        if info is None or not info.thumbnail:
            return None
        return decode_data_url(info.thumbnail)

    # --- медиа и громкость ------------------------------------------------------------------------
    async def media(self, action: str) -> Outcome:
        if action not in MEDIA_TITLES:
            raise UserFacingError(f"Неизвестная кнопка плеера: {action}")
        title = MEDIA_TITLES[action]
        if action == "play_pause":
            info = await self.ctx.run_platform(self.ctx.platform.now_playing)
            title = "Пауза" if info is not None and info.status == "playing" else "Воспроизведение"
        result = await media_action(self.ctx, action)
        message = ""
        if "volume" in result:
            message = "Звук выключен" if result["muted"] else f"Громкость {result['volume']}%"
            if action == "mute":
                title = "Звук выключен" if result["muted"] else "Звук включён"
                message = "" if result["muted"] else f"Громкость {result['volume']}%"
        data = {k: result[k] for k in ("volume", "muted") if k in result}
        return Outcome(True, title, message, data)

    async def volume(self, level: Any = None, delta: Any = None) -> Outcome:
        if level is not None:
            result = await media_action(self.ctx, "volume", level=level)
        elif delta is not None:
            try:
                step = int(float(delta))
            except (TypeError, ValueError) as e:
                raise UserFacingError("Шаг громкости — целое число") from e
            if step == 0:
                return Outcome(True, "Громкость", data=await self._volume_state())
            result = await media_action(self.ctx, "volume_up" if step > 0 else "volume_down", step=abs(step))
        else:
            raise UserFacingError("Укажите level или delta")
        message = "Звук выключен" if result["muted"] else f"{result['volume']}%"
        return Outcome(True, "Громкость", message, {"volume": result["volume"], "muted": result["muted"]})

    async def _volume_state(self) -> dict[str, Any]:
        platform = self.ctx.platform
        volume, muted = await self.ctx.run_platform(lambda: (platform.volume_get(), platform.is_muted()))
        return {"volume": volume, "muted": muted}

    # --- система ----------------------------------------------------------------------------------
    async def power(self, action: str) -> Outcome:
        if action not in POWER_TITLES:
            raise UserFacingError(f"Неизвестное действие: {action}")
        await self.ctx.run_platform(self.ctx.platform.power, action, 0)  # type: ignore[arg-type]
        log.info("пульт: питание — %s", action)
        return Outcome(True, POWER_TITLES[action])

    async def minimize_all(self) -> Outcome:
        await self.ctx.run_platform(self.ctx.platform.window_action, "minimize_all")
        return Outcome(True, "Окна свёрнуты")

    async def screenshot(self, fmt: str = "png") -> tuple[bytes, str]:
        png = await self.ctx.run_platform(self.ctx.platform.screenshot)
        if not png:
            raise UserFacingError("Скриншот не получился")
        return await self.ctx.run_io(shrink_image, png, SCREENSHOT_MAX_SIDE, fmt)

    async def system_line(self) -> str:
        """«Процессор 28% · Видеокарта 68% · Память 46%» для меню «Система»."""
        stats = await self.ctx.run_platform(self.ctx.platform.system_stats)
        parts = [f"Процессор {round(stats.cpu)}%"]
        if stats.gpu is not None:
            parts.append(f"Видеокарта {round(stats.gpu)}%")
        parts.append(f"Память {round(stats.ram)}%")
        if stats.battery is not None:
            parts.append(f"Батарея {round(stats.battery)}%" + (" ⚡" if stats.charging else ""))
        return " · ".join(parts)

    # --- команды ----------------------------------------------------------------------------------
    def _described(self) -> dict[str, dict[str, Any]]:
        try:
            items = self.ctx.commands.describe(2000)
        except Exception:
            log.exception("список команд не получен")
            return {}
        return {str(item.get("id")): item for item in items if isinstance(item, dict) and item.get("id")}

    def favorites(self) -> list[dict[str, Any]]:
        """Избранные команды из настроек, затем фразы по умолчанию."""
        items: list[dict[str, Any]] = []
        chosen = list(self.ctx.settings.value.remote.favorites)
        described = self._described() if chosen else {}
        for command_id in chosen:
            info = described.get(command_id, {})
            title = info.get("title") or info.get("name") or command_id
            items.append(
                {
                    "id": command_id,
                    "title": str(title),
                    "icon": str(info.get("icon") or ""),
                    "kind": "command",
                }
            )
        for favorite in DEFAULT_FAVORITES:
            if favorite["id"] not in chosen:
                items.append({**favorite, "kind": "phrase"})
        for item in items:
            item["key"] = favorite_key(item["id"])
        return items

    def find_favorite(self, id_or_key: str) -> dict[str, Any] | None:
        return next((f for f in self.favorites() if id_or_key in (f["id"], f["key"])), None)

    async def run_favorite(self, id_or_key: str, source: Source = "remote") -> Outcome:
        favorite = self.find_favorite(id_or_key)
        if favorite is None:
            raise UserFacingError("Такой команды на пульте нет")
        if favorite["kind"] == "phrase":
            result = await self.dispatch(favorite["text"], source)
            return Outcome(
                bool(result.get("ok")), favorite["title"], _result_text(result), {"result": result}
            )
        exec_result = await self.ctx.commands.run_command(favorite["id"], source=source)
        return Outcome(exec_result.ok, exec_result.title or favorite["title"], exec_result.message)

    async def dispatch(self, text: str, source: Source = "remote") -> dict[str, Any]:
        """Фраза для Jarvis → краткий итог {kind, ok, title, message, text}."""
        result = await self.ctx.dispatcher.handle(text, source)
        return summarize(result)


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    kind = result.get("kind", "")
    if kind == "command":
        res = result.get("result") or {}
        return {
            "kind": kind,
            "ok": bool(res.get("ok")),
            "title": str(res.get("title") or ""),
            "message": str(res.get("message") or ""),
            "text": "",
        }
    if kind == "ai":
        error = str(result.get("error") or "")
        return {
            "kind": kind,
            "ok": not error,
            "title": "Jarvis AI",
            "message": error,
            "text": result.get("text") or "",
        }
    if kind == "stop":
        return {"kind": kind, "ok": True, "title": "Остановлено", "message": "", "text": ""}
    if kind == "unknown":
        return {
            "kind": kind,
            "ok": False,
            "title": "Не понял команду",
            "message": "Попробуйте сказать иначе",
            "text": "",
        }
    return {"kind": kind or "empty", "ok": False, "title": "Пустая команда", "message": "", "text": ""}


def _result_text(result: dict[str, Any]) -> str:
    if result.get("kind") == "ai":
        return str(result.get("text") or result.get("message") or "")
    return str(result.get("message") or "")
