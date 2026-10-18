"""«Питомец»: мир для интерфейса (окна, музыка, Корзина), «съесть» файл и «покормить» с подтверждением."""

from pathlib import Path

import pytest

from jarvis.bus import Event
from jarvis.context import AppContext
from jarvis.platform.base import WindowInfo
from jarvis.platform.fake import FakePlatform
from jarvis.plugins.pet import Pet, name_forms
from jarvis.services import UserFacingError
from tests.conftest import EventLog
from tests.plugins.conftest import FakeCommands, enable, manager, wait_until


def platform_of(ctx: AppContext) -> FakePlatform:
    platform = ctx.platform
    assert isinstance(platform, FakePlatform)
    return platform


def plugin_of(ctx: AppContext) -> Pet:
    plugin = manager(ctx).get("pet")
    assert isinstance(plugin, Pet)
    return plugin


def answer_confirmations(ctx: AppContext, allow: bool) -> list[dict[str, object]]:
    """Нажать «Разрешить» / «Отклонить» на карточке, как только она появится."""
    asked: list[dict[str, object]] = []

    def on_request(event: Event) -> None:
        asked.append(event.data)
        ctx.confirmations.resolve(str(event.data["id"]), allow)

    ctx.bus.on("confirm.request", on_request)
    return asked


def test_name_forms() -> None:
    assert name_forms("Медузка") == ["медузку", "медузка"]
    assert name_forms("Барсик") == ["барсик", "барсика"]


async def test_world_state_is_published_on_change(pctx: AppContext, pevents: EventLog) -> None:
    platform = platform_of(pctx)
    platform.windows += [
        WindowInfo(201, "Свёрнутое", "notepad.exe", rect=(0, 0, 800, 600), minimized=True),
        WindowInfo(202, "Во весь экран", "code.exe", rect=(0, 0, 1920, 1040), maximized=True),
        WindowInfo(203, "Jarvis", "Jarvis.exe", rect=(10, 10, 500, 400)),
        WindowInfo(204, "Крошечное", "calc.exe", rect=(0, 0, 60, 40)),
    ]
    plugin = plugin_of(pctx)
    world = await plugin.refresh()
    assert world["playing"] is True and world["recycle_count"] == 3 and world["fullscreen"] is False
    assert [w["id"] for w in world["windows"]] == [101, 102, 103]
    assert world["windows"][1] == {"id": 102, "rect": [300, 80, 1600, 980], "foreground": True}
    assert (world["skin"], world["name"], world["size"]) == ("jelly", "Медузка", 64)
    assert pevents.of("plugin.pet.state") == [world]

    await plugin.refresh()
    assert len(pevents.of("plugin.pet.state")) == 1  # ничего не изменилось — молчим
    platform.media.status = "paused"
    assert (await plugin.refresh())["playing"] is False
    assert len(pevents.of("plugin.pet.state")) == 2

    await enable(pctx, "pet", config={"on_windows": False, "skin": "ghost"})
    await wait_until(lambda: pevents.of("plugin.pet.state")[-1]["skin"] == "ghost")
    assert pevents.of("plugin.pet.state")[-1]["windows"] == []
    assert plugin.status()["skin"] == "ghost"


async def test_eat_moves_file_to_recycle_bin(pctx: AppContext, pevents: EventLog, tmp_path: Path) -> None:
    await enable(pctx, "pet")
    doc = tmp_path / "старый отчёт.txt"
    doc.write_text("x", encoding="utf-8")
    result = await manager(pctx).action("pet", "eat", {"path": str(doc)})
    assert result == {"ok": True, "name": "старый отчёт.txt", "recycle_count": 4}
    assert ("trash", (str(doc.resolve()),)) in platform_of(pctx).calls
    assert {"action": "eat", "name": "старый отчёт.txt"} in pevents.of("plugin.pet.action")
    with pytest.raises(UserFacingError, match="не найден"):
        await manager(pctx).action("pet", "eat", {"path": str(tmp_path / "нет.txt")})
    with pytest.raises(UserFacingError, match="слишком большой"):
        await manager(pctx).action("pet", "eat", {"path": "/"})
    await manager(pctx).update("pet", config={"eats_trash": False})
    with pytest.raises(UserFacingError, match="не ест файлы"):
        await manager(pctx).action("pet", "eat", {"path": str(tmp_path)})


async def test_feed_empties_bin_after_confirmation(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "pet")
    asked = answer_confirmations(pctx, allow=True)
    result = await manager(pctx).action("pet", "feed", {})
    assert result == {"ok": True, "count": 3}
    assert asked[0]["title"] == "Очистить корзину?" and asked[0]["detail"] == "Файлов: 3"
    assert asked[0]["kind"] == "plugin"
    assert ("empty_recycle_bin", ()) in platform_of(pctx).calls
    assert {"action": "munch", "count": 3} in pevents.of("plugin.pet.action")
    assert plugin_of(pctx).status()["recycle_count"] == 0

    empty = await manager(pctx).action("pet", "feed", {})  # Корзина пуста — не спрашиваем
    assert empty["ok"] is False and empty["count"] == 0 and len(asked) == 1


async def test_feed_denied_keeps_bin(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "pet")
    answer_confirmations(pctx, allow=False)
    result = await manager(pctx).action("pet", "feed", {})
    assert result == {"ok": False, "cancelled": True, "count": 3}
    assert ("empty_recycle_bin", ()) not in platform_of(pctx).calls
    assert {"action": "refuse"} in pevents.of("plugin.pet.action")


async def test_voice_commands_use_pet_name(pctx: AppContext, pevents: EventLog) -> None:
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    await enable(pctx, "pet")
    assert {"покорми медузку", "погладь медузку", "покорми питомца"} <= commands.phrases()
    stroked = await commands.by_phrase("погладь медузку").handler({})
    assert stroked.ok and {"action": "pet"} in pevents.of("plugin.pet.action")
    answer_confirmations(pctx, allow=True)
    fed = await commands.by_phrase("покорми медузку").handler({})
    assert fed.ok and fed.message == "Медузка: съедено 3 файла из Корзины"
    await manager(pctx).update("pet", config={"name": "Барсик"})
    assert {"покорми барсика", "погладь барсика"} <= commands.phrases()
    said = await manager(pctx).action("pet", "say", {"text": "Привет!"})
    assert said == {"ok": True, "text": "Привет!"} and {"action": "say", "text": "Привет!"} in pevents.of(
        "plugin.pet.action"
    )
