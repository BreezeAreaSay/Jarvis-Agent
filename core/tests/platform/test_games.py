"""Библиотека игр: Steam (libraryfolders.vdf, appmanifest_*.acf), Epic (манифесты .item), GOG (реестр) —
в список программ; голосом «поставь игру CS2», «включи контру», «запусти игровой режим» — сразу ссылкой
лаунчера."""

import json
from pathlib import Path
from typing import Any

from jarvis.context import AppContext
from jarvis.platform.base import AppInfo
from jarvis.platform.windows import games

LIBRARY = """
"libraryfolders"
{
	"0"
	{
		"path"		"{steam}"
		"apps"		{ "730"		"123" }
	}
	"1"
	{
		"path"		"{other}"
	}
	"2"
	{
		"path"		"Z:\\\\нет такой"
	}
}
"""


def _manifest(folder: Path, app_id: str, name: str, flags: str = "4") -> None:
    (folder / "steamapps").mkdir(parents=True, exist_ok=True)
    text = (
        f'"AppState"\n{{\n\t"appid"\t\t"{app_id}"\n\t"name"\t\t"{name}"\n\t"StateFlags"\t\t"{flags}"\n'
        f'\t"installdir"\t\t"{name}"\n}}\n'
    )
    (folder / "steamapps" / f"appmanifest_{app_id}.acf").write_text(text, encoding="utf-8")


def test_steam_games_from_all_libraries(tmp_path: Path) -> None:
    steam, other = tmp_path / "Steam", tmp_path / "Games"
    _manifest(steam, "730", "Counter-Strike 2")
    _manifest(steam, "228980", "Steamworks Common Redistributables")  # не игра
    _manifest(other, "570", "Dota 2")
    _manifest(other, "1172470", "Apex Legends", flags="1026")  # обновляется, но установлена (бит 4 — нет)
    _manifest(other, "271590", "Grand Theft Auto V", flags="6")
    library = LIBRARY.replace("{steam}", str(steam).replace("\\", "\\\\")).replace(
        "{other}", str(other).replace("\\", "\\\\")
    )
    (steam / "steamapps" / "libraryfolders.vdf").write_text(library, encoding="utf-8")

    found = {g.name: g for g in games.steam_games(str(steam))}
    assert set(found) == {"Counter-Strike 2", "Dota 2", "Grand Theft Auto V"}
    assert found["Counter-Strike 2"].app_id == "steam://rungameid/730" and found["Dota 2"].source == "game"


def test_parse_vdf_keeps_escaped_paths() -> None:
    data = games.parse_vdf('"a" { "path" "C:\\\\Program Files (x86)\\\\Steam" "b" { "c" "1" } }')
    assert data == {"a": {"path": "C:\\Program Files (x86)\\Steam", "b": {"c": "1"}}}
    assert games.parse_vdf('"a" { "broken" ') == {"a": {}}  # испорченное — что успел


def test_epic_and_gog_games(tmp_path: Path) -> None:
    manifests = tmp_path / "Manifests"
    manifests.mkdir()

    def item(name: str, data: dict[str, Any]) -> None:
        (manifests / f"{name}.item").write_text(json.dumps(data), encoding="utf-8")

    fortnite = {"DisplayName": "Fortnite", "AppName": "Fortnite", "AppCategories": ["public", "games"]}
    item("a", {**fortnite, "InstallLocation": "C:\\Epic\\Fortnite", "LaunchExecutable": "Launcher.exe"})
    item("b", {"DisplayName": "Unreal Engine", "AppName": "UE_5.4", "AppCategories": ["engines"]})
    item("c", {"DisplayName": "Hades", "AppName": "Min", "bIsIncompleteInstall": True})
    (manifests / "broken.item").write_text("{не json", encoding="utf-8")
    epic = games.epic_games(str(manifests))
    assert [(g.name, g.app_id) for g in epic] == [
        ("Fortnite", "com.epicgames.launcher://apps/Fortnite?action=launch&silent=true")
    ]

    gog = games.gog_games(
        [{"gameName": "The Witcher 3", "exe": "D:\\GOG\\Witcher3\\witcher3.exe"}, {"gameName": ""}]
    )
    assert [(g.name, g.app_id) for g in gog] == [("The Witcher 3", "D:\\GOG\\Witcher3\\witcher3.exe")]


def test_games_join_the_app_list_without_duplicates() -> None:
    apps = [
        AppInfo("Steam", "steam.exe"),
        AppInfo("Counter-Strike 2", "steam://rungameid/730", source="start"),
    ]
    found = [
        AppInfo("Counter-Strike  2", "steam://rungameid/730", source="game"),
        AppInfo("Dota 2", "x", source="game"),
    ]
    merged = games.merge(apps, found)
    assert [a.name for a in merged] == ["Steam", "Counter-Strike 2", "Dota 2"]


async def test_games_by_voice(ctx: AppContext) -> None:
    platform: Any = ctx.platform
    for phrase, url in (
        ("поставь игру CS2", "steam://rungameid/730"),  # как в ролике
        ("включи контру", "steam://rungameid/730"),
        ("запусти кс два", "steam://rungameid/730"),
        ("давай поиграем в доту", "steam://rungameid/570"),
    ):
        platform.calls.clear()
        result = await ctx.dispatcher.handle(phrase, "text")
        assert result["kind"] == "command", phrase
        assert ("open_url", (url,)) in platform.calls, phrase

    platform.calls.clear()
    await ctx.dispatcher.handle("активируй игровой режим", "text")
    assert platform.calls[:2] == [
        ("window_action", ("minimize_all", None)),
        ("open_url", ("steam://open/games",)),
    ]
    platform.calls.clear()
    await ctx.dispatcher.handle("открой игровой режим", "text")  # страница параметров Windows — как раньше
    assert ("open_url", ("ms-settings:gaming-gamemode",)) in platform.calls
