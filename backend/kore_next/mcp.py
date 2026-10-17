from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import SandboxSupervisor
from .util import now


class McpToolError(RuntimeError):
    def __init__(self, payload: str) -> None:
        super().__init__("MCP tool reported an error")
        self.payload = payload


@dataclass
class McpProcess:
    config: dict[str, Any]
    supervisor: SandboxSupervisor
    process: asyncio.subprocess.Process | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    sequence: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    profile_path: Path | None = None
    started_at: str | None = None
    restart_count: int = 0
    last_call_at: str | None = None
    reader_task: asyncio.Task[None] | None = None
    refresh_task: asyncio.Task[None] | None = None
    pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            await self.close()
        self.error = None
        command = str(self.config["command"])
        args = [str(value) for value in self.config.get("args", [])]
        cwd = Path(self.config.get("cwd") or self.supervisor.workspace).expanduser().resolve()
        sandbox = self.config.get("sandbox") or {}
        filesystem = str(sandbox.get("filesystem", "workspace"))
        network = str(sandbox.get("network", "denied"))
        launch = self.supervisor.prepare_launch(
            command, args, cwd=cwd, env=self.config.get("env", {}),
            filesystem=filesystem, network=network,
            readable_roots=sandbox.get("readable_roots"),
            writable_roots=sandbox.get("writable_roots"),
            allowed_domains=sandbox.get("allowed_domains"),
        )
        self.profile_path = launch.profile_path
        try:
            self.process = await asyncio.create_subprocess_exec(
                *launch.command, cwd=launch.cwd, env=launch.env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self.reader_task = asyncio.create_task(self._read_loop())
            self.started_at = now()
            await self.request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "koreagent-next", "version": "0.1.0"},
                },
            )
            await self.notify("notifications/initialized", {})
            await self.refresh_tools()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self.refresh_task:
            self.refresh_task.cancel()
            self.refresh_task = None
        if self.reader_task:
            self.reader_task.cancel()
            self.reader_task = None
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("MCP server closed"))
        self.pending.clear()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                self.process.kill()
        if self.profile_path:
            self.profile_path.unlink(missing_ok=True)
            self.profile_path = None

    async def refresh_tools(self) -> None:
        result = await self.request("tools/list", {})
        self.tools = list(result.get("tools", []))

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError("MCP process is not running")
        # Keep one request in flight per process; the reader loop matches its
        # response to the pending request ID.
        async with self.lock:
            self.sequence += 1
            request_id = self.sequence
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            try:
                await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                response = await asyncio.wait_for(future, timeout=float(self.config.get("call_timeout_seconds", 60)))
                self.last_call_at = now()
                return response
            finally:
                self.pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise RuntimeError("MCP server exited")
                response = json.loads(line)
                if response.get("method") == "notifications/tools/list_changed":
                    # Refresh outside the reader loop so its own response can be read.
                    if not self.refresh_task or self.refresh_task.done():
                        self.refresh_task = asyncio.create_task(self.refresh_tools())
                    continue
                request_id = response.get("id")
                future = self.pending.get(request_id)
                if not future or future.done():
                    continue
                if "error" in response:
                    future.set_exception(RuntimeError(response["error"].get("message", "MCP error")))
                else:
                    future.set_result(response.get("result", {}))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.error = str(exc)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(exc)

    async def restart(self) -> None:
        self.restart_count += 1
        await self.close()
        await self.start()

    async def _write(self, payload: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("MCP process is not running")
        self.process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()


class McpManager:
    def __init__(self, workspace: Path | None = None) -> None:
        self.servers: dict[str, McpProcess] = {}
        self.supervisor = SandboxSupervisor((workspace or Path.cwd()).resolve())

    async def start(self, configs: list[dict[str, Any]]) -> None:
        for config in configs:
            if not config.get("enabled", True):
                continue
            server = McpProcess(config, self.supervisor)
            self.servers[config["name"]] = server
            try:
                await asyncio.wait_for(server.start(), timeout=float(config.get("startup_timeout_seconds", 20)))
            except Exception as exc:
                server.error = str(exc)

    async def close(self) -> None:
        await asyncio.gather(*(server.close() for server in self.servers.values()), return_exceptions=True)

    def tool_definitions(self) -> list[dict[str, Any]]:
        result = []
        for server_name, server in self.servers.items():
            if server.error:
                continue
            for tool in server.tools:
                result.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{server_name}__{tool['name']}",
                            "description": tool.get("description", f"MCP tool {tool['name']}"),
                            "parameters": tool.get("inputSchema", {"type": "object"}),
                        },
                    }
                )
        return result

    async def call(self, compound_name: str, arguments: dict[str, Any]) -> str:
        _, server_name, tool_name = compound_name.split("__", 2)
        server = self.servers.get(server_name)
        if not server or server.error:
            raise RuntimeError(f"MCP server {server_name!r} is unavailable")
        try:
            result = await server.request("tools/call", {"name": tool_name, "arguments": arguments})
        except Exception as first_error:
            try:
                await server.restart()
            except Exception as restart_error:
                server.error = str(restart_error)
            raise RuntimeError(
                f"MCP call outcome is uncertain; server restarted without repeating the call: {first_error}"
            ) from first_error
        # Keep the MCP envelope: images, resources, structured data and tool-level
        # errors are meaningful and must not be flattened into an ambiguous string.
        return json.dumps(
            {
                "content": result.get("content", []),
                "structuredContent": result.get("structuredContent"),
                "isError": bool(result.get("isError", False)),
            }, ensure_ascii=False,
        )

    def permission(self, compound_name: str) -> str:
        _, server_name, _ = compound_name.split("__", 2)
        server = self.servers.get(server_name)
        if not server:
            return "forbidden"
        configured = str(server.config.get("permission", "human_approval"))
        if configured == "forbidden":
            return "forbidden"
        sandbox = server.config.get("sandbox") or {}
        if sandbox.get("filesystem") == "host":
            return "human_approval"
        return configured
