from __future__ import annotations

from pathlib import Path

import pytest

from kore_next.application import Application
from kore_next.config import Settings
from kore_next.model.types import ModelReply


@pytest.fixture
async def workspace_app(tmp_path, monkeypatch):
    for name in (
        "KORENEXT_API_KEY",
        "KORENEXT_OPENAI_API_KEY",
        "KORENEXT_QWEN_API_KEY",
        "KORENEXT_DEEPSEEK_API_KEY",
        "KORENEXT_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    application = Application(
        Settings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "default-workspace",
            user_skills_dir=tmp_path / "user-skills",
            api_key="",
        )
    )
    await application.start()

    async def no_post_run(*_args):
        return None

    monkeypatch.setattr(application.run_post_processor, "run", no_post_run)
    yield application
    await application.close()


async def _conversation_run(app: Application, *, workspace: Path | None, text: str):
    params = {"workspace_path": str(workspace)} if workspace is not None else {}
    conversation = (await app.conversation_create(params))["conversation"]
    await app.conversations.add_message(conversation["id"], {"role": "user", "content": text})
    run = await app.runs.create(conversation["id"], text, "normal")
    return conversation, run


async def test_unbound_conversation_has_no_filesystem_tools(workspace_app, monkeypatch):
    app = workspace_app
    _, run = await _conversation_run(app, workspace=None, text="hello")
    request = {}

    async def fake_complete(_self, messages, extra_tools, base_tools, **_kwargs):
        request["messages"] = messages
        request["tools"] = {item["function"]["name"] for item in [*base_tools, *extra_tools]}
        return ModelReply(
            content="hello",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    await app._execute_run(run)

    assert request["tools"].isdisjoint({"list_files", "read_file", "write_file", "shell", "publish_file", "program"})
    assert not any(name.startswith("mcp__") for name in request["tools"])
    assert "No workspace is attached" in request["messages"][0]["content"]
    stored = (await app.runs.get({"run_id": run["id"]}))["run"]
    assert stored["workspace_path"] is None
    assert stored["workspace_source"] == "none"


async def test_run_uses_conversation_workspace_and_snapshots_it(workspace_app, monkeypatch, tmp_path):
    app = workspace_app
    project = tmp_path / "project-a"
    project.mkdir()
    _, run = await _conversation_run(app, workspace=project, text="inspect")
    request = {}

    async def fake_complete(_self, messages, extra_tools, base_tools, **_kwargs):
        request["messages"] = messages
        request["tools"] = {item["function"]["name"] for item in [*base_tools, *extra_tools]}
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    await app._execute_run(run)

    assert "list_files" in request["tools"]
    assert str(project.resolve()) in request["messages"][0]["content"]
    stored = (await app.runs.get({"run_id": run["id"]}))["run"]
    assert stored["workspace_path"] == str(project.resolve())
    assert stored["workspace_source"] == "conversation"
    events = await app.runs.runtime_events(run["id"])
    resolved = next(event for event in events if event["type"] == "workspace_resolved")
    assert resolved["workspace"]["path"] == str(project.resolve())
    assert resolved["workspace"]["source"] == "conversation"


async def test_run_override_reads_from_its_own_workspace(workspace_app, monkeypatch, tmp_path):
    app = workspace_app
    conversation_root = tmp_path / "project-a"
    override_root = tmp_path / "project-b"
    conversation_root.mkdir()
    override_root.mkdir()
    (conversation_root / "marker.txt").write_text("A", encoding="utf-8")
    (override_root / "marker.txt").write_text("B", encoding="utf-8")
    conversation = (await app.conversation_create({"workspace_path": str(conversation_root)}))["conversation"]
    await app.conversations.add_message(conversation["id"], {"role": "user", "content": "read marker"})
    run = await app.runs.create(
        conversation["id"],
        "read marker",
        "normal",
        workspace_path=str(override_root.resolve()),
        workspace_source="run_override",
    )
    calls = 0

    async def fake_complete(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[{"id": "read", "name": "read_file", "arguments": {"path": "marker.txt"}}],
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    await app._execute_run(run)

    events = await app.runs.runtime_events(run["id"])
    result = next(event["tool_result"] for event in events if event["type"] == "tool_result")
    assert result["output"] == "B"
    stored = (await app.runs.get({"run_id": run["id"]}))["run"]
    assert stored["workspace_path"] == str(override_root.resolve())
    assert stored["workspace_source"] == "run_override"
