from __future__ import annotations

import json

import pytest

from kore_next.application import Application
from kore_next.config import Settings
from kore_next.mcp import McpManager
from kore_next.model.types import ModelReply
from kore_next.tools import PROGRAM_DEFINITION, ToolArgumentError, ToolError, ToolExecutor
from kore_next.tools.validation import validate_arguments


class NoComputer:
    @staticmethod
    def tool_definitions():
        return []


async def _allow(*_args, **_kwargs):
    return True


def _executor(tmp_path):
    return ToolExecutor(
        Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"),
        _allow,
        None,
        None,
        McpManager(tmp_path),
        NoComputer(),  # type: ignore[arg-type]
    )


@pytest.fixture
async def program_app(tmp_path, monkeypatch):
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
    conversation = (await app.conversation_create({"workspace_path": str(app.settings.workspace_dir)}))["conversation"]
    await app.conversations.add_message(conversation["id"], {"role": "user", "content": "run a program"})
    return await app.runs.create(conversation["id"], "run a program", "normal")


async def test_program_orchestrates_read_only_tools(program_app, monkeypatch):
    app = program_app
    run = await _run(app)
    workspace = app.settings.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.txt").write_text("A", encoding="utf-8")
    (workspace / "b.txt").write_text("B", encoding="utf-8")
    calls = 0

    async def fake_complete(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[{"id": "catalog", "name": "tool_catalog", "arguments": {"query": "program"}}],
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        if calls == 2:
            return ModelReply(
                content="",
                reasoning=None,
                tool_calls=[
                    {
                        "id": "program",
                        "name": "program",
                        "arguments": {
                            "source": (
                                "files = tools.call('list_files', {'path': '.'})\n"
                                "names = [line for line in files.split('\\n') if line.endswith('.txt')]\n"
                                "result = [tools.call('read_file', {'path': name}) for name in sorted(names)]\n"
                            )
                        },
                    }
                ],
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return ModelReply(
            content="done",
            reasoning=None,
            tool_calls=[],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    monkeypatch.setattr("kore_next.services.run_engine.ModelClient.complete", fake_complete)
    await app._execute_run(run)

    events = await app.runs.runtime_events(run["id"])
    result = next(
        event["tool_result"]
        for event in events
        if event["type"] == "tool_result" and event["tool_result"]["tool_call_id"] == "program"
    )
    payload = json.loads(result["output"])
    assert payload["result"] == ["A", "B"]
    assert payload["calls"] == 3
    rows = await app.db.fetchall("SELECT tool_name FROM tool_executions WHERE run_id=?", (run["id"],))
    assert sorted(row["tool_name"] for row in rows) == ["program", "tool_catalog"]


async def test_program_rejects_write_tools(tmp_path):
    executor = _executor(tmp_path)
    executor.activate_tools("program", "normal")
    prepared = await executor.preflight(
        "run",
        "program",
        {"source": "result = tools.call('write_file', {'path': 'x.txt', 'content': 'x'})"},
        "program-1",
        projection=executor.project(1, "normal"),
    )
    with pytest.raises(ToolError, match="not allowed"):
        await prepared.dispatch()
    assert not (tmp_path / "workspace" / "x.txt").exists()


async def test_program_ast_blocks_dunder_access(tmp_path):
    executor = _executor(tmp_path)
    executor.activate_tools("program", "normal")
    prepared = await executor.preflight(
        "run",
        "program",
        {"source": "result = ().__class__.__base__"},
        "program-2",
        projection=executor.project(1, "normal"),
    )
    with pytest.raises(ToolError, match="not allowed"):
        await prepared.dispatch()


async def test_program_protocol_preserves_utf8(tmp_path):
    executor = _executor(tmp_path)
    executor.activate_tools("program", "normal")
    prepared = await executor.preflight(
        "run",
        "program",
        {"source": "result = '中文结果'"},
        "program-3",
        projection=executor.project(1, "normal"),
    )
    payload = json.loads(await prepared.dispatch())
    assert payload["result"] == "中文结果"


def test_program_schema_bounds_source_length():
    validate_arguments(PROGRAM_DEFINITION, {"source": "x" * 19_999})
    with pytest.raises(ToolArgumentError):
        validate_arguments(PROGRAM_DEFINITION, {"source": "x" * 20_001})
