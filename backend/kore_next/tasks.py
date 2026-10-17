from __future__ import annotations

from typing import Any

from .database import Database
from .util import dumps, loads, now, uid

TASK_STATUSES = {"pending", "active", "blocked", "completed", "cancelled"}
STEP_STATUSES = {"pending", "in_progress", "completed", "blocked", "skipped"}


class TaskService:
    def __init__(self, db: Database):
        self.db = db

    async def list(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT data_json FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))
        tasks = [loads(row["data_json"], {}) for row in rows]
        return [
            task
            for task in tasks
            if (status is None or task.get("status") == status)
            and (conversation_id is None or task.get("owner_conversation_id") == conversation_id)
        ]

    async def get(self, task_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT data_json FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise KeyError("task not found")
        return loads(row["data_json"], {})

    async def create(
        self,
        *,
        title: str,
        goal: str,
        description: str = "",
        steps: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        if status not in TASK_STATUSES:
            raise ValueError("invalid task status")
        stamp = now()
        normalized_steps = []
        for index, step in enumerate(steps or [], 1):
            normalized_steps.append(
                {
                    "id": str(step.get("id") or uid()),
                    "title": str(step.get("title") or f"Step {index}"),
                    "description": str(step.get("description") or ""),
                    "status": str(step.get("status") or "pending"),
                    "result": step.get("result"),
                    "updated_at": stamp,
                }
            )
        task = {
            "id": uid(),
            "title": title.strip()[:160] or "Task",
            "description": description.strip(),
            "goal": goal.strip(),
            "status": status,
            "priority": "normal",
            "constraints": [],
            "state": [],
            "key_facts": [],
            "steps": normalized_steps,
            "owner_conversation_id": conversation_id,
            "run_ids": [run_id] if run_id else [],
            "created_at": stamp,
            "updated_at": stamp,
            "completed_at": stamp if status == "completed" else None,
            "revision": 1,
        }
        await self.db.execute("INSERT INTO tasks(id,data_json,updated_at) VALUES(?,?,?)", (task["id"], dumps(task), stamp))
        return task

    async def update(self, task_id: str, changes: dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
        task = await self.get(task_id)
        for key in ("title", "description", "goal", "priority", "constraints", "state", "key_facts"):
            if key in changes:
                task[key] = changes[key]
        if "status" in changes:
            status = str(changes["status"])
            if status not in TASK_STATUSES:
                raise ValueError("invalid task status")
            task["status"] = status
            task["completed_at"] = now() if status == "completed" else None
        if "steps" in changes:
            task["steps"] = self._merge_steps(task.get("steps", []), changes["steps"])
        if run_id and run_id not in task.get("run_ids", []):
            task.setdefault("run_ids", []).append(run_id)
        task["revision"] = int(task.get("revision", 1)) + 1
        task["updated_at"] = now()
        await self._save(task)
        return task

    async def decide(self, task_id: str, accepted: bool) -> dict[str, Any]:
        task = await self.get(task_id)
        if task.get("status") != "pending":
            raise ValueError("task plan is already resolved")
        task["status"] = "active" if accepted else "cancelled"
        task["updated_at"] = now()
        task["revision"] = int(task.get("revision", 1)) + 1
        await self._save(task)
        return task

    async def active_context(self, conversation_id: str | None) -> str:
        tasks = await self.list(limit=100)
        active = [
            task
            for task in tasks
            if task.get("status") in {"active", "blocked"} and (conversation_id is None or task.get("owner_conversation_id") == conversation_id)
        ]
        if not active:
            return ""
        lines = []
        for task in active[:3]:
            lines.append(f"Task {task['id']}: {task['title']} [{task['status']}]\nGoal: {task.get('goal', '')}")
            for step in task.get("steps", []):
                lines.append(f"  - {step['id']}: {step['title']} [{step['status']}] {step.get('result') or ''}")
        return "\n".join(lines)

    async def _save(self, task: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE tasks SET data_json=?,updated_at=? WHERE id=?",
            (dumps(task), task["updated_at"], task["id"]),
        )

    @staticmethod
    def _merge_steps(existing: list[dict[str, Any]], incoming: Any) -> list[dict[str, Any]]:
        if not isinstance(incoming, list):
            raise ValueError("steps must be a list")
        by_id = {str(step.get("id")): dict(step) for step in existing}
        result = list(existing)
        for index, update in enumerate(incoming, 1):
            if not isinstance(update, dict):
                raise ValueError("each step must be an object")
            step_id = str(update.get("id") or "")
            if step_id and step_id in by_id:
                target = by_id[step_id]
                if "status" in update and update["status"] not in STEP_STATUSES:
                    raise ValueError("invalid step status")
                target.update({key: value for key, value in update.items() if key in {"title", "description", "status", "result"}})
                target["updated_at"] = now()
                for position, item in enumerate(result):
                    if item.get("id") == step_id:
                        result[position] = target
                        break
            else:
                status = str(update.get("status") or "pending")
                if status not in STEP_STATUSES:
                    raise ValueError("invalid step status")
                result.append(
                    {
                        "id": step_id or uid(),
                        "title": str(update.get("title") or f"Step {index}"),
                        "description": str(update.get("description") or ""),
                        "status": status,
                        "result": update.get("result"),
                        "updated_at": now(),
                    }
                )
        return result
