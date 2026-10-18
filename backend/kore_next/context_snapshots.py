from __future__ import annotations

import hashlib
from typing import Any

from .database import Database
from .util import now, uid


class ContextSnapshotStore:
    """Persistent compaction checkpoints; the raw message history stays authoritative."""

    policy_version = 1

    def __init__(self, db: Database):
        self.db = db

    async def latest(self, conversation_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM context_snapshots WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        )
        if not row:
            return None
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "covered_messages": row["covered_messages"],
            "source_digest": row["source_digest"],
            "summary": row["summary"],
            "summary_hash": row["summary_hash"],
            "provider": row["provider"],
            "model": row["model"],
            "policy_version": row["policy_version"],
            "created_at": row["created_at"],
        }

    async def save(
        self,
        *,
        conversation_id: str,
        covered_messages: int,
        source_digest: str,
        summary: str,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        record = {
            "id": uid(),
            "conversation_id": conversation_id,
            "covered_messages": max(0, int(covered_messages)),
            "source_digest": source_digest,
            "summary": summary,
            "summary_hash": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            "provider": provider,
            "model": model,
            "policy_version": self.policy_version,
            "created_at": now(),
        }
        await self.db.execute(
            "INSERT INTO context_snapshots("
            "id,conversation_id,covered_messages,source_digest,summary,summary_hash,"
            "provider,model,policy_version,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(
                record[key]
                for key in (
                    "id",
                    "conversation_id",
                    "covered_messages",
                    "source_digest",
                    "summary",
                    "summary_hash",
                    "provider",
                    "model",
                    "policy_version",
                    "created_at",
                )
            ),
        )
        return record
