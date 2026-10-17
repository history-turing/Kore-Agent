from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
import shlex
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from .computer import ComputerClient
from .config import Settings
from .mcp import McpManager, McpToolError
from .sandbox import SandboxSupervisor

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
ApprovalGate = Callable[[str, str, dict[str, Any], str], Awaitable[bool]]
DeltaSink = Callable[[str], Awaitable[None]]


def _search_result_url(raw: str) -> str | None:
    """Return an HTTP result URL, unwrapping DuckDuckGo's redirect links."""
    candidate = raw.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlsplit(candidate)
    if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path.startswith("/l/"):
        candidate = parse_qs(parsed.query).get("uddg", [""])[0]
        parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return candidate


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a UTF-8 text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a command in the workspace after human approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_file",
            "description": "Publish an existing workspace file as a deliverable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["path", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Store stable user information or reusable knowledge in long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["title", "summary", "content"],
            },
        },
    },
]


def _definition(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


TOOLS.extend(
    [
        _definition(
            "artifact_publish",
            "Publish exactly one workspace file or public http(s) URL as a deliverable.",
            {"path": {"type": "string"}, "url": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}},
            ["title"],
        ),
        _definition("get_current_time", "Return the current time in an IANA timezone.", {"timezone": {"type": "string"}}),
        _definition(
            "web_search",
            "Search the web and return titles, URLs and snippets.",
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ["query"],
        ),
        _definition(
            "http_request",
            "Perform an HTTP request to a public URL.",
            {"url": {"type": "string"}, "method": {"type": "string"}, "headers": {"type": "object"}, "body": {"type": "string"}},
            ["url"],
        ),
        _definition(
            "history_search", "Search prior messages in this conversation.", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]
        ),
        _definition("history_read", "Read a range of prior conversation messages.", {"offset": {"type": "integer"}, "limit": {"type": "integer"}}),
        _definition(
            "session_search", "Search messages across past conversations on this device.",
            {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"],
        ),
        _definition("memory_list", "List the long-term memory index.", {"status": {"type": "string"}}),
        _definition("memory_read", "Read one long-term memory record.", {"memory_id": {"type": "string"}}, ["memory_id"]),
        _definition(
            "memory_create",
            "Create a durable long-term memory.",
            {"title": {"type": "string"}, "summary": {"type": "string"}, "content": {"type": "string"}},
            ["title", "summary", "content"],
        ),
        _definition(
            "memory_update",
            "Update a long-term memory.",
            {"memory_id": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"}, "content": {"type": "string"}},
            ["memory_id"],
        ),
        _definition("memory_archive", "Archive a long-term memory.", {"memory_id": {"type": "string"}, "reason": {"type": "string"}}, ["memory_id"]),
        _definition(
            "core_memory_update", "Set one stable core-memory entry.", {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]
        ),
        _definition("core_memory_remove", "Remove one core-memory entry.", {"key": {"type": "string"}}, ["key"]),
        _definition(
            "task_create",
            "Create a tracked task with structured steps.",
            {
                "title": {"type": "string"},
                "goal": {"type": "string"},
                "description": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "object"}},
            },
            ["title", "goal"],
        ),
        _definition(
            "task_update",
            "Update task state, facts or steps.",
            {
                "task_id": {"type": "string"},
                "status": {"type": "string"},
                "description": {"type": "string"},
                "state": {"type": "array"},
                "key_facts": {"type": "array"},
                "steps": {"type": "array", "items": {"type": "object"}},
            },
            ["task_id"],
        ),
        _definition("task_get", "Read a tracked task.", {"task_id": {"type": "string"}}, ["task_id"]),
        _definition("task_list", "List tracked tasks.", {"status": {"type": "string"}, "limit": {"type": "integer"}}),
        _definition(
            "skill_read", "Read the instructions for an available skill.", {"name": {"type": "string"}, "scope": {"type": "string"}}, ["name"]
        ),
        _definition(
            "skill_resource_read",
            "Read a resource inside an installed skill.",
            {"name": {"type": "string"}, "path": {"type": "string"}, "scope": {"type": "string"}},
            ["name", "path"],
        ),
        _definition(
            "evidence_search",
            "Search durable run evidence.",
            {"query": {"type": "string"}, "run_id": {"type": "string"}, "kind": {"type": "string"}, "limit": {"type": "integer"}},
            ["query"],
        ),
        _definition("evidence_read", "Read one evidence record.", {"evidence_id": {"type": "string"}}, ["evidence_id"]),
        _definition(
            "automation_create",
            "Create a once, interval or cron automation.",
            {
                "title": {"type": "string"},
                "prompt": {"type": "string"},
                "kind": {"type": "string"},
                "run_at": {"type": "string"},
                "interval_seconds": {"type": "integer"},
                "cron_expr": {"type": "string"},
                "timezone": {"type": "string"},
            },
            ["title", "prompt", "kind"],
        ),
        _definition("automation_list", "List automations.", {}),
        _definition("automation_get", "Read an automation.", {"automation_id": {"type": "string"}}, ["automation_id"]),
        _definition("automation_pause", "Pause an automation.", {"automation_id": {"type": "string"}}, ["automation_id"]),
        _definition("automation_resume", "Resume an automation.", {"automation_id": {"type": "string"}}, ["automation_id"]),
        _definition("automation_cancel", "Cancel an automation.", {"automation_id": {"type": "string"}}, ["automation_id"]),
    ]
)

