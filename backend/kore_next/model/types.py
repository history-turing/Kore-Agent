from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DeltaSink = Callable[[str], Awaitable[None]]


@dataclass
class ModelReply:
    content: str | None
    reasoning: str | None
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]


def model_usage(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    *,
    cached_input_tokens: int | None = None,
    cache_write_input_tokens: int | None = None,
    uncached_input_tokens: int | None = None,
) -> dict[str, int]:
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if cached_input_tokens is not None:
        usage["cached_input_tokens"] = cached_input_tokens
        usage["cache_read_input_tokens"] = cached_input_tokens
        usage["uncached_input_tokens"] = uncached_input_tokens if uncached_input_tokens is not None else max(0, input_tokens - cached_input_tokens)
    if cache_write_input_tokens is not None:
        usage["cache_write_input_tokens"] = cache_write_input_tokens
    return usage


def openai_cache_usage(usage: dict[str, Any], details_name: str) -> dict[str, int | None]:
    details = usage.get(details_name) or {}
    cached = next(
        (
            int(value)
            for value in (
                usage.get("prompt_cache_hit_tokens"),
                details.get("cached_tokens"),
                usage.get("cached_tokens"),
            )
            if value is not None
        ),
        None,
    )
    cache_write = details.get("cache_creation_input_tokens")
    cache_miss = usage.get("prompt_cache_miss_tokens")
    return {
        "cached_input_tokens": cached if cached is not None else (0 if cache_write is not None else None),
        "cache_write_input_tokens": int(cache_write) if cache_write is not None else None,
        "uncached_input_tokens": int(cache_miss) if cache_miss is not None else None,
    }
