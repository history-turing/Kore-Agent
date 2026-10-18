from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kore_next.application import Application
from kore_next.config import Settings
from kore_next.model.types import ModelReply
from kore_next.resources import ResourceAuthority
from kore_next.services.run_engine import RunEngine
from kore_next.services.runtime_projections import order_tool_messages
from kore_next.tools import ToolExecutor


@pytest.fixture
async def parallel_app(tmp_path, monkeypatch):
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
    await app.conversations.add_message(conversation["id"], {"role": "user", "content": "parallel tools"})
    return await app.runs.create(conversation["id"], "parallel tools", "normal")


async def test_application_shares_one_resource_authority(parallel_app):
    assert isinstance(parallel_app.resources, ResourceAuthority)
    assert parallel_app.run_engine.resources is parallel_app.resources


async def test_writer_waits_for_readers_on_the_same_path():
    authority = ResourceAuthority()
    path = Path("shared.txt")
    reading = asyncio.Event()
    release = asyncio.Event()
    writing = asyncio.Event()

    async def reader():
        async with authority.guard("read_file", path=path):
            reading.set()
            await release.wait()

    async def writer():
        await reading.wait()
        async with authority.guard("write_file", path=path):
            writing.set()

    reader_task = asyncio.create_task(reader())
    writer_task = asyncio.create_task(writer())
    await asyncio.wait_for(reading.wait(), 1)
    await asyncio.sleep(0.02)
    assert not writing.is_set()
    release.set()
    await asyncio.wait_for(writing.wait(), 1)
    await asyncio.gather(reader_task, writer_task)


async def test_parallel_readers_do_not_block_each_other():
    authority = ResourceAuthority()
    path = Path("shared.txt")
    active = 0
    maximum = 0
    both = asyncio.Event()

    async def reader():
        nonlocal active, maximum
        async with authority.guard("read_file", path=path):
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(reader(), reader())
    assert maximum == 2


async def test_shell_excludes_file_reads():
    authority = ResourceAuthority()
    path = Path("shared.txt")
    reading = asyncio.Event()
    release = asyncio.Event()
    shell_running = asyncio.Event()

    async def reader():
        async with authority.guard("read_file", path=path):
            reading.set()
            await release.wait()

    async def shell():
        await reading.wait()
        async with authority.guard("shell"):
            shell_running.set()

    reader_task = asyncio.create_task(reader())
    shell_task = asyncio.create_task(shell())
    await asyncio.wait_for(reading.wait(), 1)
    await asyncio.sleep(0.02)
    assert not shell_running.is_set()
    release.set()
    await asyncio.wait_for(shell_running.wait(), 1)
    await asyncio.gather(reader_task, shell_task)


async def test_read_only_batch_runs_concurrently_and_keeps_model_order(parallel_app, monkeypatch):
    app = parallel_app
    run = await _run(app)
    requests: list[list[dict]] = []
    started = 0
    all_started = asyncio.Event()

    async def fake_complete(_self, messages, extra_tools, base_tools, **_kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[
                    {"id": "a", "name": "read_file", "arguments": {"path": "a.txt"}},
                    {"id": "b", "name": "read_file", "arguments": {"path": "b.txt"}},
                ],
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    async def dispatch(self, prepared):
        nonlocal started
        started += 1
        if started == 2:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), 1)
        await asyncio.sleep(0.05 if prepared.arguments["path"] == "a.txt" else 0.01)
        return f"read {prepared.arguments['path']}"

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr(ToolExecutor, "_dispatch_unlocked", dispatch)
    await app._execute_run(run)

    tool_messages = [message for message in requests[1] if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["a", "b"]
    assert started == 2
    events = await app.runs.runtime_events(run["id"])
    results = [event["tool_result"] for event in events if event["type"] == "tool_result"]
    assert {item["tool_call_id"] for item in results} == {"a", "b"}
    assert all(item["success"] for item in results)


async def test_mixed_batch_preserves_model_order(parallel_app, monkeypatch):
    app = parallel_app
    run = await _run(app)
    calls = 0
    dispatch_order: list[str] = []

    async def fake_complete(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[
                    {"id": "w", "name": "write_file", "arguments": {"path": "w.txt", "content": "x"}},
                    {"id": "r", "name": "read_file", "arguments": {"path": "r.txt"}},
                ],
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    async def allow(*_args, **_kwargs):
        return True

    async def dispatch(self, prepared):
        dispatch_order.append(prepared.name)
        await asyncio.sleep(0.01)
        return "ok"

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    monkeypatch.setattr(app.approvals, "request", allow)
    monkeypatch.setattr(ToolExecutor, "_dispatch_unlocked", dispatch)
    await app._execute_run(run)
    assert dispatch_order == ["write_file", "read_file"]


def test_completed_call_ids_do_not_skip_out_of_order_holes():
    pending = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert [call["id"] for call in RunEngine._pending_calls_to_run(pending, ["b"], 0)] == ["a", "c"]
    assert [call["id"] for call in RunEngine._pending_calls_to_run(pending, None, 2)] == ["c"]


def test_tool_messages_are_reordered_to_model_request_order():
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "a"}, {"id": "b"}],
        },
        {"role": "tool", "tool_call_id": "b", "content": "B"},
        {"role": "tool", "tool_call_id": "a", "content": "A"},
    ]
    ordered = order_tool_messages(messages, [{"id": "a"}, {"id": "b"}])
    assert [message["tool_call_id"] for message in ordered[-2:]] == ["a", "b"]