PLAN_ALLOWED_TOOLS = {
    "read_file",
    "list_files",
    "web_search",
    "get_current_time",
    "history_search",
    "history_read",
    "session_search",
    "memory_list",
    "memory_read",
    "task_create",
    "task_update",
    "task_get",
    "task_list",
    "evidence_search",
    "evidence_read",
    "tool_catalog",
}

CORE_TOOLS = {
    "read_file", "list_files", "write_file", "shell", "web_search", "get_current_time",
    "task_create", "task_update", "task_get", "task_list", "tool_catalog",
}

TOOL_CATALOG_DEFINITION = _definition(
    "tool_catalog", "Find and activate additional tools for this run by capability or name.",
    {"query": {"type": "string", "description": "Capability to find, for example memory, session search, HTTP, evidence, automation, MCP."}},
    ["query"],
)
TOOLS.append(TOOL_CATALOG_DEFINITION)


def builtin_tool_definitions(mode: str = "normal", *, web_tools: bool = True) -> list[dict[str, Any]]:
    if mode != "plan":
        items = TOOLS
    else:
        items = [item for item in TOOLS if item["function"]["name"] in PLAN_ALLOWED_TOOLS]
    if not web_tools:
        items = [item for item in items if item["function"]["name"] not in {"web_search", "http_request"}]
    return items


CONTAINER_BOOTSTRAP = (
    "if [ -f /opt/miniconda3/bin/activate ]; then "
    "source /opt/miniconda3/bin/activate; conda activate testbed 2>/dev/null || true; "
    "fi"
)


def shell_command_argv(settings: Settings, command: str) -> list[str]:
    """Build the argv for a shell call without going through a platform shell."""
    if settings.shell_backend == "docker":
        container = settings.shell_container.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise ValueError("A valid container name is required for the docker shell backend")
        bootstrap = settings.shell_bootstrap.strip() or CONTAINER_BOOTSTRAP
        script = f"{bootstrap}\n{command}" if bootstrap else command
        docker = shlex.split(settings.docker_command, posix=os.name != "nt") or ["docker"]
        return [*docker, "exec", "-w", "/testbed", container, "bash", "-lc", script]
    if settings.shell_backend != "local":
        raise ValueError(f"Unsupported shell backend: {settings.shell_backend}")
    return shlex.split(command, posix=os.name != "nt")


@dataclass
class ModelReply:
    content: str | None
    reasoning: str | None
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]


