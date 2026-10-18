"""«Умный дом»: устройства Home Assistant и Яндекса (сеть — MockTransport), действия и голосовые команды."""

import json
from typing import Any

import httpx
import pytest

from jarvis.context import AppContext
from jarvis.plugins.smart_home import SmartHome
from jarvis.services import UserFacingError
from tests.plugins.conftest import FakeCommands, enable, manager, wait_until

HA_URL = "http://ha.local:8123"
HA_STATES = [
    {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Люстра", "brightness": 128, "supported_color_modes": ["brightness"]},
    },
    {
        "entity_id": "light.lamp",
        "state": "off",
        "attributes": {"friendly_name": "Настольная лампа", "supported_color_modes": ["onoff"]},
    },
    {"entity_id": "switch.kettle", "state": "off", "attributes": {"friendly_name": "Чайник"}},
    {
        "entity_id": "climate.ac",
        "state": "cool",
        "attributes": {"friendly_name": "Кондиционер", "temperature": 23, "current_temperature": 25.5},
    },
    {
        "entity_id": "sensor.bedroom_temp",
        "state": "21.4",
        "attributes": {
            "friendly_name": "Температура в спальне",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
        },
    },
    {"entity_id": "sensor.phone_battery", "state": "80", "attributes": {"device_class": "battery"}},
    {"entity_id": "automation.morning", "state": "on", "attributes": {"friendly_name": "Утро"}},
    {"entity_id": "cover.curtains", "state": "closed", "attributes": {"friendly_name": "Шторы"}},
    {"entity_id": "switch.broken", "state": "unavailable", "attributes": {"friendly_name": "Старая розетка"}},
]
HA_AREAS = "light.kitchen\tКухня\nlight.lamp\tСпальня\nclimate.ac\tГостиная\nswitch.kettle\t\n"
YA_INFO = {
    "status": "ok",
    "request_id": "r-1",
    "rooms": [{"id": "room-1", "name": "Гостиная", "devices": ["ya-1", "ya-2", "ya-3"]}],
    "devices": [
        {
            "id": "ya-1",
            "name": "Торшер",
            "type": "devices.types.light",
            "room": "room-1",
            "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}},
                {
                    "type": "devices.capabilities.range",
                    "parameters": {"instance": "brightness", "unit": "unit.percent"},
                    "state": {"instance": "brightness", "value": 40},
                },
            ],
            "properties": [],
        },
        {
            "id": "ya-2",
            "name": "Розетка",
            "type": "devices.types.socket",
            "room": "room-1",
            "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}
            ],
            "properties": [],
        },
        {
            "id": "ya-3",
            "name": "Датчик климата",
            "type": "devices.types.sensor.climate",
            "room": "room-1",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "parameters": {"instance": "temperature", "unit": "unit.temperature.celsius"},
                    "state": {"instance": "temperature", "value": 22.5},
                }
            ],
        },
    ],
}


