"""«Создать с помощью AI»: разбор описания правилами и ответ ИИ."""

from typing import Any

import pytest

from jarvis.commands.service import Commands
from jarvis.context import AppContext
from jarvis.services import UserFacingError


async def test_offline_example_from_reference(cmds: Commands) -> None:
    result = await cmds.build("Открой OBS, подожди 2 секунды и включи музыку в Spotify")
    assert result["actions"] == [
        {"type": "Launch.App", "value": "OBS Studio"},
        {"type": "Wait", "value": "2"},
        {"type": "Launch.App", "value": "Spotify"},
        {"type": "Media.PlayPause", "value": "play"},
    ]
    assert result["steps"] == [
        {"title": "Запустить программу", "detail": "OBS Studio"},
        {"title": "Пауза", "detail": "2 секунды"},
        {"title": "Запустить программу", "detail": "Spotify"},
        {"title": "Нажать клавишу", "detail": "Play/Pause"},
    ]
    assert result["name"].startswith("Открой OBS") and result["phrases"] and result["skipped"] == []


@pytest.mark.parametrize(
    ("description", "actions"),
    [
        ("нажми ctrl+s", [{"type": "Keys.Press", "value": "ctrl+s"}]),
        ("нажми контрол шифт эс", [{"type": "Keys.Press", "value": "ctrl+shift+s"}]),
        ("громкость на 30", [{"type": "System.VolumeSet", "value": "30"}]),
        ("открой ютуб", [{"type": "Launch.Url", "value": "https://www.youtube.com"}]),
        ("сверни все окна", [{"type": "Window.MinimizeAll", "value": ""}]),
        ("открой https://example.com/page", [{"type": "Launch.Url", "value": "https://example.com/page"}]),
        ("подожди полторы минуты", [{"type": "Wait", "value": "90"}]),
        (
            "напечатай Привет, мир и нажми энтер",
            [{"type": "Keys.Type", "value": "Привет, мир"}, {"type": "Keys.Press", "value": "enter"}],
        ),
        (
            "открой фотошоп, затем сделай громче",
            [
                {"type": "Launch.App", "value": "Adobe Photoshop 2026"},
                {"type": "System.VolumeUp", "value": "10"},
            ],
        ),
    ],
)
async def test_offline_rules(cmds: Commands, description: str, actions: list[dict[str, str]]) -> None:
    assert (await cmds.build(description))["actions"] == actions


async def test_offline_reports_skipped_and_fails_on_nonsense(cmds: Commands) -> None:
    result = await cmds.build("сверни все окна и спой песню про котов")
    assert result["actions"] == [{"type": "Window.MinimizeAll", "value": ""}]
    assert result["skipped"] == ["спой песню про котов"]
    with pytest.raises(UserFacingError):
        await cmds.build("спой песню про котов")
    with pytest.raises(UserFacingError):
        await cmds.build("   ")


async def test_ai_answer_is_checked(cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def complete(messages: list[dict[str, Any]], **kw: Any) -> Any:
        seen.update(kw)
        seen["messages"] = messages
        return {
            "name": "Режим стрима",
            "phrases": ["Включи режим стрима!", "режим стрима", "режим стрима"],
            "actions": [
                {"type": "launch.app", "value": "obs"},
                {"type": "Teleport", "value": "Марс"},
                {"type": "Keys.Press", "value": "контрол шифт эс"},
                {"type": "Wait", "value": "2"},
                {"type": "System.VolumeSet", "value": 35},
            ],
        }

    monkeypatch.setattr(type(ctx.ai), "configured", property(lambda self: True))
    monkeypatch.setattr(ctx.ai, "complete", complete)
    result = await cmds.build("режим стрима: запусти обс, начни запись, громкость 35")
    assert seen["json_schema"]["required"] == ["name", "phrases", "actions"]
    assert "Launch.App" in seen["messages"][0]["content"]
    assert result["name"] == "Режим стрима"
    assert result["phrases"] == ["включи режим стрима", "режим стрима"]
    assert result["actions"] == [
        {"type": "Launch.App", "value": "OBS Studio"},
        {"type": "Keys.Press", "value": "ctrl+shift+s"},
        {"type": "Wait", "value": "2"},
        {"type": "System.VolumeSet", "value": "35"},
    ]
    assert result["skipped"] == ["Teleport"]
    assert [s["title"] for s in result["steps"]] == [
        "Запустить программу",
        "Нажать клавишу",
        "Пауза",
        "Громкость",
    ]


async def test_ai_failure_falls_back_to_rules(
    cmds: Commands, ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(messages: list[dict[str, Any]], **kw: Any) -> Any:
        raise RuntimeError("нет сети")

    monkeypatch.setattr(type(ctx.ai), "configured", property(lambda self: True))
    monkeypatch.setattr(ctx.ai, "complete", broken)
    result = await cmds.build("открой спотифай")
    assert result["actions"] == [{"type": "Launch.App", "value": "Spotify"}]
