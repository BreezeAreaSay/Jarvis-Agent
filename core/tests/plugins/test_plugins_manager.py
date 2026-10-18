"""Менеджер плагинов: описание для интерфейса, включение, настройки и секреты, изоляция сбоев, команды."""

import asyncio
import json

import pytest

from jarvis.context import AppContext
from jarvis.plugins import manager as manager_module
from jarvis.plugins.dictation import Dictation
from jarvis.plugins.manager import PLUGIN_CLASSES, UnknownPlugin
from jarvis.plugins.pet import Pet
from jarvis.plugins.reader import Reader
from jarvis.services import UserFacingError
from jarvis.settings import SettingsStore
from tests.conftest import EventLog
from tests.plugins.conftest import FakeCommands, enable, manager, wait_until

IDS = [
    "smart_home",
    "dictation",
    "subtitles",
    "summary",
    "reader",
    "autolock",
    "second_pc",
    "notify_voice",
    "pet",
    "focus",
    "clipboard",
    "file_search",
]
PLUGIN_KEYS = {
    "id",
    "name",
    "description",
    "icon",
    "color",
    "enabled",
    "config",
    "schema",
    "status",
    "actions",
}


def test_list_describes_all_plugins(pctx: AppContext) -> None:
    items = pctx.plugins.list()
    assert [i["id"] for i in items] == IDS
    assert [cls.id for cls in PLUGIN_CLASSES] == IDS
    for item in items:
        assert set(item) >= PLUGIN_KEYS
        assert item["enabled"] is False
        assert item["status"]["running"] is False
        assert len(item["color"]) == 2 and all(c.startswith("#") for c in item["color"])
        assert item["name"] and item["description"]
        for field in item["schema"]:
            assert {"key", "kind", "label"} <= set(field)
            if field["kind"] == "secret":
                assert field["set"] is False and "default" not in field
        assert all({"id", "title"} == set(a) for a in item["actions"])
    json.dumps(items, ensure_ascii=False)  # всё сериализуется
    autolock = next(i for i in items if i["id"] == "autolock")
    away = next(f for f in autolock["schema"] if f["key"] == "away_seconds")
    assert away["integer"] is True  # интерфейс показывает «20», а не «20,0»
    pet = next(i for i in items if i["id"] == "pet")
    assert pet["config"] == {
        "skin": "jelly",
        "name": "Медузка",
        "size": 64,
        "on_windows": True,
        "dance_to_music": True,
        "eats_trash": True,
    }


async def test_enable_persists_and_starts(pctx: AppContext, pevents: EventLog) -> None:
    plugins = manager(pctx)
    await plugins.start()
    info = await plugins.set_enabled("pet", True)
    assert info["enabled"] is True and info["status"]["running"] is True
    assert pctx.settings.value.plugins["pet"].enabled is True
    on_disk = SettingsStore(pctx.settings.path)
    assert on_disk.value.plugins["pet"].enabled is True
    changed = pevents.of("plugins.changed")
    assert changed and next(i for i in changed[-1]["items"] if i["id"] == "pet")["enabled"] is True

    info = await plugins.set_enabled("pet", False)
    assert info["enabled"] is False and info["status"]["running"] is False
    assert SettingsStore(pctx.settings.path).value.plugins["pet"].enabled is False


async def test_enabled_plugins_start_with_service(pctx: AppContext) -> None:
    pctx.settings.patch({"plugins": {"dictation": {"enabled": True}, "reader": {"enabled": True}}})
    plugins = manager(pctx)
    await plugins.start()
    assert plugins.is_running("dictation") and plugins.is_running("reader")
    assert not plugins.is_running("pet")
    await plugins.stop()
    assert not plugins.is_running("dictation")


