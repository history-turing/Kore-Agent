from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from ..computer import ComputerClient
from ..config import Settings
from ..mcp import McpManager, McpToolError
from ..resources import ResourceAuthority
from ..sandbox import SandboxSupervisor
from .catalog import ToolCatalog
from .definitions import CORE_TOOLS, PLAN_ALLOWED_TOOLS, builtin_tool_definitions
from .errors import (
    ToolArgumentError,
    ToolDenied,
    ToolForbidden,
    ToolNotActive,
    ToolNotVisibleYet,
)
from .projection import ToolProjection
from .shell import shell_command_argv
from .validation import validate_arguments

ApprovalGate = Callable[[str, str, dict[str, Any], str], Awaitable[bool]]

SERVICE_TOOLS = {
    "history_search",
    "history_read",
    "session_search",
    "memory_list",
    "memory_read",
    "memory_create",
    "memory_update",
    "memory_archive",
    "core_memory_update",
    "core_memory_remove",
    "task_create",
    "task_update",
    "task_get",
    "task_list",
    "skill_read",
    "skill_resource_read",
    "evidence_search",
    "evidence_read",
    "tool_result_inspect",
    "tool_result_read",
    "automation_create",
    "automation_list",
    "automation_get",
    "automation_pause",
    "automation_resume",
    "automation_cancel",
}

PERSISTENT_SERVICE_TOOLS = {
    "memory_create",
    "memory_update",
    "memory_archive",
    "core_memory_update",
    "core_memory_remove",
    "automation_create",
    "automation_pause",
    "automation_resume",
    "automation_cancel",
}


def _search_result_url(raw: str) -> str | None:
    """Return an HTTP result URL, unwrapping DuckDuckGo's redirect links."""
    candidate = raw.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlsplit(candidate)
    if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path.startswith("/l/"):
        candidate = parse_qs(parsed.query).get("uddg", [""])[0]
        parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return candidate


@dataclass(frozen=True)
class PreparedToolCall:
    """A tool call that passed visibility, argument, and approval checks."""

    executor: ToolExecutor
    run_id: str
    name: str
    arguments: dict[str, Any]
    call_id: str
    mode: str
    path: Path | None = None
    effective_step: int | None = None

    async def dispatch(self) -> str:
        return await self.executor.dispatch(self)


