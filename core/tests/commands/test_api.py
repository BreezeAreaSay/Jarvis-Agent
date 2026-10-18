"""Маршруты /api/commands, /api/packs, /api/apps (docs/CONTRACTS.md §2)."""

import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.services import DynamicCommand, ExecResult


def test_list_and_get(client: TestClient) -> None:
    data = client.get("/api/commands").json()
    assert data["total"] >= 600 and len(data["items"]) == data["total"]
    titles = {c["id"]: c["title"] for c in data["categories"]}
    assert titles["volume"] == "Громкость" and titles["mouse"] == "Мышь" and titles["apps"] == "Программы"
    assert sum(c["count"] for c in data["categories"]) == data["total"]
    item = client.get("/api/commands/builtin.media.play").json()
    assert item["source"] == "builtin" and item["editable"] is False and item["name"] == "Включить музыку"
    assert {
        "id",
        "name",
        "category",
        "phrases",
        "optional",
        "actions",
        "confirm",
        "chain",
        "icon",
        "enabled",
        "say",
    } <= set(item)
    assert client.get("/api/commands/builtin.nope").status_code == 404


def test_filters(client: TestClient) -> None:
    volume = client.get("/api/commands", params={"category": "volume"}).json()["items"]
    assert volume and all(c["category"] == "volume" for c in volume)
    found = client.get("/api/commands", params={"q": "скриншот"}).json()["items"]
    assert "builtin.system.screenshot" in {c["id"] for c in found}


def test_actions_catalog(client: TestClient) -> None:
    items = client.get("/api/commands/actions").json()["items"]
    types = {a["type"] for a in items}
    contract = {
        "Launch.File",
        "Launch.App",
        "Launch.Url",
        "Launch.Folder",
        "Launch.Shell",
        "App.Close",
        "App.Kill",
        "App.Focus",
        "Keys.Press",
        "Keys.Type",
        "Keys.Paste",
        "Mouse.ClickLeft",
        "Mouse.ClickRight",
        "Mouse.DoubleClick",
        "Mouse.Move",
        "Mouse.Scroll",
        "System.VolumeSet",
        "System.VolumeUp",
        "System.VolumeDown",
        "System.Mute",
        "System.Unmute",
        "System.MuteToggle",
        "System.BrightnessSet",
        "System.Lock",
        "System.Sleep",
        "System.Shutdown",
        "System.Restart",
        "System.CancelShutdown",
        "System.Screenshot",
        "System.EmptyRecycleBin",
        "Media.PlayPause",
        "Media.Next",
        "Media.Prev",
        "Media.Stop",
        "Window.MinimizeAll",
        "Window.Minimize",
        "Window.Maximize",
        "Window.Restore",
        "Window.Close",
        "Wait",
        "Speak",
        "Notify",
        "Clipboard.Set",
        "Web.Search",
        "Info.Time",
        "Info.Date",
        "Info.Weather",
        "Jarvis.OpenDesk",
        "Jarvis.Ask",
        "Jarvis.Command",
        "Jarvis.Plugin",
        "Jarvis.SilentMode",
        "Jarvis.ShowWindow",
    }
    assert contract <= types
    keys = next(a for a in items if a["type"] == "Keys.Press")
    assert keys["params"] == [
        {
            "name": "value",
            "kind": "keys",
            "label": "Сочетание",
            "placeholder": "ctrl+shift+s",
            "required": True,
        }
    ]
    kinds = {p["kind"] for a in items for p in a["params"]}
    assert kinds <= {"text", "number", "keys", "path", "app", "url", "select", "coords"}
    silent = next(a for a in items if a["type"] == "Jarvis.SilentMode")
    assert [o["value"] for o in silent["params"][0]["options"]] == ["toggle", "on", "off"]


