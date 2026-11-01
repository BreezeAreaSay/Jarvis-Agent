"""Секреты не уходят облачной модели: новые шаблоны маски, сообщения API, программы подписки (Claude Code и
др.), ответы инструментов Jarvis для них; модели на этом ПК видят текст как есть."""

from pathlib import Path

import pytest

from jarvis import redact as redact_module
from jarvis.ai.chatgpt.backend import ChatGpt
from jarvis.ai.cli.specs import CliRun
from jarvis.context import AppContext
from jarvis.redact import MASK, outgoing, redact, redact_messages, set_outgoing
from tests.ai.fakes import ScriptedLlm, reply

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAx4wKz\nabcdef==\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"ключ: {PEM} конец",
            f"ключ: -----BEGIN RSA PRIVATE KEY----- {MASK} -----END RSA PRIVATE KEY----- конец",
        ),
        ("postgres://admin:Sup3rS3cret@db.local:5432/app", f"postgres://admin:{MASK}@db.local:5432/app"),
        ("mongodb+srv://u:p4ssw0rd@cluster0.x.net/db", f"mongodb+srv://u:{MASK}@cluster0.x.net/db"),
        ("glpat-abcdefghij0123456789xyz", f"glpat-{MASK}"),
        ("hf_" + "a" * 34, f"hf_{MASK}"),
        ("sk_live_" + "Z" * 24, f"sk_live_{MASK}"),
        ("AQVN" + "q" * 36, f"AQVN{MASK}"),
        ("ya29.a0AfH6SMBx0123456789abcdef", f"ya29.{MASK}"),
        (
            "https://example.com/path и почта me@example.com",
            "https://example.com/path и почта me@example.com",
        ),
    ],
)
def test_new_patterns(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_redact_messages_keeps_images_and_original() -> None:
    messages = [
        {"role": "system", "content": "Ты Джарвис"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "мой пароль: Qwerty123 и вот снимок"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "token=abcdef1234567890"},
    ]
    masked = redact_messages(messages)
    assert masked[0] is messages[0]  # без секретов — тот же объект
    assert masked[1]["content"][0]["text"] == f"мой пароль: {MASK} и вот снимок"
    assert masked[1]["content"][1] == messages[1]["content"][1]
    assert masked[2]["content"] == f"token={MASK}"
    assert messages[1]["content"][0]["text"].endswith("Qwerty123 и вот снимок")  # исходное не тронуто


async def _sent(ctx: AppContext, llm: ScriptedLlm) -> str:
    llm.add(reply("ок"))
    await ctx.ai.llm.chat([{"role": "user", "content": "пароль: Qwerty123"}], stream=False)  # type: ignore[attr-defined]
    return str(llm.requests[-1]["messages"])


async def test_cloud_model_gets_masked_local_model_does_not(ctx: AppContext, llm: ScriptedLlm) -> None:
    assert "Qwerty123" not in await _sent(ctx, llm)  # OpenAI по ключу — облако
    ctx.settings.patch({"ai": {"mask_secrets": False}})
    assert "Qwerty123" in await _sent(ctx, llm)  # выключено в настройках
    ctx.settings.patch({"ai": {"mask_secrets": True, "provider": "ollama", "model": "qwen3:8b"}})
    assert "Qwerty123" in await _sent(ctx, llm)  # модель на этом ПК


def test_subscription_programs_get_masked(tmp_path: Path) -> None:
    try:
        run = CliRun(tmp_path, prompt="ключ sk-ant-" + "x" * 30, system="токен: ghp_" + "y" * 30)
        assert run.prompt == f"ключ sk-ant-{MASK}" and run.system == f"токен: ghp_{MASK}"
        items = ChatGpt._items("пароль: hunter2hunter", [])
        assert items[0]["text"] == f"пароль: {MASK}"
        set_outgoing(False)  # выключено в настройках — как есть
        assert outgoing("пароль: hunter2hunter") == "пароль: hunter2hunter"
    finally:
        set_outgoing(True)


async def test_setting_switch_follows_ai_service(running_ctx: AppContext) -> None:
    try:
        running_ctx.settings.patch({"ai": {"mask_secrets": False}})
        assert redact_module._OUTGOING["on"] is False
        running_ctx.settings.patch({"ai": {"mask_secrets": True}})
        assert redact_module._OUTGOING["on"] is True
    finally:
        set_outgoing(True)
