"""Пульт в браузере: страница и API с токеном, действия на заглушке платформы, сервер в сети, /api/remote."""

import asyncio
import re
import socket
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.context import AppContext
from jarvis.remote import net
from jarvis.remote.lan import create_remote_app
from jarvis.remote.service import Remote
from jarvis.services import ExecResult


def _remote(ctx: AppContext) -> Remote:
    service = ctx.remote
    assert isinstance(service, Remote)
    return service


@pytest.fixture
def remote(ctx: AppContext) -> Remote:
    return _remote(ctx)


@pytest.fixture
def token(remote: Remote) -> str:
    return remote.token()


@pytest.fixture
def web(remote: Remote, token: str) -> TestClient:
    return TestClient(create_remote_app(remote))


def _auth(token: str) -> dict[str, str]:
    return {"X-Remote-Token": token}


class FakeDispatcher:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def handle(self, text: str, source: str, *, speak: bool | None = None) -> dict[str, Any]:
        self.calls.append((text, source))
        return self.result


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- страница и токен ---------------------------------------------------------------------------------


def test_token_generated_once(remote: Remote, ctx: AppContext) -> None:
    first = remote.token()
    assert len(first) >= 20 and ctx.secrets.get("remote_token") == first
    assert remote.token() == first
    assert remote.check_token(first) and not remote.check_token("") and not remote.check_token(first + "x")
    assert remote.regenerate_token() != first


def test_page_is_public_but_api_is_not(web: TestClient, token: str) -> None:
    page = web.get("/")
    assert page.status_code == 200 and "Скажите Джарвису…" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert web.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert web.get("/style.css").headers["content-type"].startswith("text/css")
    assert web.get("/manifest.webmanifest").status_code == 200
    assert web.get("/icon.svg").status_code == 200
    assert web.get("/secrets.dat").status_code == 404
    assert web.get("/../settings.json").status_code == 404

    denied = web.get("/remote/api/state")
    assert denied.status_code == 401 and "Нет доступа" in denied.json()["detail"]
    assert web.get("/remote/api/state", headers=_auth("wrong")).status_code == 401
    assert web.get("/remote/api/state", params={"t": token}).status_code == 200
    state = web.get("/remote/api/state", headers=_auth(token)).json()
    assert state["volume"] == 40 and state["muted"] is False
    assert state["media"]["title"] == "Night Drive" and "thumbnail" not in state["media"]
    assert state["pc_name"] and state["assistant"] == "Джарвис" and state["voice"]["state"] == "off"


def test_too_many_bad_tokens_blocked(web: TestClient, token: str) -> None:
    for _ in range(20):
        assert web.get("/remote/api/state", headers=_auth("bad")).status_code == 401
    assert web.get("/remote/api/state", headers=_auth("bad")).status_code == 429
    assert web.get("/remote/api/state", headers=_auth(token)).status_code == 429  # и с правильным — подождать


# --- действия -----------------------------------------------------------------------------------------


def test_dispatch_goes_to_jarvis(web: TestClient, token: str, ctx: AppContext) -> None:
    fake = FakeDispatcher(
        {
            "kind": "command",
            "result": ExecResult(True, "Свернуть все окна", "Выполнено").to_json(),
            "history": {},
        }
    )
    ctx.dispatcher = fake  # type: ignore[assignment]
    reply = web.post("/remote/api/dispatch", json={"text": "  сверни все окна "}, headers=_auth(token))
    assert reply.json() == {
        "kind": "command",
        "ok": True,
        "title": "Свернуть все окна",
        "message": "Выполнено",
        "text": "",
    }
    assert fake.calls == [("сверни все окна", "remote")]

    fake.result = {"kind": "ai", "chat_id": "c1", "text": "В Москве +14°", "error": ""}
    answer = web.post("/remote/api/dispatch", json={"text": "какая погода"}, headers=_auth(token)).json()
    assert answer["kind"] == "ai" and answer["ok"] and answer["text"] == "В Москве +14°"
    assert web.post("/remote/api/dispatch", json={"text": " "}, headers=_auth(token)).status_code == 400
    assert web.post("/remote/api/dispatch", json={"text": "x"}).status_code == 401