def _model_usage(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    *,
    cached_input_tokens: int | None = None,
    cache_write_input_tokens: int | None = None,
    uncached_input_tokens: int | None = None,
) -> dict[str, int]:
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if cached_input_tokens is not None:
        usage["cached_input_tokens"] = cached_input_tokens
        usage["cache_read_input_tokens"] = cached_input_tokens
        usage["uncached_input_tokens"] = (
            uncached_input_tokens if uncached_input_tokens is not None
            else max(0, input_tokens - cached_input_tokens)
        )
    if cache_write_input_tokens is not None:
        usage["cache_write_input_tokens"] = cache_write_input_tokens
    return usage


def _openai_cache_usage(usage: dict[str, Any], details_name: str) -> dict[str, int | None]:
    details = usage.get(details_name) or {}
    cached = next(
        (int(value) for value in (
            usage.get("prompt_cache_hit_tokens"),
            details.get("cached_tokens"),
            usage.get("cached_tokens"),
        ) if value is not None),
        None,
    )
    cache_write = details.get("cache_creation_input_tokens")
    cache_miss = usage.get("prompt_cache_miss_tokens")
    return {
        "cached_input_tokens": cached if cached is not None else (0 if cache_write is not None else None),
        "cache_write_input_tokens": int(cache_write) if cache_write is not None else None,
        "uncached_input_tokens": int(cache_miss) if cache_miss is not None else None,
    }


