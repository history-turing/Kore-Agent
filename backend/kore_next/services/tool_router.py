from __future__ import annotations

from pathlib import Path
from typing import Any

from ..context_offload import ContextOffloadStore
from ..database import Database
from ..evidence import EvidenceStore
from ..memory import MemoryService
from ..rpc import required
from ..tasks import TaskService
from .artifacts import ArtifactService
from .automations import AutomationService
from .conversations import ConversationService
from .runs import RunStore
from .skills import SkillService


class ServiceToolRouter:
    """Routes service-backed tools to their owning application service."""

    def __init__(
        self,
        db: Database,
        conversations: ConversationService,
        runs: RunStore,
        memory: MemoryService,
        tasks: TaskService,
        evidence: EvidenceStore,
        skills: SkillService,
        automations: AutomationService,
        artifacts: ArtifactService,
        offload: ContextOffloadStore,
    ):
        self.db = db
        self.conversations = conversations
        self.runs = runs
        self.memory = memory
        self.tasks = tasks
        self.evidence = evidence
        self.skills = skills
        self.automations = automations
        self.artifacts = artifacts
        self.offload = offload

    async def publish_url_artifact(
        self, *, url: str, title: str, description: str | None, run_id: str
    ) -> dict[str, Any]:
        return await self.artifacts.publish_url(url=url, title=title, description=description, run_id=run_id)

    async def tool_call(self, name: str, arguments: dict[str, Any], *, run_id: str, mode: str) -> Any:
        return await self.call(name, arguments, run_id=run_id, mode=mode)

    async def call(self, name: str, arguments: dict[str, Any], *, run_id: str, mode: str) -> Any:
        run = (await self.runs.get({"run_id": run_id}))["run"]
        conversation_id = run.get("conversation_id")
        if name == "tool_result_inspect":
            return await self.offload.inspect(str(required(arguments, "sha256")))
        if name == "tool_result_read":
            return await self.offload.read(
                str(required(arguments, "sha256")),
                offset=max(0, int(arguments.get("offset", 0))),
                limit=max(1, min(int(arguments.get("limit", 4_000)), 16_000)),
            )
        if name == "history_search":
            query = str(required(arguments, "query"))
            limit = max(1, min(int(arguments.get("limit", 20)), 100))
            rows = await self.db.fetchall(
                "SELECT id,role,content,name,created_at FROM messages "
                "WHERE conversation_id=? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (conversation_id, f"%{query}%", limit),
            )
            return {"matches": [dict(row) for row in rows]}
        if name == "session_search":
            return await self.conversations.search(arguments)
        if name == "history_read":
            offset = max(0, int(arguments.get("offset", 0)))
            limit = max(1, min(int(arguments.get("limit", 50)), 200))
            rows = await self.db.fetchall(
                "SELECT id,role,content,name,tool_call_id,reasoning,created_at FROM messages "
                "WHERE conversation_id=? ORDER BY id LIMIT ? OFFSET ?",
                (conversation_id, limit, offset),
            )
            return {"messages": [dict(row) for row in rows], "offset": offset}
        if name == "memory_list":
            data = await self.memory.list()
            status = arguments.get("status")
            if status in {"active", "archived"}:
                return {"memories": data[status], "core": data["core"]}
            return data
        if name == "memory_read":
            try:
                return {"memory": await self.memory.get(str(required(arguments, "memory_id")))}
            except KeyError as exc:
                raise ValueError("memory not found") from exc
        if name == "memory_create":
            memory = await self.memory.create(
                title=str(required(arguments, "title")),
                summary=str(required(arguments, "summary")),
                content=str(required(arguments, "content")),
            )
            return {"memory": memory}
        if name == "memory_update":
            memory_id = str(required(arguments, "memory_id"))
            values = {key: arguments[key] for key in ("title", "summary", "content") if key in arguments}
            try:
                return {"memory": await self.memory.update(memory_id, **values)}
            except KeyError as exc:
                raise ValueError("memory not found") from exc
        if name == "memory_archive":
            try:
                return {
                    "memory": await self.memory.archive(
                        str(required(arguments, "memory_id")), str(arguments.get("reason") or "agent_tool")
                    )
                }
            except KeyError as exc:
                raise ValueError("memory not found") from exc
        if name == "core_memory_update":
            entries = await self.memory.update_core(str(required(arguments, "key")), str(required(arguments, "value")))
            return {"entries": entries}
        if name == "core_memory_remove":
            return {"entries": await self.memory.remove_core(str(required(arguments, "key")))}
        if name == "task_create":
            task = await self.tasks.create(
                title=str(required(arguments, "title")),
                goal=str(required(arguments, "goal")),
                description=str(arguments.get("description") or ""),
                steps=list(arguments.get("steps") or []),
                conversation_id=conversation_id,
                run_id=run_id,
                status="pending" if mode == "plan" else "active",
            )
            return {"task": task}
        if name == "task_update":
            task_id = str(required(arguments, "task_id"))
            changes = {key: value for key, value in arguments.items() if key != "task_id"}
            try:
                return {"task": await self.tasks.update(task_id, changes, run_id=run_id)}
            except KeyError as exc:
                raise ValueError("task not found") from exc
        if name == "task_get":
            try:
                return {"task": await self.tasks.get(str(required(arguments, "task_id")))}
            except KeyError as exc:
                raise ValueError("task not found") from exc
        if name == "task_list":
            tasks = await self.tasks.list(
                limit=max(1, min(int(arguments.get("limit", 100)), 500)), status=arguments.get("status")
            )
            return {"tasks": tasks, "count": len(tasks)}
        if name in {"skill_read", "skill_resource_read"}:
            skill_name = str(required(arguments, "name"))
            scope = arguments.get("scope")
            skill = next(
                (
                    item
                    for item in sorted(await self.skills.skills(), key=lambda value: value.get("scope") != "project")
                    if item.get("name") == skill_name
                    and item.get("enabled")
                    and (scope is None or item.get("scope") == scope)
                ),
                None,
            )
            if not skill:
                raise ValueError("skill not found or disabled")
            root = Path(skill["location"]).parent.resolve()
            resource = (
                Path(skill["location"]).resolve()
                if name == "skill_read"
                else (root / str(required(arguments, "path"))).resolve()
            )
            try:
                resource.relative_to(root)
            except ValueError as exc:
                raise PermissionError("skill resource path escapes the skill directory") from exc
            if not resource.is_file():
                raise ValueError("skill resource not found")
            content = resource.read_text(encoding="utf-8")
            budget = 12_000 if name == "skill_read" else 20_000
            return {
                "skill": skill_name,
                "path": str(resource.relative_to(root)),
                "content": content[:budget],
                "truncated": len(content) > budget,
            }
        if name == "evidence_search":
            items = await self.evidence.search(
                str(required(arguments, "query")),
                run_id=arguments.get("run_id"),
                kind=arguments.get("kind"),
                limit=int(arguments.get("limit", 20)),
            )
            return {"evidence": items, "count": len(items)}
        if name == "evidence_read":
            try:
                return {"evidence": await self.evidence.get(str(required(arguments, "evidence_id")))}
            except KeyError as exc:
                raise ValueError("evidence not found") from exc
        automation_methods = {
            "automation_create": self.automations.create,
            "automation_list": self.automations.list,
            "automation_get": self.automations.get,
            "automation_pause": self.automations.pause,
            "automation_resume": self.automations.resume,
            "automation_cancel": self.automations.cancel,
        }
        if name in automation_methods:
            return await automation_methods[name](arguments)
        raise ValueError(f"Unsupported service tool: {name}")
