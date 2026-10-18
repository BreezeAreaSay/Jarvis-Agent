"""Маршруты API плагинов через общую фикстуру client (ядро целиком, платформа-заглушка)."""

from fastapi.testclient import TestClient

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
]


def test_list_and_update(client: TestClient) -> None:
    r = client.get("/api/plugins")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == IDS
    assert {
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
    } <= set(items[0])

    r = client.put("/api/plugins/pet", json={"enabled": True, "config": {"name": "Бульбик", "size": 96}})
    assert r.status_code == 200
    pet = r.json()
    assert pet["enabled"] is True and pet["status"]["running"] is True
    assert pet["config"]["name"] == "Бульбик" and pet["config"]["size"] == 96
    settings = client.get("/api/settings").json()["settings"]
    assert settings["plugins"]["pet"]["enabled"] is True

    r = client.put("/api/plugins/pet", json={"config": {"skin": "dragon"}})
    assert r.status_code == 400 and "недопустимое значение" in r.json()["detail"]
    r = client.put("/api/plugins/pet", json={"config": {"size": "много"}})
    assert r.status_code == 400
    assert client.put("/api/plugins/nope", json={"enabled": True}).status_code == 404
    assert client.put("/api/plugins/pet", json={"enabled": "может быть"}).status_code == 422

    r = client.put("/api/plugins/smart_home", json={"config": {"ha_url": "ha.local:8123", "ha_token": "t-1"}})
    assert r.status_code == 200
    assert "ha_token" not in r.json()["config"]
    assert next(f for f in r.json()["schema"] if f["key"] == "ha_token")["set"] is True
    assert client.get("/api/settings").json()["secrets"]["ha_token"] is True

    r = client.put("/api/plugins/pet", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["status"]["running"] is False


def test_actions(client: TestClient) -> None:
    r = client.post("/api/plugins/dictation/action", json={"action": "toggle", "args": {}})
    assert r.status_code == 400 and "выключен" in r.json()["detail"]
    assert client.post("/api/plugins/nope/action", json={"action": "x"}).status_code == 404
    assert client.post("/api/plugins/pet/action", json={"args": {}}).status_code == 422

    assert client.put("/api/plugins/pet", json={"enabled": True}).status_code == 200
    r = client.post("/api/plugins/pet/action", json={"action": "pet"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = client.post("/api/plugins/pet/action", json={"action": "fly_away"})
    assert r.status_code == 400 and "нет действия" in r.json()["detail"]
    r = client.post("/api/plugins/pet/action", json={"action": "eat", "args": {"path": "/нет/такого/файла"}})
    assert r.status_code == 400 and r.json()["detail"] == "Файл не найден"

    assert client.put("/api/plugins/smart_home", json={"enabled": True}).status_code == 200
    r = client.post("/api/plugins/smart_home/action", json={"action": "states", "args": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["devices"] == []
