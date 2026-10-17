from __future__ import annotations

import hashlib
from typing import Any

from .database import Database
from .util import dumps, loads, now, uid


class EvidenceStore:
    def __init__(self, db: Database, *, max_records: int = 10_000):
        self.db = db
        self.max_records = max_records

    async def record(self, run_id: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        run = await self.db.fetchone("SELECT conversation_id FROM runs WHERE id=?", (run_id,))
        raw = str(data.get("output") or "")
        item = {
            "id": uid(), "run_id": run_id, "conversation_id": run["conversation_id"] if run else None,
            "kind": kind, "tool_call_id": data.get("tool_call_id"),
            "task_id": data.get("task_id"), "task_step": data.get("task_step"),
            "content_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "data": data, "created_at": now(),
        }
        await self.db.execute(
            "INSERT INTO evidence(id,run_id,kind,data_json,created_at) VALUES(?,?,?,?,?)",
            (item["id"], run_id, kind, dumps(item), item["created_at"]),
        )
        await self.db.execute(
            "DELETE FROM evidence WHERE id IN (SELECT id FROM evidence ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
            (self.max_records,),
        )
        return item

    async def get(self, evidence_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT data_json FROM evidence WHERE id=?", (evidence_id,))
        if not row:
            raise KeyError("evidence not found")
        return self._with_legacy_marker(loads(row["data_json"], {}))

    async def search(self, query: str, *, run_id: str | None = None, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        clauses, values = [], []
        if run_id:
            clauses.append("run_id=?")
            values.append(run_id)
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self.db.fetchall(f"SELECT data_json FROM evidence{where} ORDER BY created_at DESC", tuple(values))
        items = [self._with_legacy_marker(loads(row["data_json"], {})) for row in rows]
        terms = {part.lower() for part in query.split() if part}
        if terms:
            items = [item for item in items if terms & set(dumps(item).lower().split()) or all(term in dumps(item).lower() for term in terms)]
        return items[:max(1, min(limit, 100))]

    @staticmethod
    def _with_legacy_marker(item: dict[str, Any]) -> dict[str, Any]:
        if "conversation_id" not in item or "content_sha256" not in item:
            return {**item, "legacy_format": True}
        return item
