"""Tool capability policies shared by batching and program orchestration."""

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
