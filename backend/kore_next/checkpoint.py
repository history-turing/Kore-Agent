from __future__ import annotations

from typing import Any

from .database import Database
from .util import dumps, loads, now


class CheckpointStore:
    def __init__(self, db: Database):
        self.db = db

    async def save(
        self,
        *,
        run_id: str,
        conversation_id: str | None,
        sequence: int,
        state: dict[str, Any],
        conn: Any | None = None,
        through_event_seq: int = 0,
        digest: str | None = None,
    ) -> dict[str, Any]:
        stamp = now()
        sql = (
            "INSERT INTO checkpoints("
            "run_id,conversation_id,sequence,data_json,through_event_seq,digest,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "sequence=excluded.sequence,data_json=excluded.data_json,"
            "through_event_seq=excluded.through_event_seq,digest=excluded.digest,updated_at=excluded.updated_at"
        )
        args = (run_id, conversation_id, sequence, dumps(state), through_event_seq, digest, stamp, stamp)
        if conn is None:
            await self.db.execute(sql, args)
        else:
            await self.db.execute_on(conn, sql, args)
        return {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "sequence": sequence,
            "state": state,
            "through_event_seq": through_event_seq,
            "digest": digest,
            "updated_at": stamp,
        }

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM checkpoints WHERE run_id=?", (run_id,))
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "sequence": row["sequence"],
            "state": loads(row["data_json"], {}),
            "through_event_seq": row["through_event_seq"],
            "digest": row["digest"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def clone(self, source_run_id: str, target_run_id: str) -> dict[str, Any] | None:
        checkpoint = await self.get(source_run_id)
        if checkpoint is None:
            return None
        state = dict(checkpoint["state"])
        state["recovered_from_run_id"] = source_run_id
        return await self.save(
            run_id=target_run_id,
            conversation_id=checkpoint["conversation_id"],
            sequence=checkpoint["sequence"],
            state=state,
            through_event_seq=0,
            digest=None,
        )