class ModelClient:
    def __init__(self, settings: Settings, model_config: dict[str, Any] | None = None):
        self.settings = settings
        self.config = model_config or {}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]] | None = None,
        base_tools: list[dict[str, Any]] | None = None,
        on_text_delta: DeltaSink | None = None,
        on_reasoning_delta: DeltaSink | None = None,
    ) -> ModelReply:
        emitted = False

        async def text_sink(delta: str) -> None:
            nonlocal emitted
            emitted = True
            if on_text_delta:
                await on_text_delta(delta)

        async def reasoning_sink(delta: str) -> None:
            nonlocal emitted
            emitted = True
            if on_reasoning_delta:
                await on_reasoning_delta(delta)

        for attempt in range(3):
            try:
                return await self._complete_once(
                    messages,
                    extra_tools,
                    base_tools,
                    on_text_delta=text_sink if on_text_delta else None,
                    on_reasoning_delta=reasoning_sink if on_reasoning_delta else None,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                transient = status is None or status == 429 or status >= 500
                if attempt >= 2 or emitted or not transient:
                    raise RuntimeError(self._model_error(exc)) from exc
                await asyncio.sleep(0.4 * (2**attempt))
        raise RuntimeError("model request failed")

    async def _complete_once(
        self,
        messages: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]] | None = None,
        base_tools: list[dict[str, Any]] | None = None,
        on_text_delta: DeltaSink | None = None,
        on_reasoning_delta: DeltaSink | None = None,
    ) -> ModelReply:
        provider = self.config.get("provider", self.settings.provider)
        if provider == "anthropic":
            return await self._anthropic(
                messages,
                extra_tools or [],
                base_tools,
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        return await self._openai_compatible(
            messages,
            extra_tools or [],
            base_tools,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )

    def _credentials(self) -> tuple[str, str, str]:
        api_key = self.config.get("api_key") or self.settings.api_key
        model = self.config.get("model") or self.settings.model
        base_url = (self.config.get("base_url") or self.settings.base_url).rstrip("/")
        return api_key, model, base_url

    async def _openai_compatible(
        self,
        messages: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]],
        base_tools: list[dict[str, Any]] | None,
        *,
        on_text_delta: DeltaSink | None = None,
        on_reasoning_delta: DeltaSink | None = None,
    ) -> ModelReply:
        api_key, model, base_url = self._credentials()
        if not api_key:
            return ModelReply(
                content="模型尚未配置。请在“设置”中填写 API Key、模型和服务地址。",
                reasoning=None,
                tool_calls=[],
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        if self.config.get("api_style") == "responses":
            return await self._responses(
                api_key,
                model,
                base_url,
                messages,
                extra_tools,
                base_tools,
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        all_tools = (TOOLS if base_tools is None else base_tools) + extra_tools
        body: dict[str, Any] = {"model": model, "messages": messages}
        if all_tools:
            body.update(tools=all_tools, tool_choice="auto")
        if on_text_delta is not None or on_reasoning_delta is not None:
            return await self._stream_chat(
                api_key,
                base_url,
                body,
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
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {}
            calls.append(
                {
                    "id": item.get("id") or os.urandom(8).hex(),
                    "name": item.get("function", {}).get("name", ""),
                    "arguments": arguments,
                }
            )
        usage = payload.get("usage") or {}
        return ModelReply(
            content=message.get("content"),
            reasoning=message.get("reasoning_content"),
            tool_calls=calls,
            usage=_model_usage(
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
                int(usage.get("total_tokens", 0)),
                **_openai_cache_usage(usage, "prompt_tokens_details"),
            ),
        )

    async def _stream_chat(
        self,
        api_key: str,
        base_url: str,
        body: dict[str, Any],
        *,
        on_text_delta: DeltaSink | None,
        on_reasoning_delta: DeltaSink | None,
    ) -> ModelReply:
        request_body = {**body, "stream": True}
        # Some OpenAI-compatible gateways (including older DashScope/Qwen
        # deployments) reject stream_options even though their SSE stream is valid.
        provider = str(self.config.get("provider") or self.settings.provider).lower()
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
            try:
                arguments = json.loads(part["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append({"id": part["id"] or os.urandom(8).hex(), "name": part["name"], "arguments": arguments})
        inp = int(usage.get("prompt_tokens", 0))
        out = int(usage.get("completion_tokens", 0))
        return ModelReply(
            content="".join(text_parts) or None,
            reasoning="".join(reasoning_parts) or None,
            tool_calls=calls,
            usage=_model_usage(
                inp, out, int(usage.get("total_tokens", inp + out)),
                **_openai_cache_usage(usage, "prompt_tokens_details"),
            ),
        )

    async def _responses(
        self,
        api_key: str,
        model: str,
        base_url: str,
        messages: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]],
        base_tools: list[dict[str, Any]] | None,
        *,
        on_text_delta: DeltaSink | None = None,
        on_reasoning_delta: DeltaSink | None = None,
    ) -> ModelReply:
        tools = [
            {
                "type": "function",
                "name": item["function"]["name"],
                "description": item["function"]["description"],
                "parameters": item["function"]["parameters"],
                "strict": False,
            }
            for item in (TOOLS if base_tools is None else base_tools) + extra_tools
        ]
        request_body: dict[str, Any] = {"model": model, "input": messages}
        if tools:
            request_body["tools"] = tools
        if on_text_delta is not None or on_reasoning_delta is not None:
            return await self._stream_responses(
                api_key,
                base_url,
                request_body,
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
                try:
                    arguments = json.loads(raw)
                except json.JSONDecodeError:
                    arguments = {}
                calls.append(
                    {
                        "id": item.get("call_id") or item.get("id") or os.urandom(8).hex(),
                        "name": item.get("name", ""),
                        "arguments": arguments,
                    }
                )
        usage = payload.get("usage") or {}
        inp = int(usage.get("input_tokens", 0))
        out = int(usage.get("output_tokens", 0))
        return ModelReply(
            "\n".join(texts) or None,
            None,
            calls,
            _model_usage(
                inp, out, int(usage.get("total_tokens", inp + out)),
                **_openai_cache_usage(usage, "input_tokens_details"),
            ),
        )

    async def _stream_responses(
        self,
        api_key: str,
        base_url: str,
        body: dict[str, Any],
        *,
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
                response.raise_for_status()
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
            try:
                arguments = json.loads(call["raw"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            parsed_calls.append({"id": call["id"], "name": call["name"], "arguments": arguments})
        inp, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        return ModelReply(
            "".join(text_parts) or None,
            "".join(reasoning_parts) or None,
            parsed_calls,
            _model_usage(
                inp, out, int(usage.get("total_tokens", inp + out)),
                **_openai_cache_usage(usage, "input_tokens_details"),
            ),
        )

    async def _anthropic(
        self,
        messages: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]],
        base_tools: list[dict[str, Any]] | None,
        *,
        on_text_delta: DeltaSink | None = None,
        on_reasoning_delta: DeltaSink | None = None,
    ) -> ModelReply:
        api_key, model, base_url = self._credentials()
        if not api_key:
            return ModelReply(
                "模型尚未配置。请在“设置”中填写 API Key。",
                None,
                [],
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
            )
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
        anthropic_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in (TOOLS if base_tools is None else base_tools) + extra_tools
        ]
        request_body: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": clean,
        }
        if anthropic_tools:
            request_body["tools"] = anthropic_tools
        if on_text_delta is not None or on_reasoning_delta is not None:
            return await self._stream_anthropic(
                api_key,
                base_url,
                request_body,
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
        usage = payload.get("usage") or {}
        base_input, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        cache_read = usage.get("cache_read_input_tokens")
        cache_write = usage.get("cache_creation_input_tokens")
        cache_reported = cache_read is not None or cache_write is not None
        inp = base_input + int(cache_read or 0) + int(cache_write or 0) if cache_reported else base_input
        return ModelReply(
            "\n".join(texts) or None,
            None,
            calls,
            _model_usage(
                inp, out, inp + out,
                cached_input_tokens=int(cache_read or 0) if cache_reported else None,
                cache_write_input_tokens=int(cache_write) if cache_write is not None else None,
                uncached_input_tokens=base_input + int(cache_write or 0) if cache_reported else None,
            ),
        )

    async def _stream_anthropic(
        self,
        api_key: str,
        base_url: str,
        body: dict[str, Any],
        *,
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
        base_input, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        cache_read = usage.get("cache_read_input_tokens")
        cache_write = usage.get("cache_creation_input_tokens")
        cache_reported = cache_read is not None or cache_write is not None
        inp = base_input + int(cache_read or 0) + int(cache_write or 0) if cache_reported else base_input
        return ModelReply(
            "".join(text_parts) or None,
            "".join(reasoning_parts) or None,
            calls,
            _model_usage(
                inp, out, inp + out,
                cached_input_tokens=int(cache_read or 0) if cache_reported else None,
                cache_write_input_tokens=int(cache_write) if cache_write is not None else None,
                uncached_input_tokens=base_input + int(cache_write or 0) if cache_reported else None,
            ),
        )

    @staticmethod
    def _model_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            request_id = exc.response.headers.get("x-request-id") or exc.response.headers.get("request-id")
            detail = exc.response.text[:800]
            suffix = f" request_id={request_id}" if request_id else ""
            return f"Model provider returned HTTP {status}.{suffix} {detail}".strip()
        if isinstance(exc, httpx.TimeoutException):
            return "Model provider request timed out"
        return f"Model provider network error: {exc}"


class ToolExecutor:
    def __init__(
        self,
        settings: Settings,
        approve: ApprovalGate,
        publish: Callable[..., Awaitable[Any]],
        remember: Callable[..., Awaitable[Any]],
        mcp: McpManager,
        computer: ComputerClient,
        services: Any | None = None,
    ):
        self.settings = settings
        self.approve = approve
        self.publish = publish
        self.remember = remember
        self.mcp = mcp
        self.computer = computer
        self.services = services
        self.sandbox = SandboxSupervisor(settings.workspace_dir)
        self.activated_tools: set[str] = set()

    def tool_definitions(self, mode: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        available = builtin_tool_definitions(mode, web_tools=self.settings.web_tools_enabled)
        base = [definition for definition in available if definition["function"]["name"] in CORE_TOOLS | self.activated_tools]
        extra = []
        if mode != "plan":
            extra = [definition for definition in self.mcp.tool_definitions() + self.computer.tool_definitions()
                     if definition["function"]["name"] in self.activated_tools]
        return base, extra

    def activate_tools(self, query: str, mode: str) -> dict[str, Any]:
        options = builtin_tool_definitions(mode, web_tools=self.settings.web_tools_enabled)
        if mode != "plan":
            options = [*options, *self.mcp.tool_definitions(), *self.computer.tool_definitions()]
        terms = [term.lower() for term in re.split(r"\s+", query.strip()) if term]
        matches = []
        for definition in options:
            name = definition["function"]["name"]
            searchable = f"{name} {definition['function'].get('description', '')}".lower()
            if name != "tool_catalog" and (not terms or any(term in searchable for term in terms)):
                matches.append(definition)
        chosen = matches[:8]
        self.activated_tools.update(definition["function"]["name"] for definition in chosen)
        return {"activated": [definition["function"]["name"] for definition in chosen],
                "matching_count": len(matches), "message": "Activated tools are available on the next model step."}

    def resolve(self, raw: str) -> Path:
        # Container-backed benchmarks address the repository as /testbed. The
        # host workspace is the same directory through the bind mount.
        value = raw.strip()
        if value == "/testbed":
            value = "."
        elif value.startswith("/testbed/"):
            value = value[len("/testbed/") :]
        return self.sandbox.resolve(value)

    async def execute(
        self,
        run_id: str,
        name: str,
        arguments: dict[str, Any],
        call_id: str,
        *,
        mode: str = "normal",
    ) -> str:
        if mode == "plan" and name not in PLAN_ALLOWED_TOOLS and not name.startswith("computer_observe"):
            raise PermissionError(f"Tool {name} is not available in plan mode")
        if name == "tool_catalog":
            return json.dumps(self.activate_tools(str(arguments.get("query") or ""), mode), ensure_ascii=False)
        visible = {definition["function"]["name"] for group in self.tool_definitions(mode) for definition in group}
        if name not in visible:
            raise PermissionError(f"Tool {name} is not active; call tool_catalog first")
        if name.startswith("computer_"):
            if name != "computer_observe":
                allowed = await self.approve(run_id, call_id, {**arguments, "tool_name": name}, f"执行电脑操作 {name}")
                if not allowed:
                    return "Denied by user"
            return await self.computer.tool(run_id, name, arguments)
        if name.startswith("mcp__"):
            permission = self.mcp.permission(name)
            if permission == "forbidden":
                raise PermissionError("MCP tool is forbidden by policy")
            if permission == "human_approval":
                allowed = await self.approve(run_id, call_id, {**arguments, "tool_name": name}, f"调用 MCP 工具 {name}")
                if not allowed:
                    return "Denied by user"
            result = await self.mcp.call(name, arguments)
            if json.loads(result).get("isError"):
                raise McpToolError(result)
            return result
        if name == "read_file":
            path = self.resolve(str(arguments.get("path", "")))
            if not path.exists():
                raise FileNotFoundError(path)
            if not path.is_file():
                raise IsADirectoryError(path)
            return path.read_text(encoding="utf-8")
        if name == "list_files":
            path = self.resolve(str(arguments.get("path", ".")))
            if not path.exists():
                raise FileNotFoundError(path)
            if not path.is_dir():
                raise NotADirectoryError(path)
            entries = []
            for item in sorted(path.iterdir())[:500]:
                suffix = "/" if item.is_dir() else ""
                entries.append(str(item.relative_to(self.settings.workspace_dir)) + suffix)
            return "\n".join(entries)
        if name == "write_file":
            await self._require_approval(run_id, call_id, name, arguments, "写入工作区文件")
            path = self.resolve(str(arguments.get("path", "")))
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(arguments.get("content", ""))
            path.write_text(content, encoding="utf-8", newline="\n")
            return f"Wrote {len(content.encode('utf-8'))} bytes to {path.name}"
        if name == "shell":
            await self._require_approval(run_id, call_id, name, arguments, "执行本地命令")
            command = str(arguments.get("command", ""))
            if not command.strip():
                return "Empty command"
            argv = shell_command_argv(self.settings, command)
            timeout = min(max(int(arguments.get("timeout_seconds", 120)), 1), 1800)
            result = await self.sandbox.run(
                argv[0],
                argv[1:],
                timeout=float(timeout),
                filesystem=self.settings.shell_filesystem,
                network=self.settings.shell_network,
            )
            output = result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
            return f"exit_code={result.returncode}\n{output[-40_000:]}"
        if name == "get_current_time":
            timezone = str(arguments.get("timezone") or "UTC")
            try:
                current = datetime.now(ZoneInfo(timezone))
            except Exception as exc:
                raise ValueError(f"Unknown timezone: {timezone}") from exc
            return json.dumps({"timezone": timezone, "iso": current.isoformat(), "utc": datetime.now(UTC).isoformat()})
        if name == "web_search":
            return await self._web_search(str(arguments.get("query", "")), int(arguments.get("limit", 5)))
        if name == "http_request":
            method = str(arguments.get("method") or "GET").upper()
            if method not in {"GET", "HEAD"}:
                await self._require_approval(run_id, call_id, name, arguments, f"发送 {method} 网络请求")
            return await self._http_request(method, arguments)
        if name == "publish_file":
            path = self.resolve(str(arguments.get("path", "")))
            artifact = await self.publish(
                path=path,
                title=str(arguments.get("title", path.name)),
                description=arguments.get("description"),
                run_id=run_id,
            )
            return f"Published artifact {artifact['id']}"
        if name == "artifact_publish":
            path_value, url_value = arguments.get("path"), arguments.get("url")
            if bool(path_value) == bool(url_value):
                raise ValueError("Provide exactly one of path or url")
            if path_value:
                artifact = await self.publish(
                    path=self.resolve(str(path_value)),
                    title=str(arguments.get("title") or Path(str(path_value)).name),
                    description=arguments.get("description"),
                    run_id=run_id,
                )
            else:
                await self._validate_public_url(str(url_value))
                artifact = await self.services.publish_url_artifact(
                    url=str(url_value),
                    title=str(arguments.get("title") or url_value),
                    description=arguments.get("description"),
                    run_id=run_id,
                )
            return json.dumps({"artifact": artifact}, ensure_ascii=False)
        if name == "remember":
            memory = await self.remember(
                title=str(arguments.get("title", "Memory")),
                summary=str(arguments.get("summary", "")),
                content=str(arguments.get("content", "")),
            )
            return f"Stored memory {memory['id']}"
        if self.services is not None and name in {
            "history_search",
            "history_read",
            "session_search",
            "memory_list",
            "memory_read",
            "memory_create",
            "memory_update",
            "memory_archive",
            "core_memory_update",
            "core_memory_remove",
            "task_create",
            "task_update",
            "task_get",
            "task_list",
            "skill_read",
            "skill_resource_read",
            "evidence_search",
            "evidence_read",
            "automation_create",
            "automation_list",
            "automation_get",
            "automation_pause",
            "automation_resume",
            "automation_cancel",
        }:
            if name in {
                "memory_create",
                "memory_update",
                "memory_archive",
                "core_memory_update",
                "core_memory_remove",
                "automation_create",
                "automation_pause",
                "automation_resume",
                "automation_cancel",
            }:
                await self._require_approval(run_id, call_id, name, arguments, f"执行持久化操作 {name}")
            result = await self.services.tool_call(name, arguments, run_id=run_id, mode=mode)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return f"Unknown tool: {name}"

    async def _require_approval(
        self,
        run_id: str,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> None:
        allowed = await self.approve(run_id, call_id, {**arguments, "tool_name": name}, reason)
        if not allowed:
            raise PermissionError("Denied by user")

    async def _web_search(self, query: str, limit: int) -> str:
        if not query.strip():
            raise ValueError("query is required")
        limit = max(1, min(limit, 10))
        provider = self.settings.search_provider.lower()
        if provider not in {"auto", "tavily", "duckduckgo"}:
            raise ValueError("KORENEXT_SEARCH_PROVIDER must be auto, tavily, or duckduckgo")
        if provider == "tavily" and not self.settings.tavily_api_key:
            raise ValueError("Tavily API key is required")
        if provider in {"auto", "tavily"} and self.settings.tavily_api_key:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        "https://api.tavily.com/search",
                        json={"api_key": self.settings.tavily_api_key, "query": query, "max_results": limit},
                    )
                    response.raise_for_status()
                payload = response.json()
                results = []
                for item in payload.get("results", []):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    url = _search_result_url(str(item.get("url") or ""))
                    if not title or not url:
                        continue
                    results.append({"title": title, "url": url,
                                    "snippet": str(item.get("content") or ""), "score": item.get("score")})
                    if len(results) >= limit:
                        break
                return json.dumps({"query": query, "provider": "tavily", "results": results}, ensure_ascii=False)
            except (httpx.TransportError, httpx.TimeoutException, ValueError) as exc:
                if provider == "tavily":
                    raise RuntimeError(f"Tavily search failed: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                if provider == "tavily" or exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise
        headers = {"User-Agent": "Mozilla/5.0 KoreAgentNext/0.1"}
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            response = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
            response.raise_for_status()
        pattern = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
        snippet_pattern = re.compile(r'class="result__snippet"[^>]*>(.*?)</', re.I | re.S)
        links = pattern.findall(response.text)
        snippets = snippet_pattern.findall(response.text)
        results = []
        for index, (url, title) in enumerate(links):
            clean_title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            clean_snippet = html.unescape(re.sub(r"<[^>]+>", "", snippets[index] if index < len(snippets) else "")).strip()
            clean_url = _search_result_url(html.unescape(url))
            if not clean_title or not clean_url:
                continue
            results.append({"title": clean_title, "url": clean_url, "snippet": clean_snippet})
            if len(results) >= limit:
                break
        return json.dumps({"query": query, "provider": "duckduckgo", "results": results}, ensure_ascii=False)

    async def _http_request(self, method: str, arguments: dict[str, Any]) -> str:
        url = str(arguments.get("url", ""))
        headers = {str(key): str(value) for key, value in dict(arguments.get("headers") or {}).items()}
        body = arguments.get("body")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(10):
                await self._validate_public_url(url)
                response = await client.request(method, url, headers=headers, content=body)
                if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("location"):
                    break
                destination = urljoin(url, response.headers["location"])
                await self._validate_public_url(destination)
                origin, target = urlsplit(url), urlsplit(destination)
                if (origin.scheme, origin.hostname, origin.port) != (target.scheme, target.hostname, target.port):
                    headers = {key: value for key, value in headers.items() if key.lower() not in {"authorization", "cookie", "proxy-authorization"}}
                if response.status_code == 303 or (response.status_code in {301, 302} and method not in {"GET", "HEAD"}):
                    method = "GET"
                    body = None
                url = destination
            else:
                raise ValueError("Too many HTTP redirects")
        content_type = response.headers.get("content-type", "")
        body = response.text[:50_000] if "text" in content_type or "json" in content_type else f"<{len(response.content)} binary bytes>"
        return json.dumps(
            {"status": response.status_code, "headers": dict(response.headers), "body": body},
            ensure_ascii=False,
        )

    async def _validate_public_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only public http(s) URLs are allowed")
        host = parsed.hostname.lower()
        if self.settings.http_allow_private_network:
            return
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise PermissionError("Local network URLs are not allowed")
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except OSError as exc:
            raise ValueError(f"Cannot resolve host: {host}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                raise PermissionError("Private or reserved network addresses are not allowed")
