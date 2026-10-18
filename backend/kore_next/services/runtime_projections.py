from __future__ import annotations

import json
from typing import Any


def project_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project durable runtime facts into the canonical conversation shape."""
    messages: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        if kind == "user_message":
            message = event.get("message") or {}
            messages.append({"role": "user", "content": message.get("content") or ""})
        elif kind == "assistant_message":
            message = event.get("message") or {}
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "name": message.get("name"),
                    "tool_call_id": message.get("tool_call_id"),
                    "tool_calls": message.get("tool_calls") or [],
                    "reasoning": message.get("reasoning"),
                }
            )
        elif kind == "tool_result":
            result = event.get("tool_result") or {}
            messages.append(
                {
                    "role": "tool",
                    "content": result.get("output") or result.get("error") or "",
                    "name": result.get("tool_name"),
                    "tool_call_id": result.get("tool_call_id"),
                    "tool_calls": [],
                }
            )
    return messages


def project_model_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project only facts that belong in the next model request."""
    messages: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        if kind == "user_message":
            message = event.get("message") or {}
            messages.append({"role": "user", "content": message.get("content") or ""})
        elif kind == "assistant_message":
            message = event.get("message") or {}
            item = {"role": "assistant", "content": message.get("content") or ""}
            calls = message.get("tool_calls") or []
            if calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": _arguments(call.get("arguments"))},
                    }
                    for call in calls
                ]
            messages.append(item)
        elif kind == "tool_result":
            result = event.get("tool_result") or {}
            messages.append(
                {
                    "role": "tool",
                    "content": result.get("output") or result.get("error") or "",
                    "name": result.get("tool_name"),
                    "tool_call_id": result.get("tool_call_id"),
                }
            )
    return messages


def _arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def order_tool_messages(messages: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore model-request order for the trailing tool-result batch."""

    order = {str(call.get("id")): index for index, call in enumerate(calls)}
    start = 0
    for index, message in enumerate(messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            start = index + 1
    trailing = messages[start:]
    if not trailing or any(message.get("role") != "tool" for message in trailing):
        return messages
    ordered = sorted(
        trailing,
        key=lambda message: order.get(str(message.get("tool_call_id")), len(order)),
    )
    return [*messages[:start], *ordered]


def classify_terminal(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first durable terminal fact for a run."""
    for event in events:
        kind = event.get("type")
        if kind == "agent_completed":
            return {"status": "completed", "event_seq": event.get("event_seq"), "stop_reason": "completed"}
        if kind == "agent_failed":
            return {
                "status": "failed",
                "event_seq": event.get("event_seq"),
                "stop_reason": event.get("stop_reason") or "error",
                "error": event.get("error"),
            }
        if kind == "run_interrupted":
            return {"status": "interrupted", "event_seq": event.get("event_seq"), "stop_reason": "interrupted"}
    return None