class Home:
    """Поддельные Home Assistant и API Яндекса; запоминает вызовы служб."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ha_status = 200
        self.ha_down = False
        self.ya_result = "DONE"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "ha.local":
            if self.ha_down:
                raise httpx.ConnectError("нет связи", request=request)
            if request.headers.get("authorization") != "Bearer ha-secret":
                return httpx.Response(401, json={"message": "Unauthorized"})
            if self.ha_status != 200:
                return httpx.Response(self.ha_status, text="oops")
            if url.path == "/api/states":
                return httpx.Response(200, json=HA_STATES)
            if url.path == "/api/template":
                return httpx.Response(200, text=HA_AREAS)
            if url.path.startswith("/api/services/"):
                self.calls.append((url.path, json.loads(request.content)))
                return httpx.Response(200, json=[])
        if url.host == "api.iot.yandex.net":
            if request.headers.get("authorization") != "Bearer ya-secret":
                return httpx.Response(401, json={"status": "error", "message": "bad token"})
            if url.path == "/v1.0/user/info":
                return httpx.Response(200, json=YA_INFO)
            if url.path == "/v1.0/devices/actions":
                body = json.loads(request.content)
                self.calls.append((url.path, body))
                device = body["devices"][0]
                caps = [
                    {
                        "type": a["type"],
                        "state": {
                            "instance": a["state"]["instance"],
                            "action_result": {"status": self.ya_result},
                        },
                    }
                    for a in device["actions"]
                ]
                return httpx.Response(
                    200, json={"status": "ok", "devices": [{"id": device["id"], "capabilities": caps}]}
                )
        return httpx.Response(404)


@pytest.fixture
def home(pctx: AppContext) -> Home:
    fake = Home()
    manager(pctx).http_transport = httpx.MockTransport(fake)
    pctx.secrets.set("ha_token", "ha-secret")
    pctx.secrets.set("yandex_iot_token", "ya-secret")
    pctx.settings.patch({"plugins": {"smart_home": {"config": {"ha_url": HA_URL}}}})
    return fake


def plugin_of(ctx: AppContext) -> SmartHome:
    plugin = manager(ctx).get("smart_home")
    assert isinstance(plugin, SmartHome)
    return plugin


async def test_devices_from_both_providers(pctx: AppContext, home: Home) -> None:
    plugin = plugin_of(pctx)
    devices = {d.id: d.to_json() for d in await plugin.refresh()}
    assert set(devices) == {
        "ha:light.kitchen",
        "ha:light.lamp",
        "ha:switch.kettle",
        "ha:climate.ac",
        "ha:sensor.bedroom_temp",
        "ha:cover.curtains",
        "ha:switch.broken",
        "yandex:ya-1",
        "yandex:ya-2",
        "yandex:ya-3",
    }
    kitchen = devices["ha:light.kitchen"]
    assert kitchen["name"] == "Люстра" and kitchen["room"] == "Кухня" and kitchen["kind"] == "light"
    assert kitchen["state"]["on"] is True and kitchen["state"]["brightness"] == 50
    assert "brightness" in kitchen["caps"] and kitchen["provider"] == "ha"
    assert "brightness" not in devices["ha:light.lamp"]["caps"]
    assert (
        devices["ha:climate.ac"]["state"] | {"temperature": 23, "current": 25.5}
        == devices["ha:climate.ac"]["state"]
    )
    assert devices["ha:climate.ac"]["kind"] == "climate" and devices["ha:climate.ac"]["room"] == "Гостиная"
    assert devices["ha:sensor.bedroom_temp"]["state"]["value"] == "21.4"
    assert devices["ha:sensor.bedroom_temp"]["state"]["unit"] == "°C"
    assert (
        devices["ha:cover.curtains"]["kind"] == "cover"
        and devices["ha:cover.curtains"]["state"]["on"] is False
    )
    assert devices["ha:switch.broken"]["state"]["available"] is False
    torch = devices["yandex:ya-1"]
    assert torch["room"] == "Гостиная" and torch["kind"] == "light" and torch["provider"] == "yandex"
    assert torch["state"]["on"] is False and torch["state"]["brightness"] == 40
    assert devices["yandex:ya-2"]["kind"] == "switch" and devices["yandex:ya-2"]["state"]["on"] is True
    assert devices["yandex:ya-3"]["kind"] == "sensor" and devices["yandex:ya-3"]["state"]["value"] == "22.5"
    status = plugin.status()
    assert status["configured"] is True and status["devices"] == 10
    assert status["providers"]["ha"]["ok"] and status["providers"]["yandex"]["ok"]


async def test_actions_call_provider_services(pctx: AppContext, home: Home) -> None:
    plugin = plugin_of(pctx)
    await plugin.refresh()
    result = await plugin.handle("toggle", {"id": "ha:light.kitchen"})
    assert home.calls[-1] == ("/api/services/light/turn_off", {"entity_id": "light.kitchen"})
    assert result["device"]["state"]["on"] is False  # состояние меняется сразу, не дожидаясь опроса
    await plugin.handle("set", {"id": "ha:light.kitchen", "brightness": 30})
    assert home.calls[-1] == (
        "/api/services/light/turn_on",
        {"entity_id": "light.kitchen", "brightness_pct": 30},
    )
    await plugin.handle("set", {"name": "кондиционер", "temperature": "21,5"})
    assert home.calls[-1] == (
        "/api/services/climate/set_temperature",
        {"entity_id": "climate.ac", "temperature": 21.5},
    )
    await plugin.handle("turn_on", {"id": "ha:cover.curtains"})
    assert home.calls[-1] == ("/api/services/cover/open_cover", {"entity_id": "cover.curtains"})

    await plugin.handle("turn_on", {"name": "торшер"})
    path, body = home.calls[-1]
    assert path == "/v1.0/devices/actions"
    assert body == {
        "devices": [
            {
                "id": "ya-1",
                "actions": [
                    {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}
                ],
            }
        ]
    }
    await plugin.handle("set", {"id": "yandex:ya-1", "brightness": 55})
    action = home.calls[-1][1]["devices"][0]["actions"][0]
    assert action == {"type": "devices.capabilities.range", "state": {"instance": "brightness", "value": 55}}

    with pytest.raises(UserFacingError, match="недоступно"):
        await plugin.handle("turn_on", {"id": "ha:switch.broken"})
    with pytest.raises(UserFacingError, match="яркость"):
        await plugin.handle("set", {"id": "ha:light.lamp", "brightness": 10})
    with pytest.raises(UserFacingError, match="Не нашёл"):
        await plugin.handle("toggle", {"name": "телепорт"})
    home.ya_result = "ERROR"
    with pytest.raises(UserFacingError, match="Умный дом Яндекса"):
        await plugin.handle("turn_off", {"id": "yandex:ya-2"})


async def test_voice_commands_from_device_names(pctx: AppContext, home: Home) -> None:
    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    await enable(pctx, "smart_home")
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["devices"] == 10)
    await wait_until(lambda: "включи люстру" in commands.phrases())
    phrases = commands.phrases()
    for expected in (
        "включи люстру",
        "выключи настольную лампу",
        "включи свет на кухне",
        "выключи свет в спальне",
        "включи свет в гостиной",
        "яркость люстра {number}",
        "поставь кондиционер на {number} градусов",
        "какая температура в спальне",
        "открой шторы",
        "включи торшер",
        "переключи чайник",
        "выключи весь свет",
    ):
        assert expected in phrases, expected

    result = await commands.by_phrase("выключи свет на кухне").handler({})
    assert result.ok and home.calls[-1] == ("/api/services/light/turn_off", {"entity_id": "light.kitchen"})
    result = await commands.by_phrase("яркость люстра {number}").handler({"number": 40})
    assert result.ok and home.calls[-1][1] == {"entity_id": "light.kitchen", "brightness_pct": 40}
    result = await commands.by_phrase("поставь кондиционер на {number}").handler({})
    assert result.ok is False and "число" in result.message
    result = await commands.by_phrase("какая температура в спальне").handler({})
    assert result.ok and result.message == "21.4 °C" and "градусов" in result.say
    result = await commands.by_phrase("включи весь свет").handler({})
    assert result.ok and "Светильников: 3" in result.message


async def test_provider_failures_are_isolated(pctx: AppContext, home: Home) -> None:
    plugin = plugin_of(pctx)
    await plugin.refresh()
    home.ha_down = True  # сбой сети — устройства Home Assistant остаются, ошибка видна в статусе
    devices = await plugin.refresh()
    assert any(d.provider == "ha" for d in devices) and len(devices) == 10
    status = plugin.status()["providers"]
    assert status["ha"]["ok"] is False and status["ha"]["error"] == "Нет связи с сервером"
    assert status["yandex"]["ok"] is True
    home.ha_down = False
    pctx.secrets.set("ha_token", "wrong")  # неверный токен — устройства Home Assistant пропадают
    devices = await plugin.refresh()
    assert {d.provider for d in devices} == {"yandex"}
    assert "токен не подходит" in plugin.status()["providers"]["ha"]["error"]
    assert "Home Assistant" in plugin.error


async def test_not_configured(pctx: AppContext) -> None:
    info = await enable(pctx, "smart_home")
    assert info["status"]["configured"] is False and "hint" in info["status"]
    assert await manager(pctx).action("smart_home", "states", {}) == {
        "ok": True,
        "devices": [],
        "providers": {},
        "updated": plugin_of(pctx).status()["updated"],
    }
