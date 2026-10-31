"""Главный вход ядра: фраза человека (голос, строка, пульт, Telegram) → команда, ИИ или «не понял».

Порядок: «стоп» → команды (быстро, без сети) → ИИ (вопросы, сложные просьбы, «сверни окна и расскажи…») →
«не понял». Каждая фраза попадает в историю (history.*) и даёт тост (toast) в оверлее; на голосовую фразу
Jarvis отвечает голосом (если не включён тихий режим).

Пока работает агент, фраза, которая не готовая команда (или любая, если агент задал вопрос), — ему: поправка
к задаче («нет, не Ане, а Маше»), ответ на его вопрос или «да» / «нет», когда он ждёт разрешения, а не новый
запрос («стоп» — как всегда, стоп).

Агент важнее готовой команды, когда команда не справилась (NeedsAgent: «включи музыку», а трек не выбран
и музыка не заиграла) или человек сам научил, как это делать: в памяти правило «когда я прошу включить
музыку — открой Яндекс Музыку и нажми плей», и его начало — та же команда. Тогда дело сразу берёт агент
с этим правилом (если ИИ настроен, режим не «Чат» и включено «Сразу запускать агента»).

«Запомни это» («сохрани как сценарий») сразу после удачного дела агента — оно становится сценарием с теми же
шагами и фразой человека: в следующий раз — сразу, без ИИ.

С персоной «Джарвис» ответ на команду — в его характере (jarvis.voice.character): «Сворачиваю все окна, сэр.»;
долгая команда (погода из сети, музыка) сразу отвечает «Секунду, сэр.», а итог звучит следом.

После удачного дела голосом Jarvis спрашивает «Что-нибудь ещё, сэр?» и слушает продолжение без «Джарвис»
(voice.ask_more; только если окно продолжения правда откроется — voice.can_follow); «нет», «всё», «ничего» в
ответ — «Как скажете, сэр.», и он перестаёт слушать. После «спасибо», «скройся», выключения и сна — не
спрашивает: разговор окончен.
"""

import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any

from jarvis import text as textops
from jarvis.db import now
from jarvis.services import ExecResult, Source, UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

log = logging.getLogger(__name__)

OK_PHRASES = ("Готово, {address}.", "Сделано, {address}.", "Выполнено.", "Есть, {address}.")
UNKNOWN_SAY = "Не понял команду, {address}."
NO_AI_SAY = "Такой команды нет, а ИИ не настроен, {address}."
NOTED_SAY = "Понял, учту."  # поправка агенту посреди работы
ASK_MORE = ("Что-нибудь ещё, {address}?", "Ещё что-нибудь, {address}?", "Что-то ещё, {address}?")
END_TALK_SAY = ("Как скажете, {address}.", "Хорошо, {address}.", "Обращайтесь, {address}.")
ASK_MORE_WINDOW_S = 60.0  # «нет» дольше этого после «Что-нибудь ещё?» — уже не ответ на него
# «нет», «всё», «ничего» в ответ на «Что-нибудь ещё?» — разговор окончен
_DECLINE = re.compile(
    r"^(?:(?:нет|неа|не)(?: спасибо| не надо| ничего| больше ничего| все)?|ничего(?: не надо| больше)?|"
    r"больше ничего|не надо|(?:это |пока |на этом |вот и )?все(?: спасибо)?|пока ничего|спасибо все|хватит|"
    r"отбой|достаточно|свободен|можешь идти)$"
)
# после этих команд разговор окончен — «Что-нибудь ещё?» не спрашиваем
_ENDS_TALK = re.compile(
    r"^builtin\.(?:jarvis\.(?:thanks|hide|silent_on|mic_off)|"
    r"system\.(?:lock|sleep|hibernate\w*|logoff\w*|shutdown\w*|restart\w*))$"
)


def _declines(text: str) -> bool:
    """«Нет, спасибо», «это всё», «ничего» — ответ «нет» на «Что-нибудь ещё?»."""
    words = re.sub(r"[^\w\s-]", " ", text.casefold().replace("ё", "е"))
    words = re.sub(r"^(?:джарвис|jarvis)\s+", "", " ".join(words.split()))
    return bool(_DECLINE.match(words))


