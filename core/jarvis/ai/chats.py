"""Чаты Jarvis AI в базе (таблицы chats и messages) и события chat.created/updated/deleted.

В messages лежит вся переписка с моделью: сообщения человека, промежуточные ответы с вызовами инструментов
и результаты инструментов (meta.hidden — интерфейс их не показывает), итоговые ответы ассистента. У ответов и
инструментов meta.turn — id сообщения человека, на которое они отвечают: так история для модели собирается
правильно, даже если человек успел написать следующее сообщение, пока шёл ответ.
"""

from typing import TYPE_CHECKING, Any

from jarvis.db import dumps, loads, new_id, now
from jarvis.services import UserFacingError

if TYPE_CHECKING:
    from jarvis.context import AppContext

MODES = ("chat", "all", "agent")
KINDS = ("chat", "voice")
_INTERNAL_META = ("turn", "hidden", "llm_content")


class ChatNotFound(UserFacingError):
    def __init__(self) -> None:
        super().__init__("Чат не найден")


def _chat(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "mode": row["mode"],
        "model": row["model"],
        "pinned": bool(row["pinned"]),
        "created": row["created"],
        "updated": row["updated"],
    }


def _row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["meta"] = loads(row.get("meta"), {}) or {}
    out["tool_calls"] = loads(row.get("tool_calls"), None)
    return out


def ui_message(row: dict[str, Any]) -> dict[str, Any]:
    """Сообщение для интерфейса: UiMessage из docs/CONTRACTS.md."""
    meta = row["meta"] if isinstance(row.get("meta"), dict) else (loads(row.get("meta"), {}) or {})
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "role": row["role"],
        "content": row["content"],
        "created": row["created"],
        "meta": {k: v for k, v in meta.items() if k not in _INTERNAL_META and v not in (None, [], "")},
    }


class ChatStore:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    # --- чаты ---------------------------------------------------------------------------------------
    def create(
        self, *, title: str = "", kind: str = "chat", mode: str = "all", model: str = ""
    ) -> dict[str, Any]:
        ts = now()
        row = {
            "id": new_id(),
            "title": (title or "").strip()[:120] or "Новый чат",
            "kind": kind if kind in KINDS else "chat",
            "mode": mode if mode in MODES else "all",
            "model": (model or "").strip(),
            "pinned": 0,
            "created": ts,
            "updated": ts,
        }
        self.ctx.db.insert("chats", row)
        chat = _chat(row)
        self.ctx.bus.publish("chat.created", {"chat": chat})
        return chat

    def get(self, chat_id: str | None) -> dict[str, Any] | None:
        if not chat_id:
            return None
        row = self.ctx.db.query_one("SELECT * FROM chats WHERE id=?", (chat_id,))
        return _chat(row) if row else None

    def require(self, chat_id: str) -> dict[str, Any]:
        chat = self.get(chat_id)
        if chat is None:
            raise ChatNotFound()
        return chat

    def all(self) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT * FROM chats ORDER BY pinned DESC, updated DESC")
        return [_chat(r) for r in rows]

    def update(self, chat_id: str, **fields: Any) -> dict[str, Any]:
        self.require(chat_id)
        values: dict[str, Any] = {}
        if fields.get("title") is not None:
            values["title"] = str(fields["title"]).strip()[:120] or "Новый чат"
        if fields.get("pinned") is not None:
            values["pinned"] = 1 if fields["pinned"] else 0
        if fields.get("mode") is not None:
            if fields["mode"] not in MODES:
                raise UserFacingError("Режим чата: chat, all или agent")
            values["mode"] = fields["mode"]
        if fields.get("model") is not None:
            values["model"] = str(fields["model"]).strip()
        if fields.get("touch"):
            values["updated"] = now()
        if values:
            self.ctx.db.update("chats", chat_id, values)
        chat = self.require(chat_id)
        self.ctx.bus.publish("chat.updated", {"chat": chat})
        return chat

    def delete(self, chat_id: str) -> bool:
        removed = self.ctx.db.delete("chats", chat_id) > 0
        if removed:
            self.ctx.db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            self.ctx.bus.publish("chat.deleted", {"id": chat_id})
        return removed

    # --- сообщения --------------------------------------------------------------------------------
    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str = "",
        *,
        meta: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        ts = now()
        row: dict[str, Any] = {
            "id": message_id or new_id(),
            "chat_id": chat_id,
            "role": role,
            "content": content or "",
            "tool_calls": dumps(tool_calls) if tool_calls else None,
            "tool_call_id": tool_call_id,
            "name": name,
            "meta": dumps(meta) if meta else None,
            "created": ts,
        }
        self.ctx.db.insert("messages", row)
        self.ctx.db.update("chats", chat_id, {"updated": ts})
        return _row(row)

    def rows(self, chat_id: str) -> list[dict[str, Any]]:
        """Все сообщения чата по порядку (вместе со скрытыми — для истории модели)."""
        rows = self.ctx.db.query("SELECT * FROM messages WHERE chat_id=? ORDER BY created, rowid", (chat_id,))
        return [_row(r) for r in rows]

    def ui_messages(self, chat_id: str) -> list[dict[str, Any]]:
        return [
            ui_message(r)
            for r in self.rows(chat_id)
            if r["role"] in ("user", "assistant") and not r["meta"].get("hidden")
        ]
