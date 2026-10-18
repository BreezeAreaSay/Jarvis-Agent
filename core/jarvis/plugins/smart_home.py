"""«Умный дом»: устройства Home Assistant и Умного дома Яндекса — список, включение, яркость, температура.

Home Assistant: адрес (настройка ha_url) и долгосрочный токен (секрет ha_token) — REST API /api/states и
/api/services/<домен>/<служба>; комнаты — одним запросом /api/template (area_name).
Яндекс: OAuth-токен (секрет yandex_iot_token) с правами iot:view и iot:control — https://api.iot.yandex.net.

Список устройств обновляется раз в 30 с (и вскоре после каждой команды). Из названий устройств и комнат
собираются голосовые команды: «включи люстру», «выключи свет на кухне», «яркость торшера на 40»,
«поставь кондиционер на 22», «какая температура в спальне».
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from rapidfuzz import fuzz, process

from jarvis.bus import Event
from jarvis.plugins import ru
from jarvis.plugins.base import ConfigField, Plugin, PluginAction, error_text, spoken_name, unique
from jarvis.services import DynamicCommand, ExecResult, UserFacingError

log = logging.getLogger(__name__)

DeviceKind = Literal["light", "switch", "climate", "cover", "media", "sensor", "other"]
Op = Literal["on", "off", "toggle", "brightness", "temperature"]

REFRESH_S = 30.0
AFTER_ACTION_REFRESH_S = 2.0
AREAS_TTL_S = 300.0
YANDEX_API = "https://api.iot.yandex.net/v1.0"
SECRET_NAMES = ("ha_token", "yandex_iot_token")

# --- Home Assistant ---------------------------------------------------------------------------------------

HA_KINDS: dict[str, DeviceKind] = {
    "light": "light",
    "switch": "switch",
    "input_boolean": "switch",
    "fan": "switch",
    "humidifier": "switch",
    "climate": "climate",
    "water_heater": "climate",
    "cover": "cover",
    "media_player": "media",
    "sensor": "sensor",
    "binary_sensor": "sensor",
    "scene": "other",
    "script": "other",
    "vacuum": "other",
}
HA_ON_OFF = (
    "light",
    "switch",
    "input_boolean",
    "fan",
    "humidifier",
    "climate",
    "water_heater",
    "media_player",
)
HA_SENSOR_CLASSES = frozenset(
    {"temperature", "humidity", "carbon_dioxide", "pm25", "pm10", "illuminance", "power", "pressure"}
)
HA_BINARY_CLASSES = frozenset(
    {
        "door",
        "window",
        "opening",
        "garage_door",
        "motion",
        "occupancy",
        "presence",
        "moisture",
        "smoke",
        "gas",
    }
)
HA_AREAS_TEMPLATE = "{% for s in states %}{{ s.entity_id }}\t{{ area_name(s.entity_id) or '' }}\n{% endfor %}"

# --- Яндекс -----------------------------------------------------------------------------------------------

YA_ON_OFF = "devices.capabilities.on_off"
YA_RANGE = "devices.capabilities.range"
YA_UNITS = {
    "unit.temperature.celsius": "°C",
    "unit.temperature.kelvin": "K",
    "unit.percent": "%",
    "unit.ppm": "ppm",
    "unit.pressure.mmhg": "мм рт. ст.",
    "unit.pressure.atm": "атм",
    "unit.pressure.pascal": "Па",
    "unit.pressure.bar": "бар",
    "unit.illumination.lux": "лк",
    "unit.watt": "Вт",
    "unit.volt": "В",
    "unit.ampere": "А",
    "unit.kilowatt_hour": "кВт·ч",
    "unit.density.mcg_m3": "мкг/м³",
}
YA_EVENTS = {
    "opened": "открыто",
    "closed": "закрыто",
    "detected": "движение",
    "not_detected": "нет движения",
    "leak": "протечка",
    "dry": "сухо",
    "smoke": "дым",
    "gas": "газ",
}

OP_TITLES = {
    "on": "Включить",
    "off": "Выключить",
    "toggle": "Переключить",
    "brightness": "Яркость",
    "temperature": "Температура",
}


@dataclass
class Device:
    """Устройство умного дома в общем виде для интерфейса, голоса и ИИ."""

    id: str  # "ha:light.kitchen" | "yandex:<uuid>"
    name: str
    room: str
    kind: DeviceKind
    provider: Literal["ha", "yandex"]
    native: str  # entity_id в Home Assistant или id устройства в Яндексе
    domain: str = ""  # HA: light, switch…; Яндекс: devices.types.light…
    caps: frozenset[str] = frozenset()  # on_off, open, activate, brightness, temperature
    state: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "room": self.room,
            "kind": self.kind,
            "state": dict(self.state),
            "provider": self.provider,
            "caps": sorted(self.caps),
        }


def _check(response: httpx.Response, title: str) -> None:
    if response.status_code in (401, 403):
        raise UserFacingError(f"{title}: токен не подходит — проверьте его в настройках плагина")
    if response.status_code == 404:
        raise UserFacingError(f"{title}: адрес не найден — проверьте настройки плагина")
    if response.status_code >= 400:
        detail = " ".join(response.text.split())[:160]
        raise UserFacingError(f"{title}: ошибка {response.status_code}" + (f" — {detail}" if detail else ""))


def _json(response: httpx.Response, title: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise UserFacingError(f"{title}: сервер ответил не JSON") from None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ".").replace("%", "").strip())
    except ValueError:
        return None


class HomeAssistant:
    provider = "ha"
    title = "Home Assistant"

    def __init__(self, url: str, token: str) -> None:
        self.url = self.base_url(url)
        self.token = token
        self._areas: dict[str, str] = {}
        self._areas_at = -AREAS_TTL_S

    @staticmethod
    def base_url(url: str) -> str:
        """«ha.local:8123/api/» → «http://ha.local:8123»."""
        url = url.strip().rstrip("/")
        if url.endswith("/api"):
            url = url[:-4]
        if url and "://" not in url:
            url = "http://" + url
        return url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def fetch(self, client: httpx.AsyncClient, sensors: bool) -> list[Device]:
        response = await client.get(f"{self.url}/api/states", headers=self._headers())
        _check(response, self.title)
        states = _json(response, self.title)
        if not isinstance(states, list):
            raise UserFacingError(f"{self.title}: неожиданный ответ сервера")
        areas = await self._load_areas(client)
        devices = []
        for item in states:
            if isinstance(item, dict):
                device = self._device(item, areas, sensors)
                if device is not None:
                    devices.append(device)
        return devices

    async def _load_areas(self, client: httpx.AsyncClient) -> dict[str, str]:
        if time.monotonic() - self._areas_at < AREAS_TTL_S:
            return self._areas
        self._areas_at = time.monotonic()
        try:
            response = await client.post(
                f"{self.url}/api/template", headers=self._headers(), json={"template": HA_AREAS_TEMPLATE}
            )
        except httpx.HTTPError as e:
            log.info("Home Assistant: комнаты не получены (%s)", e)
            return self._areas
        if response.status_code == 200:
            areas = {}
            for line in response.text.splitlines():
                entity_id, _, area = line.partition("\t")
                if entity_id.strip() and area.strip():
                    areas[entity_id.strip()] = area.strip()
            self._areas = areas
        return self._areas

    def _device(self, item: dict[str, Any], areas: dict[str, str], sensors: bool) -> Device | None:
        entity_id = str(item.get("entity_id") or "")
        domain = entity_id.partition(".")[0]
        kind = HA_KINDS.get(domain)
        if kind is None:
            return None
        attrs = item.get("attributes") or {}
        raw = str(item.get("state") or "")
        device_class = str(attrs.get("device_class") or "")
        if domain == "sensor" and (not sensors or device_class not in HA_SENSOR_CLASSES):
            return None
        if domain == "binary_sensor" and (not sensors or device_class not in HA_BINARY_CLASSES):
            return None
        available = raw not in ("unavailable", "unknown", "")
        state: dict[str, Any] = {"on": _ha_on(domain, raw) if available else None, "available": available}
        caps: set[str] = set()
        if domain in HA_ON_OFF or domain == "vacuum":
            caps.add("on_off")
        if domain == "cover":
            caps.add("open")
            if attrs.get("current_position") is not None:
                state["position"] = attrs.get("current_position")
        if domain in ("scene", "script"):
            caps.add("activate")
        if domain == "light":
            modes = attrs.get("supported_color_modes")
            dimmable = any(m != "onoff" for m in modes) if isinstance(modes, list) else "brightness" in attrs
            if dimmable:
                caps.add("brightness")
            if attrs.get("brightness") is not None:
                state["brightness"] = round(int(attrs["brightness"]) * 100 / 255)
        if domain in ("climate", "water_heater"):
            caps.add("temperature")
            if attrs.get("temperature") is not None:
                state["temperature"] = attrs.get("temperature")
            if attrs.get("current_temperature") is not None:
                state["current"] = attrs.get("current_temperature")
            state["mode"] = raw
        if domain == "media_player":
            if attrs.get("media_title"):
                state["title"] = attrs.get("media_title")
            if attrs.get("volume_level") is not None:
                state["volume"] = round(float(attrs["volume_level"]) * 100)
        if kind == "sensor":
            state["value"] = raw
            if attrs.get("unit_of_measurement"):
                state["unit"] = attrs.get("unit_of_measurement")
        return Device(
            id=f"ha:{entity_id}",
            name=str(attrs.get("friendly_name") or entity_id),
            room=areas.get(entity_id, ""),
            kind=kind,
            provider="ha",
            native=entity_id,
            domain=domain,
            caps=frozenset(caps),
            state=state,
        )

    async def call(self, client: httpx.AsyncClient, device: Device, op: Op, value: float | None) -> None:
        domain, service, data = _ha_service(device, op, value)
        response = await client.post(
            f"{self.url}/api/services/{domain}/{service}", headers=self._headers(), json=data
        )
        _check(response, self.title)


