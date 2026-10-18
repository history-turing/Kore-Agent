from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .tool_arguments import parse_tool_arguments
from .types import DeltaSink, ModelReply, model_usage, openai_cache_usage


async def complete_chat(
    *,
    api_key: str,
    model: str,
    base_url: str,
    provider: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_text_delta: DeltaSink | None,
    on_reasoning_delta: DeltaSink | None,
) -> ModelReply:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body.update(tools=tools, tool_choice="auto")
    if on_text_delta is not None or on_reasoning_delta is not None:
        return await stream_chat(
            api_key=api_key,
            base_url=base_url,
            provider=provider,
            body=body,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
        response.raise_for_status()
    payload = response.json()
    message = payload["choices"][0]["message"]
    calls: list[dict[str, Any]] = []
    for item in message.get("tool_calls") or []:
        raw = item.get("function", {}).get("arguments", "{}")
        arguments, arguments_error = parse_tool_arguments(raw)
        call = {
            "id": item.get("id") or os.urandom(8).hex(),
            "name": item.get("function", {}).get("name", ""),
            "arguments": arguments,
        }
        if arguments_error:
            call["arguments_raw"] = raw
            call["arguments_error"] = arguments_error
        calls.append(call)
    usage = payload.get("usage") or {}
    return ModelReply(
        content=message.get("content"),
        reasoning=message.get("reasoning_content"),
        tool_calls=calls,
        usage=model_usage(
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int(usage.get("total_tokens", 0)),
            **openai_cache_usage(usage, "prompt_tokens_details"),
        ),
    )


async def stream_chat(
    *,
    api_key: str,
    base_url: str,
    provider: str,
    body: dict[str, Any],
    on_text_delta: DeltaSink | None,
    on_reasoning_delta: DeltaSink | None,
) -> ModelReply:
    request_body = {**body, "stream": True}
    # Some OpenAI-compatible gateways (including older DashScope/Qwen
    # deployments) reject stream_options even though their SSE stream is valid.
    if provider not in {"qwen"} and "dashscope" not in base_url.lower():
        request_body["stream_options"] = {"include_usage": True}
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_parts: dict[int, dict[str, str]] = {}
    usage: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=request_body,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    text_parts.append(str(content))
                    if on_text_delta:
                        await on_text_delta(str(content))
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                    if on_reasoning_delta:
                        await on_reasoning_delta(str(reasoning))
                for call in delta.get("tool_calls") or []:
                    index = int(call.get("index", 0))
                    function = call.get("function") or {}
                    part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    part["id"] += str(call.get("id") or "")
                    part["name"] += str(function.get("name") or "")
                    part["arguments"] += str(function.get("arguments") or "")
    calls = []
    for index in sorted(tool_parts):
        part = tool_parts[index]
        arguments, arguments_error = parse_tool_arguments(part["arguments"])
        call = {
            "id": part["id"] or os.urandom(8).hex(),
            "name": part["name"],
            "arguments": arguments,
        }
        if arguments_error:
            call["arguments_raw"] = part["arguments"]
            call["arguments_error"] = arguments_error
        calls.append(call)
    inp = int(usage.get("prompt_tokens", 0))
    out = int(usage.get("completion_tokens", 0))
    return ModelReply(
        content="".join(text_parts) or None,
        reasoning="".join(reasoning_parts) or None,
        tool_calls=calls,
        usage=model_usage(
            inp,
            out,
            int(usage.get("total_tokens", inp + out)),
            **openai_cache_usage(usage, "prompt_tokens_details"),
        ),
    )
