from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from kore_next.application import Application, RpcFault
from kore_next.config import Settings
from kore_next.model import ModelReply
from kore_next.services.run_engine import RunEngine
from kore_next.util import dumps, now


@pytest.fixture
async def app(tmp_path, monkeypatch):
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


def test_rpc_contract_contains_expected_methods(app):
    expected = {
        "approval.approve",
        "approval.deny",
        "approval.get",
        "approval.list",
        "artifact.get",
        "artifact.list",
        "automation.cancel",
        "automation.create",
        "automation.get",
        "automation.list",
        "automation.pause",
        "automation.resume",
        "computer.latest_observation",
        "computer.request_permission",
        "computer.status",
        "conversation.create",
        "conversation.delete",
        "conversation.get",
        "conversation.search",
        "conversation.list",
        "conversation.rename",
        "conversation.send",
        "extension.import.apply",
        "extension.import.preview",
        "extension.list",
        "mcp.add",
        "mcp.delete",
        "mcp.set_enabled",
        "memory.list",
        "model_settings.get",
        "model_settings.test",
        "model_settings.update",
        "run.cancel",
        "run.get",
        "run.interrupt",
        "run.list",
        "run.recover",
        "skill.delete",
        "skill.install",
        "skill.set_enabled",
        "system.info",
        "system.restart",
        "task.get",
        "task.list",
        "task.plan_accept",
        "task.plan_reject",
        "trace.get",
    }
    assert set(app.methods()) == expected


async def test_auto_approve_resolves_unattended_tool_request(app):
    app.settings = replace(app.settings, auto_approve=True)
    allowed = await app.approvals.request(
        "missing-run",
        "call-1",
        {"tool_name": "shell", "command": "echo hi"},
        "执行本地命令",
    )
    assert allowed is True


async def test_conversation_and_offline_run(app):
    conversation = (await app.conversation_create({}))["conversation"]
    response = await app.conversation_send({"conversation_id": conversation["id"], "content": "你好", "mode": "normal"})
    assert response["run"]["status"] == "completed"
    assert "模型尚未配置" in response["content"]
    detail = await app.conversation_get({"conversation_id": conversation["id"]})
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]


async def test_session_search_finds_other_conversations_and_tracks_deletion(app):
    first = (await app.conversation_create({"title": "First"}))["conversation"]
    second = (await app.conversation_create({"title": "Second"}))["conversation"]
    await app.conversations.add_message(first["id"], {"role": "user", "content": "青色星球的季度报告"})
    await app.conversations.add_message(second["id"], {"role": "user", "content": "无关的消息"})
    result = await app.conversation_search({"query": "星球"})
    assert [(item["conversation_id"], item["title"]) for item in result["matches"]] == [(first["id"], "First")]
    assert len((await app.conversation_search({"query": "青色星球"}))["matches"]) == 1
    await app.conversation_delete({"conversation_id": first["id"]})
    assert (await app.conversation_search({"query": "星球"}))["matches"] == []


async def test_model_requests_are_durable_and_tool_activation_survives_checkpoint(app, monkeypatch):
    conversation = (await app.conversation_create({}))["conversation"]
    await app.conversations.add_message(conversation["id"], {"role": "user", "content": "Find memory tools"})
    run = await app.runs.create(conversation["id"], "Find memory tools", "normal")
    calls = []

    async def fake_complete(_self, messages, extra_tools, base_tools, **_kwargs):
        calls.append((deepcopy(messages), [*base_tools, *extra_tools]))
        if len(calls) == 1:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[
                    {
                        "id": "find-tools",
                        "name": "tool_catalog",
                        "arguments": {"query": "memory"},
                    }
                ],
                usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            )
        return ModelReply(
            content="Found tools",
            reasoning=None,
            tool_calls=[],
            usage={
                "input_tokens": 120,
                "output_tokens": 10,
                "total_tokens": 130,
            },
        )

    async def no_post_run(*_args):
        return None

    monkeypatch.setattr("kore_next.application.ModelClient.complete", fake_complete)
    monkeypatch.setattr(app.run_post_processor, "run", no_post_run)
    await app._execute_run(run)
    checkpoint = await app.checkpoints.get(run["id"])
    assert "memory_read" in checkpoint["state"]["activated_tools"]
    assert "memory_read" in {tool["function"]["name"] for tool in calls[1][1]}
    trace = await app.trace_get({"run_id": run["id"], "include_model_requests": True})
    requests = [event for event in trace["events"] if event["type"] == "model_request"]
    assert len(requests) == 2
    assert requests[1]["request"]["messages"] == calls[1][0]


