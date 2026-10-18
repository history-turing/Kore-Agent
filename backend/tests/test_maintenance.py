from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kore_next.database import Database
from kore_next.maintenance import MaintenanceService
from kore_next.util import dumps


async def test_retention_prunes_derived_records_and_keeps_runtime_events(tmp_path: Path):
    db = Database(tmp_path / "data" / "test.db")
    await db.initialize()
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat().replace("+00:00", "Z")
    await db.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-old",
            "conversation-old",
            "completed",
            "old run",
            old,
            old,
            old,
            old,
            None,
            "completed",
            None,
            None,
            None,
            None,
            None,
            "normal",
        ),
    )
    await db.execute(
        "INSERT INTO events VALUES(?,?,?,?,?,?)",
        ("event-old", "run-old", 1, "agent_completed", old, dumps({"type": "agent_completed"})),
    )
    await db.execute(
        "INSERT INTO evidence VALUES(?,?,?,?,?)",
        ("evidence-old", "run-old", "tool_result", dumps({"data": {}}), old),
    )
    offload_root = tmp_path / "offload"
    offload_root.mkdir()
    offload_file = offload_root / "old.txt"
    offload_file.write_text("old", encoding="utf-8")
    await db.execute(
        "INSERT INTO context_offloads VALUES(?,?,?,?,?,?)",
        ("sha-old", 3, str(offload_file), "text/plain", "old", old),
    )
    await db.execute(
        "INSERT INTO runtime_events("
        "event_id,conversation_id,turn_id,run_id,event_seq,type,payload_json,created_at,schema_version,digest"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("runtime-old", "conversation-old", "run-old", "run-old", 1, "tool_result", dumps({}), old, 1, "digest"),
    )

    service = MaintenanceService(db, offload_root, retention_days=30)
    preview = await service.prune(dry_run=True)
    assert preview["legacy_events"] == 1
    assert preview["evidence"] == 1
    assert preview["offloads"] == 1
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM events"))["n"] == 1

    result = await service.prune()
    assert result["legacy_events"] == 1
    assert result["evidence"] == 1
    assert result["offloads"] == 1
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM events"))["n"] == 0
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM evidence"))["n"] == 0
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM context_offloads"))["n"] == 0
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM runtime_events"))["n"] == 1
    assert not offload_file.exists()