def test_user_command_crud(client: TestClient) -> None:
    body: dict[str, Any] = {
        "name": "Режим стрима",
        "phrases": ["включи режим стрима", "режим стрима"],
        "optional": ["пожалуйста"],
        "actions": [
            {"type": "Launch.App", "value": "OBS Studio"},
            {"type": "Keys.Press", "value": "Ctrl + Shift + S"},
            {"type": "Wait", "value": 2},
            {"type": "System.VolumeSet", "value": "35"},
        ],
        "confirm": False,
        "chain": True,
        "icon": "obs",
    }
    created = client.post("/api/commands", json=body)
    assert created.status_code == 200, created.text
    item = created.json()
    assert item["id"] == "user.rezhim_strima" and item["source"] == "user" and item["editable"] is True
    assert item["actions"][1]["value"] == "ctrl+shift+s" and item["actions"][2]["value"] == "2"
    assert client.post("/api/commands", json=body).json()["id"] == "user.rezhim_strima_2"

    match = client.post("/api/commands/test", json={"text": "включи режим стрима"}).json()["match"]
    assert match["parts"][0]["command_id"] == "user.rezhim_strima" and match["parts"][0]["score"] == 100.0

    body["name"] = "Стрим"
    body["enabled"] = False
    updated = client.put("/api/commands/user.rezhim_strima", json=body).json()
    assert updated["name"] == "Стрим" and updated["enabled"] is False
    assert client.patch("/api/commands/user.rezhim_strima", json={"enabled": True}).json()["enabled"] is True
    assert client.delete("/api/commands/user.rezhim_strima").json() == {"ok": True}
    assert client.get("/api/commands/user.rezhim_strima").status_code == 404
    assert client.delete("/api/commands/user.rezhim_strima").status_code == 404


@pytest.mark.parametrize(
    ("patch", "detail"),
    [
        ({"name": ""}, "название"),
        ({"phrases": []}, "фразу"),
        ({"actions": []}, "действие"),
        ({"actions": [{"type": "Teleport", "value": "Марс"}]}, "Неизвестное действие"),
        ({"actions": [{"type": "Keys.Press", "value": "ctrl+foo"}]}, "клавишу"),
        ({"actions": [{"type": "Wait", "value": "долго"}]}, "секунд"),
    ],
)
def test_user_command_validation(client: TestClient, patch: dict[str, Any], detail: str) -> None:
    body = {"name": "Тест", "phrases": ["тест"], "actions": [{"type": "Wait", "value": "1"}], **patch}
    response = client.post("/api/commands", json=body)
    assert response.status_code == 400 and detail in response.json()["detail"]


def test_builtin_is_not_editable_but_can_be_disabled(client: TestClient) -> None:
    body = {"name": "x", "phrases": ["x"], "actions": [{"type": "Wait", "value": "1"}]}
    assert client.put("/api/commands/builtin.media.play", json=body).status_code == 400
    assert client.delete("/api/commands/builtin.media.play").status_code == 400
    off = client.patch("/api/commands/builtin.media.play", json={"enabled": False}).json()
    assert off["enabled"] is False
    assert "builtin.media.play" in client.get("/api/settings").json()["settings"]["disabled_commands"]
    assert client.post("/api/commands/test", json={"text": "включи музыку"}).json()["match"] is None
    assert client.patch("/api/commands/builtin.media.play", json={"enabled": True}).json()["enabled"] is True


def test_run_and_test(client: TestClient) -> None:
    result = client.post("/api/commands/builtin.volume.set/run", json={"slots": {"number": 25}}).json()
    assert (
        result["ok"] is True and result["message"] == "Громкость 25%" and result["title"] == "Громкость на 25"
    )
    assert {"ok", "title", "message", "icon", "say", "command_ids", "data"} == set(result)
    no_body = client.post("/api/commands/builtin.windows.minimize_all/run")
    assert no_body.status_code == 200 and no_body.json()["ok"] is True
    assert client.post("/api/commands/builtin.nope/run", json={}).status_code == 404
    match = client.post("/api/commands/test", json={"text": "сверни все окна и громкость на 30"}).json()[
        "match"
    ]
    assert [p["command_id"] for p in match["parts"]] == ["builtin.windows.minimize_all", "builtin.volume.set"]
    assert match["parts"][1]["slots"] == {"number": 30}
    assert client.post("/api/commands/test", json={"text": "расскажи анекдот"}).json() == {"match": None}


