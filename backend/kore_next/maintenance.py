from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .database import Database

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")


class MaintenanceService:
    """Retention for derived indexes and large offload files."""

    def __init__(
        self,
        db: Database,
        offload_root: Path,
        *,
        retention_days: int = 30,
        interval_seconds: int = 86_400,
    ):
        self.db = db
        self.offload_root = offload_root
        self.retention_days = max(1, int(retention_days))
        self.interval_seconds = max(60, int(interval_seconds))

    async def prune(self, *, dry_run: bool = False) -> dict[str, Any]:
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat().replace("+00:00", "Z")
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        legacy_events = await self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM events WHERE event_time<? AND run_id IN "
            f"(SELECT id FROM runs WHERE status IN ({placeholders}))",
            (cutoff, *TERMINAL_STATUSES),
        )
        evidence = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM evidence WHERE created_at<?",
            (cutoff,),
        )
        offloads = await self.db.fetchall(
            "SELECT sha256,storage_path FROM context_offloads c WHERE created_at<? "
            "AND NOT EXISTS (SELECT 1 FROM runtime_events r WHERE r.payload_json LIKE '%'||c.sha256||'%') "
            "AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.data_json LIKE '%'||c.sha256||'%') "
            "LIMIT 500",
            (cutoff,),
        )
        result = {
            "cutoff": cutoff,
            "retention_days": self.retention_days,
            "dry_run": dry_run,
            "legacy_events": int(legacy_events["n"] if legacy_events else 0),
            "evidence": int(evidence["n"] if evidence else 0),
            "offloads": len(offloads),
        }
        if dry_run:
            return result

        await self.db.execute(
            f"DELETE FROM events WHERE event_time<? AND run_id IN "
            f"(SELECT id FROM runs WHERE status IN ({placeholders}))",
            (cutoff, *TERMINAL_STATUSES),
        )
        await self.db.execute("DELETE FROM evidence WHERE created_at<?", (cutoff,))
        for row in offloads:
            await self.db.execute(
                "DELETE FROM context_offloads WHERE sha256=?",
                (row["sha256"],),
            )
            path = Path(row["storage_path"]).resolve()
            try:
                path.relative_to(self.offload_root.resolve())
            except ValueError:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        return result

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Maintenance must never terminate the host.
                continue
