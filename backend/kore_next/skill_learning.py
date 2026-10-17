from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from .database import Database
from .util import dumps, loads, now, uid

DistillationModel = Callable[[str], Awaitable[str]]


class SkillLearningService:
    """Turn repeated successful tool traces into reviewable skill candidates.

    Candidates are deliberately never enabled automatically.  They become installable
    drafts after the same normalized workflow succeeds more than once.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def learn(
        self,
        run: dict[str, Any],
        *,
        model: DistillationModel | None = None,
        existing_skills: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if run.get("status") != "completed" or run.get("mode") == "plan":
            return None
        processed_key = f"skill_learning_processed:{run['id']}"
        if await self.db.get_json(processed_key, False):
            return None
        rows = await self.db.fetchall(
            "SELECT type,data_json FROM events WHERE run_id=? ORDER BY sequence",
            (run["id"],),
        )
        calls: list[dict[str, Any]] = []
        successful: set[str] = set()
        outcomes: dict[str, str] = {}
        for row in rows:
            event = loads(row["data_json"], {})
            if row["type"] == "tool_call" and isinstance(event.get("tool_call"), dict):
                calls.append(event["tool_call"])
            elif row["type"] == "tool_result":
                result = event.get("tool_result") or {}
                if result.get("success") is not False and result.get("tool_call_id"):
                    successful.add(str(result["tool_call_id"]))
                    outcomes[str(result["tool_call_id"])] = str(result.get("output") or "")[-1200:]
        calls = [call for call in calls if str(call.get("id")) in successful]
        # One-off reads are not a reusable workflow.  Require at least two useful steps.
        useful = [call for call in calls if call.get("name") not in {"get_current_time", "memory_list"}]
        if len(useful) < 2:
            await self.db.set_json(processed_key, True)
            return None

        tool_names = [str(call.get("name") or "unknown") for call in useful]
        signature_source = "\n".join(
            f"{call.get('name')}:{','.join(sorted((call.get('arguments') or {}).keys()))}"
            for call in useful
        )
        signature = hashlib.sha256(signature_source.encode()).hexdigest()
        similar = await self._similar_candidate(tool_names)
        if similar is not None:
            signature = similar["id"]
        row = await self.db.fetchone("SELECT * FROM skill_candidates WHERE id=?", (signature,))
        stamp = now()
        if row:
            candidate = loads(row["data_json"], {})
            candidate["success_count"] = int(candidate.get("success_count", 1)) + 1
            candidate["last_run_id"] = run["id"]
            candidate["updated_at"] = stamp
            candidate["example_run_ids"] = list(dict.fromkeys([*candidate.get("example_run_ids", []), run["id"]]))[-8:]
        else:
            title = self._title(run.get("user_message", ""), tool_names)
            candidate = {
                "id": signature,
                "name": self._slug(title),
                "title": title,
                "description": f"Reusable workflow using: {', '.join(tool_names)}",
                "tool_sequence": tool_names,
                "tool_counts": dict(Counter(tool_names)),
                "evidence": [],
                "success_count": 1,
                "first_run_id": run["id"],
                "last_run_id": run["id"],
                "example_run_ids": [run["id"]],
                "created_at": stamp,
                "updated_at": stamp,
            }
        status = "ready" if candidate["success_count"] >= 2 else "observing"
        candidate["status"] = status
        candidate["instructions"] = self._instructions(candidate, useful)
        candidate["evidence"] = self._evidence(useful, outcomes, run["id"])
        candidate["confidence"] = min(0.95, 0.35 + candidate["success_count"] * 0.2 + min(len(useful), 5) * 0.05)
        candidate["action"] = self._overlap_action(candidate, existing_skills or [])
        if status == "ready" and model is not None:
            distilled = await self._distill(candidate, model)
            if distilled:
                candidate.update(distilled)
        await self.db.execute(
            "INSERT INTO skill_candidates(id,status,data_json,created_at,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status,data_json=excluded.data_json,updated_at=excluded.updated_at",
            (signature, status, dumps(candidate), candidate["created_at"], stamp),
        )
        await self.db.set_json(processed_key, True)
        return candidate

    async def scan_completed_runs(
        self, *, existing_skills: list[dict[str, Any]] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Catch up old completed runs in bounded batches; each run contributes once."""
        rows = await self.db.fetchall(
            "SELECT runs.* FROM runs LEFT JOIN kv ON kv.key='skill_learning_processed:' || runs.id "
            "WHERE runs.status='completed' AND runs.mode!='plan' AND kv.key IS NULL "
            "ORDER BY runs.created_at ASC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        candidates = []
        for row in rows:
            candidate = await self.learn(dict(row), existing_skills=existing_skills)
            if candidate:
                candidates.append(candidate)
            await self.db.set_json("skill_learning_watermark", {"run_id": row["id"], "created_at": row["created_at"]})
        return candidates

    async def _similar_candidate(self, tool_names: list[str]) -> dict[str, Any] | None:
        target = set(tool_names)
        if not target:
            return None
        for candidate in await self.list(limit=200):
            existing = set(candidate.get("tool_sequence") or [])
            if existing and len(target & existing) / len(target | existing) >= 0.8:
                return candidate
        return None

    @staticmethod
    def _evidence(calls: list[dict[str, Any]], outcomes: dict[str, str], run_id: str) -> list[dict[str, Any]]:
        result = []
        for call in calls[:12]:
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            result.append(
                {
                    "run_id": run_id,
                    "tool_name": str(call.get("name") or "unknown"),
                    "argument_names": sorted(str(key) for key in arguments)[:12],
                    "outcome_excerpt": outcomes.get(str(call.get("id")), "")[:500],
                }
            )
        return result

    @staticmethod
    def _overlap_action(candidate: dict[str, Any], existing_skills: list[dict[str, Any]]) -> dict[str, Any]:
        candidate_terms = set(re.findall(r"[a-z0-9\u4e00-\u9fff]+", f"{candidate.get('title')} {candidate.get('description')}".lower()))
        best: tuple[float, str] | None = None
        for skill in existing_skills:
            terms = set(re.findall(r"[a-z0-9\u4e00-\u9fff]+", f"{skill.get('name')} {skill.get('description')}".lower()))
            score = len(candidate_terms & terms) / max(1, len(candidate_terms | terms))
            if best is None or score > best[0]:
                best = (score, str(skill.get("name") or ""))
        if best and best[0] >= 0.55:
            return {"kind": "update", "skill_name": best[1], "overlap": round(best[0], 3)}
        return {"kind": "create", "skill_name": None, "overlap": round(best[0], 3) if best else 0.0}

    @staticmethod
    async def _distill(candidate: dict[str, Any], model: DistillationModel) -> dict[str, Any] | None:
        prompt = (
            "Distill this repeated successful trace into a reusable skill. Do not include secrets, absolute paths, "
            "task-specific values, or claims unsupported by evidence. Return strict JSON with title, description, "
            "instructions, and applicability. Instructions must include verification and failure recovery.\n\n"
            + json.dumps(candidate, ensure_ascii=False)[:20_000]
        )
        try:
            raw = (await model(prompt)).strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
            start, end = raw.find("{"), raw.rfind("}")
            payload = json.loads(raw[start : end + 1])
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("instructions"), str):
            return None
        instructions = payload["instructions"].strip()
        if len(instructions) < 80 or len(instructions) > 12_000:
            return None
        forbidden = re.compile(r"(?:api[_ -]?key|bearer\s+[a-z0-9]|[A-Z]:\\|/Users/)", re.I)
        if forbidden.search(instructions):
            return None
        return {
            "title": str(payload.get("title") or candidate["title"])[:120],
            "description": str(payload.get("description") or candidate["description"])[:500],
            "instructions": instructions,
            "applicability": str(payload.get("applicability") or "")[:1000],
            "distilled": True,
        }

    async def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            rows = await self.db.fetchall(
                "SELECT data_json FROM skill_candidates WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, max(1, min(limit, 500))),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT data_json FROM skill_candidates ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )
        return [loads(row["data_json"], {}) for row in rows]

    async def mark_promoted(self, candidate_id: str, skill_name: str) -> None:
        row = await self.db.fetchone("SELECT data_json FROM skill_candidates WHERE id=?", (candidate_id,))
        if not row:
            raise KeyError(candidate_id)
        candidate = loads(row["data_json"], {})
        candidate.update(status="promoted", promoted_skill=skill_name, updated_at=now())
        await self.db.execute(
            "UPDATE skill_candidates SET status='promoted',data_json=?,updated_at=? WHERE id=?",
            (dumps(candidate), candidate["updated_at"], candidate_id),
        )

    @staticmethod
    def _title(user_message: str, tools: list[str]) -> str:
        text = re.sub(r"\s+", " ", user_message).strip()
        return (text[:72] if text else " → ".join(tools[:4])) or "learned workflow"

    @staticmethod
    def _slug(text: str) -> str:
        ascii_words = re.findall(r"[a-z0-9]+", text.lower())
        base = "-".join(ascii_words[:6]) or "learned-workflow"
        return f"{base[:48]}-{uid()[:6]}"

    @staticmethod
    def _instructions(candidate: dict[str, Any], calls: list[dict[str, Any]]) -> str:
        lines = [
            f"Use this workflow when the request matches: {candidate['title']}",
            "",
            "Follow these steps and validate each result before continuing:",
        ]
        for index, call in enumerate(calls, 1):
            args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            safe_keys = ", ".join(sorted(str(key) for key in args)[:8])
            suffix = f" (inputs: {safe_keys})" if safe_keys else ""
            lines.append(f"{index}. Call `{call.get('name', 'unknown')}`{suffix}.")
        lines.extend(["", "Never reuse secrets, absolute user paths, or task-specific values from the examples."])
        return "\n".join(lines)
