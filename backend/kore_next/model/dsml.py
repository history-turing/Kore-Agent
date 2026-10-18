"""Normalize DeepSeek DSML tool-call text into canonical tool calls."""

from __future__ import annotations

import html
import json
import re
import uuid
from typing import Any

from .types import ModelReply

_BLOCK = re.compile(r"<[^>]*DSML[^>]*calls>.*?</[^>]*DSML[^>]*calls>", re.S)
_INVOKE = re.compile(r"<[^>]*DSML[^>]*invoke\s+name=\"([^\"]+)\"[^>]*>(.*?)</[^>]*DSML[^>]*invoke>", re.S)
_PARAMETER = re.compile(r"<[^>]*DSML[^>]*parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</[^>]*DSML[^>]*parameter>", re.S)


def _coerce(raw: str) -> Any:
    text = html.unescape(raw).strip()
    if text == "true":
        return True
    if text == "false":
        return False
    if text in {"null", "None"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def parse_dsml_calls(content: str) -> list[dict[str, Any]]:
    calls = []
    for name, body in _INVOKE.findall(content):
        arguments = {key: _coerce(raw) for key, raw in _PARAMETER.findall(body)}
        calls.append({"id": f"dsml-{uuid.uuid4().hex}", "name": name, "arguments": arguments})
    return calls


def normalize_reply(reply: ModelReply) -> ModelReply:
    """Turn DSML text into tool calls when the provider did not parse it."""
    content = reply.content or ""
    if reply.tool_calls or "DSML" not in content:
        return reply
    calls = parse_dsml_calls(content)
    if not calls:
        return reply
    reply.content = _BLOCK.sub("", content).strip() or None
    reply.tool_calls = calls
    return reply
