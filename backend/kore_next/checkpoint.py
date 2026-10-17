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
    ) -> dict[str, Any]:
        stamp = now()
        await self.db.execute(
            "INSERT INTO checkpoints(run_id,conversation_id,sequence,data_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "sequence=excluded.sequence,data_json=excluded.data_json,updated_at=excluded.updated_at",
            (run_id, conversation_id, sequence, dumps(state), stamp, stamp),
        )
        return {"run_id": run_id, "conversation_id": conversation_id, "sequence": sequence, "state": state, "updated_at": stamp}

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM checkpoints WHERE run_id=?", (run_id,))
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "sequence": row["sequence"],
            "state": loads(row["data_json"], {}),
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
        )
