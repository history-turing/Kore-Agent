from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..config import Settings
from ..tools.definitions import TOOLS
from .anthropic_messages import complete_messages
from .openai_chat import complete_chat
from .openai_responses import complete_responses
from .presets import resolve_api_style
from .types import DeltaSink, ModelReply


class ModelClient:
    """Provider-independent entry point for model calls.

    The agent loop speaks one canonical message/tool format. This client selects
    a wire protocol, applies retry policy, and normalizes the reply and usage.
    """

    def __init__(
        self,
        settings: Settings,
        model_config: dict[str, Any] | None = None,
        *,
        default_tools: list[dict[str, Any]] | None = None,
    ):
        self.settings = settings
        self.config = model_config or {}
        self.default_tools = TOOLS if default_tools is None else default_tools

    async def complete(
        self,
        messages: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]] | None = None,
        base_tools: list[dict[str, Any]] | None = None,
        on_text_delta: DeltaSink | None = None,
        on_reasoning_delta: DeltaSink | None = None,
    ) -> ModelReply:
        emitted = False

        async def text_sink(delta: str) -> None:
            nonlocal emitted
            emitted = True
            if on_text_delta:
                await on_text_delta(delta)

        async def reasoning_sink(delta: str) -> None:
            nonlocal emitted
            emitted = True
            if on_reasoning_delta:
                await on_reasoning_delta(delta)

        for attempt in range(3):
            try:
                return await self._complete_once(
                    messages,
                    extra_tools,
                    base_tools,
                    on_text_delta=text_sink if on_text_delta else None,
                    on_reasoning_delta=reasoning_sink if on_reasoning_delta else None,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                transient = status is None or status == 429 or status >= 500
                if attempt >= 2 or emitted or not transient:
                    raise RuntimeError(self._model_error(exc)) from exc
                await asyncio.sleep(0.4 * (2**attempt))
        raise RuntimeError("model request failed")

    async def _complete_once(
        self,
        messages: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]] | None = None,
        base_tools: list[dict[str, Any]] | None = None,
        on_text_delta: DeltaSink | None = None,
        on_reasoning_delta: DeltaSink | None = None,
    ) -> ModelReply:
        provider = str(self.config.get("provider") or self.settings.provider)
        api_style = resolve_api_style(provider, self.config.get("api_style"))
        api_key, model, base_url = self._credentials()
        if not api_key:
            content = (
                "模型尚未配置。请在“设置”中填写 API Key。"
                if api_style == "anthropic_messages"
                else "模型尚未配置。请在“设置”中填写 API Key、模型和服务地址。"
            )
            return ModelReply(
                content=content,
                reasoning=None,
                tool_calls=[],
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )

        tools = (self.default_tools if base_tools is None else base_tools) + (extra_tools or [])
        if api_style == "anthropic_messages":
            return await complete_messages(
                api_key=api_key,
                model=model,
                base_url=base_url,
                messages=messages,
                tools=tools,
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        if api_style == "responses":
            return await complete_responses(
                api_key=api_key,
                model=model,
                base_url=base_url,
                messages=messages,
                tools=tools,
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        return await complete_chat(
            api_key=api_key,
            model=model,
            base_url=base_url,
            provider=provider,
            messages=messages,
            tools=tools,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )

    def _credentials(self) -> tuple[str, str, str]:
        api_key = self.config.get("api_key") or self.settings.api_key
        model = self.config.get("model") or self.settings.model
        base_url = (self.config.get("base_url") or self.settings.base_url).rstrip("/")
        return api_key, model, base_url

    @staticmethod
    def _model_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            request_id = exc.response.headers.get("x-request-id") or exc.response.headers.get("request-id")
            detail = exc.response.text[:800]
            suffix = f" request_id={request_id}" if request_id else ""
            return f"Model provider returned HTTP {status}.{suffix} {detail}".strip()
        if isinstance(exc, httpx.TimeoutException):
            return "Model provider request timed out"
        return f"Model provider network error: {exc}"
