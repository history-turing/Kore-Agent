from __future__ import annotations

from typing import Any


def _definition(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


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


TOOLS.extend(
    [
        _definition(
            "artifact_publish",
            "Publish exactly one workspace file or public http(s) URL as a deliverable.",
            {
                "path": {"type": "string"},
                "url": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            ["title"],
        ),
        _definition(
            "get_current_time", "Return the current time in an IANA timezone.", {"timezone": {"type": "string"}}
        ),
        _definition(
            "web_search",
            "Search the web and return titles, URLs and snippets.",
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ["query"],
        ),
        _definition(
            "http_request",
            "Perform an HTTP request to a public URL.",
            {
                "url": {"type": "string"},
                "method": {"type": "string"},
                "headers": {"type": "object"},
                "body": {"type": "string"},
            },
            ["url"],
        ),
        _definition(
            "history_search",
            "Search prior messages in this conversation.",
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ["query"],
        ),
        _definition(
            "history_read",
            "Read a range of prior conversation messages.",
            {"offset": {"type": "integer"}, "limit": {"type": "integer"}},
        ),
        _definition(
            "session_search",
            "Search messages across past conversations on this device.",
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ["query"],
        ),
        _definition("memory_list", "List the long-term memory index.", {"status": {"type": "string"}}),
        _definition(
            "memory_read", "Read one long-term memory record.", {"memory_id": {"type": "string"}}, ["memory_id"]
        ),
        _definition(
            "memory_create",
            "Create a durable long-term memory.",
            {"title": {"type": "string"}, "summary": {"type": "string"}, "content": {"type": "string"}},
            ["title", "summary", "content"],
        ),
        _definition(
            "memory_update",
            "Update a long-term memory.",
            {
                "memory_id": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "content": {"type": "string"},
            },
            ["memory_id"],
        ),
        _definition(
            "memory_archive",
            "Archive a long-term memory.",
            {"memory_id": {"type": "string"}, "reason": {"type": "string"}},
            ["memory_id"],
        ),
        _definition(
            "core_memory_update",
            "Set one stable core-memory entry.",
            {"key": {"type": "string"}, "value": {"type": "string"}},
            ["key", "value"],
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
            "skill_read",
            "Read the instructions for an available skill.",
            {"name": {"type": "string"}, "scope": {"type": "string"}},
            ["name"],
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
            {
                "query": {"type": "string"},
                "run_id": {"type": "string"},
                "kind": {"type": "string"},
                "limit": {"type": "integer"},
            },
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
        _definition(
            "automation_pause", "Pause an automation.", {"automation_id": {"type": "string"}}, ["automation_id"]
        ),
        _definition(
            "automation_resume", "Resume an automation.", {"automation_id": {"type": "string"}}, ["automation_id"]
        ),
        _definition(
            "automation_cancel", "Cancel an automation.", {"automation_id": {"type": "string"}}, ["automation_id"]
        ),
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
    "read_file",
    "list_files",
    "write_file",
    "shell",
    "web_search",
    "get_current_time",
    "task_create",
    "task_update",
    "task_get",
    "task_list",
    "tool_catalog",
}

TOOL_CATALOG_DEFINITION = _definition(
    "tool_catalog",
    "Find and activate additional tools for this run by capability or name.",
    {
        "query": {
            "type": "string",
            "description": "Capability to find, for example memory, session search, HTTP, evidence, automation, MCP.",
        }
    },
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
