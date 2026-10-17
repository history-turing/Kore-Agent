from __future__ import annotations

from typing import Any

from ..database import Database
from ..rpc import RpcFault, required
from ..util import dumps, loads, now, uid


class ConversationService:
    """Conversation persistence and search; no agent or model dependencies."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _conversation(row: Any, count: int = 0) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": count,
        }

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(params.get("limit", 50)), 200))
        rows = await self.db.fetchall(
            "SELECT c.*,COUNT(m.id) AS n FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id "
            "GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
        return {"conversations": [self._conversation(r, r["n"]) for r in rows]}

    async def create(self, params: dict[str, Any]) -> dict[str, Any]:
        stamp, cid = now(), uid()
        title = str(params.get("title") or "新对话").strip()[:120]
        await self.db.execute(
            "INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
            (cid, title, stamp, stamp),
        )
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id=?", (cid,))
        return {"conversation": self._conversation(row)}

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        cid = str(required(params, "conversation_id"))
        row = await self.require(cid)
        msgs = await self.db.fetchall("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,))
        messages = [
            {
                "role": m["role"],
                "content": m["content"],
                "name": m["name"],
                "tool_call_id": m["tool_call_id"],
                "tool_calls": loads(m["tool_calls_json"], []),
                "reasoning": m["reasoning"],
            }
            for m in msgs
        ]
        return {"conversation": self._conversation(row, len(messages)), "messages": messages}

    async def search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(required(params, "query")).strip()
        if not query:
            raise RpcFault(-32602, "Search query is empty")
        limit = max(1, min(int(params.get("limit", 20)), 100))
        if len(query) >= 3:
            rows = await self.db.fetchall(
                "SELECT m.id AS message_id,m.conversation_id,c.title,m.role,m.content,m.created_at "
                "FROM messages_fts JOIN messages AS m ON m.id=messages_fts.rowid "
                "JOIN conversations AS c ON c.id=m.conversation_id "
                "WHERE messages_fts MATCH ? AND m.role IN ('user','assistant') "
                "ORDER BY m.id DESC LIMIT ?",
                ('"' + query.replace('"', '""') + '"', limit),
            )
        else:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = await self.db.fetchall(
                "SELECT m.id AS message_id,m.conversation_id,c.title,m.role,m.content,m.created_at "
                "FROM messages AS m JOIN conversations AS c ON c.id=m.conversation_id "
                "WHERE m.content LIKE ? ESCAPE '\\' AND m.role IN ('user','assistant') "
                "ORDER BY m.id DESC LIMIT ?",
                (f"%{escaped}%", limit),
            )
        return {"matches": [{**dict(row), "content": str(row["content"] or "")[:1_500]} for row in rows]}

    async def rename(self, params: dict[str, Any]) -> dict[str, Any]:
        cid, title = (
            str(required(params, "conversation_id")),
            str(required(params, "title")).strip()[:120],
        )
        await self.require(cid)
        await self.db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (title, now(), cid))
        return (await self.get({"conversation_id": cid})) | {"messages": []}

    async def delete(self, params: dict[str, Any]) -> dict[str, bool]:
        cid = str(required(params, "conversation_id"))
        await self.require(cid)
        await self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        return {"deleted": True}

    async def require(self, cid: str) -> Any:
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id=?", (cid,))
        if not row:
            raise RpcFault(-32000, "Conversation not found")
        return row

    async def add_message(self, cid: str, message: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO messages(conversation_id,role,content,name,tool_call_id,tool_calls_json,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                cid,
                message["role"],
                message.get("content"),
                message.get("name"),
                message.get("tool_call_id"),
                dumps(message.get("tool_calls", [])),
                message.get("reasoning"),
                now(),
            ),
        )
        await self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
