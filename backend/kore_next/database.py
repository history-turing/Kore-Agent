from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 workspace_path TEXT
);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
 content TEXT, name TEXT, tool_call_id TEXT, tool_calls_json TEXT, reasoning TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
 content, content='messages', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages
WHEN new.role IN ('user','assistant') BEGIN
 INSERT INTO messages_fts(rowid,content) VALUES(new.id,new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,content) VALUES('delete',old.id,old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,content) VALUES('delete',old.id,old.content);
 INSERT INTO messages_fts(rowid,content) VALUES(new.id,new.content);
END;
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, conversation_id TEXT, status TEXT NOT NULL, user_message TEXT NOT NULL,
 created_at TEXT NOT NULL, started_at TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
 error TEXT, stop_reason TEXT, recovered_from_run_id TEXT, source TEXT, source_id TEXT,
 scheduled_for TEXT, triggered_at TEXT, mode TEXT NOT NULL DEFAULT 'normal',
 workspace_path TEXT, workspace_source TEXT NOT NULL DEFAULT 'none'
);
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL, type TEXT NOT NULL,
 event_time TEXT NOT NULL, data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence);
CREATE TABLE IF NOT EXISTS runtime_events(
 event_id TEXT PRIMARY KEY, conversation_id TEXT, turn_id TEXT NOT NULL, run_id TEXT NOT NULL,
 invocation_id TEXT, event_seq INTEGER NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL, schema_version INTEGER NOT NULL, prev_digest TEXT, digest TEXT NOT NULL,
 UNIQUE(run_id, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_runtime_events_conversation ON runtime_events(conversation_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_runtime_events_type ON runtime_events(run_id, type);
CREATE TABLE IF NOT EXISTS tool_executions(
 run_id TEXT NOT NULL, call_id TEXT NOT NULL, step INTEGER NOT NULL, tool_name TEXT NOT NULL,
 arguments_hash TEXT NOT NULL, status TEXT NOT NULL, output_hash TEXT, error TEXT, error_code TEXT,
 policy TEXT, attempt INTEGER NOT NULL DEFAULT 1, idempotency_key TEXT,
 started_at TEXT NOT NULL, finished_at TEXT,
 PRIMARY KEY(run_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_executions_status ON tool_executions(status, started_at);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, data_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts(
 id TEXT PRIMARY KEY, data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automations(
 id TEXT PRIMARY KEY, data_json TEXT NOT NULL, status TEXT NOT NULL, next_run_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_occurrences(
 trigger_id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, scheduled_for TEXT NOT NULL,
 run_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(automation_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS idx_automation_occurrences_automation ON automation_occurrences(automation_id, created_at);
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY, data_json TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints(
 run_id TEXT PRIMARY KEY, conversation_id TEXT, sequence INTEGER NOT NULL,
 data_json TEXT NOT NULL, through_event_seq INTEGER NOT NULL DEFAULT 0, digest TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_snapshots(
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, covered_messages INTEGER NOT NULL,
 source_digest TEXT NOT NULL, summary TEXT NOT NULL, summary_hash TEXT NOT NULL,
 provider TEXT, model TEXT, policy_version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_snapshots_conversation
 ON context_snapshots(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS context_offloads(
 sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, storage_path TEXT NOT NULL,
 mime_type TEXT, preview TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id, created_at);
CREATE TABLE IF NOT EXISTS permission_rules(
 id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, effect TEXT NOT NULL,
 scope TEXT NOT NULL, scope_id TEXT NOT NULL, matcher_json TEXT NOT NULL,
 description TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_candidates(
 id TEXT PRIMARY KEY, status TEXT NOT NULL, data_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
"""

SCHEMA_VERSION = 10


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.path.is_file() and self.path.stat().st_size > 0
        with sqlite3.connect(self.path) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Database schema version {version} is newer than this app")
            if existing and version < SCHEMA_VERSION:
                # Snapshot before any schema or FTS rebuild touches existing data.
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                backup = self.path.with_name(f"{self.path.stem}.backup-v{version}-{stamp}.db")
                with sqlite3.connect(backup) as destination:
                    connection.backup(destination)
        async with self.connect() as db:
            await db.executescript(SCHEMA)
            if version < 5:
                exists = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'")
                if await exists.fetchone():
                    columns = await db.execute("PRAGMA table_info(checkpoints)")
                    names = {row["name"] for row in await columns.fetchall()}
                    if "through_event_seq" not in names:
                        await db.execute(
                            "ALTER TABLE checkpoints ADD COLUMN through_event_seq INTEGER NOT NULL DEFAULT 0"
                        )
                    if "digest" not in names:
                        await db.execute("ALTER TABLE checkpoints ADD COLUMN digest TEXT")
            if version < 7:
                await db.execute("DROP TRIGGER IF EXISTS messages_fts_insert")
                await db.execute(
                    "CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages "
                    "WHEN new.role IN ('user','assistant') BEGIN "
                    "INSERT INTO messages_fts(rowid,content) VALUES(new.id,new.content); END"
                )
            if version < 8:
                columns = await db.execute("PRAGMA table_info(tool_executions)")
                names = {row["name"] for row in await columns.fetchall()}
                if "error_code" not in names:
                    await db.execute("ALTER TABLE tool_executions ADD COLUMN error_code TEXT")
            if version < 9:
                columns = await db.execute("PRAGMA table_info(tool_executions)")
                names = {row["name"] for row in await columns.fetchall()}
                if "policy" not in names:
                    await db.execute("ALTER TABLE tool_executions ADD COLUMN policy TEXT")
                if "attempt" not in names:
                    await db.execute("ALTER TABLE tool_executions ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
                if "idempotency_key" not in names:
                    await db.execute("ALTER TABLE tool_executions ADD COLUMN idempotency_key TEXT")
            if version < 10:
                columns = await db.execute("PRAGMA table_info(conversations)")
                names = {row["name"] for row in await columns.fetchall()}
                if "workspace_path" not in names:
                    await db.execute("ALTER TABLE conversations ADD COLUMN workspace_path TEXT")
                columns = await db.execute("PRAGMA table_info(runs)")
                names = {row["name"] for row in await columns.fetchall()}
                if "workspace_path" not in names:
                    await db.execute("ALTER TABLE runs ADD COLUMN workspace_path TEXT")
                if "workspace_source" not in names:
                    await db.execute("ALTER TABLE runs ADD COLUMN workspace_source TEXT NOT NULL DEFAULT 'none'")
            if version < 2:
                await db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            await db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )
            await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await db.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            await db.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize a short write transaction and roll it back on any failure."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def fetchone(self, sql: str, args: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.connect() as db:
            cursor = await db.execute(sql, args)
            return await cursor.fetchone()

    async def fetchall(self, sql: str, args: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.connect() as db:
            cursor = await db.execute(sql, args)
            return list(await cursor.fetchall())

    async def execute(self, sql: str, args: tuple[Any, ...] = ()) -> None:
        async with self.connect() as db:
            await db.execute(sql, args)
            await db.commit()

    @staticmethod
    async def fetchone_on(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        cursor = await db.execute(sql, args)
        return await cursor.fetchone()

    @staticmethod
    async def fetchall_on(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cursor = await db.execute(sql, args)
        return list(await cursor.fetchall())

    @staticmethod
    async def execute_on(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()) -> None:
        await db.execute(sql, args)

    async def set_json(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    async def get_json(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value_json FROM kv WHERE key=?", (key,))
        return json.loads(row["value_json"]) if row else default
