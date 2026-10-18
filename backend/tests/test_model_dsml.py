from __future__ import annotations

from kore_next.model.dsml import normalize_reply, parse_dsml_calls
from kore_next.model.types import ModelReply

DSML = (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="shell">\n'
    '<｜｜DSML｜｜ parameter name="command" string="true">cd /testbed && pytest x.py -q</｜｜DSML｜｜ parameter>\n'
    '<｜｜DSML｜｜ parameter name="timeout_seconds" string="true">60</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


def test_parse_dsml_calls_with_typed_parameters():
    calls = parse_dsml_calls(DSML)
    assert len(calls) == 1
    assert calls[0]["name"] == "shell"
    assert calls[0]["arguments"] == {"command": "cd /testbed && pytest x.py -q", "timeout_seconds": 60}


def test_normalize_reply_converts_textual_tool_call():
    reply = ModelReply(content=f"Working on it.\n{DSML}", reasoning=None, tool_calls=[], usage={})
    normalized = normalize_reply(reply)
    assert normalized.content == "Working on it."
    assert normalized.tool_calls[0]["name"] == "shell"


def test_normalize_reply_leaves_plain_text_alone():
    reply = ModelReply(content="All tests pass.", reasoning=None, tool_calls=[], usage={})
    assert normalize_reply(reply).content == "All tests pass."
