"""Tool capability policies shared by batching and program orchestration."""

from dataclasses import dataclass

CONCURRENT_READ_TOOLS = {
    "read_file",
    "list_files",
    "get_current_time",
    "web_search",
    "history_search",
    "history_read",
    "session_search",
    "memory_list",
    "memory_read",
    "task_get",
    "task_list",
    "skill_read",
    "skill_resource_read",
    "evidence_search",
    "evidence_read",
    "tool_result_inspect",
    "tool_result_read",
    "automation_list",
    "automation_get",
}

PROGRAM_READ_TOOLS = {
    *CONCURRENT_READ_TOOLS,
    "computer_observe",
    "http_request",
}


@dataclass(frozen=True)
class ToolPolicy:
    side_effect: str
    idempotent: bool
    recovery: str


_CONTROL = ToolPolicy("none", True, "safe_retry")
_READ = ToolPolicy("read", True, "safe_retry")
_WRITE = ToolPolicy("write", False, "reconcile")
_EXTERNAL = ToolPolicy("external", False, "park")

_TOOL_POLICIES = {
    "write_file": _WRITE,
    "publish_file": _WRITE,
    "artifact_publish": _WRITE,
    "remember": _WRITE,
    "memory_create": _WRITE,
    "memory_update": _WRITE,
    "memory_archive": _WRITE,
    "core_memory_update": _WRITE,
    "core_memory_remove": _WRITE,
    "task_create": _WRITE,
    "task_update": _WRITE,
    "automation_create": _WRITE,
    "automation_pause": _WRITE,
    "automation_resume": _WRITE,
    "automation_cancel": _WRITE,
    "shell": _EXTERNAL,
    "http_request": _WRITE,
    "program": _READ,
    "tool_catalog": _CONTROL,
    "get_current_time": _CONTROL,
}


def tool_policy(name: str, arguments: dict | None = None) -> ToolPolicy:
    if name == "http_request":
        method = str(arguments.get("method") or "GET").upper() if isinstance(arguments, dict) else "GET"
        return _READ if method in {"GET", "HEAD"} else _WRITE
    policy = _TOOL_POLICIES.get(name)
    if policy is not None:
        return policy
    if name in CONCURRENT_READ_TOOLS:
        return _READ
    if name == "computer_observe":
        return _READ
    if name.startswith("computer_") or name.startswith("mcp__"):
        return _EXTERNAL
    return _EXTERNAL
