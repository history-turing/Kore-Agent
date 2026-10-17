from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kore_next.checkpoint import CheckpointStore
from kore_next.computer_guard import ComputerStagnationGuard
from kore_next.context import ContextManager, TokenEstimator
from kore_next.database import Database
from kore_next.evidence import EvidenceStore
from kore_next.mcp import McpProcess
from kore_next.memory import MemoryService
from kore_next.permissions import PermissionRuleStore
from kore_next.runtime_policy import RepeatedToolCallGuard, RunBudget, RunBudgetConfig, RuntimePolicyError
from kore_next.sandbox import SandboxError, SandboxSupervisor
from kore_next.skill_learning import SkillLearningService
from kore_next.tasks import TaskService
from kore_next.util import dumps, now


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "data" / "test.db")
    await db.initialize()
    return db


async def test_context_compacts_old_rounds_and_persists_summary(database: Database):
    history = []
    for index in range(420):
        history.append({"role": "user", "content": f"request-{index} " + "x" * 1200})
        history.append({"role": "assistant", "content": f"answer-{index} " + "y" * 1200})
    manager = ContextManager(database, reserve_tokens=4096)
    decision = await manager.prepare(
        conversation_id="conversation",
        history=history,
        provider="openai",
        model="small-context-model",
        base_system="system",
    )
    assert decision.compacted is True
    assert decision.dropped_messages > 0
    assert decision.estimated_tokens <= decision.max_input_tokens
    saved = await database.get_json("conversation_summary:conversation")
    assert saved["covered_messages"] == decision.dropped_messages


async def test_step_context_compacts_growing_tool_history(database: Database):
    manager = ContextManager(database)
    messages = [{"role": "system", "content": "Follow the user's task."}, {"role": "user", "content": "Analyze the files."}]
    for index in range(90):
        call_id = f"call-{index}"
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "name": "read_file"}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": f"result-{index} " + "x" * 4_000})
    decision = await manager.prepare_step(
        messages=messages, provider="openai", model="small-context-model", tool_schema_tokens=500,
    )
    assert decision.compacted is True
    assert decision.estimated_tokens <= decision.max_input_tokens
    assert "Run context summary:" in decision.messages[0]["content"]
    assert decision.messages[-1]["tool_call_id"] == "call-89"
    assert len(decision.messages) < len(messages)


def test_token_estimator_counts_cjk_and_tool_payloads():
    estimator = TokenEstimator()
    plain = estimator.count_text("hello world")
    chinese = estimator.count_text("这是一个中文上下文")
    with_tool = estimator.count_message({"role": "assistant", "content": "ok", "tool_calls": [{"name": "read_file"}]})
    assert plain > 0
    assert chinese > plain
    assert with_tool > estimator.count_message({"role": "assistant", "content": "ok"})


async def test_checkpoint_can_be_cloned_for_recovery(database: Database):
    store = CheckpointStore(database)
    await store.save(run_id="old", conversation_id="c1", sequence=4, state={"next_step": 5, "model_messages": []})
    cloned = await store.clone("old", "new")
    assert cloned is not None
    assert cloned["sequence"] == 4
    assert cloned["state"]["recovered_from_run_id"] == "old"
    assert (await store.get("new"))["state"]["next_step"] == 5


async def test_existing_messages_are_indexed_during_database_upgrade(tmp_path: Path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,created_at TEXT,updated_at TEXT)")
        connection.execute(
            "CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,role TEXT,content TEXT,"
            "name TEXT,tool_call_id TEXT,tool_calls_json TEXT,reasoning TEXT,created_at TEXT)"
        )
        connection.execute("INSERT INTO conversations VALUES('old','Old','now','now')")
        connection.execute("INSERT INTO messages VALUES(1,'old','user','历史星球记录',NULL,NULL,'[]',NULL,'now')")
        connection.execute("PRAGMA user_version=1")
    upgraded = Database(path)
    await upgraded.initialize()
    rows = await upgraded.fetchall("SELECT rowid FROM messages_fts WHERE content LIKE '%星球%'")
    assert [row["rowid"] for row in rows] == [1]


