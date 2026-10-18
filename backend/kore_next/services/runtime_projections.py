from __future__ import annotations

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
