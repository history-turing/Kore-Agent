from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .tool_arguments import parse_tool_arguments
from .types import DeltaSink, ModelReply, model_usage, openai_cache_usage


def _responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": item["function"]["name"],
            "description": item["function"]["description"],
            "parameters": item["function"]["parameters"],
            "strict": False,
        }
        for item in tools
    ]


async def complete_responses(
    *,
    api_key: str,
    model: str,
    base_url: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_text_delta: DeltaSink | None,
    on_reasoning_delta: DeltaSink | None,
) -> ModelReply:
    request_body: dict[str, Any] = {"model": model, "input": messages}
    response_tools = _responses_tools(tools)
    if response_tools:
        request_body["tools"] = response_tools
    if on_text_delta is not None or on_reasoning_delta is not None:
        return await stream_responses(
            api_key=api_key,
            base_url=base_url,
            body=request_body,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{base_url}/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=request_body,
        )
        response.raise_for_status()
    payload = response.json()
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in payload.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") in {"output_text", "text"}:
                    texts.append(part.get("text", ""))
        elif item.get("type") == "function_call":
            raw = item.get("arguments", "{}")
            arguments, arguments_error = parse_tool_arguments(raw)
            call = {
                "id": item.get("call_id") or item.get("id") or os.urandom(8).hex(),
                "name": item.get("name", ""),
                "arguments": arguments,
            }
            if arguments_error:
                call["arguments_raw"] = raw
                call["arguments_error"] = arguments_error
            calls.append(call)
    usage = payload.get("usage") or {}
    inp = int(usage.get("input_tokens", 0))
    out = int(usage.get("output_tokens", 0))
    return ModelReply(
        "\n".join(texts) or None,
        None,
        calls,
        model_usage(
            inp,
            out,
            int(usage.get("total_tokens", inp + out)),
            **openai_cache_usage(usage, "input_tokens_details"),
        ),
    )


async def stream_responses(
    *,
    api_key: str,
    base_url: str,
    body: dict[str, Any],
    on_text_delta: DeltaSink | None,
    on_reasoning_delta: DeltaSink | None,
) -> ModelReply:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[str, dict[str, Any]] = {}
    final: dict[str, Any] | None = None
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{base_url}/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={**body, "stream": True},
        ) as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                # Preserve provider error details instead of surfacing the
                # httpx ResponseNotRead exception raised by response.text.
                await response.aread()
                raise
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = str(event.get("type") or "")
                if kind == "response.output_text.delta":
                    delta = str(event.get("delta") or "")
                    text_parts.append(delta)
                    if delta and on_text_delta:
                        await on_text_delta(delta)
                elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                    delta = str(event.get("delta") or "")
                    reasoning_parts.append(delta)
                    if delta and on_reasoning_delta:
                        await on_reasoning_delta(delta)
                elif kind == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        key = str(item.get("call_id") or item.get("id") or os.urandom(8).hex())
                        calls[key] = {"id": key, "name": item.get("name", ""), "raw": str(item.get("arguments") or "")}
                elif kind == "response.function_call_arguments.delta":
                    key = str(event.get("call_id") or event.get("item_id") or "")
                    call = calls.setdefault(key, {"id": key, "name": event.get("name", ""), "raw": ""})
                    call["raw"] += str(event.get("delta") or "")
                elif kind == "response.completed":
                    final = event.get("response") or {}
    usage = (final or {}).get("usage") or {}
    parsed_calls = []
    for call in calls.values():
        arguments, arguments_error = parse_tool_arguments(call["raw"])
        parsed_call = {
            "id": call["id"],
            "name": call["name"],
            "arguments": arguments,
        }
        if arguments_error:
            parsed_call["arguments_raw"] = call["raw"]
            parsed_call["arguments_error"] = arguments_error
        parsed_calls.append(parsed_call)
    inp, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    return ModelReply(
        "".join(text_parts) or None,
        "".join(reasoning_parts) or None,
        parsed_calls,
        model_usage(
            inp,
            out,
            int(usage.get("total_tokens", inp + out)),
            **openai_cache_usage(usage, "input_tokens_details"),
        ),
    )
