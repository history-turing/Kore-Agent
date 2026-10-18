"""Tool capability policies shared by batching and program orchestration."""

import re
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


_BROAD_COMMANDS = (
    (
        re.compile(r"\bgit\s+cat-file\s+--batch-all-objects\b"),
        "Scanning every git object is too broad. Search source files with `rg -n \"symbol\" path/`.",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)find\s+/\s"),
        "Scanning the whole filesystem is too broad. Search a specific source directory.",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)grep\s+-[A-Za-z]*r[A-Za-z]*\s+/\s"),
        "Use `rg -n` on a specific source directory instead of a filesystem-wide grep.",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)du\s+-[A-Za-z]*s[A-Za-z]*\s+/\s"),
        "Do not scan the whole filesystem for disk usage.",
    ),
)

_PYTEST_COMMAND = re.compile(r"(?:^|[;&|]\s*)(?:python(?:3(?:\.\d+)?)?\s+-m\s+)?pytest\b")
_PATH_WITH_SLASH = re.compile(r"(?:^|\s)(?:\./)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _is_full_test_suite(command: str) -> bool:
    """Detect pytest invocations that would collect an entire repository."""
    if not _PYTEST_COMMAND.search(command):
        return False
    if ".py" in command or "::" in command or "--pyargs" in command:
        return False
    if "--version" in command or "--help" in command:
        return False
    if re.search(r"(?:^|\s)-k(?:\s+|=)", command):
        return False
    # A directory with a slash (e.g. astropy/modeling/tests) is targeted enough.
    return not _PATH_WITH_SLASH.search(command)


def shell_policy_violation(command: str) -> str | None:
    """Return a benchmark-mode rejection message for a wasteful shell command."""
    for pattern, message in _BROAD_COMMANDS:
        if pattern.search(command):
            return message
    if _is_full_test_suite(command):
        return (
            "Full test-suite runs are blocked in benchmark mode. "
            "Run one targeted test file or node id, for example "
            "`python -m pytest path/to/test_file.py -q`."
        )
    return None


_MUTATING_SHELL_PATTERNS = (
    re.compile(r"\bsed\s+-[A-Za-z]*i"),
    re.compile(r"\bperl\s+-[A-Za-z]*i"),
    re.compile(r"(?:^|[^0-9&])>{1,2}(?!&|/dev/null)"),
    re.compile(r"\btee\s+"),
    re.compile(r"\bgit\s+apply\b"),
    re.compile(r"\bpatch\s+-p"),
    re.compile(r"\bpython\d*\s+-[^\n]*<<.*\bopen\s*\(", re.S),
    re.compile(r"\bpython\d*\s+-c\s+.*\bwrite_text\b", re.S),
)


def is_mutating_shell_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in _MUTATING_SHELL_PATTERNS)


def is_mutating_tool(name: str, arguments: dict | None = None) -> bool:
    if name == "write_file":
        return True
    if name == "shell" and isinstance(arguments, dict):
        return is_mutating_shell_command(str(arguments.get("command") or ""))
    return False
