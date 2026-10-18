from __future__ import annotations

import pytest

from kore_next.application import Application
from kore_next.config import Settings
from kore_next.services.runtime_projections import classify_terminal, project_messages
from kore_next.util import dumps, now


@pytest.fixture
async def runtime_app(tmp_path):
    app = Application(
        Settings(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            user_skills_dir=tmp_path / "skills",
            api_key="",
        )
    )
    await app.db.initialize()
    return app


async def _new_run(app: Application) -> tuple[dict, dict]:
    conversation = (await app.conversations.create({}))["conversation"]
    run = await app.runs.create(conversation["id"], "inspect", "normal")
    return conversation, run


async def test_runtime_events_have_ordered_digest_chain(runtime_app):
    app = runtime_app
    _, run = await _new_run(app)
    await app.runs.event(run, "user_message", message={"role": "user", "content": "hello"})
    await app.runs.event(
        run,
        "assistant_message",
        step=1,
        message={"role": "assistant", "content": "world"},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )

    events = await app.runs.runtime_events(run["id"])
    assert [event["event_seq"] for event in events] == [1, 2]
    assert events[1]["prev_digest"] == events[0]["digest"]
    assert events[1]["invocation_id"] == f"{run['id']}:1"
    assert (await app.runs.verify_events(run["id"]))["valid"] is True

    await app.db.execute(
        "UPDATE runtime_events SET payload_json=? WHERE run_id=? AND event_seq=1",
        (dumps({"message": {"role": "user", "content": "tampered"}}), run["id"]),
    )
    assert (await app.runs.verify_events(run["id"]))["valid"] is False


async def test_runtime_events_backfill_legacy_rows_once(runtime_app):
    app = runtime_app
    conversation, run = await _new_run(app)
    legacy = {
        "event_id": "legacy-event",
        "run_id": run["id"],
        "conversation_id": conversation["id"],
        "sequence": 1,
        "type": "user_message",
        "event_time": now(),
        "message": {"role": "user", "content": "legacy"},
    }
    await app.db.execute(
        "INSERT INTO events VALUES(?,?,?,?,?,?)",
        (legacy["event_id"], run["id"], 1, "user_message", legacy["event_time"], dumps(legacy)),
    )
    await app.runs.event(run, "assistant_message", step=1, message={"role": "assistant", "content": "new"})

    events = await app.runs.runtime_events(run["id"])
    assert [event["type"] for event in events] == ["user_message", "assistant_message"]
    assert [event["event_seq"] for event in events] == [1, 2]
    assert (await app.runs.verify_events(run["id"]))["valid"] is True


async def test_projections_build_messages_and_terminal_state(runtime_app):
    app = runtime_app
    _, run = await _new_run(app)
    await app.runs.event(run, "user_message", message={"role": "user", "content": "hello"})
    await app.runs.event(
        run,
        "assistant_message",
        step=1,
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "task_list", "arguments": {}}],
        },
    )
    await app.runs.event(
        run,
        "tool_result",
        step=1,
        tool_result={
            "tool_call_id": "call-1",
            "tool_name": "task_list",
            "success": True,
            "output": "[]",
            "error": None,
        },
    )
    await app.runs.event(run, "agent_completed", stop_reason="completed")

    events = await app.runs.runtime_events(run["id"])
    messages = project_messages(events)
    assert [message["role"] for message in messages] == ["user", "assistant", "tool"]
    assert messages[1]["tool_calls"][0]["id"] == "call-1"
    assert messages[2]["tool_call_id"] == "call-1"
    assert classify_terminal(events)["status"] == "completed"