def test_media_and_volume(web: TestClient, token: str, ctx: AppContext) -> None:
    platform: Any = ctx.platform
    paused = web.post("/remote/api/media/play_pause", headers=_auth(token)).json()
    assert paused == {"ok": True, "title": "Пауза", "message": ""}
    assert platform.media.status == "paused"
    resumed = web.post("/remote/api/media/play_pause", headers=_auth(token)).json()
    assert resumed["title"] == "Воспроизведение"
    assert web.post("/remote/api/media/next", headers=_auth(token)).json()["title"] == "Следующий трек"
    assert [c for c in platform.calls if c[0] == "media_key"] == [
        ("media_key", ("play_pause",)),
        ("media_key", ("play_pause",)),
        ("media_key", ("next",)),
    ]

    louder = web.post("/remote/api/media/volume_up", headers=_auth(token)).json()
    assert louder["volume"] == 50 and louder["message"] == "Громкость 50%"
    muted = web.post("/remote/api/media/mute", headers=_auth(token)).json()
    assert muted["title"] == "Звук выключен" and muted["muted"] is True

    assert web.post("/remote/api/volume", json={"level": 25}, headers=_auth(token)).json()["volume"] == 25
    assert web.post("/remote/api/volume", json={"delta": -5}, headers=_auth(token)).json()["volume"] == 20
    assert web.post("/remote/api/volume", json={}, headers=_auth(token)).status_code == 400
    assert web.post("/remote/api/media/explode", headers=_auth(token)).status_code == 404


def test_thumbnail(web: TestClient, token: str, ctx: AppContext) -> None:
    platform: Any = ctx.platform
    assert web.get("/remote/api/media/thumbnail", headers=_auth(token)).status_code == 404
    platform.media.thumbnail = "data:image/png;base64,iVBORw0KGgo="
    state = web.get("/remote/api/state", headers=_auth(token)).json()
    assert len(state["media"]["thumb"]) == 12
    cover = web.get("/remote/api/media/thumbnail", headers=_auth(token))
    assert cover.status_code == 200 and cover.headers["content-type"] == "image/png"
    assert cover.content.startswith(b"\x89PNG")


def test_screenshot_power_and_windows(web: TestClient, token: str, ctx: AppContext) -> None:
    platform: Any = ctx.platform
    shot = web.get("/remote/api/screenshot", headers=_auth(token))
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"
    assert shot.content.startswith(b"\x89PNG")
    assert ("screenshot", (None,)) in platform.calls

    assert (
        web.post("/remote/api/power/lock", headers=_auth(token)).json()["title"] == "Компьютер заблокирован"
    )
    assert web.post("/remote/api/power/shutdown", headers=_auth(token)).json()["ok"] is True
    assert web.post("/remote/api/power/format_c", headers=_auth(token)).status_code == 404
    assert ("power", ("lock", 0)) in platform.calls and ("power", ("shutdown", 0)) in platform.calls

    assert (
        web.post("/remote/api/window/minimize_all", headers=_auth(token)).json()["title"] == "Окна свёрнуты"
    )
    assert ("window_action", ("minimize_all", None)) in platform.calls