async def test_task_plan_requires_a_single_decision(database: Database):
    service = TaskService(database)
    task = await service.create(title="Plan", goal="Ship", steps=[{"title": "Build"}], status="pending")
    accepted = await service.decide(task["id"], True)
    assert accepted["status"] == "active"
    with pytest.raises(ValueError, match="already resolved"):
        await service.decide(task["id"], False)


async def test_memory_reflection_deduplicates_and_updates_core(database: Database):
    service = MemoryService(database)

    async def model(_: str) -> str:
        return dumps(
            {
                "memories": [{"title": "Preferred language", "summary": "Chinese replies", "content": "The user prefers Chinese."}],
                "core_updates": {"response_language": "Chinese"},
            }
        )

    first = await service.reflect(user_text="中文回答", assistant_text="好的", model=model)
    second = await service.reflect(user_text="继续中文", assistant_text="好的", model=model)
    assert len(first) == len(second) == 1
    listing = await service.list()
    assert listing["active_count"] == 1
    assert listing["active"][0]["revision"] == 2
    assert "response_language: Chinese" in listing["core"]


async def test_memory_maintenance_only_archives_model_selected_candidates(database: Database):
    service = MemoryService(database, max_active=1)
    first = await service.create(title="First", summary="One", content="One")
    second = await service.create(title="Second", summary="Two", content="Two")
    assert (await service.list())["active_count"] == 2

    async def failed_model(_: str) -> str:
        raise RuntimeError("offline")
    assert await service.maintain(failed_model) == []
    assert (await service.list())["active_count"] == 2

    async def decision(_: str) -> str:
        return dumps({"decisions": [
            {"id": "invented", "action": "archive"},
            {"id": first["id"], "action": "archive", "reason": "obsolete"},
            {"id": second["id"], "action": "archive"},
        ]})
    archived = await service.maintain(decision)
    assert [item["id"] for item in archived] == [first["id"]]
    listing = await service.list()
    assert listing["active_count"] == 1
    assert listing["archived"][0]["id"] == first["id"]


async def test_skill_learning_never_auto_promotes(database: Database):
    stamp = now()
    await database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run1", None, "completed", "Create a report", stamp, stamp, stamp, stamp, None, "completed", None, None, None, None, None, "normal"),
    )
    for sequence, payload in enumerate(
        (
            {"type": "tool_call", "tool_call": {"id": "a", "name": "read_file", "arguments": {"path": "input.md"}}},
            {"type": "tool_result", "tool_result": {"tool_call_id": "a", "success": True}},
            {"type": "tool_call", "tool_call": {"id": "b", "name": "artifact_publish", "arguments": {"path": "report.md"}}},
            {"type": "tool_result", "tool_result": {"tool_call_id": "b", "success": True}},
        ),
        1,
    ):
        await database.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (f"e{sequence}", "run1", sequence, payload["type"], stamp, dumps(payload)),
        )
    learner = SkillLearningService(database)
    run = {"id": "run1", "status": "completed", "mode": "normal", "user_message": "Create a report"}
    first = await learner.learn(run)
    second = await learner.learn(run)
    assert first["status"] == "observing"
    assert second is None
    await database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run2", None, "completed", "Create a report", stamp, stamp, stamp, stamp, None, "completed", None, None, None, None, None, "normal"),
    )
    for sequence in range(1, 5):
        row = await database.fetchone("SELECT type,data_json FROM events WHERE run_id='run1' AND sequence=?", (sequence,))
        await database.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (f"run2-e{sequence}", "run2", sequence, row["type"], stamp, row["data_json"]),
        )
    ready = await learner.learn({**run, "id": "run2"})
    assert ready["status"] == "ready"
    assert all(item["status"] != "promoted" for item in await learner.list())


def test_sandbox_confines_paths_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = SandboxSupervisor(workspace)
    assert supervisor.resolve("safe/file.txt") == workspace / "safe" / "file.txt"
    with pytest.raises(SandboxError, match="escapes"):
        supervisor.resolve("../outside.txt")
    monkeypatch.setenv("KORENEXT_API_KEY", "secret")
    assert "KORENEXT_API_KEY" not in supervisor.clean_environment()