def _ha_on(domain: str, raw: str) -> bool | None:
    if domain in ("light", "switch", "input_boolean", "fan", "humidifier", "script", "binary_sensor"):
        return raw == "on"
    if domain in ("climate", "water_heater"):
        return raw != "off"
    if domain == "media_player":
        return raw not in ("off", "standby")
    if domain == "cover":
        return raw in ("open", "opening")
    if domain == "vacuum":
        return raw == "cleaning"
    return None


def _ha_service(device: Device, op: Op, value: float | None) -> tuple[str, str, dict[str, Any]]:
    domain = device.domain
    data: dict[str, Any] = {"entity_id": device.native}
    if op == "toggle":
        op = "off" if device.state.get("on") else "on"
    if op == "brightness":
        level = round(value or 0)
        if level <= 0:
            return "light", "turn_off", data
        return "light", "turn_on", {**data, "brightness_pct": min(100, level)}
    if op == "temperature":
        return domain, "set_temperature", {**data, "temperature": value}
    if domain == "cover":
        return "cover", "open_cover" if op == "on" else "close_cover", data
    if domain in ("scene", "script"):
        if op != "on":
            raise UserFacingError(f"«{device.name}» можно только запустить")
        return domain, "turn_on", data
    if domain == "vacuum":
        return "vacuum", "start" if op == "on" else "return_to_base", data
    return domain, "turn_on" if op == "on" else "turn_off", data


