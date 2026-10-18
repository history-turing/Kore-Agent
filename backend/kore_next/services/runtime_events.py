from __future__ import annotations

import hashlib
import json
from typing import Any

from ..database import Database
from ..util import dumps, loads

ENVELOPE_FIELDS = {
    "event_id",
    "conversation_id",
    "turn_id",
    "run_id",
    "invocation_id",
    "event_seq",
    "sequence",
    "type",
    "event_time",
    "created_at",
    "schema_version",
    "prev_digest",
    "digest",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RuntimeEventStore:
    """Append-only runtime facts with a per-run sequence and digest chain."""

    schema_version = 1

    def __init__(self, db: Database):
        self.db = db

    async def append(self, conn: Any, *, run: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        latest = await self.db.fetchone_on(
            conn,
            "SELECT event_seq FROM runtime_events WHERE run_id=? ORDER BY event_seq DESC LIMIT 1",
            (run["id"],),
        )
        if latest is None:
            await self._backfill_legacy(conn, run)
        return await self._append(conn, run=run, event=event)

    async def _backfill_legacy(self, conn: Any, run: dict[str, Any]) -> None:
        rows = await self.db.fetchall_on(
            conn,
            "SELECT * FROM events WHERE run_id=? ORDER BY sequence",
            (run["id"],),
        )
        for row in rows:
            event = loads(row["data_json"], {})
            event.setdefault("event_id", row["id"])
            event.setdefault("run_id", run["id"])
            event.setdefault("conversation_id", run.get("conversation_id"))
            event.setdefault("sequence", row["sequence"])
            event.setdefault("type", row["type"])
            event.setdefault("event_time", row["event_time"])
            await self._append(conn, run=run, event=event)

    async def _append(self, conn: Any, *, run: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in event.items() if key not in ENVELOPE_FIELDS}
        previous = await self.db.fetchone_on(
            conn,
            "SELECT digest FROM runtime_events WHERE run_id=? ORDER BY event_seq DESC LIMIT 1",
            (run["id"],),
        )
        prev_digest = previous["digest"] if previous else None
        turn_id = str(run.get("turn_id") or run["id"])
        invocation_id = event.get("invocation_id")
        body = {
            "event_id": event["event_id"],
            "conversation_id": event.get("conversation_id") or run.get("conversation_id"),
            "turn_id": turn_id,
            "run_id": run["id"],
            "invocation_id": invocation_id,
            "event_seq": int(event["sequence"]),
            "type": event["type"],
            "created_at": event["event_time"],
            "schema_version": self.schema_version,
            "payload": payload,
        }
        digest = hashlib.sha256(((prev_digest or "") + _canonical(body)).encode("utf-8")).hexdigest()
        await self.db.execute_on(
            conn,
            "INSERT INTO runtime_events("
            "event_id,conversation_id,turn_id,run_id,invocation_id,event_seq,type,payload_json,created_at,"
            "schema_version,prev_digest,digest"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                body["event_id"],
                body["conversation_id"],
                turn_id,
                body["run_id"],
                invocation_id,
                body["event_seq"],
                body["type"],
                dumps(payload),
                body["created_at"],
                self.schema_version,
                prev_digest,
                digest,
            ),
        )
        return {**body, "prev_digest": prev_digest, "digest": digest}

    async def list(self, run_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM runtime_events WHERE run_id=? AND event_seq>? ORDER BY event_seq LIMIT ?",
            (run_id, max(0, after_seq), max(1, min(limit, 20_000))),
        )
        return [
            {
                "event_id": row["event_id"],
                "conversation_id": row["conversation_id"],
                "turn_id": row["turn_id"],
                "run_id": row["run_id"],
                "invocation_id": row["invocation_id"],
                "event_seq": row["event_seq"],
                "sequence": row["event_seq"],
                "type": row["type"],
                "created_at": row["created_at"],
                "event_time": row["created_at"],
                "schema_version": row["schema_version"],
                "prev_digest": row["prev_digest"],
                "digest": row["digest"],
                **loads(row["payload_json"], {}),
            }
            for row in rows
        ]

    async def verify(self, run_id: str, *, through_event_seq: int | None = None) -> dict[str, Any]:
        clause = " AND event_seq<=?" if through_event_seq is not None else ""
        args = (run_id, through_event_seq) if through_event_seq is not None else (run_id,)
        rows = await self.db.fetchall(
            f"SELECT * FROM runtime_events WHERE run_id=?{clause} ORDER BY event_seq",
            args,
        )
        previous: str | None = None
        for expected, row in enumerate(rows, start=1):
            if int(row["event_seq"]) != expected:
                return {"valid": False, "event_seq": row["event_seq"], "error": "event sequence gap"}
            payload = loads(row["payload_json"], {})
            body = {
                "event_id": row["event_id"],
                "conversation_id": row["conversation_id"],
                "turn_id": row["turn_id"],
                "run_id": row["run_id"],
                "invocation_id": row["invocation_id"],
                "event_seq": int(row["event_seq"]),
                "type": row["type"],
                "created_at": row["created_at"],
                "schema_version": row["schema_version"],
                "payload": payload,
            }
            expected_digest = hashlib.sha256(((previous or "") + _canonical(body)).encode("utf-8")).hexdigest()
            if row["prev_digest"] != previous or row["digest"] != expected_digest:
                return {"valid": False, "event_seq": row["event_seq"], "error": "digest mismatch"}
            previous = row["digest"]
        return {
            "valid": True,
            "event_seq": int(rows[-1]["event_seq"]) if rows else 0,
            "digest": previous,
            "events": len(rows),
        }

    async def latest(self, run_id: str, *, conn: Any | None = None) -> dict[str, Any]:
        sql = "SELECT event_seq,digest FROM runtime_events WHERE run_id=? ORDER BY event_seq DESC LIMIT 1"
        row = await (
            self.db.fetchone_on(conn, sql, (run_id,)) if conn is not None else self.db.fetchone(sql, (run_id,))
        )
        return (
            {"event_seq": int(row["event_seq"]), "digest": row["digest"]} if row else {"event_seq": 0, "digest": None}
        )

    async def latest_seq(self, run_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT COALESCE(MAX(event_seq),0) AS n FROM runtime_events WHERE run_id=?",
            (run_id,),
        )
        return int(row["n"]) if row else 0