def test_favorites_and_run(web: TestClient, token: str, ctx: AppContext) -> None:
    class FakeCommands:
        def __init__(self) -> None:
            self.runs: list[tuple[str, str]] = []

        def describe(self, limit: int = 300) -> list[dict[str, Any]]:
            return [{"id": "user.stream_mode", "title": "Режим стрима", "icon": "obs"}]

        async def run_command(
            self, command_id: str, slots: dict[str, Any] | None = None, source: str = "ui"
        ) -> ExecResult:
            self.runs.append((command_id, source))
            return ExecResult(True, "Режим стрима", "Выполнено", icon="obs")

    commands = FakeCommands()
    ctx.commands = commands  # type: ignore[assignment]
    dispatcher = FakeDispatcher(
        {"kind": "command", "result": ExecResult(True, "Время", "Сейчас 14:32").to_json()}
    )
    ctx.dispatcher = dispatcher  # type: ignore[assignment]
    ctx.settings.patch({"remote": {"favorites": ["user.stream_mode", "user.missing"]}})

    items = web.get("/remote/api/commands", headers=_auth(token)).json()["items"]
    assert items[0] == {
        "id": "user.stream_mode",
        "title": "Режим стрима",
        "icon": "obs",
        "kind": "command",
        "key": items[0]["key"],
    }
    assert items[1]["title"] == "user.missing"
    assert {i["id"] for i in items} >= {"say:time", "say:weather", "say:music"}

    ran = web.post("/remote/api/run/user.stream_mode", headers=_auth(token)).json()
    assert ran["ok"] and ran["title"] == "Режим стрима" and commands.runs == [("user.stream_mode", "remote")]
    said = web.post("/remote/api/run/say:time", headers=_auth(token)).json()
    assert said["title"] == "Который час" and said["message"] == "Сейчас 14:32"
    assert dispatcher.calls == [("который час", "remote")]
    assert web.post("/remote/api/run/rm -rf", headers=_auth(token)).status_code == 400


# --- адреса, QR-код и состояние -----------------------------------------------------------------------


