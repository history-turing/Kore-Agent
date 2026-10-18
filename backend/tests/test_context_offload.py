from __future__ import annotations

from pathlib import Path

from kore_next.context_offload import ContextOffloadStore
from kore_next.database import Database


async def test_context_offload_archives_and_reads_bounded_slices(tmp_path: Path):
    db = Database(tmp_path / "data" / "test.db")
    await db.initialize()
    store = ContextOffloadStore(db, tmp_path / "offload", threshold=20, preview_chars=10)
    assert await store.archive("short") is None

    text = "0123456789" * 20
    record = await store.archive(text)
    assert record is not None
    assert Path(record["storage_path"]).is_file()
    assert record["size_bytes"] == len(text.encode("utf-8"))
    inspected = await store.inspect(record["sha256"])
    assert inspected["preview"] == text[:10]
    chunk = await store.read(record["sha256"], offset=5, limit=7)
    assert chunk["content"] == text[5:12]
    assert chunk["next_offset"] == 12
    assert chunk["truncated"] is True


async def test_context_compaction_persists_a_materialized_snapshot(tmp_path: Path):
    from kore_next.context import ContextManager

    db = Database(tmp_path / "data" / "test.db")
    await db.initialize()
    manager = ContextManager(db, reserve_tokens=4_096)
    history = []
    for index in range(420):
        history.append({"role": "user", "content": f"request-{index} " + "x" * 1_200})
        history.append({"role": "assistant", "content": f"answer-{index} " + "y" * 1_200})
    await manager.prepare(
        conversation_id="conversation",
        history=history,
        provider="openai",
        model="small-context-model",
        base_system="system",
    )
    row = await db.fetchone("SELECT * FROM context_snapshots WHERE conversation_id='conversation'")
    assert row is not None
    assert row["covered_messages"] > 0
    assert len(row["source_digest"]) == 64
    assert len(row["summary_hash"]) == 64
