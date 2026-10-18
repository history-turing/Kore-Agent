from __future__ import annotations

import json
from typing import Any


def parse_tool_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Parse provider tool arguments without silently accepting malformed JSON."""

    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    if not isinstance(raw, str):
        return {}, "Tool arguments must be a JSON object"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON arguments: {exc.msg} at line {exc.lineno} column {exc.colno}"
    if not isinstance(value, dict):
        return {}, "Tool arguments must be a JSON object"
    return value, None
