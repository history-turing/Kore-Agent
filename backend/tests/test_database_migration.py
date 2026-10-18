from __future__ import annotations

import sqlite3
from pathlib import Path

from kore_next.database import Database


async def test_v9_database_gets_workspace_columns(tmp_path: Path):
    path = tmp_path / "koreagent-next.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations(
              id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE runs(
              id TEXT PRIMARY KEY, conversation_id TEXT, status TEXT NOT NULL, user_message TEXT NOT NULL,
              created_at TEXT NOT NULL, started_at TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
              error TEXT, stop_reason TEXT, recovered_from_run_id TEXT, source TEXT, source_id TEXT,
              scheduled_for TEXT, triggered_at TEXT, mode TEXT NOT NULL DEFAULT 'normal'
            );
            PRAGMA user_version=9;
            """
        )

    await Database(path).initialize()

    with sqlite3.connect(path) as connection:
        conversation_columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)").fetchall()}
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "workspace_path" in conversation_columns
    assert {"workspace_path", "workspace_source"} <= run_columns
    assert version == 10
