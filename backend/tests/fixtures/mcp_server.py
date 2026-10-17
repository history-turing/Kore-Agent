"""Small newline-JSON MCP server used only by local integration tests."""

from __future__ import annotations

import json
import sys

changed = False


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        continue
    request_id = request.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}})
    elif method == "tools/list":
        names = ["echo", "new"] if changed else ["echo"]
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
            {"name": name, "description": name, "inputSchema": {"type": "object"}} for name in names
        ]}})
    elif method == "tools/call":
        changed = True
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": "one"}, {"type": "image", "data": "AAAA", "mimeType": "image/png"}],
            "structuredContent": {"answer": 42}, "isError": True,
        }})
