"""Главный вход ядра: фраза человека (голос, строка, пульт, Telegram) → команда, ИИ или «не понял».

Порядок: «стоп» → команды (быстро, без сети) → ИИ (вопросы, сложные просьбы, «сверни окна и расскажи…») →
«не понял». Каждая фраза попадает в историю (history.*) и даёт тост (toast) в оверлее; на голосовую фразу
Jarvis отвечает голосом (если не включён тихий режим).

Агент важнее готовой команды, когда команда не справилась (NeedsAgent: «включи музыку», а трек не выбран
и музыка не заиграла) или человек сам научил, как это делать: в памяти правило «когда я прошу включить
музыку — открой Яндекс Музыку и нажми плей», и его начало — та же команда. Тогда дело сразу берёт агент
с этим правилом (если ИИ настроен, режим не «Чат» и включено «Сразу запускать агента»).
"""

import logging
import random
import re
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
# правило из памяти: «когда я прошу включить музыку, надо…» — «включить музыку» до запятой или «надо»
_RULE = re.compile(
    r"(?<!\w)(?:когда|если|как только)\s+(?:я\s+)?(?:прошу|попрошу|говорю|скажу|хочу|захочу)\s+"
    r"(?P<what>.+?)(?=\s*[,.;:!?—–]|\s+(?:то|надо|нужно|ты|сначала|делай|открывай|нажимай)(?!\w)|$)",
    re.I,
)
_REMEMBER = re.compile(r"^\s*(?:запомни|не забудь)\s*,?\s*(?:что\s+)?", re.I)
MAX_RULES = 30  # записей памяти с правилами, которые сверяем с командой


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

    async def stop(self) -> None:
        """«Стоп»: замолчать, бросить ответ ИИ и агента, отклонить открытые запросы «Разрешить?», остановить
        идущие сценарии, перестать слушать (окна продолжения и ожидания)."""
        self.ctx.tts.silence()
        try:
            self.ctx.ai.stop_all()  # ответ ИИ, который сейчас пишется и озвучивается
            await self.ctx.agent.stop_run()
        except Exception:
            log.exception("ИИ или агент не остановились")
        try:
            self.ctx.confirmations.cancel_all()  # карточка, оставшаяся после «Стоп», не сработает потом
            stop_all = getattr(self.ctx.scenarios, "stop_all", None)
            if callable(stop_all):
                stop_all()
        except Exception:
            log.exception("подтверждения или сценарии не остановились")
        try:
            await self.ctx.voice.stop_listening()
        except Exception:
            log.exception("голос не перестал слушать")

    # --- главный вход ----------------------------------------------------------------------------------
    async def handle(self, text: str, source: Source, *, speak: bool | None = None) -> dict[str, Any]:
        raw = textops.clean(text)
        if not raw:
            return {"kind": "empty"}
        if speak is None:
            speak = source == "voice"
        self.ctx.bus.publish("dispatch.started", {"text": raw, "source": source})

        if textops.is_stop(raw):
            await self.stop()
            self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "stop"})
            return {"kind": "stop"}

        # команды
        match = None
        try:
            match = self.ctx.commands.match(raw)
        except Exception:
            log.exception("разбор команды упал: %r", raw)
        if match is not None:
            rule = self._taught_rule(match) if self._agent_ready() else ""
            if rule:  # человек научил, как это делать, — агенту с его правилом
                task = f"{raw}. Пользователь просил делать это так: {rule}"
                return await self._run_ai(raw, source, speak, agent_task=task)
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
        handoff = str(result.data.get("agent_task") or "")
        if not result.ok and handoff and self._agent_ready():
            # готовая команда не справилась (музыка не заиграла) — дело берёт агент
            self._history_update(item, status="error", title=result.title or title, detail=result.message)
            self.ctx.bus.publish(
                "command.executed", {"text": raw, "source": source, "result": result.to_json()}
            )
            self.toast(result.title or title, f"{result.message} — передаю агенту", status="info", icon="ai")
            return await self._run_ai(raw, source, speak, agent_task=handoff)
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
            # программа только запускается — «Открываю», а не «Готово»: что она открылась, мы ещё не видели
            app = str(result.data.get("app") or "")
            await self._say(f"Открываю {app}." if app else self._fmt(random.choice(OK_PHRASES)), speak)
        elif not result.ok:
            await self._say(result.message or "Не получилось.", speak)
        self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "command", "ok": result.ok})
        return {"kind": "command", "result": result.to_json(), "history": item}

    def _agent_ready(self) -> bool:
        """Дело можно сразу отдать агенту: ИИ включён и настроен, режим не «Чат», «Сразу запускать агента»."""
        ai = self.ctx.settings.value.ai
        if not (ai.enabled and ai.auto_agent and ai.mode != "chat"):
            return False
        try:
            return bool(self.ctx.ai.configured)
        except Exception:
            return False

    def _taught_rule(self, match: Any) -> str:
        """Правило из памяти для этой команды («когда я прошу включить музыку, надо…»); "" — такого нет.

        Начало правила сверяется разбором команд: «включить музыку» — та же команда, что «включи музыку».
        """
        ids = {p.command_id for p in match.parts}
        try:
            items = self.ctx.notes.memory_list()
        except Exception:
            return ""
        checked = 0
        for item in items:
            text = str(item.get("text") or "").strip()
            for found in _RULE.finditer(text):
                checked += 1
                if checked > MAX_RULES:
                    return ""
                try:
                    trigger = self.ctx.commands.match(found["what"])
                except Exception:
                    trigger = None
                if trigger is not None and ids & {p.command_id for p in trigger.parts}:
                    return _REMEMBER.sub("", text).strip()
        return ""

    async def _run_ai(self, raw: str, source: Source, speak: bool, *, agent_task: str = "") -> dict[str, Any]:
        item = self._history_add(source, raw, "ai", "pending", title=raw, icon="ai")
        chat_id = None
        if source in ("voice", "telegram", "remote"):
            chat_id = self.ctx.db.kv_get(f"voice_chat:{source}")
        try:
            result = await self.ctx.ai.ask(raw, chat_id=chat_id, source=source, agent_task=agent_task)
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
                # Jarvis спросил — ответ слушаем без слова-активатора (и это не «бесконечный разговор»)
                self.ctx.voice.follow_up(question=short.rstrip().endswith("?"))
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
