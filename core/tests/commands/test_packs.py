"""Паки команд: формат файлов, настоящие сочетания, установка и удаление."""

import json

import pytest

from jarvis import paths
from jarvis.commands import catalog
from jarvis.commands.keys import parse_sequence
from jarvis.commands.packs import PackStore, parse_pack
from jarvis.commands.service import Commands
from jarvis.commands.validate import check_actions
from jarvis.context import AppContext
from tests.conftest import EventLog

PACKS_DIR = paths.resource_dir() / "packs"
EXPECTED = {
    "spotify",
    "chrome",
    "yandex_browser",
    "firefox",
    "photoshop",
    "premiere",
    "after_effects",
    "illustrator",
    "lightroom",
    "obs",
    "discord",
    "telegram",
    "vscode",
    "word",
    "excel",
    "powerpoint",
    "explorer",
    "youtube",
    "vlc",
    "steam",
    "minecraft",
    "cs2",
    "dota2",
    "figma",
    "notion",
    "zoom",
    "blender",
    "davinci",
    "yandex_music",
    "windows_extra",
}


def test_all_packs_load_and_are_valid() -> None:
    store = PackStore(PACKS_DIR)
    assert store.errors == {}
    packs = {p.id: p for p in store.all()}
    assert set(packs) == EXPECTED
    for pack in packs.values():
        assert pack.name and pack.description and pack.icon, pack.id
        assert 5 <= len(pack.commands) <= 15, pack.id
        names = set()
        for command in pack.commands:
            assert command.id.startswith(f"pack.{pack.id}."), command.id
            assert command.name and command.phrases and command.actions, command.id
            assert command.name not in names, command.id
            names.add(command.name)
            assert check_actions(command.actions, user=False) == [], command.id
            for action in command.actions:
                assert action.type in catalog.TYPES
                assert action.type != "Launch.Shell", command.id
                if action.type == "Keys.Press":
                    assert parse_sequence(action.value), command.id
                if action.type == "App.Focus":
                    assert action.value == pack.app, command.id


def test_pack_files_are_utf8_json_with_short_ids() -> None:
    for path in sorted(PACKS_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["id"] == path.stem
        assert {"id", "name", "app", "icon", "description", "commands"} <= set(raw)
        for item in raw["commands"]:
            assert "." not in item["id"] and item["id"].isascii()


def test_parse_pack_rejects_bad_data() -> None:
    with pytest.raises(ValueError):
        parse_pack({"id": "x", "commands": []})
    with pytest.raises(ValueError):
        parse_pack({"id": "x", "commands": [{"id": "a", "name": "A", "phrases": ["a"], "actions": []}]})
    with pytest.raises(ValueError):
        parse_pack(
            {
                "id": "x",
                "commands": [
                    {"id": "a", "name": "A", "phrases": ["a"], "actions": [{"type": "Wait"}]},
                    {"id": "a", "name": "B", "phrases": ["b"], "actions": [{"type": "Wait"}]},
                ],
            }
        )


def test_bad_pack_file_is_reported(tmp_path: object) -> None:
    from pathlib import Path

    folder = Path(str(tmp_path))
    (folder / "broken.json").write_text("{oops", encoding="utf-8")
    (folder / "ok.json").write_text(
        json.dumps(
            {
                "id": "ok",
                "name": "Ok",
                "commands": [
                    {"id": "a", "name": "A", "phrases": ["а"], "actions": [{"type": "Wait", "value": "1"}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    store = PackStore(folder)
    assert "broken.json" in store.errors and [p.id for p in store.all()] == ["ok"]


async def test_install_and_uninstall(cmds: Commands, ctx: AppContext, events: EventLog) -> None:
    assert cmds.match("спотифай следующий трек") is None
    info = cmds.set_pack("spotify", True)
    assert info["installed"] is True and info["commands_count"] == 13
    assert "spotify" in ctx.settings.value.packs
    assert {"packs.changed", "commands.changed"} <= set(events.types())
    match = cmds.match("спотифай следующий трек")
    assert match is not None and match.parts[0].command_id == "pack.spotify.next"
    result = await cmds.execute(match, "text")
    assert result.ok
    assert ctx.platform.calls[-2:] == [
        ("window_action", ("focus", "Spotify.exe")),
        ("press_keys", ("ctrl+right",)),
    ]  # type: ignore[attr-defined]
    entry = cmds.get_entry("pack.spotify.next")
    assert entry is not None and entry.info()["source"] == "pack" and entry.info()["pack"] == "spotify"
    cmds.set_pack("spotify", False)
    assert cmds.get_entry("pack.spotify.next") is None and cmds.match("спотифай следующий трек") is None


async def test_pack_commands_can_be_disabled(cmds: Commands) -> None:
    cmds.set_pack("obs", True)
    assert cmds.match("начни запись в обс") is not None
    cmds.set_enabled("pack.obs.record_start", False)
    match = cmds.match("начни запись в обс")
    assert match is None or match.parts[0].command_id != "pack.obs.record_start"
    assert "pack.obs.record_start" in cmds.ctx.settings.value.disabled_commands


def test_unknown_pack(ctx: AppContext) -> None:
    from jarvis.commands.service import CommandNotFoundError

    with pytest.raises(CommandNotFoundError):
        ctx.commands.set_pack("nope", True)  # type: ignore[attr-defined]


def test_packs_json_totals(ctx: AppContext) -> None:
    data = ctx.commands.packs_json()  # type: ignore[attr-defined]
    assert data["totals"]["packs"] == 30 and data["totals"]["commands"] == sum(
        p["commands_count"] for p in data["items"]
    )
    first = data["items"][0]
    assert set(first) == {
        "id",
        "name",
        "app",
        "icon",
        "description",
        "commands_count",
        "installed",
        "commands",
    }
    assert set(first["commands"][0]) == {"id", "name", "phrases"}
