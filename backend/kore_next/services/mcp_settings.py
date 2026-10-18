from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..database import Database
from ..mcp import McpManager
from ..rpc import RpcFault, required


class McpSettingsService:
    """MCP server configuration and runtime state presentation."""

    def __init__(
        self,
        db: Database,
        *,
        settings: Callable[[], Settings],
        mcp_manager: Callable[[], McpManager],
    ):
        self.db = db
        self._settings = settings
        self._mcp_manager = mcp_manager

    async def mcp(self) -> dict[str, Any]:
        return await self.db.get_json("mcp", {"servers": []})

    def runtime_servers(self) -> dict[str, Any]:
        return self._mcp_manager().servers

    async def save_mcp(self, data: dict[str, Any]) -> None:
        settings = self._settings()
        await self.db.set_json("mcp", data)
        path = settings.data_dir / "mcp.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def mcp_view(self, item: dict[str, Any]) -> dict[str, Any]:
        runtime = self._mcp_manager().servers.get(item["name"])
        state = "restart_required" if item.get("enabled", True) else "stopped"
        error = None
        tool_names: list[str] = []
        if runtime:
            error = runtime.error
            state = "failed" if error else "running"
            tool_names = [tool.get("name", "") for tool in runtime.tools]
        sandbox = item.get("sandbox") or {}
        filesystem = sandbox.get("filesystem", "workspace")
        network = sandbox.get("network", "denied")
        host_mode = filesystem == "host" and network == "allowed"
        native_sandbox = sys.platform == "darwin" and not host_mode
        supported = host_mode or (
            native_sandbox and network in {"allowed", "denied"} and filesystem in {"workspace", "readonly"}
        )
        supported = supported and not any(
            sandbox.get(key) for key in ("readable_roots", "writable_roots", "allowed_domains")
        )
        if item.get("enabled", True) and not supported and not runtime:
            state = "unavailable"
            error = "所选沙箱策略在此平台不可用，进程不会启动"
        return {
            **item,
            "env_names": sorted(item.get("env", {}).keys()),
            "state": state,
            "tool_names": tool_names,
            "error": error,
            "started_at": runtime.started_at if runtime else None,
            "last_call_at": runtime.last_call_at if runtime else None,
            "restart_count": runtime.restart_count if runtime else 0,
            "sandboxed": native_sandbox,
            "sandbox_backend": "macos-seatbelt"
            if native_sandbox and supported
            else ("host" if host_mode and supported else "unavailable"),
        }

    async def mcp_add(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        name = str(required(params, "name"))
        command = str(required(params, "command"))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RpcFault(-32602, "Invalid MCP server name")
        data = await self.mcp()
        if any(s["name"] == name for s in data["servers"]):
            raise RpcFault(-32001, "MCP server already exists")
        item = {
            "name": name,
            "command": command,
            "args": list(params.get("args", [])),
            "cwd": params.get("cwd"),
            "enabled": bool(params.get("enabled", True)),
            "permission": params.get("permission", "human_approval"),
            "env": dict(params.get("env", {})),
            "startup_timeout_seconds": float(params.get("startup_timeout_seconds", 20)),
            "call_timeout_seconds": float(params.get("call_timeout_seconds", 60)),
            "sandbox": params.get(
                "sandbox",
                {
                    "filesystem": "workspace",
                    "network": "denied",
                    "readable_roots": [],
                    "writable_roots": [],
                    "allowed_domains": [],
                },
            ),
        }
        data["servers"].append(item)
        await self.save_mcp(data)
        return {
            "server": self.mcp_view(item),
            "restart_required": True,
            "config_path": str(settings.data_dir / "mcp.json"),
        }

    async def mcp_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        data, name = await self.mcp(), str(required(params, "name"))
        item = next((s for s in data["servers"] if s["name"] == name), None)
        if not item:
            raise RpcFault(-32000, "MCP server not found")
        item["enabled"] = bool(params.get("enabled"))
        await self.save_mcp(data)
        return {"server": self.mcp_view(item)}

    async def mcp_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        data, name = await self.mcp(), str(required(params, "name"))
        before = len(data["servers"])
        data["servers"] = [s for s in data["servers"] if s["name"] != name]
        if len(data["servers"]) == before:
            raise RpcFault(-32000, "MCP server not found")
        await self.save_mcp(data)
        return {"deleted": True, "restart_required": True}