class ToolExecutor:
    def __init__(
        self,
        settings: Settings,
        approve: ApprovalGate,
        publish: Callable[..., Awaitable[Any]],
        remember: Callable[..., Awaitable[Any]],
        mcp: McpManager,
        computer: ComputerClient,
        services: Any | None = None,
        resources: ResourceAuthority | None = None,
    ):
        self.settings = settings
        self.approve = approve
        self.publish = publish
        self.remember = remember
        self.mcp = mcp
        self.computer = computer
        self.services = services
        self.resources = resources or ResourceAuthority()
        self.sandbox = SandboxSupervisor(settings.workspace_dir)
        # Activation is monotonic within a run: name -> first model step where
        # the tool schema becomes visible.
        self.activation_steps: dict[str, int] = {}

    def restore_activations(self, steps: dict[str, Any]) -> None:
        for name, value in steps.items():
            try:
                step = int(value)
            except (TypeError, ValueError):
                step = 0
            self.activation_steps.setdefault(str(name), max(0, step))

    def latest_step(self) -> int:
        return max(self.activation_steps.values(), default=0)

    def _definition_groups(self, mode: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        builtin = builtin_tool_definitions(mode, web_tools=self.settings.web_tools_enabled)
        extra = [] if mode == "plan" else [*self.mcp.tool_definitions(), *self.computer.tool_definitions()]
        return builtin, extra

    def _available_definitions(self, mode: str) -> list[dict[str, Any]]:
        builtin, extra = self._definition_groups(mode)
        return [*builtin, *extra]

    def project(self, step: int, mode: str) -> ToolProjection:
        """Freeze the tool schemas visible to one model request."""

        builtin, extra = self._definition_groups(mode)
        core = CORE_TOOLS if mode != "plan" else PLAN_ALLOWED_TOOLS
        visible = set(core) | {name for name, effective_step in self.activation_steps.items() if effective_step <= step}
        base = tuple(definition for definition in builtin if definition["function"]["name"] in visible)
        extra_visible = tuple(definition for definition in extra if definition["function"]["name"] in visible)
        return ToolProjection(
            step=step,
            visible=frozenset(visible),
            base=base,
            extra=extra_visible,
        )

    def tool_definitions(
        self, mode: str, *, step: int | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        projection = self.project(self.latest_step() if step is None else step, mode)
        return list(projection.base), list(projection.extra)

    def activate_tools(
        self,
        query: str,
        mode: str,
        *,
        effective_step: int | None = None,
        category: str | None = None,
        names: list[str] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        catalog = ToolCatalog(
            [
                definition
                for definition in self._available_definitions(mode)
                if definition["function"]["name"] != "tool_catalog"
            ],
            active_names=set(self.activation_steps),
        )
        result = catalog.search(
            query=query,
            category=category,
            names=names,
            limit=limit,
        )
        step = 0 if effective_step is None else max(0, int(effective_step))
        activated = []
        for name in result.get("activated", []):
            if name in self.activation_steps:
                continue
            self.activation_steps[name] = step
            activated.append(name)
        result["activated"] = activated
        result["effective_step"] = step
        return result

    def resolve(self, raw: str) -> Path:
        # Container-backed benchmarks address the repository as /testbed. The
        # host workspace is the same directory through the bind mount.
        value = raw.strip()
        if value == "/testbed":
            value = "."
        elif value.startswith("/testbed/"):
            value = value[len("/testbed/") :]
        return self.sandbox.resolve(value)

    async def preflight(
        self,
        run_id: str,
        name: str,
        arguments: dict[str, Any],
        call_id: str,
        *,
        mode: str = "normal",
        projection: ToolProjection | None = None,
        effective_step: int | None = None,
        arguments_error: str | None = None,
    ) -> PreparedToolCall:
        """Validate and authorize a call without causing external side effects."""

        if arguments_error:
            raise ToolArgumentError(arguments_error)
        if projection is None:
            projection = self.project(self.latest_step(), mode)
        if mode == "plan" and name not in PLAN_ALLOWED_TOOLS and not name.startswith("computer_observe"):
            raise ToolNotActive(f"Tool {name} is not available in plan mode", code="not_available_in_plan")
        if not projection.contains(name):
            activated_step = self.activation_steps.get(name)
            if activated_step is not None and activated_step > projection.step:
                raise ToolNotVisibleYet(name, effective_step=activated_step)
            raise ToolNotActive(f"Tool {name} is not active; call tool_catalog first")
        definition = next(
            (item for item in self._available_definitions(mode) if item["function"]["name"] == name),
            None,
        )
        if definition is None:
            raise ToolNotActive(f"Tool {name} is not registered for this run")
        validate_arguments(definition, arguments)
        path = None
        if name in {"read_file", "write_file", "publish_file"}:
            path = self.resolve(str(arguments.get("path", "")))
        elif name == "list_files":
            path = self.resolve(str(arguments.get("path") or "."))
        elif name == "artifact_publish":
            path_value, url_value = arguments.get("path"), arguments.get("url")
            if bool(path_value) == bool(url_value):
                raise ToolArgumentError("Provide exactly one of path or url")
            if path_value:
                path = self.resolve(str(path_value))
            else:
                await self._validate_public_url(str(url_value))
        reason = self._approval_reason(name, arguments)
        if reason is not None:
            allowed = await self.approve(run_id, call_id, {**arguments, "tool_name": name}, reason)
            if not allowed:
                raise ToolDenied("Denied by user")
        return PreparedToolCall(
            executor=self,
            run_id=run_id,
            name=name,
            arguments=arguments,
            call_id=call_id,
            mode=mode,
            path=path,
            effective_step=effective_step,
        )

    def _approval_reason(self, name: str, arguments: dict[str, Any]) -> str | None:
        if name.startswith("computer_"):
            return None if name == "computer_observe" else f"执行电脑操作 {name}"
        if name.startswith("mcp__"):
            permission = self.mcp.permission(name)
            if permission == "forbidden":
                raise ToolForbidden("MCP tool is forbidden by policy")
            return f"调用 MCP 工具 {name}" if permission == "human_approval" else None
        if name == "write_file":
            return "写入工作区文件"
        if name == "shell":
            return "执行本地命令"
        if name == "http_request":
            method = str(arguments.get("method") or "GET").upper()
            return f"发送 {method} 网络请求" if method not in {"GET", "HEAD"} else None
        if name in PERSISTENT_SERVICE_TOOLS:
            return f"执行持久化操作 {name}"
        return None

    async def execute(
        self,
        run_id: str,
        name: str,
        arguments: dict[str, Any],
        call_id: str,
        *,
        mode: str = "normal",
        projection: ToolProjection | None = None,
        effective_step: int | None = None,
        arguments_error: str | None = None,
    ) -> str:
        prepared = await self.preflight(
            run_id,
            name,
            arguments,
            call_id,
            mode=mode,
            projection=projection,
            effective_step=effective_step,
            arguments_error=arguments_error,
        )
        return await prepared.dispatch()

    async def dispatch(self, prepared: PreparedToolCall) -> str:
        async with self.resources.guard(prepared.name, path=prepared.path):
            return await self._dispatch_unlocked(prepared)

    async def _dispatch_unlocked(self, prepared: PreparedToolCall) -> str:
        name = prepared.name
        arguments = prepared.arguments
        run_id = prepared.run_id
        mode = prepared.mode
        if name == "tool_catalog":
            return json.dumps(
                self.activate_tools(
                    str(arguments.get("query") or ""),
                    mode,
                    effective_step=prepared.effective_step,
                    category=(str(arguments["category"]) if arguments.get("category") else None),
                    names=(
                        [str(item) for item in arguments["names"]] if isinstance(arguments.get("names"), list) else None
                    ),
                    limit=int(arguments.get("limit") or 8),
                ),
                ensure_ascii=False,
            )
        if name.startswith("computer_"):
            return await self.computer.tool(run_id, name, arguments)
        if name.startswith("mcp__"):
            result = await self.mcp.call(name, arguments)
            if json.loads(result).get("isError"):
                raise McpToolError(result)
            return result
        if name == "read_file":
            path = prepared.path or self.resolve(str(arguments.get("path", "")))
            if not path.exists():
                raise FileNotFoundError(path)
            if not path.is_file():
                raise IsADirectoryError(path)
            return path.read_text(encoding="utf-8")
        if name == "list_files":
            path = prepared.path or self.resolve(str(arguments.get("path") or "."))
            if not path.exists():
                raise FileNotFoundError(path)
            if not path.is_dir():
                raise NotADirectoryError(path)
            entries = []
            for item in sorted(path.iterdir())[:500]:
                suffix = "/" if item.is_dir() else ""
                entries.append(str(item.relative_to(self.settings.workspace_dir)) + suffix)
            return "\n".join(entries)
        if name == "write_file":
            path = prepared.path or self.resolve(str(arguments.get("path", "")))
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(arguments.get("content", ""))
            path.write_text(content, encoding="utf-8", newline="\n")
            return f"Wrote {len(content.encode('utf-8'))} bytes to {path.name}"
        if name == "shell":
            command = str(arguments.get("command", ""))
            if not command.strip():
                return "Empty command"
            argv = shell_command_argv(self.settings, command)
            timeout = min(max(int(arguments.get("timeout_seconds", 120)), 1), 1800)
            result = await self.sandbox.run(
                argv[0],
                argv[1:],
                timeout=float(timeout),
                filesystem=self.settings.shell_filesystem,
                network=self.settings.shell_network,
            )
            output = result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
            return f"exit_code={result.returncode}\n{output[-40_000:]}"
        if name == "get_current_time":
            timezone = str(arguments.get("timezone") or "UTC")
            try:
                current = datetime.now(ZoneInfo(timezone))
            except Exception as exc:
                raise ValueError(f"Unknown timezone: {timezone}") from exc
            return json.dumps(
                {
                    "timezone": timezone,
                    "iso": current.isoformat(),
                    "utc": datetime.now(UTC).isoformat(),
                }
            )
        if name == "web_search":
            return await self._web_search(str(arguments.get("query", "")), int(arguments.get("limit", 5)))
        if name == "http_request":
            method = str(arguments.get("method") or "GET").upper()
            return await self._http_request(method, arguments)
        if name == "publish_file":
            path = prepared.path or self.resolve(str(arguments.get("path", "")))
            artifact = await self.publish(
                path=path,
                title=str(arguments.get("title", path.name)),
                description=arguments.get("description"),
                run_id=run_id,
            )
            return f"Published artifact {artifact['id']}"
        if name == "artifact_publish":
            path_value, url_value = arguments.get("path"), arguments.get("url")
            if path_value:
                path = prepared.path or self.resolve(str(path_value))
                artifact = await self.publish(
                    path=path,
                    title=str(arguments.get("title") or Path(str(path_value)).name),
                    description=arguments.get("description"),
                    run_id=run_id,
                )
            else:
                artifact = await self.services.publish_url_artifact(
                    url=str(url_value),
                    title=str(arguments.get("title") or url_value),
                    description=arguments.get("description"),
                    run_id=run_id,
                )
            return json.dumps({"artifact": artifact}, ensure_ascii=False)
        if name == "remember":
            memory = await self.remember(
                title=str(arguments.get("title", "Memory")),
                summary=str(arguments.get("summary", "")),
                content=str(arguments.get("content", "")),
            )
            return f"Stored memory {memory['id']}"
        if self.services is not None and name in SERVICE_TOOLS:
            result = await self.services.tool_call(name, arguments, run_id=run_id, mode=mode)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        raise ToolNotActive(f"Tool {name} has no executor")

    async def _web_search(self, query: str, limit: int) -> str:
        if not query.strip():
            raise ValueError("query is required")
        limit = max(1, min(limit, 10))
        provider = self.settings.search_provider.lower()
        if provider not in {"auto", "tavily", "duckduckgo"}:
            raise ValueError("KORENEXT_SEARCH_PROVIDER must be auto, tavily, or duckduckgo")
        if provider == "tavily" and not self.settings.tavily_api_key:
            raise ValueError("Tavily API key is required")
        if provider in {"auto", "tavily"} and self.settings.tavily_api_key:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": self.settings.tavily_api_key,
                            "query": query,
                            "max_results": limit,
                        },
                    )
                    response.raise_for_status()
                payload = response.json()
                results = []
                for item in payload.get("results", []):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    url = _search_result_url(str(item.get("url") or ""))
                    if not title or not url:
                        continue
                    results.append(
                        {
                            "title": title,
                            "url": url,
                            "snippet": str(item.get("content") or ""),
                            "score": item.get("score"),
                        }
                    )
                    if len(results) >= limit:
                        break
                return json.dumps(
                    {"query": query, "provider": "tavily", "results": results},
                    ensure_ascii=False,
                )
            except (httpx.TransportError, httpx.TimeoutException, ValueError) as exc:
                if provider == "tavily":
                    raise RuntimeError(f"Tavily search failed: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                if provider == "tavily" or exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise
        headers = {"User-Agent": "Mozilla/5.0 KoreAgentNext/0.1"}
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            response = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
            response.raise_for_status()
        pattern = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
        snippet_pattern = re.compile(r'class="result__snippet"[^>]*>(.*?)</', re.I | re.S)
        links = pattern.findall(response.text)
        snippets = snippet_pattern.findall(response.text)
        results = []
        for index, (url, title) in enumerate(links):
            clean_title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            clean_snippet = html.unescape(
                re.sub(r"<[^>]+>", "", snippets[index] if index < len(snippets) else "")
            ).strip()
            clean_url = _search_result_url(html.unescape(url))
            if not clean_title or not clean_url:
                continue
            results.append({"title": clean_title, "url": clean_url, "snippet": clean_snippet})
            if len(results) >= limit:
                break
        return json.dumps(
            {"query": query, "provider": "duckduckgo", "results": results},
            ensure_ascii=False,
        )

    async def _http_request(self, method: str, arguments: dict[str, Any]) -> str:
        url = str(arguments.get("url", ""))
        headers = {str(key): str(value) for key, value in dict(arguments.get("headers") or {}).items()}
        body = arguments.get("body")
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for _ in range(10):
                await self._validate_public_url(url)
                response = await client.request(method, url, headers=headers, content=body)
                if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("location"):
                    break
                destination = urljoin(url, response.headers["location"])
                await self._validate_public_url(destination)
                origin, target = urlsplit(url), urlsplit(destination)
                if (origin.scheme, origin.hostname, origin.port) != (
                    target.scheme,
                    target.hostname,
                    target.port,
                ):
                    headers = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() not in {"authorization", "cookie", "proxy-authorization"}
                    }
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and method not in {"GET", "HEAD"}
                ):
                    method = "GET"
                    body = None
                url = destination
            else:
                raise ValueError("Too many HTTP redirects")
        content_type = response.headers.get("content-type", "")
        body = (
            response.text[:50_000]
            if "text" in content_type or "json" in content_type
            else f"<{len(response.content)} binary bytes>"
        )
        return json.dumps(
            {"status": response.status_code, "headers": dict(response.headers), "body": body},
            ensure_ascii=False,
        )

    async def _validate_public_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only public http(s) URLs are allowed")
        host = parsed.hostname.lower()
        if self.settings.http_allow_private_network:
            return
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ToolForbidden("Local network URLs are not allowed")
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        except OSError as exc:
            raise ValueError(f"Cannot resolve host: {host}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                raise ToolForbidden("Private or reserved network addresses are not allowed")
