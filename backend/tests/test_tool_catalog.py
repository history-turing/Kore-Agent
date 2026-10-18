from __future__ import annotations

import pytest

from kore_next.config import Settings
from kore_next.mcp import McpManager
from kore_next.tools import TOOL_CATALOG_DEFINITION, ToolArgumentError, ToolExecutor
from kore_next.tools.validation import validate_arguments


class NoComputer:
    @staticmethod
    def tool_definitions():
        return []


class FakeMcp:
    @staticmethod
    def tool_definitions():
        return [
            {
                "type": "function",
                "function": {
                    "name": "mcp__filesystem__read",
                    "description": "Read a file from the filesystem server.",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mcp__filesystem__write",
                    "description": "Write a file through the filesystem server.",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            },
        ]


def _executor(tmp_path, mcp=None):
    return ToolExecutor(
        Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"),
        None,
        None,
        None,
        mcp or McpManager(tmp_path),
        NoComputer(),  # type: ignore[arg-type]
    )


def test_empty_query_lists_inventory_without_activating(tmp_path):
    executor = _executor(tmp_path)
    result = executor.activate_tools("", "normal")
    categories = {item["category"] for item in result["inventory"]}
    assert {"memory", "tasks", "automation"} <= categories
    assert result["activated"] == []
    assert result["matching_count"] > 0


def test_exact_name_ranks_first_and_is_activated(tmp_path):
    executor = _executor(tmp_path)
    result = executor.activate_tools("memory_read", "normal", effective_step=3)
    assert result["matches"][0]["name"] == "memory_read"
    assert result["matches"][0]["matched_on"][0] == "name"
    assert "memory_read" in result["activated"]
    assert result["effective_step"] == 3


def test_category_query_activates_the_whole_bounded_group(tmp_path):
    executor = _executor(tmp_path)
    result = executor.activate_tools("memory", "normal")
    assert result["matches"][0]["category"] == "memory"
    assert "memory_read" in result["activated"]
    assert all(item["category"] == "memory" for item in result["matches"])


def test_explicit_names_activate_only_the_requested_tools(tmp_path):
    executor = _executor(tmp_path)
    result = executor.activate_tools("", "normal", names=["memory_read"])
    assert result["activated"] == ["memory_read"]
    assert result["unknown"] == []


def test_category_filter_and_mcp_server_grouping(tmp_path):
    executor = _executor(tmp_path, FakeMcp())
    inventory = executor.activate_tools("", "normal")
    mcp_group = next(item for item in inventory["inventory"] if item["category"] == "mcp:filesystem")
    assert mcp_group["count"] == 2
    result = executor.activate_tools("", "normal", category="mcp:filesystem")
    assert {item["name"] for item in result["matches"]} == {
        "mcp__filesystem__read",
        "mcp__filesystem__write",
    }


def test_tool_catalog_schema_accepts_empty_and_bounds_explicit_names():
    validate_arguments(TOOL_CATALOG_DEFINITION, {})
    with pytest.raises(ToolArgumentError):
        validate_arguments(TOOL_CATALOG_DEFINITION, {"names": ["x"] * 21})