async def test_model_test_never_sends_saved_key_to_custom_endpoint(app, monkeypatch):
    async def unexpected_complete(*_args, **_kwargs):
        pytest.fail("custom endpoint received saved key")

    monkeypatch.setattr("kore_next.application.ModelClient.complete", unexpected_complete)
    monkeypatch.setattr(app.secrets, "get", lambda provider: "saved-secret")
    blocked = await app.model_settings_test(
        {
            "provider": "qwen",
            "model": "qwen-test",
            "base_url": "https://api.example.invalid/v1",
        }
    )
    assert blocked["success"] is False
    assert "官方 HTTPS" in blocked["error"]
    blocked_http = await app.model_settings_test(
        {
            "provider": "qwen",
            "model": "qwen-test",
            "base_url": "http://dashscope.aliyuncs.com/compatible-mode/v1",
        }
    )
    assert blocked_http["success"] is False


async def test_skill_scopes_are_isolated_and_project_name_wins(app):
    user = (
        await app.skill_install(
            {"name": "report", "scope": "user", "description": "User", "instructions": "User instructions"}
        )
    )["skill"]
    project = (
        await app.skill_install(
            {
                "name": "report",
                "scope": "project",
                "description": "Project",
                "instructions": "Project instructions",
            }
        )
    )["skill"]
    assert user["location"].startswith(str(app.settings.user_skills_dir))
    assert project["location"].startswith(str(app.settings.data_dir))
    catalog = RunEngine.skill_catalog(await app.skill_service.skills())
    assert "report (project): Project" in catalog
    assert "report (user)" not in catalog
    conversation = (await app.conversation_create({}))["conversation"]
    run = await app.runs.create(conversation["id"], "Read", "normal")
    result = await app.tool_call("skill_read", {"name": "report"}, run_id=run["id"], mode="normal")
    assert "Project instructions" in result["content"]


async def test_trace_reports_each_model_call_once_and_keeps_auxiliary_usage(app):
    conversation = (await app.conversation_create({}))["conversation"]
    run = await app.runs.create(conversation["id"], "Inspect usage", "normal")
    await app.runs.event(
        run,
        "assistant_message",
        step=1,
        message={"role": "assistant", "content": "one"},
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_input_tokens": 60,
            "uncached_input_tokens": 40,
            "cache_read_input_tokens": 60,
        },
    )
    await app.runs.event(
        run,
        "assistant_message",
        step=2,
        message={"role": "assistant", "content": "two"},
        usage={"input_tokens": 80, "output_tokens": 10, "total_tokens": 90},
    )
    await app.runs.event(run, "agent_completed", usage={"input_tokens": 180, "output_tokens": 30, "total_tokens": 210})
    await app.runs.event(
        run,
        "auxiliary_model_completed",
        role="memory_reflection",
        usage={"input_tokens": 30, "output_tokens": 5, "total_tokens": 35},
    )
    report = await app.trace_get({"run_id": run["id"]})
    assert report["trace"]["total_tokens"] == 210
    assert report["usage_summary"]["main_agent"]["model_calls"] == 2
    assert report["usage_summary"]["main_agent"]["cached_input_tokens"] is None
    assert report["usage_summary"]["memory_reflection"]["total_tokens"] == 35
    assert report["usage_summary"]["provider_total"]["total_tokens"] == 245