def test_dynamic_commands_in_api(client: TestClient) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(slots: dict[str, Any]) -> ExecResult:
        seen.append(slots)
        return ExecResult(True, "Открыт стол «Игры»", icon="desk")

    commands = client.ctx.commands  # type: ignore[attr-defined]
    commands.register_provider(
        "desks_test",
        lambda: [
            DynamicCommand(
                "desks:abc",
                "Открыть стол «Игры»",
                ["открой стол игры"],
                handler,
                category="desks",
                icon="desk",
            ),
        ],
    )
    commands.refresh_dynamic()
    data = client.get("/api/commands", params={"category": "desks"}).json()
    item = next(c for c in data["items"] if c["id"] == "desks:abc")
    assert item["source"] == "dynamic" and item["editable"] is False and item["actions"] == []
    assert {
        "id": "desks",
        "title": "Столы",
        "count": data["categories"][[c["id"] for c in data["categories"]].index("desks")]["count"],
    } in data["categories"]
    assert client.get("/api/commands/desks:abc").json()["name"] == "Открыть стол «Игры»"
    result = client.post("/api/commands/desks:abc/run", json={}).json()
    assert result["ok"] is True and seen == [{"_source": "ui"}]
    assert client.patch("/api/commands/desks:abc", json={"enabled": False}).json()["enabled"] is False


def test_build(client: TestClient) -> None:
    result = client.post(
        "/api/commands/build", json={"description": "Открой OBS, подожди 2 секунды и включи музыку в Spotify"}
    ).json()
    assert [s["detail"] for s in result["steps"]] == ["OBS Studio", "2 секунды", "Spotify", "Play/Pause"]
    assert {"name", "phrases", "actions", "steps"} <= set(result)
    response = client.post("/api/commands/build", json={"description": "спой песню"})
    assert response.status_code == 400


def test_record(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pynput", None)
    response = client.post("/api/commands/record/start")
    assert response.status_code == 400 and "Запись действий недоступна" in response.json()["detail"]
    assert client.post("/api/commands/record/stop").json() == {"actions": []}


def test_packs_routes(client: TestClient) -> None:
    data = client.get("/api/packs").json()
    assert data["totals"]["packs"] == 30 and data["totals"]["commands"] >= 300
    spotify = next(p for p in data["items"] if p["id"] == "spotify")
    assert spotify["installed"] is False and spotify["name"] == "Spotify" and spotify["app"] == "Spotify.exe"
    installed = client.post("/api/packs/spotify/install").json()
    assert installed["installed"] is True and installed["commands_count"] == len(installed["commands"])
    assert client.get("/api/commands/pack.spotify.next").json()["pack"] == "spotify"
    removed = client.delete("/api/packs/spotify").json()
    assert removed["installed"] is False
    assert client.get("/api/commands/pack.spotify.next").status_code == 404
    assert client.post("/api/packs/nope/install").status_code == 404


def test_apps(client: TestClient) -> None:
    items = client.get("/api/apps").json()["items"]
    assert {"name", "app_id", "path", "source"} == set(items[0])
    assert "Spotify" in {a["name"] for a in items}
    assert client.get("/api/apps", params={"refresh": 1}).json()["items"] == items


def test_dispatch_runs_commands(client: TestClient) -> None:
    result = client.post("/api/dispatch", json={"text": "громкость на 30"}).json()
    assert result["kind"] == "command" and result["result"]["ok"] is True
    assert result["history"]["title"] == "Громкость на 30"
