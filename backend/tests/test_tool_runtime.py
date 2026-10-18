from __future__ import annotations

import asyncio
import json

import pytest

from kore_next.application import Application
from kore_next.config import Settings
from kore_next.mcp import McpManager
from kore_next.model.tool_arguments import parse_tool_arguments
from kore_next.model.types import ModelReply
from kore_next.tools import (
    ToolArgumentError,
    ToolExecutor,
    ToolNotVisibleYet,
)
from kore_next.tools.definitions import TOOLS
from kore_next.tools.errors import DuplicateToolCall
from kore_next.tools.validation import validate_arguments


class NoComputer:
    @staticmethod
    def tool_definitions():
        return []

    async def tool(self, *_args, **_kwargs):
        raise AssertionError("computer tool should not be called")


async def _allow(*_args, **_kwargs):
    return True


def _executor(tmp_path, *, approve=None):
    return ToolExecutor(
        Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"),
        approve or _allow,
        None,
        None,
        McpManager(tmp_path),
        NoComputer(),  # type: ignore[arg-type]
    )


@pytest.fixture
async def tool_app(tmp_path, monkeypatch):
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


async def _run(app: Application):
    conversation = (await app.conversation_create({}))["conversation"]
    await app.conversations.add_message(conversation["id"], {"role": "user", "content": "test tools"})
    return await app.runs.create(conversation["id"], "test tools", "normal")


async def test_tool_activation_is_visible_only_on_the_next_model_step(tmp_path):
    executor = _executor(tmp_path)
    first_step = executor.project(1, "normal")
    result = json.loads(
        await executor.execute(
            "run",
            "tool_catalog",
            {"query": "memory"},
            "catalog-1",
            projection=first_step,
            effective_step=2,
        )
    )
    assert result["effective_step"] == 2
    assert "memory_read" not in first_step.visible
    assert "memory_read" in executor.project(2, "normal").visible
    with pytest.raises(ToolNotVisibleYet) as raised:
        await executor.preflight(
            "run",
            "memory_read",
            {"memory_id": "m1"},
            "memory-1",
            projection=first_step,
            effective_step=2,
        )
    assert raised.value.effective_step == 2


async def test_activation_and_tool_call_in_one_batch_does_not_execute_second_call(tool_app, monkeypatch):
    app = tool_app
    run = await _run(app)
    requests: list[list[dict]] = []
    router_calls: list[str] = []

    async def fake_complete(_self, _messages, extra_tools, base_tools, **_kwargs):
        requests.append([*base_tools, *extra_tools])
        if len(requests) == 1:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[
                    {"id": "catalog", "name": "tool_catalog", "arguments": {"query": "memory"}},
                    {"id": "memory", "name": "memory_list", "arguments": {}},
                ],
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    async def record_tool(name, *_args, **_kwargs):
        router_calls.append(name)
        return "{}"

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr(app.tool_router, "tool_call", record_tool)
    await app._execute_run(run)

    assert router_calls == []
    assert "memory_list" not in {item["function"]["name"] for item in requests[0]}
    assert "memory_list" in {item["function"]["name"] for item in requests[1]}
    events = await app.runs.runtime_events(run["id"])
    memory_result = next(
        event["tool_result"]
        for event in events
        if event["type"] == "tool_result" and event["tool_result"]["tool_call_id"] == "memory"
    )
    assert memory_result["success"] is False
    assert memory_result["error_code"] == "not_visible_yet"
    assert memory_result["dispatched"] is False


async def test_invalid_arguments_are_rejected_before_dispatch(tmp_path):
    executor = _executor(tmp_path)
    with pytest.raises(ToolArgumentError):
        await executor.preflight(
            "run",
            "write_file",
            {"path": "empty.txt"},
            "write-1",
            projection=executor.project(1, "normal"),
        )
    assert not (tmp_path / "workspace" / "empty.txt").exists()


def test_schema_validation_checks_required_fields_and_types():
    shell = next(item for item in TOOLS if item["function"]["name"] == "shell")
    with pytest.raises(ToolArgumentError) as raised:
        validate_arguments(shell, {"timeout_seconds": "soon"})
    assert any("'command' is a required property" in item["message"] for item in raised.value.errors)
    assert any(item["path"] == "$.timeout_seconds" for item in raised.value.errors)


def test_malformed_tool_arguments_are_not_silently_accepted():
    arguments, error = parse_tool_arguments('{"path": ')
    assert arguments == {}
    assert error and "Invalid JSON" in error


async def test_denied_tool_is_not_recorded_as_success(tool_app, monkeypatch):
    app = tool_app
    run = await _run(app)
    calls = 0

    async def fake_complete(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            return ModelReply(
                content="done",
                reasoning=None,
                tool_calls=[],
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return ModelReply(
            content="",
            reasoning=None,
            tool_calls=[
                {
                    "id": "write-1",
                    "name": "write_file",
                    "arguments": {"path": "denied.txt", "content": "should not exist"},
                }
            ],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    async def deny(*_args, **_kwargs):
        return False

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr(app.approvals, "request", deny)
    await app._execute_run(run)

    assert not (app.settings.workspace_dir / "denied.txt").exists()
    events = await app.runs.runtime_events(run["id"])
    result = next(event["tool_result"] for event in events if event["type"] == "tool_result")
    assert result["success"] is False
    assert result["error_code"] == "denied"
    assert result["dispatched"] is False
    rows = await app.db.fetchall("SELECT status FROM tool_executions WHERE run_id=?", (run["id"],))
    assert rows == []


async def test_approval_wait_is_not_a_dispatched_tool(tool_app, monkeypatch):
    app = tool_app
    run = await _run(app)
    started = asyncio.Event()

    async def fake_complete(_self, *_args, **_kwargs):
        return ModelReply(
            content="",
            reasoning=None,
            tool_calls=[
                {
                    "id": "write-1",
                    "name": "write_file",
                    "arguments": {"path": "blocked.txt", "content": "blocked"},
                }
            ],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    async def block_approval(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr(app.approvals, "request", block_approval)
    task = asyncio.create_task(app._execute_run(run))
    app.running[run["id"]] = task
    await asyncio.wait_for(started.wait(), 5)
    result = await app.run_interrupt({"run_id": run["id"]})
    await asyncio.gather(task, return_exceptions=True)
    app.running.pop(run["id"], None)

    assert result["run"]["stop_reason"] == "interrupted"
    rows = await app.db.fetchall("SELECT status FROM tool_executions WHERE run_id=?", (run["id"],))
    assert rows == []
    assert not (app.settings.workspace_dir / "blocked.txt").exists()


async def test_duplicate_call_id_with_different_arguments_is_rejected(tool_app):
    app = tool_app
    run = await _run(app)
    call = {"id": "same-id", "name": "read_file", "arguments": {"path": "a.txt"}}
    await app.runs.begin_tool(run, call, 1)
    with pytest.raises(DuplicateToolCall):
        await app.runs.begin_tool(
            run,
            {"id": "same-id", "name": "read_file", "arguments": {"path": "b.txt"}},
            1,
        )
