from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Settings
from ..database import Database
from ..rpc import RpcFault, required
from ..skill_learning import SkillLearningService


class SkillService:
    """Skill discovery, installation, enablement, and deletion."""

    def __init__(
        self,
        db: Database,
        skill_learning: SkillLearningService,
        *,
        settings: Callable[[], Settings],
    ):
        self.db = db
        self.skill_learning = skill_learning
        self._settings = settings

    async def skills(self) -> list[dict[str, Any]]:
        settings = self._settings()
        skills = list(await self.db.get_json("skills", []))
        known = {(item.get("name"), item.get("scope")) for item in skills}
        for scope, root in (("project", settings.data_dir / "skills" / "project"), ("user", settings.user_skills_dir)):
            if not root.is_dir():
                continue
            for folder in root.iterdir():
                if not folder.is_dir() or folder.is_symlink() or (folder.name, scope) in known:
                    continue
                skill_file = folder / "SKILL.md"
                if not skill_file.is_file() or skill_file.is_symlink() or skill_file.stat().st_size > 2 * 1024 * 1024:
                    continue
                metadata, _ = self.parse_skill_document(skill_file.read_text(encoding="utf-8"))
                skills.append(
                    {
                        "name": folder.name,
                        "description": metadata.get("description", ""),
                        "scope": scope,
                        "location": str(skill_file),
                        "enabled": True,
                        "discovered": True,
                    }
                )
        for skill in skills:
            root = Path(str(skill.get("location") or "")).parent
            if root.is_dir():
                skill["resources"] = [
                    {"path": str(path.relative_to(root)).replace("\\", "/"), "size_bytes": path.stat().st_size}
                    for path in root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                ][:100]
        return skills

    async def migrate_user_skills(self) -> None:
        settings = self._settings()
        saved = await self.db.get_json("skills", [])
        changed = False
        old_root = (settings.data_dir / "skills" / "user").resolve()
        for item in saved:
            if item.get("scope") != "user":
                continue
            source = Path(str(item.get("location") or "")).resolve()
            try:
                source.relative_to(old_root)
            except ValueError:
                continue
            if not source.is_file():
                continue
            destination = settings.user_skills_dir / str(item["name"])
            if not destination.exists():
                shutil.copytree(source.parent, destination)
            item["location"] = str(destination / "SKILL.md")
            changed = True
        if changed:
            await self.db.set_json("skills", saved)

    @staticmethod
    def parse_skill_document(content: str) -> tuple[dict[str, str], str]:
        if not content.startswith("---\n"):
            return {}, content
        end = content.find("\n---", 4)
        if end < 0:
            return {}, content
        metadata = {}
        for line in content[4:end].splitlines():
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip().strip("\"'")
        return metadata, content[end + 4 :].lstrip()

    @classmethod
    def copy_skill_resource(cls, source: Path, target: Path, *, root: Path) -> None:
        if source.is_symlink():
            raise RpcFault(-32602, f"Skill resources may not contain symlinks: {source.name}")
        resolved = source.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise RpcFault(-32602, "Skill resource escapes its package") from exc
        if source.is_file():
            if source.stat().st_size > 20 * 1024 * 1024:
                raise RpcFault(-32602, f"Skill resource is too large: {source.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            return
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in source.iterdir():
                if child.name in {".git", "node_modules", ".venv", "dist", "build"}:
                    continue
                cls.copy_skill_resource(child, target / child.name, root=root)

    async def skill_install(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        name = str(required(params, "name")).strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
            raise RpcFault(-32602, "Invalid skill name")
        scope = str(params.get("scope", "project"))
        if scope not in {"project", "user"}:
            raise RpcFault(-32602, "Skill scope must be project or user")
        description = str(params.get("description", ""))
        folder = (settings.user_skills_dir if scope == "user" else settings.data_dir / "skills" / "project") / name
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
            f"{required(params, 'instructions')}\n",
            encoding="utf-8",
        )
        skills = [s for s in await self.skills() if not (s["name"] == name and s["scope"] == scope)]
        item = {
            "name": name,
            "description": description,
            "scope": scope,
            "location": str(path),
            "enabled": True,
        }
        skills.append(item)
        await self.db.set_json("skills", skills)
        if params.get("candidate_id"):
            try:
                await self.skill_learning.mark_promoted(str(params["candidate_id"]), name)
            except KeyError as exc:
                raise RpcFault(-32000, "Skill candidate not found") from exc
        return {"skill": item}

    async def skill_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        name, scope = str(required(params, "name")), str(required(params, "scope"))
        skills = await self.skills()
        item = next((s for s in skills if s["name"] == name and s["scope"] == scope), None)
        if not item:
            raise RpcFault(-32000, "Skill not found")
        item["enabled"] = bool(params.get("enabled"))
        await self.db.set_json("skills", skills)
        return {"skill": item}

    async def skill_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        name, scope = str(required(params, "name")), str(required(params, "scope"))
        skills = await self.skills()
        selected = next((s for s in skills if s["name"] == name and s["scope"] == scope), None)
        if not selected:
            raise RpcFault(-32000, "Skill not found")
        path = Path(selected["location"])
        shutil.rmtree(path.parent, ignore_errors=True)
        await self.db.set_json("skills", [s for s in skills if s is not selected])
        return {"deleted": True}
