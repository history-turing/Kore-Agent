"""Compatibility facade for the agent's model and tool boundaries.

The implementation lives in ``kore_next.model`` and ``kore_next.tools``. This
module keeps existing imports stable for callers, benchmarks, and tests.
"""

from .model import ModelClient, ModelReply
from .model.types import model_usage as _model_usage
from .model.types import openai_cache_usage as _openai_cache_usage
from .tools import (
    CORE_TOOLS,
    PLAN_ALLOWED_TOOLS,
    TOOL_CATALOG_DEFINITION,
    TOOLS,
    ToolExecutor,
    builtin_tool_definitions,
    shell_command_argv,
)

__all__ = [
    "CORE_TOOLS",
    "ModelClient",
    "ModelReply",
    "PLAN_ALLOWED_TOOLS",
    "TOOLS",
    "TOOL_CATALOG_DEFINITION",
    "ToolExecutor",
    "_model_usage",
    "_openai_cache_usage",
    "builtin_tool_definitions",
    "shell_command_argv",
]
