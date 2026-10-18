from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from jsonschema.validators import validator_for

from .errors import ToolArgumentError


def _format_path(path: Any) -> str:
    parts = ["$"]
    for item in path:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            parts.append(f".{item}")
    return "".join(parts)


def validate_arguments(definition: dict[str, Any], arguments: Any) -> None:
    """Validate a tool call against its declared JSON Schema before dispatch."""

    if not isinstance(arguments, dict):
        raise ToolArgumentError("Tool arguments must be a JSON object")
    parameters = definition.get("function", {}).get("parameters") or {"type": "object"}
    if not isinstance(parameters, dict):
        raise ToolArgumentError("Tool schema must be a JSON object")
    validator_class = validator_for(parameters, default=Draft202012Validator)
    try:
        validator_class.check_schema(parameters)
    except SchemaError:
        # MCP servers occasionally publish schemas with older or vendor-specific
        # keywords. Keep the validation path available instead of blocking the tool.
        validator_class = Draft202012Validator
    errors = sorted(
        validator_class(parameters).iter_errors(arguments),
        key=lambda error: [str(item) for item in error.path],
    )
    if not errors:
        return
    details = [
        {
            "path": _format_path(error.path),
            "message": error.message,
            "validator": error.validator,
        }
        for error in errors
    ]
    summary = "; ".join(f"{item['path']}: {item['message']}" for item in details[:5])
    raise ToolArgumentError(f"Invalid tool arguments: {summary}", errors=details)