# правило из памяти: «когда я прошу включить музыку, надо…» — «включить музыку» до запятой или «надо»
_RULE = re.compile(
    r"(?<!\w)(?:когда|если|как только)\s+(?:я\s+)?(?:прошу|попрошу|говорю|скажу|хочу|захочу)\s+"
    r"(?P<what>.+?)(?=\s*[,.;:!?—–]|\s+(?:то|надо|нужно|ты|сначала|делай|открывай|нажимай)(?!\w)|$)",
    re.I,
)
_REMEMBER = re.compile(r"^\s*(?:запомни|не забудь)\s*,?\s*(?:что\s+)?", re.I)
# «запомни это» сразу после дела агента — запомнить его сценарием (в следующий раз — без ИИ, сразу)
_SAVE_RUN = re.compile(
    r"^(?:(?:запомни|сохрани)\s+(?:это(?:\s+как\s+сценарий)?|как\s+сценарий)|запомни\s+как\s+(?:это\s+)?делать"
    r"|сделай\s+(?:из\s+этого\s+)?сценарий)(?:,?\s+пожалуйста)?$"
)
MAX_RULES = 30  # записей памяти с правилами, которые сверяем с командой
# «включи на ютубе лофи», «поставь видео с котами»: готовая команда только откроет поиск, а включить ролик
# (щёлкнуть по нему, мимо рекламы) — дело агента на странице
_PLAY = re.compile(
    r"^\W*(?:включи|включить|поставь|поставить|запусти|запустить|воспроизведи|вруби|сыграй|проиграй)(?!\w)",
    re.I,
)
_VIDEO_SEARCH = {"builtin.search.youtube": "YouTube", "builtin.search.rutube": "RuTube"}
# чем стала фраза, пока работает агент (Agent.interject), — для истории
AGENT_REPLY = {
    "answer": "Ответ агенту",
    "approval": "Решение для агента",
    "note": "Агенту — поправка к задаче",
}


