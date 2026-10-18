"""«Второй компьютер»: пароли в секретах, опрос пульта и пересылка фраз (сеть — httpx.MockTransport)."""

import json
from typing import Any

import httpx
import pytest

from jarvis.context import AppContext
from jarvis.plugins.second_pc import SECRET_PREFIX, SecondPc, normalize_url
from jarvis.services import UserFacingError
from tests.plugins.conftest import FakeCommands, enable, manager, wait_until

STATE = {
    "pc_name": "LAPTOP-ANNA",
    "assistant": "Джарвис",
    "version": "2.0.0",
    "volume": 35,
    "muted": False,
    "media": {
        "title": "Night Drive",
        "artist": "Neon Coast",
        "app": "Spotify",
        "status": "playing",
        "thumb": "",
    },
}


class Lan:
    """Пульт Jarvis на ноутбуке (192.168.1.20) и выключенный игровой ПК (192.168.1.21)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply: dict[str, Any] = {
            "kind": "command",
            "ok": True,
            "title": "Включить музыку",
            "message": "Выполнено",
            "text": "",
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "192.168.1.21":
            raise httpx.ConnectError("нет маршрута", request=request)
        if request.headers.get("x-remote-token") != "pc-secret":
            return httpx.Response(401, json={"detail": "Нет доступа"})
        if request.url.path == "/remote/api/state":
            return httpx.Response(200, json=STATE)
        if request.url.path == "/remote/api/dispatch":
            return httpx.Response(200, json=self.reply)
        return httpx.Response(404)

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


PCS = [
    {"name": "Ноутбук", "url": "192.168.1.20", "token": "pc-secret"},
    {"name": "Игровой ПК", "url": "http://192.168.1.21:9000/remote", "token": ""},
]


@pytest.fixture
def lan(pctx: AppContext) -> Lan:
    fake = Lan()
    manager(pctx).http_transport = httpx.MockTransport(fake)
    return fake


def plugin_of(ctx: AppContext) -> SecondPc:
    plugin = manager(ctx).get("second_pc")
    assert isinstance(plugin, SecondPc)
    return plugin


def test_normalize_url() -> None:
    assert normalize_url("192.168.1.20") == "http://192.168.1.20:8766"
    assert normalize_url("https://pc.local:9000/remote/") == "https://pc.local:9000"
    assert normalize_url("  ") == ""


async def test_passwords_go_to_secrets(pctx: AppContext) -> None:
    plugins = manager(pctx)
    info = await plugins.update("second_pc", config={"pcs": PCS})
    pcs = info["config"]["pcs"]
    assert [(p["name"], p["url"]) for p in pcs] == [
        ("Ноутбук", "http://192.168.1.20:8766"),
        ("Игровой ПК", "http://192.168.1.21:9000"),
    ]
    assert [p["token_set"] for p in pcs] == [True, False] and all(p["token"] == "" for p in pcs)
    laptop_id = pcs[0]["id"]
    assert pctx.secrets.get(SECRET_PREFIX + laptop_id) == "pc-secret"
    stored = pctx.settings.value.plugins["second_pc"].config["pcs"]
    assert all("token" not in pc for pc in stored)
    assert "pc-secret" not in pctx.settings.path.read_text(encoding="utf-8")

    # пароль не прислали — остаётся прежний; убрали компьютер — пароль удаляется
    info = await plugins.update("second_pc", config={"pcs": [{**pcs[0], "token": ""}]})
    assert info["config"]["pcs"][0]["token_set"] is True
    await plugins.update("second_pc", config={"pcs": [pcs[1]]})
    assert pctx.secrets.get(SECRET_PREFIX + laptop_id) == ""
    with pytest.raises(UserFacingError, match="не указан адрес"):
        await plugins.update("second_pc", config={"pcs": [{"name": "Без адреса", "url": ""}]})


async def test_ping_and_forward_voice_phrase(pctx: AppContext, lan: Lan) -> None:
    await enable(pctx, "second_pc", config={"pcs": PCS})
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["online"] == 1)
    laptop, gaming = plugin.status()["pcs"]
    assert laptop["online"] is True and laptop["volume"] == 35 and laptop["remote_name"] == "LAPTOP-ANNA"
    assert laptop["now_playing"] == {
        "title": "Night Drive",
        "artist": "Neon Coast",
        "app": "Spotify",
        "status": "playing",
    }
    assert gaming["online"] is False and gaming["error"] == "Не в сети"

    commands = pctx.commands
    assert isinstance(commands, FakeCommands)
    phrases = commands.phrases()
    for expected in (
        "на ноутбуке {text}",
        "на компьютере ноутбук {text}",
        "на втором компьютере {text}",
        "на игровом пк {text}",
    ):
        assert expected in phrases, expected
    result = await commands.by_phrase("на ноутбуке {text}").handler({"text": "включи музыку"})
    assert result.ok and result.title == "Ноутбук: Включить музыку" and result.message == "Выполнено"
    request = lan.requests[-1]
    assert request.url == "http://192.168.1.20:8766/remote/api/dispatch"
    assert request.headers["x-remote-token"] == "pc-secret"
    assert json.loads(request.content) == {"text": "включи музыку"}

    lan.reply = {"kind": "ai", "ok": True, "title": "Jarvis AI", "message": "", "text": "Свободно 120 ГБ."}
    answer = await manager(pctx).action("second_pc", "dispatch", {"name": "ноутбук", "text": "сколько места"})
    assert answer["ok"] is True and answer["result"]["say"] == "Свободно 120 ГБ."
    lan.reply = {"kind": "unknown", "ok": False, "title": "Не понял команду", "message": "", "text": ""}
    unknown = await commands.by_phrase("на втором компьютере {text}").handler({"text": "спой"})
    assert unknown.ok is False and "такой команды нет" in unknown.message

    offline = await commands.by_phrase("на игровом пк {text}").handler({"text": "выключись"})
    assert offline.ok is False and offline.message == "«Игровой ПК» не в сети"
    empty = await commands.by_phrase("на ноутбуке {text}").handler({})
    assert empty.ok is False and "Скажите команду" in empty.message


async def test_wrong_password_backs_off(pctx: AppContext, lan: Lan) -> None:
    await manager(pctx).update(
        "second_pc", config={"pcs": [{"name": "Ноутбук", "url": "192.168.1.20", "token": "wrong"}]}
    )
    plugin = plugin_of(pctx)
    await plugin.ping_all()
    assert plugin.status()["pcs"][0]["error"] == "Неверный пароль пульта"
    await plugin.ping_all()
    assert lan.count("/remote/api/state") == 1  # пульт блокирует после 20 ошибок — повторно не стучимся
    pctx.secrets.set("remote_token", "pc-secret")  # общий пароль пульта; свой пароль компьютера — неверный
    pc_id = plugin.pcs()[0]["id"]
    pctx.secrets.set(SECRET_PREFIX + pc_id, "")
    plugin._auth_backoff.clear()  # так делает подписка на secrets.changed, пока плагин работает
    await plugin.ping_all()
    assert plugin.status()["pcs"][0]["online"] is True


async def test_new_password_retries_at_once(pctx: AppContext, lan: Lan) -> None:
    await enable(
        pctx, "second_pc", config={"pcs": [{"name": "Ноутбук", "url": "192.168.1.20", "token": "wrong"}]}
    )
    plugin = plugin_of(pctx)
    await wait_until(lambda: plugin.status()["pcs"][0].get("error") == "Неверный пароль пульта")
    await manager(pctx).update("second_pc", config={"pcs": [{**plugin.pcs()[0], "token": "pc-secret"}]})
    await wait_until(lambda: plugin.status()["online"] == 1)
