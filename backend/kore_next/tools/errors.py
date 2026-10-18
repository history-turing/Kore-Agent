from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """A tool failure that can be reported to the model without crashing the run."""

    code = "tool_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}


class ToolNotActive(ToolError):
    code = "not_active"


class ToolNotVisibleYet(ToolError):
    code = "not_visible_yet"

    def __init__(self, name: str, *, effective_step: int) -> None:
        super().__init__(
            f"Tool {name} was activated for model step {effective_step}. "
            "Retry it on the next model step, after its schema is visible.",
            details={"tool_name": name, "effective_step": effective_step},
        )
        self.name = name
        self.effective_step = effective_step


class ToolDenied(ToolError):
    code = "denied"


class ToolForbidden(ToolError):
    code = "forbidden"


class ToolArgumentError(ToolError):
    code = "invalid_arguments"

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message, details={"errors": errors or []})
        self.errors = errors or []


class DuplicateToolCall(ToolError):
    code = "duplicate_call_id"


class ToolUnavailable(ToolError):
    code = "unavailable"


class WorkspaceRequired(ToolError):
    code = "workspace_required"
