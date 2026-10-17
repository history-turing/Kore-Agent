from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Settings
from ..database import Database
from ..rpc import RpcFault, required
from ..skill_learning import SkillLearningService
from .mcp_settings import McpSettingsService
from .skills import SkillService


class ExtensionService:
    """Extension import workflow and a facade over skill and MCP sub-services."""

    def __init__(
        self,
        db: Database,
        skill_learning: SkillLearningService,
        *,
        settings: Callable[[], Settings],
        skills: SkillService,
        mcp_settings: McpSettingsService,
    ):
        self.db = db
        self.skill_learning = skill_learning
        self._settings = settings
        self.skill_service = skills
        self.mcp_settings = mcp_settings

    async def skills(self) -> list[dict[str, Any]]:
        return await self.skill_service.skills()

    async def migrate_user_skills(self) -> None:
        await self.skill_service.migrate_user_skills()

    async def mcp(self) -> dict[str, Any]:
        return await self.mcp_settings.mcp()

    async def save_mcp(self, data: dict[str, Any]) -> None:
        await self.mcp_settings.save_mcp(data)

    async def extension_list(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        mcp = await self.mcp()
        skills = await self.skills()
        diagnostics = []
        for skill in skills:
            location = Path(str(skill.get("location") or ""))
            if not location.is_file():
                diagnostics.append({"name": skill.get("name"), "scope": skill.get("scope"), "error": "SKILL.md is missing"})
                continue
            try:
                content = location.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                diagnostics.append({"name": skill.get("name"), "scope": skill.get("scope"), "error": str(exc)})
                continue
            metadata, instructions = self.skill_service.parse_skill_document(content)
            if not instructions.strip() or (metadata.get("name") and metadata["name"] != skill.get("name")):
                diagnostics.append(
                    {
                        "name": skill.get("name"),
                        "scope": skill.get("scope"),
                        "error": "Skill metadata is inconsistent or instructions are empty",
                    }
                )
        return {
            "skills": skills,
            "skill_candidates": await self.skill_learning.list(),
            "skill_diagnostics": diagnostics,
            "mcp": {
                "config_path": str(settings.data_dir / "mcp.json"),
                "error": None,
                "restart_required": any(
                    item.get("enabled", True) and item["name"] not in self.mcp_settings.runtime_servers() for item in mcp["servers"]
                ),
                "servers": [self.mcp_settings.mcp_view(item) for item in mcp["servers"]],
            },
        }

    async def import_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = str(required(params, "input"))
        scope = str(params.get("skill_scope", "project"))
        return {"plan": self.extension_plan(raw, scope)}

    async def import_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("confirmed") is not True:
            raise RpcFault(-32602, "confirmed must be true")
        settings = self._settings()
        raw = str(required(params, "input"))
        scope = str(params.get("skill_scope", "project"))
        plan = self.extension_plan(raw, scope)
        if params.get("fingerprint") != plan["fingerprint"]:
            raise RpcFault(-32001, "Import plan changed")
        if plan.get("mcp_configs"):
            installed = []
            for config in plan["mcp_configs"]:
                result = await self.mcp_add({**config, "permission": params.get("mcp_permission", "human_approval")})
                installed.append(result["server"])
            return {"skills": [], "mcp_servers": installed, "restart_required": bool(installed)}

        checkout: Path | None = None
        temporary_root: Path | None = None
        try:
            source = plan["source_path"]
            if plan["requires_download"]:
                temporary_root = Path(tempfile.mkdtemp(prefix="koreagent-import-", dir=settings.data_dir))
                checkout = temporary_root / "repository"
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    source,
                    str(checkout),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
                if process.returncode != 0:
                    raise RpcFault(-32002, "Extension download failed", {"details": (stdout + stderr).decode(errors="replace")[-2000:]})
            else:
                checkout = Path(source).expanduser().resolve()
            skill_files = self.discover_skill_files(checkout)
            if not skill_files:
                raise RpcFault(-32602, "No SKILL.md was found in the extension source")
            imported = [await self.install_skill_tree(skill_file, scope, raw) for skill_file in skill_files]
            return {
                "skills": [{"name": item["name"], "scope": item["scope"], "source": raw} for item in imported],
                "mcp_servers": [],
                "restart_required": False,
            }
        finally:
            if temporary_root:
                shutil.rmtree(temporary_root, ignore_errors=True)

    def extension_plan(self, raw: str, scope: str) -> dict[str, Any]:
        normalized = raw.strip()
        fingerprint = hashlib.sha256(f"{scope}\n{normalized}".encode()).hexdigest()
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("mcpServers"), dict):
            configs = []
            items = []
            for name, value in payload["mcpServers"].items():
                if not isinstance(value, dict) or not value.get("command"):
                    continue
                config = {
                    "name": str(name),
                    "command": str(value["command"]),
                    "args": list(value.get("args") or []),
                    "cwd": value.get("cwd"),
                    "env": dict(value.get("env") or {}),
                    "enabled": True,
                    "sandbox": value.get("sandbox") or {"filesystem": "workspace", "network": "denied"},
                }
                configs.append(config)
                items.append({"kind": "mcp", "name": str(name), "source": "inline JSON", "summary": config["command"]})
            if not configs:
                raise RpcFault(-32602, "mcpServers does not contain a valid server")
            return {
                "fingerprint": fingerprint,
                "items": items,
                "actions": [f"Add MCP server {item['name']}" for item in items],
                "warnings": ["MCP commands will start only after Host restart."],
                "requires_download": False,
                "requires_restart": True,
                "mcp_configs": configs,
            }

        source = re.sub(r"^npx\s+skills\s+add\s+", "", normalized, flags=re.I).strip().strip("\"'")
        github = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", source)
        requires_download = github is not None
        if github:
            source_path = f"https://github.com/{github.group(1)}/{github.group(2)}.git"
            display_name = github.group(2)
        else:
            local = Path(source).expanduser()
            if not local.exists():
                raise RpcFault(-32602, "Extension input must be a GitHub repository, local path, or mcpServers JSON")
            source_path = str(local.resolve())
            display_name = local.stem or local.name
        return {
            "fingerprint": fingerprint,
            "items": [{"kind": "skill", "name": display_name, "source": source, "summary": "Scan and install SKILL.md files", "scope": scope}],
            "actions": [f"Scan source and install discovered Skills into {scope} scope"],
            "warnings": [],
            "requires_download": requires_download,
            "requires_restart": False,
            "source_path": source_path,
            "mcp_configs": [],
        }

    @staticmethod
    def discover_skill_files(root: Path) -> list[Path]:
        if root.is_file():
            return [root] if root.name.lower() == "skill.md" and not root.is_symlink() and root.stat().st_size <= 2 * 1024 * 1024 else []
        results = []
        for candidate in root.rglob("SKILL.md"):
            relative = candidate.relative_to(root)
            if any(part in {".git", "node_modules", ".venv", "dist", "build"} for part in relative.parts):
                continue
            if candidate.is_symlink() or candidate.stat().st_size > 2 * 1024 * 1024:
                continue
            results.append(candidate)
        return sorted(results)[:100]

    async def install_skill_tree(self, skill_file: Path, scope: str, source: str) -> dict[str, Any]:
        content = skill_file.read_text(encoding="utf-8")
        metadata, instructions = self.skill_service.parse_skill_document(content)
        name = str(metadata.get("name") or skill_file.parent.name).lower().replace("_", "-")
        name = re.sub(r"[^a-z0-9-]", "-", name).strip("-")[:64]
        if not name:
            raise RpcFault(-32602, f"Invalid Skill name in {skill_file}")
        description = str(metadata.get("description") or f"Imported from {source}")
        result = await self.skill_install({"name": name, "description": description, "instructions": instructions, "scope": scope})
        destination = Path(result["skill"]["location"]).parent
        for child in skill_file.parent.iterdir():
            if child.name == "SKILL.md" or child.name in {".git", "node_modules", ".venv"}:
                continue
            self.skill_service.copy_skill_resource(child, destination / child.name, root=skill_file.parent)
        return result["skill"]

    async def skill_install(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.skill_service.skill_install(params)

    async def skill_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.skill_service.skill_set_enabled(params)

    async def skill_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.skill_service.skill_delete(params)

    async def mcp_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.mcp_settings.mcp_add(params)

    async def mcp_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.mcp_settings.mcp_set_enabled(params)

    async def mcp_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.mcp_settings.mcp_delete(params)
