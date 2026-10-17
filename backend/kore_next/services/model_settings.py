from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from ..database import Database
from ..model import ModelClient
from ..model.presets import (
    PROVIDER_PRESETS,
    SUPPORTED_PROVIDERS,
    default_api_style,
    official_hosts,
)
from ..rpc import RpcFault, required
from ..secrets import SecretStore


class ModelSettingsService:
    """Provider configuration, role overrides, and connection testing."""

    def __init__(
        self,
        db: Database,
        secrets: SecretStore,
        *,
        settings: Callable[[], Settings],
        runtime_servers: Callable[[], dict[str, Any]],
        running_tasks: Callable[[], dict[str, asyncio.Task[Any]]],
        model_client_factory: Callable[[Settings, dict[str, Any]], ModelClient] = ModelClient,
    ):
        self.db = db
        self.secrets = secrets
        self._settings = settings
        self._runtime_servers = runtime_servers
        self._running_tasks = running_tasks
        self._model_client_factory = model_client_factory

    async def view(self) -> dict[str, Any]:
        settings = self._settings()
        saved = await self.db.get_json("model_settings", {})
        default_provider = saved.get("default_provider", settings.provider)
        known = {item.get("provider"): item for item in saved.get("providers", [])}
        providers = []
        for name, preset in PROVIDER_PRESETS.items():
            item = known.get(name, {})
            configured = bool(self.secrets.get(name) or (name == settings.provider and settings.api_key))
            providers.append(
                {
                    "provider": name,
                    "label": preset.label,
                    "model": item.get("model") or (settings.model if name == settings.provider else ""),
                    "base_url": item.get("base_url") or (settings.base_url if name == settings.provider else None),
                    "api_style": item.get("api_style", default_api_style(name)),
                    "configured": configured,
                    "key_source": self.secrets.source(name) if self.secrets.get(name) else ("environment" if configured else "none"),
                }
            )
        role_default = {"enabled": True, "inherit_main": True, "provider": None, "model": None}
        roles = {key: saved.get(key, role_default) for key in ("reflection", "maintenance", "summary", "skill_learning")}
        running = self._running_tasks()
        return {
            "default_provider": default_provider,
            "providers": providers,
            **roles,
            "active_provider": default_provider,
            "active_model": next((p["model"] for p in providers if p["provider"] == default_provider), ""),
            "active_roles": {
                "main": {
                    "enabled": True,
                    "provider": default_provider,
                    "model": next((p["model"] for p in providers if p["provider"] == default_provider), ""),
                },
                **{
                    k: {
                        "enabled": v["enabled"],
                        "provider": default_provider if v.get("inherit_main", True) else v.get("provider"),
                        "model": (
                            next((p["model"] for p in providers if p["provider"] == default_provider), "")
                            if v.get("inherit_main", True)
                            else v.get("model")
                        ),
                    }
                    for k, v in roles.items()
                },
            },
            "restart_required": any(
                item.get("enabled", True) and item["name"] not in self._runtime_servers() for item in (await self.mcp_servers())["servers"]
            ),
            "restart_supported": True,
            "restart_blocked_by_run_ids": [rid for rid, task in running.items() if not task.done()],
            "can_restart": not any(not task.done() for task in running.values()),
        }

    async def mcp_servers(self) -> dict[str, Any]:
        return await self.db.get_json("mcp", {"servers": []})

    async def update(self, params: dict[str, Any]) -> dict[str, Any]:
        provider = required(params, "default_provider")
        if provider not in SUPPORTED_PROVIDERS or not isinstance(params.get("providers"), list):
            raise RpcFault(-32602, "Invalid model settings")
        clean = []
        for item in params["providers"]:
            if item.get("provider") not in SUPPORTED_PROVIDERS:
                raise RpcFault(-32602, "Unsupported provider")
            row = dict(item)
            secret = row.pop("api_key", None)
            if secret:
                self.secrets.set(row["provider"], str(secret))
            clean.append(row)
        saved = {"default_provider": provider, "providers": clean}
        for key in ("reflection", "maintenance", "summary", "skill_learning"):
            saved[key] = params.get(key, {"enabled": True, "inherit_main": True, "provider": None, "model": None})
        await self.db.set_json("model_settings", saved)
        return await self.view()

    async def test(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        started = asyncio.get_running_loop().time()
        config = dict(params)
        provider = str(config.get("provider") or settings.provider)
        saved = await self.db.get_json("model_settings", {})
        provider_config = next((p for p in saved.get("providers", []) if p.get("provider") == provider), {})
        base_url = str(config.get("base_url") or provider_config.get("base_url") or settings.base_url)
        endpoint = urlsplit(base_url)
        official = (
            endpoint.scheme == "https"
            and endpoint.hostname in official_hosts(provider)
            and endpoint.username is None
            and endpoint.password is None
            and endpoint.port in {None, 443}
        )
        if not config.get("api_key"):
            if not official:
                return {
                    "success": False,
                    "provider": provider,
                    "model": config.get("model", settings.model),
                    "duration_ms": 0,
                    "error": "已保存的 API Key 仅能发送到提供商官方 HTTPS 端点；自定义地址测试请单独输入 Key。",
                }
            config["api_key"] = self.secrets.get(provider) or (settings.api_key if provider == settings.provider else "")
        config["base_url"] = base_url
        client = self._model_client_factory(settings, config)
        try:
            await client.complete([{"role": "user", "content": "Reply with OK."}])
            success = True
        except (httpx.HTTPError, KeyError, ValueError):
            success = False
        return {
            "success": success,
            "provider": config.get("provider", settings.provider),
            "model": config.get("model", settings.model),
            "duration_ms": round((asyncio.get_running_loop().time() - started) * 1000),
        }

    async def active_config(self) -> dict[str, Any]:
        settings = self._settings()
        saved = await self.db.get_json("model_settings", {})
        provider = saved.get("default_provider", settings.provider)
        item = next((p for p in saved.get("providers", []) if p.get("provider") == provider), {})
        return {"provider": provider, **item, "api_key": self.secrets.get(provider)}

    async def role_config(self, role: str) -> dict[str, Any] | None:
        saved = await self.db.get_json("model_settings", {})
        role_settings = saved.get(role, {"enabled": True, "inherit_main": True})
        if role_settings.get("enabled", True) is False:
            return None
        main = await self.active_config()
        if role_settings.get("inherit_main", True):
            return main
        provider = role_settings.get("provider")
        item = next((entry for entry in saved.get("providers", []) if entry.get("provider") == provider), {})
        return {"provider": provider, **item, "model": role_settings.get("model") or item.get("model"), "api_key": self.secrets.get(provider)}
