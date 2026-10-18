from __future__ import annotations

import asyncio

import pytest

from kore_next.application import Application, RpcFault
from kore_next.config import Settings
from kore_next.model.types import ModelReply


@pytest.fixture
async def recovery_app(tmp_path, monkeypatch):
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

    async def no_post_run(*_args):
        return None

    monkeypatch.setattr(application.run_post_processor, "run", no_post_run)
    yield application
    await application.close()


async def _conversation_and_run(app: Application, text: str = "test task"):
    conversation = (await app.conversation_create({}))["conversation"]
    await app.conversations.add_message(conversation["id"], {"role": "user", "content": text})
    run = await app.runs.create(conversation["id"], text, "normal")
    return conversation, run


async def test_interrupted_tool_requires_reconciliation_and_is_not_replayed(recovery_app, monkeypatch):
    app = recovery_app
    conversation, run = await _conversation_and_run(app)
    started = asyncio.Event()

    async def fake_complete(_self, *_args, **_kwargs):
        return ModelReply(
            content="",
            reasoning=None,
            tool_calls=[{"id": "call-1", "name": "task_list", "arguments": {}}],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async def hanging_tool(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr("kore_next.tools.executor.ToolExecutor.dispatch", hanging_tool)

    task = asyncio.create_task(app._execute_run(run))
    app.running[run["id"]] = task
    await asyncio.wait_for(started.wait(), 5)
    interrupted = await app.run_interrupt({"run_id": run["id"]})
    assert interrupted["run"]["status"] == "interrupted"
    assert interrupted["run"]["stop_reason"] == "tool_unknown"
    await asyncio.gather(task, return_exceptions=True)
    app.running.pop(run["id"], None)

    execution = await app.db.fetchone(
        "SELECT status FROM tool_executions WHERE run_id=? AND call_id=?",
        (run["id"], "call-1"),
    )
    assert execution["status"] == "unknown"
    with pytest.raises(RpcFault) as error:
        await app.run_recover({"run_id": run["id"]})
    assert error.value.code == -32002
    recovered = await app.db.fetchone(
        "SELECT COUNT(*) AS n FROM runs WHERE recovered_from_run_id=?",
        (run["id"],),
    )
    assert recovered["n"] == 0


async def test_crash_after_assistant_message_resumes_tools_without_model_replay(recovery_app, monkeypatch):
    app = recovery_app
    _, run = await _conversation_and_run(app)
    model_calls = 0

    async def fake_complete(_self, *_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return ModelReply(
            content="",
            reasoning=None,
            tool_calls=[{"id": "call-1", "name": "task_list", "arguments": {}}],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    original_begin_tool = app.runs.begin_tool

    async def fail_once(*args, **kwargs):
        raise RuntimeError("simulated crash after assistant message")

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr(app.runs, "begin_tool", fail_once)
    with pytest.raises(RpcFault):
        await app._execute_run(run)
    checkpoint = await app.checkpoints.get(run["id"])
    assert checkpoint["state"]["phase"] == "assistant_ready"
    assert model_calls == 1

    async def final_complete(_self, *_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async def successful_tool(*_args, **_kwargs):
        return "tool output"

    monkeypatch.setattr(app.runs, "begin_tool", original_begin_tool)
    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", final_complete)
    monkeypatch.setattr("kore_next.tools.executor.ToolExecutor.dispatch", successful_tool)
    recovered = await app.run_recover({"run_id": run["id"]})
    assert recovered["result"]["final_message"]["content"] == "done"
    assert model_calls == 2
    messages = (await app.conversation_get({"conversation_id": run["conversation_id"]}))["messages"]
    roles = [message["role"] for message in messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["id"] == "call-1"
    assert messages[2]["tool_call_id"] == "call-1"


async def test_final_ready_checkpoint_completes_without_model_call(recovery_app, monkeypatch):
    app = recovery_app
    conversation, run = await _conversation_and_run(app)
    await app.checkpoints.save(
        run_id=run["id"],
        conversation_id=conversation["id"],
        sequence=2,
        state={
            "phase": "final_ready",
            "final": "already answered",
            "model_messages": [{"role": "assistant", "content": "already answered"}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "next_step": 3,
            "plan_task_id": None,
            "activated_tools": [],
            "model_calls": 2,
            "pending_tool_calls": [],
            "tool_cursor": 0,
        },
    )
    await app.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (run["id"],))

    async def unexpected_complete(*_args, **_kwargs):
        pytest.fail("final_ready recovery must not call the model")

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", unexpected_complete)
    recovered = await app.run_recover({"run_id": run["id"]})
    assert recovered["result"]["final_message"]["content"] == "already answered"


async def test_startup_interrupts_manual_running_runs(recovery_app):
    app = recovery_app
    _, run = await _conversation_and_run(app)
    await app.db.execute("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
    assert await app.runs.recover_stale_runs() == 1
    row = await app.db.fetchone("SELECT status,stop_reason FROM runs WHERE id=?", (run["id"],))
    assert (row["status"], row["stop_reason"]) == ("interrupted", "host_restart")


async def test_message_and_checkpoint_rollback_together(recovery_app, monkeypatch):
    app = recovery_app
    conversation, run = await _conversation_and_run(app)
    original_save = app.checkpoints.save
    saves = 0

    async def flaky_save(*args, **kwargs):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise RuntimeError("simulated checkpoint failure")
        return await original_save(*args, **kwargs)

    async def fake_complete(_self, *_args, **_kwargs):
        return ModelReply(
            content="answer",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(app.checkpoints, "save", flaky_save)
    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    with pytest.raises(RpcFault):
        await app._execute_run(run)
    messages = (await app.conversation_get({"conversation_id": conversation["id"]}))["messages"]
    assert [message["role"] for message in messages] == ["user"]


async def test_checkpoint_digest_mismatch_blocks_recovery(recovery_app):
    app = recovery_app
    conversation, run = await _conversation_and_run(app)
    await app.runs.event(run, "user_message", message={"role": "user", "content": "hello"})
    await app.checkpoints.save(
        run_id=run["id"],
        conversation_id=conversation["id"],
        sequence=1,
        state={"phase": "model_pending", "model_messages": [], "usage": {}, "next_step": 1},
        through_event_seq=1,
        digest="not-the-right-digest",
    )
    await app.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (run["id"],))
    with pytest.raises(RpcFault) as error:
        await app.run_recover({"run_id": run["id"]})
    assert error.value.code == -32003


async def test_run_checkpoint_records_event_watermark(recovery_app, monkeypatch):
    app = recovery_app
    _, run = await _conversation_and_run(app)

    async def fake_complete(_self, *_args, **_kwargs):
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    await app._execute_run(run)
    checkpoint = await app.checkpoints.get(run["id"])
    assert checkpoint["through_event_seq"] > 0
    assert checkpoint["digest"]
    verified = await app.runs.verify_events(run["id"])
    assert verified["valid"] is True
    assert verified["event_seq"] >= checkpoint["through_event_seq"]


async def test_snapshot_replays_tail_without_model_call(recovery_app, monkeypatch):
    app = recovery_app
    conversation, run = await _conversation_and_run(app)
    await app.runs.event(run, "user_message", message={"role": "user", "content": "hello"})
    events = await app.runs.runtime_events(run["id"])
    await app.checkpoints.save(
        run_id=run["id"],
        conversation_id=conversation["id"],
        sequence=1,
        state={
            "phase": "model_pending",
            "model_messages": [{"role": "user", "content": "hello"}],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "next_step": 1,
            "plan_task_id": None,
            "activated_tools": [],
            "model_calls": 0,
            "pending_tool_calls": [],
            "tool_cursor": 0,
        },
        through_event_seq=events[0]["event_seq"],
        digest=events[0]["digest"],
    )
    await app.runs.event(
        run,
        "assistant_message",
        step=1,
        message={"role": "assistant", "content": "from the tail"},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    await app.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (run["id"],))

    async def unexpected_complete(*_args, **_kwargs):
        pytest.fail("tail replay must not call the model")

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", unexpected_complete)
    recovered = await app.run_recover({"run_id": run["id"]})
    assert recovered["result"]["final_message"]["content"] == "from the tail"


async def test_large_tool_result_is_offloaded_before_the_model_sees_it(recovery_app, monkeypatch):
    app = recovery_app
    conversation, run = await _conversation_and_run(app)
    calls = 0

    async def fake_complete(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[{"id": "call-1", "name": "task_list", "arguments": {}}],
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async def large_tool(*_args, **_kwargs):
        return "x" * 70_000

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr("kore_next.tools.executor.ToolExecutor.dispatch", large_tool)
    await app._execute_run(run)

    messages = (await app.conversation_get({"conversation_id": conversation["id"]}))["messages"]
    tool_message = next(message for message in messages if message["role"] == "tool")
    assert tool_message["content"].startswith("[tool result archived")
    row = await app.db.fetchone("SELECT sha256,size_bytes FROM context_offloads")
    assert row["size_bytes"] == 70_000
    chunk = await app.context_offload.read(row["sha256"], limit=5)
    assert chunk["content"] == "xxxxx"


async def _interrupt_during_tool(app: Application, monkeypatch, run: dict, tool):
    started = asyncio.Event()

    async def fake_complete(_self, *_args, **_kwargs):
        return ModelReply(
            content="",
            reasoning=None,
            tool_calls=[{"id": "call-1", "name": "task_list", "arguments": {}}],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async def wrapped_tool(*args, **kwargs):
        started.set()
        return await tool(*args, **kwargs)

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr("kore_next.tools.executor.ToolExecutor.dispatch", wrapped_tool)
    task = asyncio.create_task(app._execute_run(run))
    app.running[run["id"]] = task
    await asyncio.wait_for(started.wait(), 5)
    await app.run_interrupt({"run_id": run["id"]})
    await asyncio.gather(task, return_exceptions=True)
    app.running.pop(run["id"], None)


async def test_unknown_tool_can_be_retried_after_explicit_resolution(recovery_app, monkeypatch):
    app = recovery_app
    _, run = await _conversation_and_run(app)
    mode = {"value": "hang"}
    tool_calls = 0

    async def tool(*_args, **_kwargs):
        nonlocal tool_calls
        tool_calls += 1
        if mode["value"] == "hang":
            await asyncio.Event().wait()
        return "retried output"

    await _interrupt_during_tool(app, monkeypatch, run, tool)
    mode["value"] = "ok"
    calls = 0

    async def final_complete(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ModelReply(
            content="recovered",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", final_complete)
    result = await app.run_resolve_tool({"run_id": run["id"], "action": "retry"})
    assert result["action"] == "retry"
    assert result["recovery"]["result"]["final_message"]["content"] == "recovered"
    assert tool_calls == 2


async def test_unknown_tool_can_be_abandoned_and_repairs_conversation(recovery_app, monkeypatch):
    app = recovery_app
    conversation, run = await _conversation_and_run(app)

    async def tool(*_args, **_kwargs):
        await asyncio.Event().wait()

    await _interrupt_during_tool(app, monkeypatch, run, tool)
    result = await app.run_resolve_tool({"run_id": run["id"], "action": "abandon"})
    assert result["run"]["status"] == "failed"
    assert result["run"]["stop_reason"] == "tool_abandoned"
    messages = (await app.conversation_get({"conversation_id": conversation["id"]}))["messages"]
    assert [(message["role"], message.get("tool_call_id")) for message in messages][-2:] == [
        ("assistant", None),
        ("tool", "call-1"),
    ]
    with pytest.raises(RpcFault) as error:
        await app.run_recover({"run_id": run["id"]})
    assert error.value.code == -32001
