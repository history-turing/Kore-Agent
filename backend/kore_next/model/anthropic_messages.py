from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .types import DeltaSink, ModelReply, model_usage


def _anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = "You are KoreAgent Next, a careful local AI agent."
    clean: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system = str(message.get("content") or system)
            continue
        if role == "tool":
            clean.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id"),
                            "content": message.get("content") or "",
                        }
                    ],
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message["tool_calls"]:
                function = call.get("function", {})
                raw = function.get("arguments", "{}")
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "input": arguments,
                    }
                )
            clean.append({"role": "assistant", "content": blocks})
        else:
            clean.append({"role": role, "content": message.get("content") or ""})
    return system, clean


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": item["function"]["name"],
            "description": item["function"]["description"],
            "input_schema": item["function"]["parameters"],
        }
        for item in tools
    ]


def _anthropic_usage(usage: dict[str, Any]) -> dict[str, int]:
    base_input, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    cache_read = usage.get("cache_read_input_tokens")
    cache_write = usage.get("cache_creation_input_tokens")
    cache_reported = cache_read is not None or cache_write is not None
    inp = base_input + int(cache_read or 0) + int(cache_write or 0) if cache_reported else base_input
    return model_usage(
        inp,
        out,
        inp + out,
        cached_input_tokens=int(cache_read or 0) if cache_reported else None,
        cache_write_input_tokens=int(cache_write) if cache_write is not None else None,
        uncached_input_tokens=base_input + int(cache_write or 0) if cache_reported else None,
    )


async def complete_messages(
    *,
    api_key: str,
    model: str,
    base_url: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_text_delta: DeltaSink | None,
    on_reasoning_delta: DeltaSink | None,
) -> ModelReply:
    system, clean = _anthropic_messages(messages)
    request_body: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": clean,
    }
    anthropic_tools = _anthropic_tools(tools)
    if anthropic_tools:
        request_body["tools"] = anthropic_tools
    if on_text_delta is not None or on_reasoning_delta is not None:
        return await stream_messages(
            api_key=api_key,
            base_url=base_url,
            body=request_body,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{base_url}/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json=request_body,
        )
        response.raise_for_status()
    payload = response.json()
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in payload.get("content", []):
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block["id"], "name": block["name"], "arguments": block.get("input", {})})
    return ModelReply(
        "\n".join(texts) or None,
        None,
        calls,
        _anthropic_usage(payload.get("usage") or {}),
    )


async def stream_messages(
    *,
    api_key: str,
    base_url: str,
    body: dict[str, Any],
    on_text_delta: DeltaSink | None,
    on_reasoning_delta: DeltaSink | None,
) -> ModelReply:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{base_url}/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={**body, "stream": True},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "message_start":
                    usage.update((event.get("message") or {}).get("usage") or {})
                elif kind == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tools[int(event.get("index", 0))] = {
                            "id": block.get("id") or os.urandom(8).hex(),
                            "name": block.get("name", ""),
                            "raw": "",
                            "input": block.get("input") or {},
                        }
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = str(delta.get("text") or "")
                        text_parts.append(text)
                        if text and on_text_delta:
                            await on_text_delta(text)
                    elif delta_type == "thinking_delta":
                        text = str(delta.get("thinking") or "")
                        reasoning_parts.append(text)
                        if text and on_reasoning_delta:
                            await on_reasoning_delta(text)
                    elif delta_type == "input_json_delta":
                        tool = tools.setdefault(
                            int(event.get("index", 0)),
                            {"id": os.urandom(8).hex(), "name": "", "raw": "", "input": {}},
                        )
                        tool["raw"] += str(delta.get("partial_json") or "")
                elif kind == "message_delta":
                    usage.update(event.get("usage") or {})
    calls = []
    for item in tools.values():
        try:
            arguments = json.loads(item["raw"]) if item["raw"] else item["input"]
        except json.JSONDecodeError:
            arguments = item["input"]
        calls.append({"id": item["id"], "name": item["name"], "arguments": arguments})
    return ModelReply(
        "".join(text_parts) or None,
        "".join(reasoning_parts) or None,
        calls,
        _anthropic_usage(usage),
    )
