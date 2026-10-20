"""«Системные операции»: датчики (с паузой после неудачи), микшер громкости, фраза «открой системные операции»
и маршруты /api/system/*."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.services import UserFacingError
from jarvis.widgets import sysops
from jarvis.widgets.sysops import FAIL_RETRY_S, SysOps, parse_nvidia
from tests.conftest import EventLog


def test_parse_nvidia() -> None:
    assert parse_nvidia("61, NVIDIA GeForce RTX 4070, 37\n") == (61.0, "NVIDIA GeForce RTX 4070", 37.0)
    assert parse_nvidia("[N/A], NVIDIA T400, 5") == (None, "NVIDIA T400", 5.0)
    assert parse_nvidia("") == (None, "", None)


def test_top_processes_merges_same_names() -> None:
    rows = sysops.top_processes(limit=50)
    names = [r["name"].casefold() for r in rows]
    assert len(names) == len(set(names))
    assert all({"name", "cpu", "ram_mb"} <= set(r) for r in rows)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _real(ctx: AppContext) -> tuple[SysOps, Clock, dict[str, int]]:
    """Служба как на настоящем ПК (не fake): датчики — подставные, считают вызовы."""
    service = SysOps(ctx)
    service._fake = lambda: False  # type: ignore[method-assign]
    clock = Clock()
    service.clock = clock
    calls = {"gpu": 0, "cpu": 0}

    def gpu() -> tuple[float | None, str, float | None]:
        calls["gpu"] += 1
        raise FileNotFoundError("nvidia-smi")

    def cpu() -> float | None:
        calls["cpu"] += 1
        return 48.25

    service.gpu_probe = gpu
    service.cpu_probe = cpu
    service.top_probe = lambda: [{"name": "chrome.exe", "cpu": 7.5, "ram_mb": 900}]
    return service, clock, calls


async def test_sensors_cache_and_retry_pause(ctx: AppContext) -> None:
    service, clock, calls = _real(ctx)
    first = await service.sensors()
    assert first["cpu_temp"] == pytest.approx(48.25, abs=0.1)
    assert first["gpu_temp"] is None and first["gpu_name"] == ""
    assert first["top"][0]["name"] == "chrome.exe"
    assert calls == {"gpu": 1, "cpu": 1}

    # в пределах нескольких секунд — из кэша
    await service.sensors()
    assert calls == {"gpu": 1, "cpu": 1}

    # кэш устарел: процессор спрашиваем снова, видеокарту — нет (её нет, пауза после неудачи)
    clock.now += 10
    await service.sensors()
    assert calls == {"gpu": 1, "cpu": 2}

    clock.now += FAIL_RETRY_S
    await service.sensors()
    assert calls == {"gpu": 2, "cpu": 3}


async def test_sensors_fake_platform(ctx: AppContext) -> None:
    data = await SysOps(ctx).sensors()
    assert data["cpu_temp"] is not None and data["gpu_temp"] is not None
    assert data["top"] and data["updated"] > 0


async def test_mixer_and_app_volume(ctx: AppContext) -> None:
    service = SysOps(ctx)
    mixer = await service.mixer()
    assert mixer["master"] == {"volume": 40, "muted": False}
    apps = {i["app"]: i for i in mixer["items"]}
    assert apps["Spotify.exe"]["active"] is True

    after = await service.set_app_volume("chrome.exe", 33)
    chrome = next(i for i in after["items"] if i["app"] == "chrome.exe")
    assert chrome["level"] == 33
    assert ctx.platform.calls[-1] == ("app_volume_set", ("chrome.exe", 33))  # type: ignore[attr-defined]

    with pytest.raises(UserFacingError):
        await service.set_app_volume("  ", 10)


async def test_app_volume_without_sound(ctx: AppContext) -> None:
    service = SysOps(ctx)
    ctx.platform.app_volume_set = lambda app, level: False  # type: ignore[method-assign]
    with pytest.raises(UserFacingError, match="без звука"):
        await service.set_app_volume("notepad.exe", 50)


async def test_voice_phrase_opens_panel(running_ctx: AppContext) -> None:
    events = EventLog()
    running_ctx.bus.on("ui.show", events)
    match = running_ctx.commands.match("открой системные операции")
    assert match is not None
    assert match.parts[0].command_id == "sysops:open"
    result = await running_ctx.commands.run_text("покажи панель системы", "voice")
    assert result is not None and result.ok
    assert events.of("ui.show")[-1] == {"page": "system"}
    # «микшер» — встроенная команда (микшер Windows), её не перехватываем
    mixer = running_ctx.commands.match("микшер")
    assert mixer is None or mixer.parts[0].command_id != "sysops:open"


def test_api(client: TestClient) -> None:
    r = client.get("/api/system/mixer")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert "master" in body and body["items"]

    r = client.post("/api/system/mixer", json={"app": "Discord.exe", "level": 20})
    assert r.status_code == 200
    assert next(i for i in r.json()["items"] if i["app"] == "Discord.exe")["level"] == 20

    assert client.post("/api/system/mixer", json={"app": "Discord.exe", "level": 150}).status_code == 422
    assert client.post("/api/system/mixer", json={"app": " ", "level": 10}).status_code == 400

    r = client.get("/api/system/sensors")
    assert r.status_code == 200
    assert {"cpu_temp", "gpu_temp", "gpu_name", "top", "updated"} <= set(r.json())
