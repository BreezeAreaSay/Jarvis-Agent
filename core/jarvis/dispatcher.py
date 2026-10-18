"""Главный вход ядра: фраза человека (голос, строка, пульт, Telegram) → команда, ИИ или «не понял».

Порядок: «стоп» → команды (быстро, без сети) → ИИ (вопросы, сложные просьбы, «сверни окна и расскажи…») →
«не понял». Каждая фраза попадает в историю (history.*) и даёт тост (toast) в оверлее; на голосовую фразу
Jarvis отвечает голосом (если не включён тихий режим).
"""

import logging
import random
import time
from typing import TYPE_CHECKING, Any

from jarvis import text as textops
from jarvis.db import now
from jarvis.services import ExecResult, Source

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

OK_PHRASES = ("Готово, {address}.", "Сделано, {address}.", "Выполнено.", "Есть, {address}.")
UNKNOWN_SAY = "Не понял команду, {address}."
NO_AI_SAY = "Такой команды нет, а ИИ не настроен, {address}."


class MainDispatcher:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._last_text = ""
        self._last_at = 0.0

    # --- история ------------------------------------------------------------------------------------
    def _history_add(
        self, source: Source, text: str, kind: str, status: str, **fields: Any
    ) -> dict[str, Any]:
        item = {
            "ts": now(),
            "source": source,
            "text": text,
            "kind": kind,
            "title": fields.get("title", ""),
            "status": status,
            "detail": fields.get("detail", ""),
            "icon": fields.get("icon", ""),
            "command_id": fields.get("command_id", ""),
        }
        item["id"] = self.ctx.db.insert("history", item)
        self.ctx.bus.publish("history.added", {"item": item})
        return item

    def _history_update(self, item: dict[str, Any], **fields: Any) -> None:
        item.update(fields)
        self.ctx.db.update("history", item["id"], {k: v for k, v in fields.items() if k != "id"})
        self.ctx.bus.publish("history.updated", {"item": item})

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.ctx.db.query("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))

    def clear_history(self) -> None:
        self.ctx.db.execute("DELETE FROM history")
        self.ctx.bus.publish("history.cleared", {})

    # --- голос ---------------------------------------------------------------------------------------
    def _address(self) -> str:
        return self.ctx.settings.value.profile.address or ""

    def _fmt(self, phrase: str) -> str:
        address = self._address()
        out = phrase.format(address=address)
        if not address:
            out = out.replace(", .", ".").replace(" ,", ",").replace(",.", ".")
        return out

    async def _say(self, text: str, speak: bool) -> None:
        if not speak or not text or self.ctx.settings.value.voice.silent_mode:
            return
        try:
            await self.ctx.tts.say(text, interrupt=True)
        except Exception:
            log.exception("озвучка не удалась")

    def toast(self, title: str, message: str = "", *, status: str = "ok", icon: str = "") -> None:
        self.ctx.bus.publish(
            "toast", {"title": title, "message": message, "status": status, "icon": icon, "ts": time.time()}
        )

    # --- главный вход ----------------------------------------------------------------------------------
    async def handle(self, text: str, source: Source, *, speak: bool | None = None) -> dict[str, Any]:
        raw = textops.clean(text)
        if not raw:
            return {"kind": "empty"}
        if speak is None:
            speak = source == "voice"
        self.ctx.bus.publish("dispatch.started", {"text": raw, "source": source})

        if textops.is_stop(raw):
            self.ctx.tts.silence()
            try:
                self.ctx.ai.stop_all()  # ответ ИИ, который сейчас пишется и озвучивается
                await self.ctx.agent.stop_run()
            except Exception:
                log.exception("ИИ или агент не остановились")
            self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "stop"})
            return {"kind": "stop"}

        # команды
        match = None
        try:
            match = self.ctx.commands.match(raw)
        except Exception:
            log.exception("разбор команды упал: %r", raw)
        if match is not None:
            return await self._run_command(raw, source, match, speak)

        # ИИ
        ai_settings = self.ctx.settings.value.ai
        if ai_settings.enabled and self.ctx.ai.configured:
            return await self._run_ai(raw, source, speak)

        item = self._history_add(source, raw, "unknown", "error", title=raw, detail="Не понял команду")
        self.toast("Не понял команду", raw, status="error", icon="unknown")
        await self._say(self._fmt(NO_AI_SAY if ai_settings.enabled else UNKNOWN_SAY), speak)
        self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "unknown"})
        return {"kind": "unknown", "history": item}

    async def _run_command(self, raw: str, source: Source, match: Any, speak: bool) -> dict[str, Any]:
        title = " и ".join(p.title for p in match.parts) or raw
        item = self._history_add(
            source,
            raw,
            "command",
            "pending",
            title=title,
            command_id=",".join(p.command_id for p in match.parts),
        )
        try:
            result: ExecResult = await self.ctx.commands.execute(match, source)
        except Exception as e:
            log.exception("команда упала: %r", raw)
            result = ExecResult(False, title, f"Ошибка: {e}", icon="error")
        status = "ok" if result.ok else ("cancelled" if result.data.get("cancelled") else "error")
        self._history_update(
            item,
            status=status,
            title=result.title or title,
            detail=result.message or ("Выполнено" if result.ok else "Ошибка"),
            icon=result.icon,
        )
        self.ctx.bus.publish("command.executed", {"text": raw, "source": source, "result": result.to_json()})
        if not result.data.get("silent"):
            self.toast(
                result.title or title,
                result.message or ("Выполнено" if result.ok else "Ошибка"),
                status="ok" if result.ok else "error",
                icon=result.icon,
            )
        settings = self.ctx.settings.value.voice
        if result.say:
            await self._say(result.say, speak)
        elif result.ok and settings.speak_confirmations == "short":
            await self._say(self._fmt(random.choice(OK_PHRASES)), speak)
        elif not result.ok:
            await self._say(result.message or "Не получилось.", speak)
        self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "command", "ok": result.ok})
        return {"kind": "command", "result": result.to_json(), "history": item}

    async def _run_ai(self, raw: str, source: Source, speak: bool) -> dict[str, Any]:
        item = self._history_add(source, raw, "ai", "pending", title=raw, icon="ai")
        chat_id = None
        if source in ("voice", "telegram", "remote"):
            chat_id = self.ctx.db.kv_get(f"voice_chat:{source}")
        try:
            result = await self.ctx.ai.ask(raw, chat_id=chat_id, source=source)
        except Exception as e:
            log.exception("ИИ упал: %r", raw)
            self._history_update(item, status="error", detail=str(e))
            self.toast("Jarvis AI", str(e), status="error", icon="ai")
            await self._say("Не получилось связаться с ИИ.", speak)
            return {"kind": "ai", "error": str(e), "history": item}
        if source in ("voice", "telegram", "remote") and result.chat_id:
            self.ctx.db.kv_set(f"voice_chat:{source}", result.chat_id)
        if result.error:
            self._history_update(item, status="error", detail=result.error)
            self.toast("Jarvis AI", result.error, status="error", icon="ai")
            await self._say(result.error, speak)
        else:
            short = (result.spoken or result.text).strip()
            self._history_update(item, status="ok", detail=_one_line(result.text, 140))
            await self._say(short, speak)
            if speak:
                self.ctx.voice.follow_up()
        self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "ai", "ok": not result.error})
        return {
            "kind": "ai",
            "chat_id": result.chat_id,
            "text": result.text,
            "actions": result.actions,
            "sources": result.sources,
            "error": result.error,
            "history": item,
        }


def _one_line(text: str, limit: int) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"
