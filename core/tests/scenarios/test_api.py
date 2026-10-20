"""Маршруты /api/scenarios: шаблоны, создание из шаблона, CRUD, проверка, запуск, события."""

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import EventLog
from tests.scenarios.conftest import calls

KEYS = {
    "id",
    "name",
    "icon",
    "enabled",
    "triggers",
    "conditions",
    "actions",
    "say",
    "template",
    "last_run",
    "last_ok",
    "last_trigger",
    "last_error",
    "runs",
    "created",
    "updated",
}


def body(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Режим работы",
        "icon": "briefcase",
        "triggers": [
            {"type": "time", "at": "9:00", "weekdays": [0, 1, 2, 3, 4]},
            {"type": "phrase", "phrases": ["рабочий режим"]},
        ],
        "conditions": {"time_from": "08:00", "time_to": "20:00"},
        "actions": [
            {"type": "Launch.App", "value": "Telegram"},
            {"type": "Keys.Press", "value": "Ctrl + Shift + S"},
            {"type": "System.VolumeSet", "value": 35},
        ],
        "say": "Хорошего дня",
    }
    data.update(over)
    return data


def test_templates_and_from_template(client: TestClient) -> None:
    items = client.get("/api/scenarios/templates").json()["items"]
    assert [t["id"] for t in items] == [
        "good_morning",
        "leaving",
        "game_mode",
        "end_of_day",
        "eye_break",
        "welcome_back",
    ]
    morning = items[0]
    assert morning["name"] == "Доброе утро" and morning["icon"] == "sunrise" and morning["added"] is False
    assert morning["triggers"] == [
        {"type": "time", "at": "08:00", "weekdays": [0, 1, 2, 3, 4]},
        {"type": "phrase", "phrases": ["доброе утро"]},
    ]
    assert morning["actions"] == [{"type": "Jarvis.Briefing", "value": ""}]
    eyes = next(t for t in items if t["id"] == "eye_break")
    assert eyes["say"] == "Отдохните, сэр: 20 секунд посмотрите вдаль"

    created = client.post("/api/scenarios/from-template", json={"template_id": "good_morning"})
    assert created.status_code == 200, created.text
    item = created.json()
    assert set(item) == KEYS
    assert item["enabled"] is True and item["template"] == "good_morning" and item["runs"] == 0
    assert item["last_run"] is None and item["conditions"] == {"weekdays": [], "time_from": "", "time_to": ""}
    again = client.post("/api/scenarios/from-template", json={"template_id": "good_morning"}).json()
    assert again["name"] == "Доброе утро 2"
    assert client.get("/api/scenarios/templates").json()["items"][0]["added"] is True
    assert client.post("/api/scenarios/from-template", json={"template_id": "nope"}).status_code == 404

    # шаблон, настроенный в редакторе, тоже помнит, откуда он
    edited = client.post(
        "/api/scenarios", json={**items[4], "name": "Глаза", "template": "eye_break", "id": "x"}
    ).json()
    assert edited["template"] == "eye_break" and edited["name"] == "Глаза"
    assert next(t for t in client.get("/api/scenarios/templates").json()["items"] if t["id"] == "eye_break")[
        "added"
    ]
    assert client.post("/api/scenarios", json={**items[1], "template": "nope"}).json()["template"] == ""

    # фраза шаблона сразу работает как команда
    match = client.post("/api/commands/test", json={"text": "доброе утро"}).json()["match"]
    assert match["parts"][0]["command_id"] == f"scenarios:{item['id']}"


