"""Подтверждения: итог каждого запроса (разрешено, отклонено, нет ответа, отменено «Стопом») — в истории."""

import asyncio

from jarvis.context import AppContext
from tests.conftest import EventLog


async def _ask(ctx: AppContext, title: str, detail: str = "", timeout: float = 5.0) -> asyncio.Task[bool]:
    request = ctx.confirmations.request(title, detail, kind="agent", source="text", timeout=timeout)
    task = asyncio.create_task(request)
    for _ in range(100):
        if ctx.confirmations.pending():
            break
        await asyncio.sleep(0.005)
    return task


def _rows(ctx: AppContext) -> list[dict]:
    return ctx.db.query("SELECT * FROM history WHERE kind='confirm' ORDER BY id")


async def test_outcomes_are_written_to_history(ctx: AppContext, events: EventLog) -> None:
    task = await _ask(ctx, "Отправить письмо?", "Будет отправлено: «оплата картой 4111 1111 1111 1111»")
    ctx.confirmations.resolve(ctx.confirmations.pending()[0]["id"], True)
    assert await task is True

    task = await _ask(ctx, "Удалить файл?")
    ctx.confirmations.resolve(ctx.confirmations.pending()[0]["id"], False)
    assert await task is False

    assert await (await _ask(ctx, "Купить подписку?", timeout=0.05)) is False

    task = await _ask(ctx, "Выключить компьютер?")
    assert ctx.confirmations.cancel_all() == 1 and ctx.confirmations.cancel_all() == 0
    assert await task is False

    rows = _rows(ctx)
    assert [(r["title"], r["status"]) for r in rows] == [
        ("Отправить письмо?", "ok"),
        ("Удалить файл?", "denied"),
        ("Купить подписку?", "cancelled"),
        ("Выключить компьютер?", "cancelled"),
    ]
    assert rows[0]["detail"] == "Разрешено · Будет отправлено: «оплата картой •••• 1111»"  # без номера карты
    assert rows[1]["detail"] == "Отклонено" and rows[2]["detail"] == "Нет ответа — отказ"
    assert rows[3]["detail"] == "Отменено" and rows[0]["source"] == "text" and rows[0]["icon"] == "lock"
    added = [e["item"] for e in events.of("history.added") if e["item"]["kind"] == "confirm"]
    assert [a["status"] for a in added] == ["ok", "denied", "cancelled", "cancelled"]
    assert [e["allow"] for e in events.of("confirm.resolved")] == [True, False, False, False]


async def test_cancelled_request_is_recorded(ctx: AppContext) -> None:
    task = await _ask(ctx, "Оплатить заказ?")
    task.cancel()  # агента остановили, пока он ждал ответа
    await asyncio.gather(task, return_exceptions=True)
    assert [(r["title"], r["detail"]) for r in _rows(ctx)] == [("Оплатить заказ?", "Отменено")]
    assert ctx.confirmations.pending() == []


def test_high_risk_words() -> None:
    from jarvis.confirm import high_risk

    for title in (
        "Отправить сообщение Ане?",
        "Нажать «Купить» на ozon.ru?",
        "Ввести пароль в поле «Пароль»?",
        "Удалить выделенное (Delete)?",
        "Оплатить заказ?",
        "Ввести номер карты?",
    ):
        assert high_risk(title) == "high", title
    for title in ("Открыть файл «Отчёт.docx»?", "Переключиться на окно «Блокнот»?", "Запустить «Paint»?"):
        assert high_risk(title) == "normal", title
    assert high_risk("Нажать «Готово»?", "Будет отправлено: «Привет»") == "high"


class _Voice:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer

    async def ask_yes_no(self, timeout: float = 8.0) -> bool | None:
        return self.answer


class _Tts:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, text: str, **kwargs: object) -> None:
        self.said.append(text)


async def _voice_request(ctx: AppContext, answer: bool | None, risk: str, timeout: float = 0.3) -> bool:
    tts = _Tts()
    ctx.tts = tts  # type: ignore[assignment]
    ctx.voice = _Voice(answer)  # type: ignore[assignment]
    allowed = await ctx.confirmations.request(
        "Отправить письмо?",
        kind="agent",
        source="voice",
        timeout=timeout,
        risk=risk,  # type: ignore[arg-type]
    )
    ctx._said = tts.said  # type: ignore[attr-defined]
    return allowed


async def test_irreversible_needs_button_not_voice_yes(ctx: AppContext) -> None:
    from jarvis.confirm import BUTTON_ONLY, NOT_BY_VOICE

    ctx.loop = asyncio.get_running_loop()
    assert await _voice_request(ctx, True, "normal") is True  # обычное — голосом можно
    assert await _voice_request(ctx, True, "high") is False  # «да» голосом не разрешает — нет ответа, отказ
    said = ctx._said  # type: ignore[attr-defined]
    assert BUTTON_ONLY in said[0] and said[-1] == NOT_BY_VOICE
    assert await _voice_request(ctx, False, "high") is False  # «нет» голосом — отказ
    ctx.settings.patch({"voice": {"confirm_high_by_voice": True}})
    assert await _voice_request(ctx, True, "high") is True  # включили в настройках — и голосом
