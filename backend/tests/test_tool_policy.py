from __future__ import annotations

import hashlib

import pytest

from kore_next.application import Application
from kore_next.config import Settings
from kore_next.tools.policies import tool_policy
from kore_next.util import dumps, now


@pytest.fixture
async def policy_app(tmp_path, monkeypatch):
    for name in (
        "KORENEXT_API_KEY",
        "KORENEXT_OPENAI_API_KEY",
        "KORENEXT_QWEN_API_KEY",
        "KORENEXT_DEEPSEEK_API_KEY",
        "KORENEXT_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        user_skills_dir=tmp_path / "user-skills",
        api_key="",
    )
    application = Application(settings)
    await application.start()
    yield application
    await application.close()


def test_tool_policies_are_conservative_for_side_effects():
    assert tool_policy("read_file").recovery == "safe_retry"
    assert tool_policy("memory_read").recovery == "safe_retry"
    assert tool_policy("write_file").recovery == "reconcile"
    assert tool_policy("shell").recovery == "park"
    assert tool_policy("mcp__filesystem__write").recovery == "park"
    assert tool_policy("computer_click").recovery == "park"
    assert tool_policy("computer_observe").recovery == "safe_retry"
    assert tool_policy("http_request", {"method": "GET"}).recovery == "safe_retry"
    assert tool_policy("http_request", {"method": "POST"}).recovery == "reconcile"


async def _run_with_dispatch(app: Application, conversation_id: str, text: str):
    run = await app.runs.create(conversation_id, text, "normal")
    await app.db.execute("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
    return run


async def _dispatch_row(
    app: Application,
    run_id: str,
    call_id: str,
    tool_name: str,
    arguments: dict,
):
    arguments_hash = hashlib.sha256(dumps(arguments).encode("utf-8")).hexdigest()
    await app.db.execute(
        "INSERT INTO tool_executions("
        "run_id,call_id,step,tool_name,arguments_hash,status,started_at"
        ") VALUES(?,?,?,?,?,'dispatched',?)",
        (run_id, call_id, 1, tool_name, arguments_hash, now()),
    )


async def test_stale_recovery_retries_reads_and_parks_writes(policy_app):
    app = policy_app
    conversation = (await app.conversation_create({}))["conversation"]
    read_run = await _run_with_dispatch(app, conversation["id"], "read run")
    write_run = await _run_with_dispatch(app, conversation["id"], "write run")
    await _dispatch_row(app, read_run["id"], "read-1", "read_file", {"path": "a.txt"})
    await _dispatch_row(
        app,
        write_run["id"],
        "write-1",
        "write_file",
        {"path": "x.txt", "content": "x"},
    )

    await app.runs.recover_stale_runs()

    read_row = await app.db.fetchone(
        "SELECT status,attempt FROM tool_executions WHERE run_id=? AND call_id=?",
        (read_run["id"], "read-1"),
    )
    write_row = await app.db.fetchone(
        "SELECT status FROM tool_executions WHERE run_id=? AND call_id=?",
        (write_run["id"], "write-1"),
    )
    read_run_row = await app.db.fetchone("SELECT status,stop_reason FROM runs WHERE id=?", (read_run["id"],))
    write_run_row = await app.db.fetchone("SELECT stop_reason FROM runs WHERE id=?", (write_run["id"],))
    assert read_row["status"] == "retry_ready"
    assert read_run_row["status"] == "interrupted"
    assert read_run_row["stop_reason"] == "host_restart"
    assert write_row["status"] == "unknown"
    assert write_run_row["stop_reason"] == "tool_unknown"

    await app.runs.begin_tool(
        read_run,
        {"id": "read-1", "name": "read_file", "arguments": {"path": "a.txt"}},
        1,
    )
    retried = await app.db.fetchone(
        "SELECT status,attempt,policy,idempotency_key FROM tool_executions WHERE run_id=? AND call_id=?",
        (read_run["id"], "read-1"),
    )
    assert retried["status"] == "dispatched"
    assert retried["attempt"] == 2
    assert retried["policy"] == "read/safe_retry"
    assert retried["idempotency_key"] == f"{read_run['id']}:read-1"
