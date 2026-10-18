from __future__ import annotations

import json

import httpx

from kore_next.config import Settings
from kore_next.model import ModelClient
from kore_next.model.presets import resolve_api_style


def _tool(name: str = "read_file") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }


def _mock_client(monkeypatch, module, handler):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_protocol_selection_is_style_aware():
    assert resolve_api_style("anthropic", "chat_completions") == "anthropic_messages"
    assert resolve_api_style("openai", "responses") == "responses"
    assert resolve_api_style("deepseek", None) == "chat_completions"


async def test_openai_chat_request_and_tool_call_shape(monkeypatch):
    import kore_next.model.openai_chat as protocol

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        )

    _mock_client(monkeypatch, protocol, handler)
    settings = Settings(provider="openai", model="gpt-test", base_url="https://api.openai.com/v1", api_key="env-key")
    client = ModelClient(
        settings,
        {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "api_key": "request-key",
        },
    )
    reply = await client.complete([{"role": "user", "content": "hi"}], [], [_tool()])

    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["authorization"] == "Bearer request-key"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["tools"][0]["function"]["name"] == "read_file"
    assert seen["body"]["tool_choice"] == "auto"
    assert reply.tool_calls == [{"id": "call-1", "name": "read_file", "arguments": {"path": "a.txt"}}]
    assert reply.usage["total_tokens"] == 13


async def test_openai_responses_request_and_output_shape(monkeypatch):
    import kore_next.model.openai_responses as protocol

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
                    {
                        "type": "function_call",
                        "call_id": "call-2",
                        "name": "read_file",
                        "arguments": '{"path":"b.txt"}',
                    },
                ],
                "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
            },
        )

    _mock_client(monkeypatch, protocol, handler)
    settings = Settings(provider="openai", model="gpt-test", base_url="https://api.openai.com/v1", api_key="key")
    client = ModelClient(
        settings,
        {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "api_style": "responses",
            "api_key": "key",
        },
    )
    reply = await client.complete([{"role": "user", "content": "hi"}], [], [_tool()])

    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert seen["body"]["input"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["tools"][0]["name"] == "read_file"
    assert "function" not in seen["body"]["tools"][0]
    assert reply.content == "done"
    assert reply.tool_calls == [{"id": "call-2", "name": "read_file", "arguments": {"path": "b.txt"}}]
    assert reply.usage["total_tokens"] == 12


async def test_anthropic_messages_converts_tools_and_history(monkeypatch):
    import kore_next.model.anthropic_messages as protocol

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("x-api-key")
        seen["version"] = request.headers.get("anthropic-version")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "done"},
                    {"type": "tool_use", "id": "call-3", "name": "read_file", "input": {"path": "c.txt"}},
                ],
                "usage": {
                    "input_tokens": 6,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 1,
                },
            },
        )

    _mock_client(monkeypatch, protocol, handler)
    settings = Settings(
        provider="anthropic", model="claude-test", base_url="https://api.anthropic.com/v1", api_key="key"
    )
    client = ModelClient(
        settings,
        {
            "provider": "anthropic",
            "model": "claude-test",
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "key",
        },
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-2", "content": "file contents"},
    ]
    reply = await client.complete(messages, [], [_tool()])

    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["api_key"] == "key"
    assert seen["version"] == "2023-06-01"
    assert seen["body"]["system"] == "system prompt"
    assert seen["body"]["tools"][0]["input_schema"] == _tool()["function"]["parameters"]
    assert seen["body"]["messages"][1]["content"][0]["type"] == "tool_use"
    assert seen["body"]["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call-2",
        "content": "file contents",
    }
    assert reply.tool_calls == [{"id": "call-3", "name": "read_file", "arguments": {"path": "c.txt"}}]
    assert reply.usage["cached_input_tokens"] == 3
    assert reply.usage["cache_write_input_tokens"] == 1


async def test_missing_key_returns_setup_message_without_request(monkeypatch):
    import kore_next.model.openai_chat as protocol

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request should not be sent")

    _mock_client(monkeypatch, protocol, handler)
    settings = Settings(provider="openai", model="gpt-test", base_url="https://api.openai.com/v1", api_key="")
    client = ModelClient(settings, {"provider": "openai", "model": "gpt-test"})
    reply = await client.complete([{"role": "user", "content": "hi"}])

    assert reply.tool_calls == []
    assert "模型尚未配置" in (reply.content or "")