async def test_config_validation_and_secrets(pctx: AppContext) -> None:
    plugins = manager(pctx)
    info = await plugins.update(
        "smart_home", config={"ha_url": " http://ha.local:8123 ", "ha_token": "secret-1", "sensors": "false"}
    )
    assert info["config"] == {"ha_url": "http://ha.local:8123", "sensors": False}
    assert pctx.secrets.get("ha_token") == "secret-1"
    assert next(f for f in info["schema"] if f["key"] == "ha_token")["set"] is True
    assert "secret-1" not in pctx.settings.path.read_text(encoding="utf-8")

    await plugins.update("smart_home", config={"ha_token": ""})  # пусто — не трогать
    assert pctx.secrets.get("ha_token") == "secret-1"
    await plugins.update("smart_home", config={"ha_token": None})  # None — удалить
    assert pctx.secrets.get("ha_token") == ""

    info = await plugins.update("reader", config={"speed": 5})
    assert info["config"]["speed"] == 2.0  # число зажимается в допустимые пределы
    with pytest.raises(UserFacingError, match="нужно число"):
        await plugins.update("reader", config={"speed": "быстро"})
    with pytest.raises(UserFacingError, match="недопустимое значение"):
        await plugins.update("pet", config={"skin": "dragon", "name": "Бульбик"})
    assert plugins.get("pet").config["name"] == "Медузка"  # при ошибке не меняется ничего
    info = await plugins.update("pet", config={"name": "Бульбик", "unknown": 1})
    assert info["config"]["name"] == "Бульбик" and "unknown" not in info["config"]


async def test_failing_plugin_does_not_break_others(
    pctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: Pet) -> None:
        raise RuntimeError("камера сгорела")

    def broken_status(self: Reader) -> dict[str, object]:
        raise ValueError("сломался статус")

    monkeypatch.setattr(Pet, "start", boom)
    monkeypatch.setattr(Reader, "status", broken_status)
    pctx.settings.patch({"plugins": {name: {"enabled": True} for name in ("pet", "dictation", "reader")}})
    plugins = manager(pctx)
    await plugins.start()
    assert plugins.is_running("dictation") and plugins.is_running("reader")
    pet = plugins.info("pet")
    assert pet["enabled"] is True and pet["status"]["running"] is False
    assert "камера сгорела" in pet["status"]["error"]
    reader = plugins.info("reader")
    assert reader["status"]["running"] is True and "сломался статус" in reader["status"]["error"]
    assert len(plugins.list()) == len(IDS)
    with pytest.raises(UserFacingError, match=r"не запущен: .*камера сгорела"):
        await plugins.action("pet", "pet", {})

    monkeypatch.undo()  # плагин починили — повторное включение запускает его
    info = await plugins.update("pet", enabled=True)
    assert info["status"]["running"] is True and "error" not in info["status"]


async def test_hanging_start_times_out(pctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(self: Dictation) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(Dictation, "start", hang)
    monkeypatch.setattr(manager_module, "START_TIMEOUT", 0.1)
    info = await enable(pctx, "dictation")
    assert info["status"]["running"] is False
    assert info["status"]["error"] == "Плагин запускался слишком долго"


async def test_voice_commands_follow_enabled_plugins(pctx: AppContext) -> None:
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    plugins = manager(pctx)
    await plugins.start()
    assert "plugins" in commands.providers
    assert commands.dynamic() == []
    before = commands.refreshes
    await plugins.set_enabled("dictation", True)
    assert commands.refreshes > before
    start = commands.get("plugin.dictation:start")
    assert "начни диктовку" in start.phrases and start.category == "plugins"
    assert all(c.id.startswith("plugin.dictation:") for c in commands.dynamic())
    await plugins.set_enabled("dictation", False)
    assert commands.dynamic() == []
    result = await start.handler({})  # старая ссылка на команду выключенного плагина
    assert result.ok is False and "выключен" in result.message


async def test_actions(pctx: AppContext) -> None:
    plugins = manager(pctx)
    await plugins.start()
    with pytest.raises(UnknownPlugin):
        await plugins.action("nope", "x", {})
    with pytest.raises(UserFacingError, match="выключен"):
        await plugins.action("pet", "pet", {})
    await plugins.set_enabled("pet", True)
    with pytest.raises(UserFacingError, match="нет действия"):
        await plugins.action("pet", "dance_forever", {})
    with pytest.raises(UserFacingError, match="нет действия"):
        await plugins.action("pet", "__class__", {})
    assert await plugins.action("pet", "pet", {}) == {"ok": True}


async def test_status_changes_are_batched(pctx: AppContext, pevents: EventLog) -> None:
    await enable(pctx, "dictation")
    before = len(pevents.of("plugins.changed"))
    plugin = manager(pctx).get("dictation")
    for _ in range(5):
        plugin.changed()
    await wait_until(lambda: len(pevents.of("plugins.changed")) > before)
    await asyncio.sleep(manager_module.CHANGED_DELAY + 0.1)
    assert len(pevents.of("plugins.changed")) == before + 1