class YandexHome:
    provider = "yandex"
    title = "Умный дом Яндекса"

    def __init__(self, token: str) -> None:
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def fetch(self, client: httpx.AsyncClient, sensors: bool) -> list[Device]:
        response = await client.get(f"{YANDEX_API}/user/info", headers=self._headers())
        _check(response, self.title)
        data = _json(response, self.title)
        if not isinstance(data, dict) or data.get("status", "ok") != "ok":
            message = data.get("message") if isinstance(data, dict) else ""
            raise UserFacingError(f"{self.title}: {message or 'ошибка ответа'}")
        rooms = {
            str(r.get("id")): str(r.get("name") or "") for r in data.get("rooms") or [] if isinstance(r, dict)
        }
        devices = []
        for item in data.get("devices") or []:
            if isinstance(item, dict):
                device = _ya_device(item, rooms)
                if device is not None and (sensors or device.kind != "sensor"):
                    devices.append(device)
        return devices

    async def call(self, client: httpx.AsyncClient, device: Device, op: Op, value: float | None) -> None:
        body = {"devices": [{"id": device.native, "actions": _ya_actions(device, op, value)}]}
        response = await client.post(f"{YANDEX_API}/devices/actions", headers=self._headers(), json=body)
        _check(response, self.title)
        data = _json(response, self.title)
        for item in (data.get("devices") or []) if isinstance(data, dict) else []:
            for cap in item.get("capabilities") or []:
                result = ((cap.get("state") or {}).get("action_result")) or {}
                if result.get("status") == "ERROR":
                    reason = result.get("error_message") or result.get("error_code") or "команда не выполнена"
                    raise UserFacingError(f"{self.title}: {reason}")


