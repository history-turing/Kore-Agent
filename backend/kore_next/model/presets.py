"""Provider presets used by settings, protocol selection, and key safety checks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    label: str
    default_api_style: str
    official_hosts: frozenset[str]


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        "openai",
        "OpenAI",
        "chat_completions",
        frozenset({"api.openai.com"}),
    ),
    "qwen": ProviderPreset(
        "qwen",
        "Qwen",
        "chat_completions",
        frozenset({"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}),
    ),
    "deepseek": ProviderPreset(
        "deepseek",
        "DeepSeek",
        "chat_completions",
        frozenset({"api.deepseek.com"}),
    ),
    "anthropic": ProviderPreset(
        "anthropic",
        "Anthropic",
        "anthropic_messages",
        frozenset({"api.anthropic.com"}),
    ),
}

SUPPORTED_PROVIDERS = frozenset(PROVIDER_PRESETS)


def default_api_style(provider: str) -> str:
    preset = PROVIDER_PRESETS.get(provider)
    return preset.default_api_style if preset else "chat_completions"


def official_hosts(provider: str) -> frozenset[str]:
    preset = PROVIDER_PRESETS.get(provider)
    return preset.official_hosts if preset else frozenset()


def resolve_api_style(provider: str, configured_style: str | None) -> str:
    """Map configuration to the wire protocol used by ModelClient.

    Anthropic compatibility is selected by provider for backwards compatibility.
    Other providers opt into the Responses API through ``api_style``.
    """
    if provider == "anthropic":
        return "anthropic_messages"
    if configured_style == "responses":
        return "responses"
    return "chat_completions"