async def test_windows_workspace_processes_fail_before_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import kore_next.mcp as mcp_module
    import kore_next.sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module.sys, "platform", "win32")
    async def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("sandboxed process was spawned")
    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", unexpected_spawn)
    monkeypatch.setattr(mcp_module.asyncio, "create_subprocess_exec", unexpected_spawn)
    supervisor = SandboxSupervisor(tmp_path)
    with pytest.raises(SandboxError, match="unavailable"):
        await supervisor.run("cmd", ["/c", "echo unsafe"], filesystem="workspace")
    server = McpProcess({"command": "cmd", "name": "test", "sandbox": {"filesystem": "workspace", "network": "denied"}}, supervisor)
    with pytest.raises(SandboxError, match="unavailable"):
        await server.start()
    launch = supervisor.prepare_launch("cmd", ["/c", "echo allowed"], filesystem="host", network="allowed")
    assert launch.backend == "host"


def test_run_budget_warns_finalizes_and_stops():
    budget = RunBudget(
        RunBudgetConfig(
            warning_tokens=10,
            finalization_tokens=20,
            hard_tokens=30,
            warning_model_calls=2,
            finalization_model_calls=3,
            hard_model_calls=4,
        )
    )
    assert budget.evaluate({"input_tokens": 2, "output_tokens": 2}, 1).status == "normal"
    assert budget.evaluate({"input_tokens": 10}, 1).status == "warning"
    assert budget.evaluate({"input_tokens": 1}, 3).status == "finalizing"
    stopped = budget.evaluate({"input_tokens": 31}, 1)
    assert stopped.exceeded is True
    assert stopped.reason == "tokens"


def test_repeated_tool_calls_are_stopped_on_third_identical_round():
    guard = RepeatedToolCallGuard(limit=3)
    calls = [{"name": "read_file", "arguments": {"path": "notes.md"}}]
    guard.observe(calls)
    guard.observe(calls)
    with pytest.raises(RuntimePolicyError, match="Repeated tool call loop") as raised:
        guard.observe(calls)
    assert raised.value.stop_reason == "repeated_tool_call"


async def test_permission_rules_are_scoped_and_do_not_store_secret_values(database: Database):
    store = PermissionRuleStore(database, signing_key=b"a" * 32)
    await store.add(
        tool_name="write_file",
        effect="allow",
        scope="conversation",
        scope_id="conversation-a",
        arguments={"path": "reports/final.md", "content": "private"},
        description="Allow this report path",
    )
    changed = await store.evaluate(
        tool_name="write_file",
        conversation_id="conversation-a",
        run_id="run-a",
        arguments={"path": "reports/final.md", "content": "different"},
    )
    allowed = await store.evaluate(
        tool_name="write_file",
        conversation_id="conversation-a",
        run_id="run-a",
        arguments={"content": "private", "path": "reports/final.md"},
    )
    unrelated = await store.evaluate(
        tool_name="write_file",
        conversation_id="conversation-b",
        run_id="run-b",
        arguments={"path": "reports/final.md"},
    )
    row = await database.fetchone("SELECT matcher_json FROM permission_rules")
    assert changed.effect == "ask"
    assert allowed.effect == "allow"
    assert unrelated.effect == "ask"
    assert "private" not in str(row["matcher_json"])


async def test_permission_rules_never_match_legacy_or_other_shell_arguments(database: Database):
    store = PermissionRuleStore(database, signing_key=b"b" * 32)
    await store.add(
        tool_name="shell", effect="allow", scope="global", scope_id="*",
        arguments={"command": "echo safe"}, description="Exact shell command",
    )
    assert (await store.evaluate(tool_name="shell", conversation_id="c", run_id="r", arguments={"command": "echo safe"})).effect == "allow"
    assert (await store.evaluate(tool_name="shell", conversation_id="c", run_id="r", arguments={"command": "echo unsafe"})).effect == "ask"
    await database.execute(
        "INSERT INTO permission_rules(id,tool_name,effect,scope,scope_id,matcher_json,description,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("old", "shell", "allow", "run", "r", "{}", "Legacy wildcard", "9999-01-01T00:00:00Z"),
    )
    assert (await store.evaluate(tool_name="shell", conversation_id="c", run_id="r", arguments={"command": "echo unsafe"})).effect == "ask"