def test_info_has_urls_and_qr(remote: Remote, token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jarvis.remote.service.lan_ips", lambda: ["192.168.1.50", "10.0.0.7"])
    info = remote.info()
    lan = info["lan"]
    assert lan["enabled"] is False and lan["port"] == 8766 and lan["status"] == "off"
    assert lan["urls"] == [f"http://192.168.1.50:8766/?t={token}", f"http://10.0.0.7:8766/?t={token}"]
    assert lan["qr_svg"].startswith("<svg") and "viewBox" in lan["qr_svg"]
    telegram = info["telegram"]
    assert telegram == {
        "enabled": False,
        "status": "off",
        "bot": "",
        "owner_id": 0,
        "pairing_code": "",
        "pairing_expires": None,
        "pairing_link": "",
        "token_set": False,
        "error": "",
    }


def test_qr_svg_matches_matrix() -> None:
    import qrcode

    text = "http://192.168.1.50:8766/?t=abc"
    svg = net.qr_svg(text)
    filled: set[tuple[int, int]] = set()
    for x, y, run in re.findall(r"M(\d+) (\d+)h(\d+)", svg):
        filled |= {(int(x) + i, int(y)) for i in range(int(run))}
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    expected = {(x, y) for y, row in enumerate(qr.get_matrix()) for x, cell in enumerate(row) if cell}
    assert filled == expected


def test_lan_ips_skip_virtual_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import psutil

    def addr(ip: str) -> Any:
        return SimpleNamespace(family=socket.AF_INET, address=ip)

    monkeypatch.setattr(
        psutil,
        "net_if_addrs",
        lambda: {
            "Loopback Pseudo-Interface 1": [addr("127.0.0.1")],
            "vEthernet (WSL)": [addr("172.28.0.1")],
            "VirtualBox Host-Only Network": [addr("192.168.56.1")],
            "Ethernet 2": [addr("169.254.10.10")],
            "Беспроводная сеть": [addr("192.168.1.34")],
            "Ethernet": [addr("10.0.0.15")],
            "Disconnected": [addr("192.168.7.7")],
        },
    )
    monkeypatch.setattr(
        psutil,
        "net_if_stats",
        lambda: {"Disconnected": SimpleNamespace(isup=False)},
    )
    assert net.lan_ips(refresh=True) == ["192.168.1.34", "10.0.0.15"]


def test_pairing_code_lifecycle(remote: Remote, ctx: AppContext) -> None:
    pairing = remote.pair()
    code = pairing["pairing_code"]
    assert re.fullmatch(r"\d{6}", code) and pairing["expires"] > 0
    assert remote.info()["telegram"]["pairing_code"] == code
    assert remote.consume_pairing("000000" if code != "000000" else "111111") is False
    assert remote.consume_pairing(code) is True
    assert remote.consume_pairing(code) is False  # одноразовый
    assert remote.info()["telegram"]["pairing_code"] == ""

    burned = remote.pair()["pairing_code"]
    wrong = "123456" if burned != "123456" else "654321"
    for _ in range(5):
        assert remote.consume_pairing(wrong) is False
    assert remote.consume_pairing(burned) is False  # пять неверных попыток — код сгорел


# --- сервер в сети ------------------------------------------------------------------------------------


async def test_lan_server_lifecycle(ctx: AppContext) -> None:
    ctx.loop = asyncio.get_running_loop()
    ctx.bus.bind_loop(ctx.loop)
    remote = _remote(ctx)
    token = remote.token()
    port = _free_port()
    await remote.lan.apply(True, port)
    try:
        assert remote.lan.status == "running" and remote.lan.port == port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as http:
            assert (await http.get("/")).status_code == 200
            assert (await http.get("/remote/api/state")).status_code == 401
            state = await http.get("/remote/api/state", headers=_auth(token))
            assert state.status_code == 200 and state.json()["volume"] == 40
        await remote.lan.apply(True, port)  # те же настройки — без перезапуска
        assert remote.lan.status == "running"
    finally:
        await remote.lan.stop()
    assert remote.lan.status == "off"
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient(timeout=2) as http:
            await http.get(f"http://127.0.0.1:{port}/")


async def test_lan_port_busy_is_reported(ctx: AppContext) -> None:
    ctx.loop = asyncio.get_running_loop()
    remote = _remote(ctx)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        await remote.lan.apply(True, port)
        assert remote.lan.status == "error" and str(port) in remote.lan.error
        assert remote.info()["lan"]["error"] == remote.lan.error
    await remote.lan.apply(False, port)
    assert remote.lan.status == "off" and remote.lan.error == ""


async def test_settings_toggle_lan_server(running_ctx: AppContext) -> None:
    remote = _remote(running_ctx)
    seen: list[dict[str, Any]] = []
    running_ctx.bus.on("remote.changed", lambda e: seen.append(e.data))
    port = _free_port()
    running_ctx.settings.patch({"remote": {"lan_enabled": True, "lan_port": port}})
    for _ in range(100):
        if remote.lan.status == "running":
            break
        await asyncio.sleep(0.05)
    assert remote.lan.status == "running" and remote.lan.port == port
    assert any(item["lan"]["status"] == "running" for item in seen)

    running_ctx.settings.patch({"remote": {"lan_enabled": False}})
    for _ in range(100):
        if remote.lan.status == "off":
            break
        await asyncio.sleep(0.05)
    assert remote.lan.status == "off"


# --- API ядра -----------------------------------------------------------------------------------------


def test_api_remote(client: TestClient) -> None:
    info = client.get("/api/remote").json()
    assert set(info) == {"lan", "telegram"}
    assert {"enabled", "port", "urls", "qr_svg"} <= set(info["lan"])
    assert {"enabled", "status", "bot", "owner_id", "pairing_code"} <= set(info["telegram"])
    assert info["telegram"]["status"] == "off"

    paired = client.post("/api/remote/telegram/pair").json()
    assert re.fullmatch(r"\d{6}", paired["pairing_code"])
    assert client.get("/api/remote").json()["telegram"]["pairing_code"] == paired["pairing_code"]

    ctx: AppContext = client.ctx  # type: ignore[attr-defined]
    old = ctx.secrets.get("remote_token")
    assert old  # создан при запуске
    renewed = client.post("/api/remote/token").json()
    assert ctx.secrets.get("remote_token") != old
    assert all(old not in url for url in renewed["lan"]["urls"])
