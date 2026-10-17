from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from kore_next.agent import ToolExecutor
from kore_next.config import Settings
from kore_next.mcp import McpManager


def executor(tmp_path: Path, **settings: object) -> ToolExecutor:
    config = Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace", **settings)
    return ToolExecutor(config, None, None, None, None, None)  # type: ignore[arg-type]


async def test_search_uses_duckduckgo_without_key_and_falls_back_on_tavily_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import kore_next.agent as agent_module

    original_client = httpx.AsyncClient
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.tavily.com":
            return httpx.Response(503)
        return httpx.Response(200, text='<a class="result__a" href="https://example.com">Example</a>')

    monkeypatch.setattr(agent_module.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    no_key = executor(tmp_path, search_provider="auto", tavily_api_key="")
    result = json.loads(await no_key._web_search("query", 5))
    assert result["provider"] == "duckduckgo"
    assert requests == ["https://html.duckduckgo.com/html/"]

    requests.clear()
    with_key = executor(tmp_path, search_provider="auto", tavily_api_key="test-key")
    result = json.loads(await with_key._web_search("query", 5))
    assert result["provider"] == "duckduckgo"
    assert len(requests) == 2


async def test_search_filters_before_limit_and_unwraps_redirects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import kore_next.agent as agent_module

    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [
                {"title": "Unsafe", "url": "javascript:alert(1)"},
                {"title": "", "url": "https://example.com/empty"},
                {"title": "First", "url": "https://example.com/first"},
                {"title": "Second", "url": "https://example.com/second"},
            ]})
        return httpx.Response(200, text=(
            '<a class="result__a" href="javascript:alert(1)">Unsafe</a>'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ffirst">First</a>'
            '<a class="result__a" href="https://example.com/second">Second</a>'
        ))

    monkeypatch.setattr(agent_module.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    for provider in ("tavily", "duckduckgo"):
        tool = executor(tmp_path, search_provider=provider, tavily_api_key="test-key")
        result = json.loads(await tool._web_search("query", 2))
        assert [item["url"] for item in result["results"]] == [
            "https://example.com/first", "https://example.com/second",
        ]


async def test_http_redirect_rechecks_target_before_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import kore_next.agent as agent_module

    original_client = httpx.AsyncClient
    requested: list[str] = []
    validated: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(agent_module.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    tool = executor(tmp_path)

    async def validate(url: str) -> None:
        validated.append(url)
        if "127.0.0.1" in url:
            raise PermissionError("Private address blocked")

    monkeypatch.setattr(tool, "_validate_public_url", validate)
    with pytest.raises(PermissionError, match="Private address"):
        await tool._http_request("GET", {"url": "https://example.com/start"})
    assert requested == ["https://example.com/start"]
    assert validated[-1] == "http://127.0.0.1/private"


def test_tool_catalog_exposes_optional_tools_only_after_activation(tmp_path: Path):
    class NoComputer:
        @staticmethod
        def tool_definitions():
            return []

    tool = ToolExecutor(
        Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"),
        None, None, None, McpManager(tmp_path), NoComputer(),  # type: ignore[arg-type]
    )
    normal = {item["function"]["name"] for group in tool.tool_definitions("normal") for item in group}
    plan = {item["function"]["name"] for group in tool.tool_definitions("plan") for item in group}
    assert "tool_catalog" in normal and "tool_catalog" in plan
    assert "shell" in normal and "shell" not in plan
    assert "memory_read" not in normal
    assert "memory_read" in tool.activate_tools("memory", "normal")["activated"]
    assert "memory_read" in {item["function"]["name"] for group in tool.tool_definitions("normal") for item in group}
