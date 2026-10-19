"""Прокси: поле «Прокси» Jarvis AI и системный прокси Windows, который включает VPN-программа."""

import os
from typing import Any

import httpx
import pytest

from jarvis import netproxy
from jarvis.ai import llm as llm_module
from jarvis.ai.cli import errors
from jarvis.ai.llm import resolve_config
from jarvis.context import AppContext

# так urllib читает из реестра запись VPN-программы «socks=127.0.0.1:10808»
VPN_SOCKS = {
    "socks": "socks://127.0.0.1:10808",
    "http": "socks4://127.0.0.1:10808",
    "https": "socks4://127.0.0.1:10808",
}
MESSAGES = [{"role": "user", "content": "Привет"}]
VPN_HTTP = {
    "http": "http://127.0.0.1:10809",
    "https": "http://127.0.0.1:10809",
    "ftp": "http://127.0.0.1:10809",
}


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Системный прокси, который «включил VPN»: тест кладёт в словарь записи реестра."""
    found: dict[str, str] = {}
    monkeypatch.setattr(netproxy, "source", lambda: dict(found))
    for name in list(os.environ):
        if name.upper() in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            monkeypatch.delenv(name)  # прокси окружения машины, где идут тесты, не мешает
    return found


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("   ", ""),
        (" 127.0.0.1:10808 ", "http://127.0.0.1:10808"),
        ("localhost:1080", "http://localhost:1080"),
        ("user:pass@1.2.3.4:3128", "http://user:pass@1.2.3.4:3128"),
        ("socks://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
        ("socks4://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
        ("SOCKS5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
        ("socks5h://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
        ("https://proxy.example:443", "https://proxy.example:443"),
        ("bad://proxy", "bad://proxy"),
    ],
)
def test_normalize(value: str, expected: str) -> None:
    assert netproxy.normalize(value) == expected


def test_system_socks_from_vpn_does_not_break_httpx(system: dict[str, str]) -> None:
    system.update(VPN_SOCKS)
    netproxy.install()
    # раньше: ValueError «Unknown scheme for proxy URL socks4://…» у каждого клиента httpx
    assert netproxy.system_proxies() == {
        "http": "socks5://127.0.0.1:10808",
        "https": "socks5://127.0.0.1:10808",
    }
    httpx.AsyncClient()
    assert netproxy.system_https() == "socks5://127.0.0.1:10808"

    system.clear()
    system.update({"https": "bad://127.0.0.1:1", "http": "http://127.0.0.1:10809", "no": "localhost"})
    assert netproxy.system_proxies() == {"http": "http://127.0.0.1:10809", "no": "localhost"}
    httpx.AsyncClient()
    assert netproxy.system_https() == ""


def test_proxy_setting_is_normalized(ctx: AppContext) -> None:
    ctx.settings.patch({"ai": {"proxy": " 127.0.0.1:10808 "}})
    assert ctx.settings.value.ai.proxy == "http://127.0.0.1:10808"
    ctx.settings.patch({"ai": {"proxy": "socks4://127.0.0.1:1080"}})
    assert ctx.settings.value.ai.proxy == "socks5://127.0.0.1:1080"
    ctx.settings.patch({"ai": {"proxy": ""}})
    assert ctx.settings.value.ai.proxy == ""


def test_llm_client_follows_system_proxy(ctx: AppContext, system: dict[str, str]) -> None:
    llm: Any = ctx.ai.llm  # type: ignore[attr-defined]
    cfg = resolve_config(ctx.settings.value.ai, ctx.secrets)
    direct = llm._client(cfg)
    assert llm._client(cfg) is direct
    system.update(VPN_HTTP)  # VPN включили, пока Jarvis работал
    via_vpn = llm._client(cfg)
    assert via_vpn is not direct and llm._client(cfg) is via_vpn
    # свой прокси в настройках — системный не важен
    ctx.settings.patch({"ai": {"proxy": "socks5://127.0.0.1:1080"}})
    own = resolve_config(ctx.settings.value.ai, ctx.secrets)
    first = llm._client(own)
    system.clear()
    assert llm._client(own) is first


async def test_network_error_names_the_system_proxy(
    ctx: AppContext, system: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refused(transport: Any, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refused)  # сеть «не отвечает»
    llm: Any = ctx.ai.llm  # type: ignore[attr-defined]
    llm.max_retries = 0
    ctx.secrets.set("ai_api_key", "sk-test")
    with pytest.raises(llm_module.LlmError) as info:
        await llm.chat(MESSAGES, stream=False)
    assert str(info.value) == llm_module.NETWORK
    system.update(VPN_HTTP)  # VPN-программа закрылась, а системный прокси остался
    with pytest.raises(llm_module.LlmError) as info:
        await llm.chat(MESSAGES, stream=False)
    assert "системный прокси http://127.0.0.1:10809" in str(info.value) and "VPN" in str(info.value)
    ctx.settings.patch({"ai": {"proxy": "http://127.0.0.1:3128"}})
    with pytest.raises(llm_module.LlmError) as info:
        await llm.chat(MESSAGES, stream=False)
    assert str(info.value) == f"{llm_module.NETWORK} — проверьте прокси в настройках Jarvis AI"


def test_programs_get_the_system_proxy_but_not_socks(
    ctx: AppContext, system: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    claude: Any = ctx.ai.cli.get("claude_code")  # type: ignore[attr-defined]
    gemini: Any = ctx.ai.cli.get("gemini_cli")  # type: ignore[attr-defined]
    assert "HTTPS_PROXY" not in claude._env() and claude._socks_problem() == ""

    # Node.js не читает системный прокси Windows — Jarvis передаёт его сам
    system.update(VPN_HTTP)
    env = claude._env()
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:10809"

    # системный SOCKS — не передаём, а ошибку связи объясняем
    system.clear()
    system.update(VPN_SOCKS)
    assert "HTTPS_PROXY" not in claude._env() and "HTTPS_PROXY" not in gemini._env()
    assert claude._socks_problem() == errors.SYSTEM_SOCKS.format(
        program="Claude Code", proxy="socks5://127.0.0.1:10808"
    )

    # SOCKS из настроек Claude Code не понимает (его документация), Gemini CLI получает как раньше
    ctx.settings.patch({"ai": {"proxy": "socks5://127.0.0.1:1080"}})
    assert "HTTPS_PROXY" not in claude._env()
    assert claude._socks_problem() == errors.NO_SOCKS.format(
        program="Claude Code", proxy="socks5://127.0.0.1:1080"
    )
    assert gemini._env()["HTTPS_PROXY"] == "socks5://127.0.0.1:1080" and gemini._socks_problem() == ""

    # прокси из настроек главнее прокси окружения в любом регистре
    monkeypatch.setenv("https_proxy", "http://other:1")
    ctx.settings.patch({"ai": {"proxy": "127.0.0.1:10809"}})
    env = claude._env()
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:10809" and "https_proxy" not in env
    assert claude._socks_problem() == ""


def test_telegram_reconnects_when_the_system_proxy_changes(ctx: AppContext, system: dict[str, str]) -> None:
    bot: Any = ctx.remote.telegram  # type: ignore[attr-defined]
    bot.connect("123456:token")
    client = bot._client
    bot._follow_system_proxy()
    assert bot._client is client
    system.update(VPN_HTTP)  # VPN включили — Telegram без своего прокси пойдёт через системный
    bot._follow_system_proxy()
    assert bot._client is not client and bot.api is not None