def _ya_kind(device_type: str) -> DeviceKind:
    if device_type.startswith("devices.types.light"):
        return "light"
    if device_type.startswith(("devices.types.socket", "devices.types.switch")):
        return "switch"
    if device_type.startswith("devices.types.thermostat"):
        return "climate"
    if device_type.startswith(
        ("devices.types.humidifier", "devices.types.purifier", "devices.types.ventilation")
    ):
        return "switch"
    if device_type.startswith("devices.types.openable"):
        return "cover"
    if device_type.startswith("devices.types.media_device"):
        return "media"
    if device_type.startswith("devices.types.sensor"):
        return "sensor"
    return "other"


def _ya_device(item: dict[str, Any], rooms: dict[str, str]) -> Device | None:
    device_id = str(item.get("id") or "")
    if not device_id:
        return None
    device_type = str(item.get("type") or "")
    kind = _ya_kind(device_type)
    caps: set[str] = set()
    state: dict[str, Any] = {"on": None, "available": str(item.get("state") or "online") != "offline"}
    for cap in item.get("capabilities") or []:
        cap_type = cap.get("type")
        cap_state = cap.get("state") or {}
        instance = cap_state.get("instance") or (cap.get("parameters") or {}).get("instance")
        value = cap_state.get("value")
        if cap_type == YA_ON_OFF:
            caps.add("on_off")
            if isinstance(value, bool):
                state["on"] = value
        elif cap_type == YA_RANGE and instance == "brightness":
            caps.add("brightness")
            if value is not None:
                state["brightness"] = value
        elif cap_type == YA_RANGE and instance == "temperature":
            caps.add("temperature")
            if value is not None:
                state["temperature"] = value
        elif cap_type == YA_RANGE and instance == "open":
            caps.add("open")
            if value is not None:
                state["position"] = value
    for prop in item.get("properties") or []:
        params = prop.get("parameters") or {}
        prop_state = prop.get("state") or {}
        instance = params.get("instance") or prop_state.get("instance")
        value = prop_state.get("value")
        if value is None:
            continue
        if instance == "temperature" and kind == "climate":
            state["current"] = value
        if kind == "sensor" and "value" not in state:
            state["value"] = YA_EVENTS.get(str(value), str(value))
            unit = YA_UNITS.get(str(params.get("unit") or ""), "")
            if unit:
                state["unit"] = unit
    if kind == "cover" and "on_off" in caps:
        caps.add("open")
    return Device(
        id=f"yandex:{device_id}",
        name=str(item.get("name") or "Устройство"),
        room=rooms.get(str(item.get("room") or ""), ""),
        kind=kind,
        provider="yandex",
        native=device_id,
        domain=device_type,
        caps=frozenset(caps),
        state=state,
    )


def _ya_actions(device: Device, op: Op, value: float | None) -> list[dict[str, Any]]:
    if op == "toggle":
        op = "off" if device.state.get("on") else "on"
    if op in ("on", "off"):
        turn_on = op == "on"
        if "on_off" in device.caps:
            return [{"type": YA_ON_OFF, "state": {"instance": "on", "value": turn_on}}]
        if "open" in device.caps:
            return [{"type": YA_RANGE, "state": {"instance": "open", "value": 100 if turn_on else 0}}]
        raise UserFacingError(f"«{device.name}» нельзя включить или выключить")
    if op == "brightness":
        level = round(value or 0)
        if level <= 0:
            return [{"type": YA_ON_OFF, "state": {"instance": "on", "value": False}}]
        return [{"type": YA_RANGE, "state": {"instance": "brightness", "value": min(100, level)}}]
    return [{"type": YA_RANGE, "state": {"instance": "temperature", "value": value}}]


Provider = HomeAssistant | YandexHome