class MainDispatcher:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._last_text = ""
        self._last_at = 0.0
        self._asked_more_at = 0.0  # когда спросили «Что-нибудь ещё?» (0 — не спрашивали)
        add_stock = getattr(getattr(ctx, "tts", None), "add_stock", None)
        if callable(add_stock):
            add_stock(self.stock_phrases)

    def stock_phrases(self) -> list[str]:
        """Частые короткие ответы голосом: их звук готовится заранее и хранится на диске — звучат сразу."""
        phrases = (*OK_PHRASES, UNKNOWN_SAY, NO_AI_SAY, *ASK_MORE, *END_TALK_SAY)
        return [self._fmt(p) for p in phrases] + [NOTED_SAY]

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
        if fields.get("status", "pending") != "pending" and item.get("status") == "pending":
            fields["ms"] = max(0, round((now() - float(item["ts"])) * 1000))  # сколько длилось — для истории
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
            out = out.replace(", .", ".").replace(", ?", "?").replace(" ,", ",").replace(",.", ".")
        return out

    async def _say(self, text: str, speak: bool, *, interrupt: bool = True) -> None:
        if not speak or not text or self.ctx.settings.value.voice.silent_mode:
            return
        try:
            await self.ctx.tts.say(text, interrupt=interrupt)
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

        asked, self._asked_more_at = self._asked_more_at, 0.0
        if source == "voice" and asked and time.monotonic() - asked < ASK_MORE_WINDOW_S and _declines(raw):
            return await self._end_talk(raw, speak)

        agent = getattr(self.ctx, "agent", None)
        busy = bool(getattr(agent, "busy", False))
        recent = getattr(agent, "recent_recipe", None)
        if not busy and callable(recent) and recent() and _SAVE_RUN.match(raw.casefold().strip(" .!?")):
            return await self._save_run(raw, source, speak)

        # команды
        match = None
        try:
            match = self.ctx.commands.match(raw)
        except Exception:
            log.exception("разбор команды упал: %r", raw)
        if busy and (match is None or getattr(agent, "waiting_answer", False)):
            return await self._to_agent(raw, source, speak)
        if match is not None:
            rule = self._taught_rule(match) if self._agent_ready() else ""
            if rule:  # человек научил, как это делать, — агенту с его правилом
                task = f"{raw}. Пользователь просил делать это так: {rule}"
                return await self._run_ai(raw, source, speak, agent_task=task)
            video = _video_task(raw, match) if self._agent_ready() else ""
            if video:  # «включи на ютубе …»: поиск откроет и агент, а ролик включит только он
                return await self._run_ai(raw, source, speak, agent_task=video)
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
        character = getattr(self.ctx, "character", None)
        acked = False  # уже прозвучало «Секунду, сэр» — итог говорим следом, не перебивая
        try:
            ack = character.ack_phrases(match) if speak and character is not None else ()
            if ack:
                result, acked = await character.with_ack(self.ctx.commands.execute(match, source), ack)
            else:
                result = await self.ctx.commands.execute(match, source)
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
        # персона «Джарвис»: ответ в его характере (jarvis.voice.character); None — как обычно
        confirmations = settings.speak_confirmations == "short"
        spoken = None
        if speak and character is not None:
            spoken = character.spoken(result, confirmations=confirmations)
        text, interrupt = "", True
        if spoken:
            text, interrupt = spoken, not acked
        elif result.say:
            text, interrupt = result.say, not acked
        elif result.ok and confirmations:
            # программа только запускается — «Открываю», а не «Готово»: что она открылась, мы ещё не видели
            app = str(result.data.get("app") or "")
            if app:
                keep = getattr(self.ctx.tts, "keep", None)
                if callable(keep):
                    keep(f"Открываю {app}.")  # название программы — не личное: звук хранится на диске
                text = f"Открываю {app}."
            else:
                text = self._fmt(random.choice(OK_PHRASES))
        elif not result.ok:
            text, interrupt = result.message or "Не получилось.", not acked
        ends = any(_ENDS_TALK.match(p.command_id) for p in match.parts)
        more = self._ask_more(text, speak) if result.ok and not ends else ""
        if text:
            await self._say(f"{text} {more}" if more else text, speak, interrupt=interrupt)
        if more:
            self.ctx.voice.follow_up(question=True)
        self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "command", "ok": result.ok})
        return {"kind": "command", "result": result.to_json(), "history": item}

    def _ask_more(self, said: str, speak: bool) -> str:
        """«Что-нибудь ещё, сэр?» к ответу голосом — только если Jarvis правда будет слушать продолжение
        (voice.can_follow: голос слушает, продолжение не выключено, не играет музыка, лимит окон не
        исчерпан)."""
        settings = self.ctx.settings.value.voice
        if not speak or not said.strip() or said.rstrip().endswith("?"):
            return ""
        if not settings.ask_more or settings.silent_mode:
            return ""
        can_follow = getattr(self.ctx.voice, "can_follow", None)
        try:
            if not callable(can_follow) or not can_follow(question=True):
                return ""
        except Exception:
            return ""
        self._asked_more_at = time.monotonic()
        return self._fmt(random.choice(ASK_MORE))

    async def _end_talk(self, raw: str, speak: bool) -> dict[str, Any]:
        """«Нет», «всё» в ответ на «Что-нибудь ещё?»: коротко ответить и больше не слушать без «Джарвис»."""
        await self._say(self._fmt(random.choice(END_TALK_SAY)), speak)
        self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "end"})
        return {"kind": "end"}

    async def _to_agent(self, raw: str, source: Source, speak: bool) -> dict[str, Any]:
        """Фраза, пока агент работает: ему — ответом на вопрос, решением о разрешении или поправкой
        (на поправку — «Понял, учту» вслух)."""
        agent = self.ctx.agent
        kind = str(agent.interject(raw, source) or "")
        delivered = bool(kind)
        detail = AGENT_REPLY.get(kind, "Агент уже закончил")
        item = self._history_add(source, raw, "ai", "ok" if delivered else "error", title=raw, detail=detail)
        if kind == "note":
            await self._say(NOTED_SAY, speak)
        self.ctx.bus.publish("dispatch.done", {"text": raw, "kind": "ai", "ok": delivered})
        current = getattr(agent, "current", None)
        return {
            "kind": "ai",
            "chat_id": getattr(current, "chat_id", "") or "",
            "text": detail,
            "actions": [],
            "sources": [],
            "error": "" if delivered else detail,
            "history": item,
        }

    async def _save_run(self, raw: str, source: Source, speak: bool) -> dict[str, Any]:
        """«Запомни это»: последнее удачное дело агента — сценарием с теми же шагами и фразой человека."""
        try:
            scenario = self.ctx.agent.save_scenario()
        except UserFacingError as e:
            result = ExecResult(False, "Запомнить сценарием", str(e), icon="error")
        else:
            result = ExecResult(
                True,
                f"Сценарий «{scenario.get('name', '')}»",
                "Сохранён: в следующий раз — сразу, без ИИ",
                icon="ai",
                say=self._fmt("Запомнил, {address}. В следующий раз сделаю сразу."),
                data={"scenario_id": scenario.get("id", "")},
            )
        status = "ok" if result.ok else "error"
        item = self._history_add(source, raw, "command", status, title=result.title, detail=result.message)
        self.toast(result.title, result.message, status=status, icon=result.icon)
        await self._say(result.say or result.message, speak)
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
            question = short.rstrip().endswith("?")
            more = "" if question else self._ask_more(short, speak)
            await self._say(f"{short} {more}" if more else short, speak)
            if speak:
                # Jarvis спросил — ответ слушаем без слова-активатора (и это не «бесконечный разговор»)
                self.ctx.voice.follow_up(question=question or bool(more))
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


def _video_task(raw: str, match: Any) -> str:
    """«Включи на ютубе лофи» — задача агенту: найти и включить ролик; "" — это не просьба включить видео."""
    parts = list(getattr(match, "parts", None) or [])
    if len(parts) != 1 or parts[0].command_id not in _VIDEO_SEARCH or not _PLAY.match(raw):
        return ""
    site = _VIDEO_SEARCH[parts[0].command_id]
    query = str((parts[0].slots or {}).get("query") or "").strip()
    what = f"по запросу «{query}»" if query else "по этой просьбе"
    return f"{raw}. Найди на {site} видео {what} и включи его (не рекламу); проверь, что оно играет."
