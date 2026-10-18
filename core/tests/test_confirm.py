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