class SmartHome(Plugin):
    id = "smart_home"
    name = "Умный дом"
    description = (
        "Свет, розетки, климат и шторы из Home Assistant и Умного дома Яндекса — голосом и с плитки."
    )
    icon = "home"
    color = ("#34D399", "#059669")
    fields = (
        ConfigField(
            "ha_url",
            "text",
            "Адрес Home Assistant",
            default="",
            placeholder="http://homeassistant.local:8123",
        ),
        ConfigField(
            "ha_token",
            "secret",
            "Токен Home Assistant",
            secret="ha_token",
            hint="Профиль → Безопасность → Долгосрочные токены доступа",
        ),
        ConfigField(
            "yandex_token",
            "secret",
            "Токен Умного дома Яндекса",
            secret="yandex_iot_token",
            hint="OAuth-токен Яндекса с правами iot:view и iot:control",
        ),
        ConfigField("sensors", "bool", "Показывать датчики", default=True),
    )
    actions = (PluginAction("refresh", "Обновить устройства"),)
    examples = ("включи свет на кухне", "выключи торшер", "поставь кондиционер на 22")

    def __init__(self, ctx: Any, manager: Any) -> None:
        super().__init__(ctx, manager)
        self._devices: dict[str, Device] = {}
        self._providers: dict[str, dict[str, Any]] = {}
        self._updated = 0.0
        self._wake: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._ha: HomeAssistant | None = None
        self._refresh_timer: asyncio.TimerHandle | None = None
        self._commands_key: tuple[Any, ...] = ()
        self._status_key: Any = None

    # --- жизненный цикл ------------------------------------------------------------------------------
    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._unsubscribe = self.ctx.bus.on("secrets.changed", self._on_secret)
        self.spawn(self._loop(), "refresh")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        self._devices = {}
        self._providers = {}
        self._commands_key = ()
        self._status_key = None
        self._updated = 0.0

    async def on_config(self, config: dict[str, Any], old: dict[str, Any]) -> None:
        self.wake()

    def _on_secret(self, event: Event) -> None:
        if event.data.get("name") in SECRET_NAMES:
            self.wake()

    def wake(self) -> None:
        """Обновить список устройств сейчас (из цикла ядра)."""
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        assert self._wake is not None
        while True:
            self._wake.clear()
            try:
                await self.refresh()
            except Exception:
                log.exception("умный дом: обновление упало")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(REFRESH_S):
                    await self._wake.wait()

    # --- провайдеры ----------------------------------------------------------------------------------
    def _configured(self) -> list[Provider]:
        providers: list[Provider] = []
        url = str(self.config.get("ha_url") or "").strip()
        ha_token = self.secret("ha_token")
        if url and ha_token:
            if self._ha is None or self._ha.token != ha_token or self._ha.url != HomeAssistant.base_url(url):
                self._ha = HomeAssistant(url, ha_token)
            providers.append(self._ha)
        else:
            self._ha = None
        yandex_token = self.secret("yandex_iot_token")
        if yandex_token:
            providers.append(YandexHome(yandex_token))
        return providers

    def _provider_for(self, device: Device) -> Provider:
        for provider in self._configured():
            if provider.provider == device.provider:
                return provider
        raise UserFacingError("Этот умный дом больше не подключён — проверьте настройки плагина")

    async def refresh(self) -> list[Device]:
        """Перечитать устройства у всех подключённых провайдеров."""
        lock = self._lock or asyncio.Lock()
        async with lock:
            providers = self._configured()
            sensors = bool(self.config.get("sensors", True))
            devices: dict[str, Device] = {}
            statuses: dict[str, dict[str, Any]] = {}
            if providers:
                async with self.http(timeout=10.0) as client:
                    results = await asyncio.gather(
                        *(p.fetch(client, sensors) for p in providers), return_exceptions=True
                    )
                for provider, result in zip(providers, results, strict=True):
                    if isinstance(result, BaseException):
                        if isinstance(result, asyncio.CancelledError):
                            raise result
                        if not isinstance(result, UserFacingError | httpx.HTTPError):
                            log.error("умный дом: %s упал", provider.provider, exc_info=result)
                        statuses[provider.provider] = {"ok": False, "error": error_text(result), "count": 0}
                        if isinstance(result, httpx.HTTPError):
                            # сбой сети — оставить известные устройства, чтобы голосовые команды не пропадали
                            for device in self._devices.values():
                                if device.provider == provider.provider:
                                    devices[device.id] = device
                    else:
                        statuses[provider.provider] = {"ok": True, "error": "", "count": len(result)}
                        for device in result:
                            devices[device.id] = device
            self._apply(devices, statuses)
            return list(devices.values())

    def _apply(self, devices: dict[str, Device], statuses: dict[str, dict[str, Any]]) -> None:
        before = [d.to_json() for d in self._devices.values()]
        self._devices = devices
        self._providers = statuses
        self._updated = time.time()
        after = [d.to_json() for d in devices.values()]
        if after != before:
            self.publish("devices", {"devices": after})
        commands_key = tuple(
            sorted((d.id, d.name, d.room, d.kind, tuple(sorted(d.caps))) for d in devices.values())
        )
        if commands_key != self._commands_key:
            self._commands_key = commands_key
            self.commands_changed()
        errors = [s["error"] for s in statuses.values() if not s["ok"]]
        self.error = "; ".join(errors)
        status_key = (len(devices), tuple(sorted((k, v["ok"], v["error"]) for k, v in statuses.items())))
        if status_key != self._status_key:
            self._status_key = status_key
            self.changed()

    def _schedule_refresh(self, delay: float = AFTER_ACTION_REFRESH_S) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        self._refresh_timer = asyncio.get_running_loop().call_later(delay, self.wake)

    # --- управление ----------------------------------------------------------------------------------
    def devices(self) -> list[Device]:
        return list(self._devices.values())

    def find(self, args: dict[str, Any]) -> Device:
        """Устройство по id (точно) или по названию (нечётко)."""
        device_id = str(args.get("id") or "")
        if device_id:
            device = self._devices.get(device_id)
            if device is None:
                raise UserFacingError("Устройство не найдено — обновите список")
            return device
        name = str(args.get("name") or "").strip()
        if not name:
            raise UserFacingError("Не указано устройство")
        choices = {d.id: spoken_name(d.name) for d in self._devices.values()}
        best = process.extractOne(spoken_name(name), choices, scorer=fuzz.WRatio, score_cutoff=75)
        if best is None:
            raise UserFacingError(f"Не нашёл устройство «{name}»")
        return self._devices[best[2]]

    async def operate(self, device: Device, op: Op, value: float | None = None) -> Device:
        if op in ("on", "off", "toggle") and not device.caps & {"on_off", "open", "activate"}:
            raise UserFacingError(f"«{device.name}» нельзя включить или выключить")
        if op == "brightness" and "brightness" not in device.caps:
            raise UserFacingError(f"У «{device.name}» не меняется яркость")
        if op == "temperature" and "temperature" not in device.caps:
            raise UserFacingError(f"У «{device.name}» не задаётся температура")
        if op in ("brightness", "temperature") and value is None:
            raise UserFacingError("Не указано значение")
        if not device.state.get("available", True):
            raise UserFacingError(f"«{device.name}» сейчас недоступно")
        provider = self._provider_for(device)
        async with self.http(timeout=10.0) as client:
            await provider.call(client, device, op, value)
        self._optimistic(device, op, value)
        with contextlib.suppress(RuntimeError):
            self._schedule_refresh()
        return device

    def _optimistic(self, device: Device, op: Op, value: float | None) -> None:
        state = dict(device.state)
        if op == "toggle":
            state["on"] = not state.get("on")
        elif op in ("on", "off"):
            state["on"] = op == "on"
        elif op == "brightness" and value is not None:
            state["on"] = value > 0
            if value > 0:
                state["brightness"] = round(min(100.0, value))
        elif op == "temperature" and value is not None:
            state["temperature"] = value
        device.state = state
        self.publish("devices", {"devices": [d.to_json() for d in self._devices.values()]})

    # --- статус и действия ---------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        configured = bool(self._configured_flags())
        data: dict[str, Any] = {
            "configured": configured,
            "devices": len(self._devices),
            "providers": self._providers,
            "updated": self._updated,
        }
        if not configured:
            data["hint"] = "Укажите адрес и токен Home Assistant или токен Умного дома Яндекса"
        return data

    def _configured_flags(self) -> list[str]:
        flags = []
        if str(self.config.get("ha_url") or "").strip() and self.secret("ha_token"):
            flags.append("ha")
        if self.secret("yandex_iot_token"):
            flags.append("yandex")
        return flags

    def _states(self) -> dict[str, Any]:
        return {
            "devices": [d.to_json() for d in self._devices.values()],
            "providers": self._providers,
            "updated": self._updated,
        }

    async def act_states(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("refresh") or not self._updated:
            await self.refresh()
        return self._states()

    async def act_refresh(self, args: dict[str, Any]) -> dict[str, Any]:
        await self.refresh()
        return self._states()

    async def act_toggle(self, args: dict[str, Any]) -> dict[str, Any]:
        device = await self.operate(self.find(args), "toggle")
        return {"device": device.to_json()}

    async def act_turn_on(self, args: dict[str, Any]) -> dict[str, Any]:
        device = await self.operate(self.find(args), "on")
        return {"device": device.to_json()}

    async def act_turn_off(self, args: dict[str, Any]) -> dict[str, Any]:
        device = await self.operate(self.find(args), "off")
        return {"device": device.to_json()}

    async def act_set(self, args: dict[str, Any]) -> dict[str, Any]:
        """{id | name, on?, brightness?: 0..100, temperature?}"""
        device = self.find(args)
        done = False
        if "on" in args:
            await self.operate(device, "on" if args["on"] else "off")
            done = True
        brightness = _number(args.get("brightness"))
        if brightness is not None:
            await self.operate(device, "brightness", brightness)
            done = True
        temperature = _number(args.get("temperature"))
        if temperature is not None:
            await self.operate(device, "temperature", temperature)
            done = True
        if not done:
            raise UserFacingError("Нечего менять: укажите on, brightness или temperature")
        return {"device": device.to_json()}

    # --- голосовые команды ---------------------------------------------------------------------------
    def commands(self) -> list[DynamicCommand]:
        out: list[DynamicCommand] = []
        rooms: dict[str, str] = {}
        has_lights = False
        for device in self._devices.values():
            name = spoken_name(device.name)
            if not name:
                continue
            forms = unique([ru.accusative(name), name])
            if device.kind == "light" and "on_off" in device.caps:
                has_lights = True
                if device.room:
                    rooms.setdefault(spoken_name(device.room), device.room)
            out.extend(self._device_commands(device, name, forms))
        for spoken, room in rooms.items():
            where = ru.place(spoken)
            out.append(
                self.command(
                    f"room_on:{room}",
                    f"Свет {where}: включить",
                    [f"включи свет {where}", f"зажги свет {where}", f"включи свет {spoken}"],
                    lambda slots, r=room: self._run_lights("on", r),
                )
            )
            out.append(
                self.command(
                    f"room_off:{room}",
                    f"Свет {where}: выключить",
                    [f"выключи свет {where}", f"погаси свет {where}", f"выключи свет {spoken}"],
                    lambda slots, r=room: self._run_lights("off", r),
                )
            )
        if has_lights:
            out.append(
                self.command(
                    "all_on",
                    "Включить весь свет",
                    ["включи весь свет", "включи свет везде", "включи свет во всем доме"],
                    lambda slots: self._run_lights("on", None),
                )
            )
            out.append(
                self.command(
                    "all_off",
                    "Выключить весь свет",
                    [
                        "выключи весь свет",
                        "выключи свет везде",
                        "погаси весь свет",
                        "выключи свет во всем доме",
                    ],
                    lambda slots: self._run_lights("off", None),
                )
            )
        return out

    def _device_commands(self, device: Device, name: str, forms: list[str]) -> list[DynamicCommand]:
        did = device.id
        out: list[DynamicCommand] = []

        def add(op: str, title: str, phrases: list[str]) -> None:
            out.append(
                self.command(
                    f"{op}:{did}", title, phrases, lambda slots, o=op: self._run_device(did, o, slots)
                )
            )

        if device.kind == "cover":
            add(
                "on",
                f"Открыть «{device.name}»",
                [f"{verb} {f}" for verb in ("открой", "подними") for f in forms],
            )
            add(
                "off",
                f"Закрыть «{device.name}»",
                [f"{verb} {f}" for verb in ("закрой", "опусти") for f in forms],
            )
        elif "on_off" in device.caps:
            on_verbs = ("включи", "зажги") if device.kind == "light" else ("включи",)
            off_verbs = ("выключи", "погаси") if device.kind == "light" else ("выключи",)
            add("on", f"Включить «{device.name}»", [f"{v} {f}" for v in on_verbs for f in forms])
            add("off", f"Выключить «{device.name}»", [f"{v} {f}" for v in off_verbs for f in forms])
            add("toggle", f"Переключить «{device.name}»", [f"переключи {f}" for f in forms])
        elif "activate" in device.caps:
            phrases = [f"{v} {f}" for v in ("включи", "запусти", "активируй") for f in forms]
            add("on", f"Запустить «{device.name}»", [*phrases, f"включи сцену {name}"])
        if "brightness" in device.caps:
            acc = forms[0]
            add(
                "brightness",
                f"Яркость «{device.name}»",
                [
                    f"яркость {name} {{number}}",
                    f"яркость {name} на {{number}}",
                    f"установи яркость {name} на {{number}}",
                    f"сделай {acc} на {{number}} процентов",
                ],
            )
        if "temperature" in device.caps:
            acc = forms[0]
            add(
                "temperature",
                f"Температура «{device.name}»",
                [
                    f"поставь {acc} на {{number}}",
                    f"поставь {acc} на {{number}} градусов",
                    f"температура {name} {{number}}",
                    f"установи температуру {name} на {{number}}",
                ],
            )
        if device.kind == "sensor":
            add("query", f"Датчик «{device.name}»", [f"какая {name}", f"какой {name}", f"покажи {name}"])
        return out

    async def _run_device(self, device_id: str, op: str, slots: dict[str, Any]) -> ExecResult:
        device = self._devices.get(device_id)
        if device is None:
            return ExecResult(False, "Умный дом", "Устройство больше не найдено", icon="home")
        if op == "query":
            value = device.state.get("value", "—")
            unit = str(device.state.get("unit") or "")
            shown = f"{value} {unit}".strip()
            spoken = shown.replace("°C", "градусов").replace("%", "процентов")
            return ExecResult(True, device.name, shown, icon="home", say=f"{device.name}: {spoken}")
        value = _number(slots.get("number"))
        if op in ("brightness", "temperature") and value is None:
            return ExecResult(False, OP_TITLES[op], "Не расслышал число", icon="home")
        await self.operate(device, op, value)  # type: ignore[arg-type]
        if op == "brightness":
            return ExecResult(True, f"Яркость «{device.name}»", f"{int(value or 0)}%", icon="home")
        if op == "temperature":
            return ExecResult(True, f"Температура «{device.name}»", f"{value:g}°", icon="home")
        if device.kind == "cover":
            title = f"{'Открыть' if op == 'on' else 'Закрыть'} «{device.name}»"
        else:
            title = f"{OP_TITLES[op]} «{device.name}»"
        if op == "toggle":
            return ExecResult(True, title, "Включено" if device.state.get("on") else "Выключено", icon="home")
        return ExecResult(True, title, "Выполнено", icon="home")

    async def _run_lights(self, op: Op, room: str | None) -> ExecResult:
        lights = [
            d
            for d in self._devices.values()
            if d.kind == "light" and "on_off" in d.caps and (room is None or d.room == room)
        ]
        where = ru.place(spoken_name(room)) if room else "во всём доме"
        title = f"Свет {where}: {'включить' if op == 'on' else 'выключить'}"
        if not lights:
            return ExecResult(False, title, "Светильники не найдены", icon="home")
        results = await asyncio.gather(*(self.operate(d, op) for d in lights), return_exceptions=True)
        failed = [d.name for d, r in zip(lights, results, strict=True) if isinstance(r, BaseException)]
        if len(failed) == len(lights):
            first = next(r for r in results if isinstance(r, BaseException))
            return ExecResult(False, title, error_text(first), icon="home")
        if failed:
            return ExecResult(True, title, "Не ответили: " + ", ".join(failed), icon="home")
        return ExecResult(True, title, f"Светильников: {len(lights)}", icon="home")