async def test_recovered_run_budget_includes_inherited_usage_without_double_counting_model_usage(app):
    conversation = (await app.conversation_create({}))["conversation"]
    run = await app.runs.create(conversation["id"], "Resume", "normal")
    await app.runs.event(
        run,
        "run_recovered",
        step=3,
        inherited_usage={"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
        inherited_model_calls=2,
    )
    await app.runs.event(
        run,
        "assistant_message",
        step=3,
        usage={"input_tokens": 50, "output_tokens": 20, "total_tokens": 70},
    )
    report = await app.trace_get({"run_id": run["id"]})
    assert report["usage_summary"]["main_agent"]["total_tokens"] == 70
    assert report["usage_summary"]["budget"]["run_budget_chargeable_tokens"] == 670
    assert report["usage_summary"]["budget"]["run_budget_model_calls"] == 3


async def test_artifact_list_filters_run_before_limit(app):
    target = {"id": "target", "run_id": "older-run", "conversation_id": "conversation", "title": "Wanted"}
    await app.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (target["id"], dumps(target), "2020-01-01T00:00:00Z"))
    for index in range(3):
        item = {"id": f"other-{index}", "run_id": "newer-run", "title": "Other"}
        await app.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (item["id"], dumps(item), now()))
    result = await app.artifact_list({"run_id": "older-run", "limit": 1})
    assert [item["id"] for item in result["artifacts"]] == ["target"]


async def test_data_is_created_only_under_new_namespace(app, tmp_path):
    await app.conversation_create({})
    assert app.settings.database.is_file()
    assert ".koreagent-next" not in str(tmp_path) or app.settings.data_dir == tmp_path / "data"
    assert not (tmp_path / ".vesta").exists()


async def test_automation_lifecycle(app):
    item = (
        await app.automation_create(
            {"title": "Daily", "prompt": "Summarize", "kind": "interval", "interval_seconds": 3600}
        )
    )["automation"]
    assert item["status"] == "active"
    assert (await app.automation_pause({"automation_id": item["id"]}))["automation"]["status"] == "paused"
    assert (await app.automation_resume({"automation_id": item["id"]}))["automation"]["status"] == "active"
    assert (await app.automation_cancel({"automation_id": item["id"]}))["automation"]["status"] == "cancelled"


async def test_automation_occurrence_is_atomic_and_unique(app):
    item = (
        await app.automation_create(
            {
                "title": "Once",
                "prompt": "Run once",
                "kind": "once",
                "run_at": "2020-01-01T00:00:00Z",
            }
        )
    )["automation"]
    first = await app.automations.claim_occurrence(item["id"])
    assert first is not None
    assert await app.automations.claim_occurrence(item["id"]) is None
    assert (await app.automation_get({"automation_id": item["id"]}))["automation"]["status"] == "completed"
    messages = await app.db.fetchall("SELECT * FROM messages WHERE conversation_id=?", (first["conversation_id"],))
    occurrences = await app.db.fetchall("SELECT * FROM automation_occurrences WHERE automation_id=?", (item["id"],))
    assert len(messages) == len(occurrences) == 1
    assert occurrences[0]["run_id"] == first["id"]
    with pytest.raises(RpcFault, match="Cannot change automation"):
        await app.automation_resume({"automation_id": item["id"]})