def test_crud(client: TestClient) -> None:
    log = EventLog()
    client.ctx.bus.on("scenario*", log)  # type: ignore[attr-defined]
    assert client.get("/api/scenarios").json() == {"items": []}

    created = client.post("/api/scenarios", json=body())
    assert created.status_code == 200, created.text
    item = created.json()
    assert item["triggers"][0] == {"type": "time", "at": "09:00", "weekdays": [0, 1, 2, 3, 4]}
    assert item["conditions"] == {"weekdays": [], "time_from": "08:00", "time_to": "20:00"}
    assert item["actions"][1] == {"type": "Keys.Press", "value": "ctrl+shift+s"}
    assert item["actions"][2] == {"type": "System.VolumeSet", "value": "35"}
    assert "armed_at" not in item and "marks" not in item
    sid = item["id"]
    assert client.get(f"/api/scenarios/{sid}").json() == item
    assert client.get("/api/scenarios").json()["items"] == [item]

    patched = client.patch(f"/api/scenarios/{sid}", json={"enabled": False})
    assert patched.status_code == 200 and patched.json()["enabled"] is False
    assert patched.json()["triggers"] == item["triggers"]  # остальное не тронуто
    renamed = client.patch(f"/api/scenarios/{sid}", json={"name": "Работа", "conditions": None}).json()
    assert renamed["name"] == "Работа" and renamed["conditions"]["time_from"] == ""
    assert client.post("/api/scenarios", json=body(name="работа")).status_code == 400  # имя занято

    assert client.delete(f"/api/scenarios/{sid}").json() == {"ok": True}
    assert client.get(f"/api/scenarios/{sid}").status_code == 404
    assert client.patch(f"/api/scenarios/{sid}", json={"enabled": True}).status_code == 404
    assert client.delete(f"/api/scenarios/{sid}").status_code == 404
    assert client.post(f"/api/scenarios/{sid}/run").status_code == 404
    ops = [e["op"] for e in log.of("scenarios.changed")]
    assert ops == ["add", "update", "update", "delete"]


def test_validation_errors_are_russian(client: TestClient) -> None:
    cases = [
        (body(name="  "), "Укажите название сценария"),
        (body(triggers=[]), "Добавьте хотя бы один триггер"),
        (body(triggers=[{"type": "time", "at": "утром"}]), "Время запуска пишется как «08:00»"),
        (body(actions=[], say=""), "Добавьте действие или фразу"),
        (body(actions=[{"type": "Magic.Do", "value": "x"}]), "Неизвестное действие «Magic.Do»"),
        (body(actions=[{"type": "Keys.Press", "value": ""}]), "не заполнено значение"),
        (body(conditions={"weekdays": [9]}), "Дни недели"),
    ]
    for data, text in cases:
        response = client.post("/api/scenarios", json=data)
        assert response.status_code == 400, data
        assert text in response.json()["detail"], response.json()
    sid = client.post("/api/scenarios", json=body()).json()["id"]
    bad = client.patch(f"/api/scenarios/{sid}", json={"actions": [], "say": ""})
    assert bad.status_code == 400 and "Добавьте действие" in bad.json()["detail"]


def test_run_now(client: TestClient) -> None:
    log = EventLog()
    client.ctx.bus.on("*", log)  # type: ignore[attr-defined]
    sid = client.post("/api/scenarios", json=body(enabled=False)).json()["id"]
    result = client.post(f"/api/scenarios/{sid}/run")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["ok"] is True and data["title"] == "Сценарий «Режим работы»" and data["icon"] == "task"
    assert data["say"] == "Хорошего дня" and data["command_ids"] == [f"scenarios:{sid}"]
    assert data["data"]["trigger"] == "manual"
    ctx = client.ctx  # type: ignore[attr-defined]
    assert [c[0] for c in ctx.platform.calls if c[0] in ("launch", "press_keys", "volume_set")] == [
        "launch",
        "press_keys",
        "volume_set",
    ]
    assert calls(ctx, "volume_set") == [(35,)]
    assert log.of("scenario.run")[-1] == {"id": sid, "name": "Режим работы", "trigger": "manual", "ok": True}
    assert log.of("toast")[-1]["title"] == "Сценарий «Режим работы»"
    item = client.get(f"/api/scenarios/{sid}").json()
    assert item["runs"] == 1 and item["last_ok"] is True and item["last_trigger"] == "manual"
    history = client.get("/api/history").json()["items"][0]
    assert history["title"] == "Сценарий «Режим работы»" and history["source"] == "scenario"
    assert client.post(f"/api/scenarios/{sid}/stop").json() == {"ok": True, "stopped": False}
    assert client.post("/api/scenarios/nope/stop").status_code == 404


def test_briefing_action_in_catalog(client: TestClient) -> None:
    items = client.get("/api/commands/actions").json()["items"]
    briefing = next(a for a in items if a["type"] == "Jarvis.Briefing")
    assert briefing["title"] == "Сводка дня" and briefing["category"] == "Джарвис"
    assert [o["value"] for o in briefing["params"][0]["options"]] == [
        "",
        "short",
        "tasks",
        "today",
        "missed",
        "welcome",
        "hello",
    ]
