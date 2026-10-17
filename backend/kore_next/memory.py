from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .database import Database
from .util import dumps, loads, now, uid

ReflectionModel = Callable[[str], Awaitable[str]]


class MemoryService:
    def __init__(self, db: Database, *, max_active: int = 50):
        self.db = db
        self.max_active = max_active

    async def list(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT data_json FROM memories ORDER BY updated_at DESC")
        items = [loads(row["data_json"], {}) for row in rows]
        active = [item for item in items if item.get("status") == "active"]
        archived = [item for item in items if item.get("status") == "archived"]
        return {
            "core": await self.render_core(),
            "core_entries": await self.core_entries_detailed(),
            "active": active,
            "archived": archived,
            "active_count": len(active),
            "max_active": self.max_active,
            "maintenance": await self.db.get_json("memory_maintenance_state", {"last_run_at": None, "archived": 0}),
        }

    async def create(self, *, title: str, summary: str, content: str, reason: str = "agent_tool") -> dict[str, Any]:
        stamp = now()
        memory = {
            "id": uid(),
            "title": title.strip()[:120] or "Memory",
            "summary": summary.strip(),
            "content": content.strip(),
            "created_at": stamp,
            "updated_at": stamp,
            "last_accessed_at": stamp,
            "access_count": 0,
            "revision": 1,
            "status": "active",
            "last_update_reason": reason,
            "archive_reason": None,
        }
        await self.db.execute(
            "INSERT INTO memories(id,data_json,status,updated_at) VALUES(?,?,?,?)",
            (memory["id"], dumps(memory), "active", stamp),
        )
        await self.maintain()
        return memory

    async def get(self, memory_id: str, *, touch: bool = True) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT data_json FROM memories WHERE id=?", (memory_id,))
        if not row:
            raise KeyError("memory not found")
        memory = loads(row["data_json"], {})
        if touch:
            memory["access_count"] = int(memory.get("access_count", 0)) + 1
            memory["last_accessed_at"] = now()
            await self._save(memory)
        return memory

    async def update(
        self, memory_id: str, *, title: str | None = None, summary: str | None = None, content: str | None = None, reason: str = "agent_tool"
    ) -> dict[str, Any]:
        memory = await self.get(memory_id, touch=False)
        if title is not None:
            memory["title"] = title.strip()[:120]
        if summary is not None:
            memory["summary"] = summary.strip()
        if content is not None:
            memory["content"] = content.strip()
        memory["revision"] = int(memory.get("revision", 1)) + 1
        memory["updated_at"] = now()
        memory["last_update_reason"] = reason
        await self._save(memory)
        return memory

    async def archive(self, memory_id: str, reason: str = "agent_tool") -> dict[str, Any]:
        memory = await self.get(memory_id, touch=False)
        memory.update(status="archived", archive_reason=reason, updated_at=now())
        memory["revision"] = int(memory.get("revision", 1)) + 1
        await self._save(memory)
        return memory

    async def index(self) -> str:
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='active' ORDER BY updated_at DESC")
        lines = []
        for row in rows:
            memory = loads(row["data_json"], {})
            lines.append(f"- {memory['id']}: {memory.get('title', '')} — {memory.get('summary', '')}")
        return "\n".join(lines)

    async def core_entries(self) -> dict[str, str]:
        data = await self.db.get_json("core_memory_entries", {})
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value.get("value", "")) if isinstance(value, dict) else str(value)
            for key, value in data.items()
        }

    async def core_entries_detailed(self) -> list[dict[str, Any]]:
        data = await self.db.get_json("core_memory_entries", {})
        if not isinstance(data, dict):
            return []
        result = []
        for key, raw in sorted(data.items()):
            if isinstance(raw, dict):
                result.append({"key": str(key), **raw})
            else:
                result.append({"key": str(key), "value": str(raw), "revision": 1, "updated_at": None})
        return result

    async def render_core(self) -> str:
        entries = await self.core_entries()
        return "\n".join(f"- {key}: {value}" for key, value in sorted(entries.items()))

    async def update_core(self, key: str, value: str) -> dict[str, str]:
        normalized = key.strip().lower().replace(" ", "_")[:80]
        if not normalized:
            raise ValueError("core memory key is required")
        raw = await self.db.get_json("core_memory_entries", {})
        entries = raw if isinstance(raw, dict) else {}
        previous = entries.get(normalized)
        revision = int(previous.get("revision", 0)) + 1 if isinstance(previous, dict) else 1
        entries[normalized] = {"value": value.strip(), "revision": revision, "updated_at": now()}
        await self.db.set_json("core_memory_entries", entries)
        return await self.core_entries()

    async def remove_core(self, key: str) -> dict[str, str]:
        raw = await self.db.get_json("core_memory_entries", {})
        entries = raw if isinstance(raw, dict) else {}
        entries.pop(key.strip().lower().replace(" ", "_"), None)
        await self.db.set_json("core_memory_entries", entries)
        return await self.core_entries()

    async def maintain(self, model: ReflectionModel | None = None) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='active'")
        memories = [loads(row["data_json"], {}) for row in rows]
        if len(memories) <= self.max_active:
            return []
        memories.sort(key=self._retention_score)
        candidates = memories[: min(len(memories), max(20, len(memories) - self.max_active + 10))]
        if model is None:
            await self.db.set_json("memory_maintenance_state", {
                "last_run_at": now(), "archived": 0, "deferred": len(memories) - self.max_active,
                "reason": "maintenance_model_unavailable",
            })
            return []
        prompt = (
            "Memory capacity is full. Decide which of the listed candidate memories to archive or defer. "
            "Never invent IDs. Prefer defer when uncertain. Return strict JSON: "
            '{"decisions":[{"id":"candidate-id","action":"archive|defer","reason":"..."}]}. '
            f"Archive at most {len(memories) - self.max_active} memories. Archived memories remain recoverable.\n\n"
            + dumps([{
                "id": item["id"], "title": item.get("title"), "summary": item.get("summary"),
                "access_count": item.get("access_count"), "updated_at": item.get("updated_at"),
            } for item in candidates])
        )
        try:
            decisions = self._json_object(await model(prompt)).get("decisions", [])
        except Exception as exc:
            await self.db.set_json("memory_maintenance_state", {
                "last_run_at": now(), "archived": 0, "deferred": len(memories) - self.max_active,
                "reason": f"model_failed: {type(exc).__name__}",
            })
            return []
        candidate_ids = {item["id"] for item in candidates}
        archived = []
        seen: set[str] = set()
        for decision in decisions if isinstance(decisions, list) else []:
            if not isinstance(decision, dict):
                continue
            memory_id = str(decision.get("id") or "")
            if memory_id in seen or memory_id not in candidate_ids:
                continue
            seen.add(memory_id)
            if decision.get("action") == "archive" and len(archived) < len(memories) - self.max_active:
                archived.append(await self.archive(memory_id, f"capacity_maintenance: {str(decision.get('reason') or '')[:200]}"))
        await self.db.set_json("memory_maintenance_state", {
            "last_run_at": now(), "archived": len(archived),
            "deferred": max(0, len(memories) - len(archived) - self.max_active),
        })
        return archived

    @staticmethod
    def should_reflect(user_text: str, assistant_text: str) -> bool:
        joined = f"{user_text}\n{assistant_text}".strip()
        if len(joined) < 12:
            return False
        durable_markers = (
            "prefer",
            "always",
            "remember",
            "my name",
            "i am",
            "decided",
            "constraint",
            "偏好",
            "以后",
            "记住",
            "我的",
            "决定",
            "约束",
            "长期",
        )
        return len(joined) >= 240 or any(marker in joined.lower() for marker in durable_markers)

    async def reflect(self, *, user_text: str, assistant_text: str, model: ReflectionModel) -> list[dict[str, Any]]:
        prompt = (
            "Extract only stable user preferences, durable facts, important decisions, or reusable knowledge from this run. "
            "Do not store transient task details. Return strict JSON: "
            '{"memories":[{"title":"...","summary":"...","content":"..."}],"core_updates":{"key":"value"}}.\n\n'
            f"USER:\n{user_text[-12000:]}\n\nASSISTANT:\n{assistant_text[-12000:]}"
        )
        raw = await model(prompt)
        payload = self._json_object(raw)
        created: list[dict[str, Any]] = []
        for item in payload.get("memories", [])[:5]:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            if float(item.get("confidence", 1.0)) < 0.6:
                continue
            duplicate = await self._find_duplicate(str(item.get("title", "")), str(item.get("summary", "")))
            if duplicate:
                created.append(
                    await self.update(
                        duplicate["id"],
                        summary=str(item.get("summary", duplicate.get("summary", ""))),
                        content=str(item["content"]),
                        reason="post_run_reflection",
                    )
                )
            else:
                created.append(
                    await self.create(
                        title=str(item.get("title", "Memory")),
                        summary=str(item.get("summary", "")),
                        content=str(item["content"]),
                        reason="post_run_reflection",
                    )
                )
        updates = payload.get("core_updates", {})
        if isinstance(updates, dict):
            for key, value in list(updates.items())[:10]:
                await self.update_core(str(key), str(value))
        return created

    async def _find_duplicate(self, title: str, summary: str) -> dict[str, Any] | None:
        target = self._terms(title + " " + summary)
        if not target:
            return None
        rows = await self.db.fetchall("SELECT data_json FROM memories WHERE status='active'")
        for row in rows:
            item = loads(row["data_json"], {})
            terms = self._terms(str(item.get("title", "")) + " " + str(item.get("summary", "")))
            if terms and len(target & terms) / max(1, len(target | terms)) >= 0.72:
                return item
        return None

    async def _save(self, memory: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE memories SET data_json=?,status=?,updated_at=? WHERE id=?",
            (dumps(memory), memory["status"], memory["updated_at"], memory["id"]),
        )

    @staticmethod
    def _retention_score(memory: dict[str, Any]) -> tuple[float, str]:
        access = int(memory.get("access_count", 0))
        updated = str(memory.get("updated_at", ""))
        try:
            age_days = max(0.0, (datetime.now(UTC) - datetime.fromisoformat(updated.replace("Z", "+00:00"))).total_seconds() / 86400)
        except (TypeError, ValueError):
            age_days = 365.0
        score = math.log1p(access) * 2.0 + max(0.0, 4.0 - age_days / 30.0) + min(2.0, int(memory.get("revision", 1)) / 3)
        return score, updated

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {part.lower() for part in text.replace("，", " ").replace("。", " ").split() if len(part) > 1}

    @staticmethod
    def _json_object(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            return {}
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