async def test_automation_transaction_rolls_back_before_run_creation(app, monkeypatch):
    item = (
        await app.automation_create(
            {
                "title": "Repeat",
                "prompt": "Run",
                "kind": "interval",
                "interval_seconds": 60,
            }
        )
    )["automation"]
    item["next_run_at"] = "2020-01-01T00:00:00Z"
    await app.db.execute(
        "UPDATE automations SET data_json=?,next_run_at=? WHERE id=?",
        (dumps(item), item["next_run_at"], item["id"]),
    )
    original = app.automations.schedule_next
    monkeypatch.setattr(
        app.automations,
        "schedule_next",
        lambda *_: (_ for _ in ()).throw(RuntimeError("injected crash")),
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        await app.automations.claim_occurrence(item["id"])
    assert await app.db.fetchall("SELECT * FROM runs WHERE source_id=?", (item["id"],)) == []
    assert await app.db.fetchall("SELECT * FROM automation_occurrences WHERE automation_id=?", (item["id"],)) == []
    monkeypatch.setattr(app.automations, "schedule_next", original)
    assert await app.automations.claim_occurrence(item["id"]) is not None


async def test_automation_restart_resumes_claimed_run_without_duplicate_occurrence(app, monkeypatch):
    app.automation_task.cancel()
    await asyncio.gather(app.automation_task, return_exceptions=True)
    item = (
        await app.automation_create(
            {
                "title": "Restart once",
                "prompt": "Resume after crash",
                "kind": "once",
                "run_at": "2020-01-01T00:00:00Z",
            }
        )
    )["automation"]
    claimed = await app.automations.claim_occurrence(item["id"])
    assert claimed is not None

    restarted = Application(app.settings)
    await restarted.db.initialize()
    executed = []

    async def record_run(run):
        executed.append(run["id"])

    monkeypatch.setattr(restarted, "_execute_run", record_run)
    await restarted.automations.restore()
    await asyncio.sleep(0)
    assert executed == [claimed["id"]]
    assert await restarted.automations.claim_occurrence(item["id"]) is None
    assert (
        len(await restarted.db.fetchall("SELECT * FROM automation_occurrences WHERE automation_id=?", (item["id"],)))
        == 1
    )


async def test_automation_restart_interrupts_started_run_without_replay(app, monkeypatch):
    app.automation_task.cancel()
    await asyncio.gather(app.automation_task, return_exceptions=True)
    item = (
        await app.automation_create(
            {
                "title": "Started once",
                "prompt": "Do not replay",
                "kind": "once",
                "run_at": "2020-01-01T00:00:00Z",
            }
        )
    )["automation"]
    claimed = await app.automations.claim_occurrence(item["id"])
    assert claimed is not None
    await app.db.execute("UPDATE runs SET status='running' WHERE id=?", (claimed["id"],))
    restarted = Application(app.settings)
    await restarted.db.initialize()
    executed = []

    async def record_run(run):
        executed.append(run["id"])

    monkeypatch.setattr(restarted, "_execute_run", record_run)
    await restarted.automations.restore()
    assert executed == []
    row = await restarted.db.fetchone("SELECT status,stop_reason FROM runs WHERE id=?", (claimed["id"],))
    assert (row["status"], row["stop_reason"]) == ("interrupted", "host_restart")
    assert await restarted.automations.claim_occurrence(item["id"]) is None


async def test_approval_is_single_use(app):
    waiter = asyncio.create_task(app.approvals.request("run", "call", {"command": "echo hi"}, "test"))
    approvals = []
    for _ in range(50):
        approvals = (await app.approval_list({"status": "pending"}))["approvals"]
        if approvals:
            break
        await asyncio.sleep(0.01)
    approval = approvals[0]
    await app.approval_approve({"approval_id": approval["id"]})
    assert await waiter is True
    with pytest.raises(RpcFault) as error:
        await app.approval_deny({"approval_id": approval["id"]})
    assert error.value.code == -32001


async def test_skill_and_mcp_management(app):
    skill = (
        await app.skill_install(
            {
                "name": "writer",
                "description": "Writes",
                "instructions": "Write clearly",
                "scope": "project",
            }
        )
    )["skill"]
    assert skill["enabled"] is True
    disabled = (await app.skill_set_enabled({"name": "writer", "scope": "project", "enabled": False}))["skill"]
    assert disabled["enabled"] is False
    mcp = await app.mcp_add({"name": "files", "command": "node", "args": ["server.js"], "env": {}, "enabled": True})
    assert mcp["restart_required"] is True
    overview = await app.extension_list({})
    assert overview["skills"][0]["name"] == "writer"
    assert overview["mcp"]["servers"][0]["name"] == "files"
