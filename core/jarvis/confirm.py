"""Подтверждения опасных действий: команды с флажком «Подтверждать» и шаги агента («Отправить письмо?»).

request() публикует confirm.request — интерфейс показывает карточку «Отклонить / Разрешить» (оверлей,
главное окно, пульт). Если просьба пришла голосом, Jarvis ещё и спрашивает вслух и ждёт «да» / «нет».
Первый ответ решает; нет ответа за timeout — отказ. Каждый итог (разрешено, отклонено, нет ответа, отменено
«Стопом») пишется в историю команд (kind confirm) — видно, что и когда человек разрешал.
"""

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from jarvis.db import new_id, now
from jarvis.redact import redact

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

YES_WORDS = ("да", "давай", "конечно", "подтверждаю", "разрешаю", "выполняй", "ага", "угу", "можно", "yes")
NO_WORDS = ("нет", "не надо", "отмена", "отмени", "стоп", "не нужно", "запрещаю", "no")


@dataclass
class Confirmation:
    id: str
    title: str  # «Выключить компьютер?»
    detail: str = ""  # «Будет отправлено: „Отчёт готов…“»
    kind: Literal["command", "agent", "plugin"] = "command"
    source: str = "ui"
    created: float = field(default_factory=now)
    timeout: float = 60.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


Outcome = Literal["approved", "denied", "timeout", "cancelled"]
# итог → (status строки истории, что показать)
_OUTCOMES: dict[str, tuple[str, str]] = {
    "approved": ("ok", "Разрешено"),
    "denied": ("denied", "Отклонено"),
    "timeout": ("cancelled", "Нет ответа — отказ"),
    "cancelled": ("cancelled", "Отменено"),
}


class Confirmations:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._pending: dict[str, tuple[Confirmation, asyncio.Future[bool]]] = {}
        self._cancelled: set[str] = set()  # отменены «Стопом» (cancel_all), а не отклонены человеком

    def pending(self) -> list[dict[str, Any]]:
        return [c.to_json() for c, _ in self._pending.values()]

    async def request(
        self,
        title: str,
        detail: str = "",
        *,
        kind: Literal["command", "agent", "plugin"] = "command",
        source: str = "ui",
        timeout: float = 60.0,
        spoken: str = "",
    ) -> bool:
        conf = Confirmation(new_id(), title, detail, kind, source, timeout=timeout)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[conf.id] = (conf, future)
        self.ctx.bus.publish("confirm.request", conf.to_json())
        voice_task: asyncio.Task[None] | None = None
        if source == "voice" and self.ctx.settings.value.voice.confirm_by_voice:
            voice_task = self.ctx.spawn(self._ask_voice(conf, spoken or title), "confirm-voice")
        outcome: Outcome = "cancelled"
        try:
            allowed = await asyncio.wait_for(asyncio.shield(future), timeout)
            outcome = "approved" if allowed else ("cancelled" if conf.id in self._cancelled else "denied")
            return allowed
        except TimeoutError:
            log.info("подтверждение «%s» не получено за %.0f с — отказ", title, timeout)
            outcome = "timeout"
            return False
        finally:
            self._pending.pop(conf.id, None)
            self._cancelled.discard(conf.id)
            if voice_task is not None:
                voice_task.cancel()
            allowed = future.done() and not future.cancelled() and future.result()
            self.ctx.bus.publish("confirm.resolved", {"id": conf.id, "allow": bool(allowed)})
            self._record(conf, outcome)

    def _record(self, conf: Confirmation, outcome: Outcome) -> None:
        """Итог подтверждения — в историю команд: что спрашивали, откуда и что решил человек."""
        status, text = _OUTCOMES[outcome]
        detail = text if not conf.detail else f"{text} · {redact(' '.join(conf.detail.split()))[:200]}"
        item: dict[str, Any] = {
            "ts": now(),
            "source": conf.source,
            "text": conf.title,
            "kind": "confirm",
            "title": conf.title,
            "status": status,
            "detail": detail,
            "icon": "lock",
            "command_id": "",
        }
        try:
            item["id"] = self.ctx.db.insert("history", item)
        except Exception:
            log.exception("итог подтверждения не записан в историю")
            return
        self.ctx.bus.publish("history.added", {"item": item})

    def resolve(self, confirmation_id: str, allow: bool) -> bool:
        item = self._pending.get(confirmation_id)
        if item is None:
            return False
        _, future = item
        if not future.done():
            future.set_result(bool(allow))
        return True

    def cancel_all(self) -> int:
        """«Стоп»: все открытые подтверждения — отказ (в историю — «Отменено»). Вернёт, сколько отменено."""
        count = 0
        for conf, future in list(self._pending.values()):
            if not future.done():
                self._cancelled.add(conf.id)
                future.set_result(False)
                count += 1
        return count

    async def _ask_voice(self, conf: Confirmation, question: str) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await self.ctx.tts.say(f"{question} Да или нет?", interrupt=True, wait=True)
                answer = await self.ctx.voice.ask_yes_no(timeout=8.0)
            except Exception:
                log.exception("голосовое подтверждение не удалось")
                return
            if answer is not None:
                self.resolve(conf.id, answer)


def parse_yes_no(text: str) -> bool | None:
    """«да, давай» → True, «нет, не надо» → False, иначе None."""
    t = " ".join(text.casefold().replace("ё", "е").split())
    if not t:
        return None
    if any(t == w or t.startswith(w + " ") or t.startswith(w + ",") for w in NO_WORDS):
        return False
    if any(t == w or t.startswith(w + " ") or t.startswith(w + ",") for w in YES_WORDS):
        return True
    return None
