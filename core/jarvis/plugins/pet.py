"""«Питомец»: пиксельная медуза (котик или призрак) гуляет по экрану, сидит на окнах, танцует под музыку и
ест файлы из Корзины.

Рисует питомца интерфейс, а ядро даёт ему «мир»: раз в ~2 с, когда что-то изменилось, plugin.pet.state
{playing, recycle_count, windows: [{id, rect, foreground}], fullscreen, skin, name, size, eats_trash}
(rect — [left, top, right, bottom] в физических пикселях; свёрнутые и развёрнутые окна не годятся для
посиделок). Дела питомца: eat {path} — брошенный на него файл уходит в Корзину; feed — очистить Корзину
(после подтверждения); pet — погладить; say {text} — облачко с репликой. Реакции — plugin.pet.action {action}.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from jarvis.platform.base import WindowInfo
from jarvis.plugins import ru
from jarvis.plugins.base import ConfigField, Option, Plugin, PluginAction, error_text, spoken_name, unique
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

TICK_S = 2.0
RECYCLE_EVERY_S = 10.0  # Корзину считать реже: на больших корзинах это небыстро, а поток платформы общий
MAX_WINDOWS = 24
MIN_WIDTH = 120
MIN_HEIGHT = 80
OWN_EXES = ("jarvis", "electron")  # окна самого Jarvis (в том числе оверлей и питомец) не в счёт
_CONSONANTS = "бвгджзклмнпрстфхцчшщ"


def perchable(window: WindowInfo) -> bool:
    """Годится ли окно, чтобы на нём посидеть: видимое, обычного размера, не наше."""
    left, top, right, bottom = window.rect
    if window.minimized or window.maximized or not window.title.strip():
        return False
    if right - left < MIN_WIDTH or bottom - top < MIN_HEIGHT:
        return False
    exe = window.exe.casefold()
    return not any(own in exe for own in OWN_EXES)


def name_forms(name: str) -> list[str]:
    """Как обратиться к питомцу во фразе: «медузка» → [«медузку», «медузка»], «барсик» → [… «барсика»]."""
    base = spoken_name(name)
    if not base:
        return []
    forms = [ru.accusative(base), base]
    last = base[-1]
    if " " not in base and last in _CONSONANTS:
        forms.append(base + "а")  # одушевлённый: «покорми барсика»
    elif " " not in base and last in "йь":
        forms.append(base[:-1] + "я")
    return unique(forms)


class Pet(Plugin):
    id = "pet"
    name = "Питомец"
    description = (
        "Пиксельная медуза гуляет по экрану, сидит на окнах, танцует под музыку и ест файлы из Корзины."
    )
    icon = "pet"
    color = ("#C084FC", "#818CF8")
    fields = (
        ConfigField(
            "skin",
            "select",
            "Кто",
            default="jelly",
            options=(Option("jelly", "Медуза"), Option("cat", "Котик"), Option("ghost", "Призрак")),
        ),
        ConfigField("name", "text", "Имя", default="Медузка"),
        ConfigField("size", "number", "Размер, пикселей", default=64, min=32, max=256, step=8, integer=True),
        ConfigField("on_windows", "bool", "Сидит на окнах", default=True),
        ConfigField("dance_to_music", "bool", "Танцует под музыку", default=True),
        ConfigField("eats_trash", "bool", "Ест файлы из Корзины", default=True),
    )
    actions = (PluginAction("feed", "Покормить (очистить Корзину)"), PluginAction("pet", "Погладить"))
    examples = ("покорми медузку", "погладь питомца")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        self._world: dict[str, Any] = {}
        self._recycle = 0
        self._recycle_at = -RECYCLE_EVERY_S

    async def start(self) -> None:
        self._world = {}
        self.spawn(self._loop(), "world")

    async def stop(self) -> None:
        self._world = {}

    async def on_config(self, config: dict[str, Any], old: dict[str, Any]) -> None:
        await self.refresh(force=True)
        if config.get("name") != old.get("name"):
            self.commands_changed()

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:
                log.warning("питомец: мир не обновился (%s)", error_text(e))
            await asyncio.sleep(TICK_S)

    # --- мир питомца ----------------------------------------------------------------------------------
    def _collect(self, config: dict[str, Any], recycle_too: bool) -> dict[str, Any]:
        """Выполняется в потоке платформы: музыка, Корзина (если recycle_too), окна, полноэкранный режим."""
        platform = self.ctx.platform
        playing = False
        if config.get("dance_to_music"):
            try:
                media = platform.now_playing()
                playing = bool(media is not None and media.status == "playing")
            except Exception:
                playing = False
        recycle = self._recycle
        if recycle_too:
            try:
                recycle = int(platform.recycle_bin_count())
            except Exception:
                recycle = 0
        try:
            fullscreen = bool(platform.is_fullscreen_app())
        except Exception:
            fullscreen = False
        windows: list[dict[str, Any]] = []
        if config.get("on_windows") and not fullscreen:
            try:
                for window in platform.list_windows():
                    if perchable(window):
                        windows.append(
                            {"id": window.hwnd, "rect": list(window.rect), "foreground": window.foreground}
                        )
                    if len(windows) >= MAX_WINDOWS:
                        break
            except Exception:
                windows = []
        return {"playing": playing, "recycle_count": recycle, "windows": windows, "fullscreen": fullscreen}

    async def refresh(self, force: bool = False) -> dict[str, Any]:
        config = self.config
        recycle_too = force or time.monotonic() - self._recycle_at >= RECYCLE_EVERY_S
        world = await self.ctx.run_platform(self._collect, config, recycle_too)
        if recycle_too:
            self._recycle = world["recycle_count"]
            self._recycle_at = time.monotonic()
        world.update(
            {
                "skin": config.get("skin"),
                "name": config.get("name"),
                "size": config.get("size"),
                "eats_trash": config.get("eats_trash"),
            }
        )
        if force or world != self._world:
            self._world = world
            self.publish("state", world)
        return world

    def status(self) -> dict[str, Any]:
        return dict(self._world)

    def _pet_name(self) -> str:
        return str(self.config.get("name") or "Питомец")

    # --- дела -----------------------------------------------------------------------------------------
    async def act_state(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"state": await self.refresh(force=bool(args.get("publish")))}

    async def act_eat(self, args: dict[str, Any]) -> dict[str, Any]:
        """Съесть брошенный на питомца файл: он уходит в Корзину (его можно восстановить)."""
        raw = str(args.get("path") or "").strip()
        if not raw:
            raise UserFacingError("Какой файл съесть? Перетащите его на питомца")
        if not self.config.get("eats_trash"):
            raise UserFacingError(f"{self._pet_name()} сейчас не ест файлы — это включается в настройках")
        path = Path(raw)
        exists = await self.ctx.run_io(path.exists)
        if not exists:
            raise UserFacingError("Файл не найден")
        resolved = await self.ctx.run_io(path.resolve)
        if resolved.parent == resolved or resolved == Path.home():
            raise UserFacingError("Это слишком большой кусок — такое питомец не ест")
        await self.ctx.run_platform(self.ctx.platform.trash, str(resolved))
        name = resolved.name or raw
        self.publish("action", {"action": "eat", "name": name})
        world = await self.refresh(force=True)
        return {"name": name, "recycle_count": world.get("recycle_count", 0)}

    async def act_feed(self, args: dict[str, Any]) -> dict[str, Any]:
        """Покормить: очистить Корзину, если человек подтвердит."""
        if not self.config.get("eats_trash"):
            raise UserFacingError(f"{self._pet_name()} сейчас не ест файлы — это включается в настройках")
        count = int(await self.ctx.run_platform(self.ctx.platform.recycle_bin_count))
        if count <= 0:
            self.publish("action", {"action": "sad"})
            return {"ok": False, "count": 0, "message": "Корзина пуста — кормить нечем"}
        source = str(args.get("_source") or "ui")
        allowed = await self.ctx.confirmations.request(
            "Очистить корзину?", f"Файлов: {count}", kind="plugin", source=source
        )
        if not allowed:
            self.publish("action", {"action": "refuse"})
            return {"ok": False, "cancelled": True, "count": count}
        await self.ctx.run_platform(self.ctx.platform.empty_recycle_bin)
        self.publish("action", {"action": "munch", "count": count})
        await self.refresh(force=True)
        return {"ok": True, "count": count}

    async def act_pet(self, args: dict[str, Any]) -> dict[str, Any]:
        self.publish("action", {"action": "pet"})
        return {}

    async def act_say(self, args: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(args.get("text") or "").split())[:200]
        if not text:
            raise UserFacingError("Что сказать?")
        self.publish("action", {"action": "say", "text": text})
        if args.get("voice") and not self.ctx.settings.value.voice.silent_mode:
            await self.ctx.tts.say(text)
        return {"text": text}

    # --- голосовые команды ----------------------------------------------------------------------------
    def commands(self) -> list[DynamicCommand]:
        forms = name_forms(self._pet_name())
        feed = ["покорми питомца", "накорми питомца"] + [
            f"{v} {f}" for v in ("покорми", "накорми") for f in forms
        ]
        pet = ["погладь питомца"] + [f"погладь {f}" for f in forms]
        return [
            self.command("feed", "Покормить питомца", feed, self._cmd_feed),
            self.command("pet", "Погладить питомца", pet, self._cmd_pet),
        ]

    async def _cmd_feed(self, slots: dict[str, Any]) -> ExecResult:
        name = self._pet_name()
        result = await self.act_feed({"_source": slots.get("_source") or "ui"})
        if result.get("ok"):
            count = result["count"]
            files = ru.plural(count, "файл", "файла", "файлов")
            return ExecResult(
                True, "Покормить питомца", f"{name}: съедено {count} {files} из Корзины", icon=self.icon
            )
        if result.get("cancelled"):
            return ExecResult(False, "Очистить корзину", "Отменено", icon=self.icon, data={"cancelled": True})
        return ExecResult(True, name, str(result.get("message") or "Кормить нечем"), icon=self.icon)

    async def _cmd_pet(self, slots: dict[str, Any]) -> ExecResult:
        await self.act_pet({})
        return ExecResult(
            True, "Погладить питомца", f"{self._pet_name()} мурлычет", icon=self.icon, say="Мур."
        )