async def test_permission_rules_match_empty_arguments_and_scope_priority(database: Database):
    store = PermissionRuleStore(database, signing_key=b"c" * 32)
    await store.add(tool_name="read", effect="allow", scope="global", scope_id="*", arguments={}, description="global")
    await store.add(tool_name="read", effect="deny", scope="conversation", scope_id="c", arguments={}, description="conversation")
    await store.add(tool_name="read", effect="allow", scope="run", scope_id="r", arguments={}, description="run")
    assert (await store.evaluate(tool_name="read", conversation_id="c", run_id="r", arguments={})).effect == "allow"
    assert (await store.evaluate(tool_name="read", conversation_id="c", run_id="other", arguments={})).effect == "deny"
    assert (await store.evaluate(tool_name="read", conversation_id="other", run_id="other", arguments={})).effect == "allow"
    assert (await store.evaluate(tool_name="read", conversation_id="c", run_id="r", arguments={"extra": 1})).effect == "ask"


async def test_evidence_keeps_raw_output_and_filters_before_limit(database: Database):
    stamp = now()
    await database.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-evidence", "conversation-evidence", "completed", "Inspect",
            stamp, stamp, stamp, stamp, None, None, None, None, None, None, None, "normal",
        ),
    )
    store = EvidenceStore(database)
    target = await store.record("run-evidence", "tool_result", {
        "tool_call_id": "call-1", "task_id": "task-1", "task_step": 2,
        "output": "needle " + "x" * 30_000,
    })
    for index in range(3):
        await store.record("run-evidence", "tool_result", {"output": f"unrelated-{index}"})
    found = await store.search("needle", limit=1)
    assert len(found) == 1
    assert found[0]["id"] == target["id"]
    assert len(found[0]["data"]["output"]) > 20_000
    assert found[0]["conversation_id"] == "conversation-evidence"
    assert found[0]["task_step"] == 2
    assert len(found[0]["content_sha256"]) == 64


async def test_database_upgrade_keeps_old_data_and_creates_backup(tmp_path: Path):
    import sqlite3

    path = tmp_path / "old" / "koreagent-next.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
        connection.execute("INSERT INTO kv VALUES('old-setting', '{\"kept\":true}')")
    db = Database(path)
    await db.initialize()
    await db.initialize()
    assert await db.get_json("old-setting") == {"kept": True}
    assert len(list(path.parent.glob("koreagent-next.backup-v0-*.db"))) == 1
    assert await db.fetchone("SELECT name FROM sqlite_master WHERE name='automation_occurrences'") is not None


def test_computer_guard_stops_repeated_failures_and_stale_observations():
    failures = ComputerStagnationGuard(failure_limit=2)
    failures.record(run_id="r1", tool_name="computer_click", success=False, output="", error="not_found: button")
    with pytest.raises(RuntimePolicyError, match="repeated not_found"):
        failures.record(run_id="r1", tool_name="computer_click", success=False, output="", error="not_found: field")

    stagnant = ComputerStagnationGuard(stagnation_limit=2)
    observation = dumps({"active_app": "Editor", "windows": ["Document"], "elements": [{"id": "save"}]})
    stagnant.record(run_id="r2", tool_name="computer_observe", success=True, output=observation, error=None)
    for _ in range(2):
        stagnant.record(run_id="r2", tool_name="computer_click", success=True, output="{}", error=None)
        if _ == 0:
            stagnant.record(run_id="r2", tool_name="computer_observe", success=True, output=observation, error=None)
        else:
            with pytest.raises(RuntimePolicyError, match="fresh observations did not change"):
                stagnant.record(run_id="r2", tool_name="computer_observe", success=True, output=observation, error=None)


async def test_task_listing_can_be_scoped_to_a_conversation(database: Database):
    service = TaskService(database)
    first = await service.create(title="First", goal="A", conversation_id="conversation-a")
    await service.create(title="Second", goal="B", conversation_id="conversation-b")
    scoped = await service.list(conversation_id="conversation-a")
    assert [task["id"] for task in scoped] == [first["id"]]
