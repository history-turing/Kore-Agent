"""Tool definitions and execution boundary for the agent loop."""

from .definitions import (
    CORE_TOOLS,
    PLAN_ALLOWED_TOOLS,
    PROGRAM_DEFINITION,
    TOOL_CATALOG_DEFINITION,
    TOOLS,
    builtin_tool_definitions,
)
from .errors import ToolArgumentError, ToolDenied, ToolError, ToolNotVisibleYet
from .executor import ApprovalGate, ToolExecutor
from .projection import ToolProjection
from .shell import shell_command_argv

__all__ = [
    "ApprovalGate",
    "CORE_TOOLS",
    "PLAN_ALLOWED_TOOLS",
    "PROGRAM_DEFINITION",
    "TOOLS",
    "TOOL_CATALOG_DEFINITION",
    "ToolArgumentError",
    "ToolDenied",
    "ToolError",
    "ToolExecutor",
    "ToolNotVisibleYet",
    "ToolProjection",
    "builtin_tool_definitions",
    "shell_command_argv",
]
