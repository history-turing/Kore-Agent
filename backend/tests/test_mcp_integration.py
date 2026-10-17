from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from kore_next.mcp import McpManager


async def test_mcp_structured_result_and_dynamic_tool_refresh(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "mcp_server.py"
    manager = McpManager(tmp_path)
    await manager.start([{
        "name": "fixture", "command": sys.executable, "args": [str(fixture)],
        "sandbox": {"filesystem": "host", "network": "allowed"},
        "permission": "human_approval",
    }])
    try:
        assert {tool["function"]["name"] for tool in manager.tool_definitions()} == {"mcp__fixture__echo"}
        result = json.loads(await manager.call("mcp__fixture__echo", {}))
        assert result["content"][1]["type"] == "image"
        assert result["structuredContent"] == {"answer": 42}
        assert result["isError"] is True
        for _ in range(20):
            if "mcp__fixture__new" in {tool["function"]["name"] for tool in manager.tool_definitions()}:
                break
            await asyncio.sleep(0.05)
        assert "mcp__fixture__new" in {tool["function"]["name"] for tool in manager.tool_definitions()}
    finally:
        await manager.close()
