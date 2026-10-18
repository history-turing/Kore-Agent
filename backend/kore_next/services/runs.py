from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..checkpoint import CheckpointStore
from ..config import Settings
from ..database import Database
from ..rpc import RpcFault, required
from ..run_insights import summarize_run_usage
from ..runtime_policy import RunBudget
from ..util import dumps, loads, now, uid

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]
ExecuteRun = Callable[[dict[str, Any], dict[str, Any] | None], Awaitable[dict[str, Any]]]

RUN_FIELDS = (
    "id",
    "conversation_id",
    "status",
    "user_message",
    "created_at",
    "started_at",
    "updated_at",
    "completed_at",
    "error",
    "stop_reason",
    "recovered_from_run_id",
    "source",
    "source_id",
    "scheduled_for",
    "triggered_at",
    "mode",
)


class SqlExecutor(Protocol):
    async def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any: ...


async def insert_run(db: SqlExecutor, run: dict[str, Any]) -> None:
    placeholders = ",".join("?" for _ in RUN_FIELDS)
    await db.execute(f"INSERT INTO runs VALUES({placeholders})", tuple(run[field] for field in RUN_FIELDS))


class RunStore:
    """Run lifecycle persistence, event journal, recovery, and trace queries."""

    def __init__(
        self,
        db: Database,
        checkpoints: CheckpointStore,
        run_budget: RunBudget,
        *,
        settings: Callable[[], Settings],
        broadcast: Callable[[], Broadcast],
        running: dict[str, asyncio.Task[Any]],
        execute_run: ExecuteRun,
    ):
        self.db = db
        self.checkpoints = checkpoints
        self.run_budget = run_budget
        self._settings = settings
        self._broadcast = broadcast
        self.running = running
        self.execute_run = execute_run

    async def create(self, cid: str | None, text: str, mode: str, **extra: Any) -> dict[str, Any]:
        stamp, rid = now(), uid()
        run = {
            "id": rid,
            "conversation_id": cid,
            "status": "pending",
            "user_message": text,
            "created_at": stamp,
            "started_at": None,
            "updated_at": stamp,
            "completed_at": None,
            "error": None,
            "stop_reason": None,
            "recovered_from_run_id": extra.get("recovered_from_run_id"),
            "source": extra.get("source"),
            "source_id": extra.get("source_id"),
            "scheduled_for": extra.get("scheduled_for"),
            "triggered_at": extra.get("triggered_at"),
            "mode": mode,
        }
        await insert_run(self.db, run)
        return run

    async def save(self, run: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE runs SET status=?,started_at=?,updated_at=?,completed_at=?,error=?,stop_reason=? WHERE id=?",
            (
                run["status"],
                run["started_at"],
                run["updated_at"],
                run["completed_at"],
                run["error"],
                run["stop_reason"],
                run["id"],
            ),
        )
        if run.get("source") == "automation":
            await self.db.execute(
                "UPDATE automation_occurrences SET state=? WHERE run_id=?",
                (run["status"], run["id"]),
            )
        await self._broadcast()("run.status", {"run_id": run["id"], "status": run["status"]})

    async def event(self, run: dict[str, Any], kind: str, **data: Any) -> dict[str, Any]:
        settings = self._settings()
        row = await self.db.fetchone("SELECT COALESCE(MAX(sequence),0)+1 AS n FROM events WHERE run_id=?", (run["id"],))
        event = {
            "event_id": uid(),
            "run_id": run["id"],
            "conversation_id": run["conversation_id"],
            "sequence": row["n"],
            "type": kind,
            "event_time": now(),
            "step": data.pop("step", None),
            "provider": settings.provider,
            "model": settings.model,
            **data,
        }
        await self.db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (
                event["event_id"],
                run["id"],
                event["sequence"],
                kind,
                event["event_time"],
                dumps(event),
            ),
        )
        # Keep the full model request in the durable log without flooding the UI.
        broadcast_event = (
            {key: value for key, value in event.items() if key != "request"} if kind == "model_request" else event
        )
        await self._broadcast()("agent.event", broadcast_event)
        return event

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        clauses, values = [], []
        if params.get("conversation_id"):
            clauses.append("conversation_id=?")
            values.append(params["conversation_id"])
        if params.get("status"):
            clauses.append("status=?")
            values.append(params["status"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(params.get("limit", 100)), 500)))
        rows = await self.db.fetchall(f"SELECT * FROM runs{where} ORDER BY created_at DESC LIMIT ?", tuple(values))
        return {"runs": [dict(r) for r in rows]}

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM runs WHERE id=?", (str(required(params, "run_id")),))
        if not row:
            raise RpcFault(-32000, "Run not found")
        return {"run": dict(row)}

    async def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        rid = str(required(params, "run_id"))
        task = self.running.get(rid)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RpcFault):
                pass
        return await self.get({"run_id": rid})

    async def interrupt(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self.cancel(params)
        if result["run"]["status"] == "cancelled":
            await self.db.execute(
                "UPDATE runs SET status='interrupted',stop_reason='interrupted' WHERE id=?",
                (result["run"]["id"],),
            )
        return await self.get(params)

    async def recover(self, params: dict[str, Any]) -> dict[str, Any]:
        old = (await self.get(params))["run"]
        if old["status"] not in {"failed", "cancelled", "interrupted"}:
            raise RpcFault(-32001, "Run is not recoverable")
        checkpoint = await self.checkpoints.get(old["id"])
        if checkpoint is None:
            raise RpcFault(-32001, "Run has no recoverable checkpoint")
        run = await self.create(
            old["conversation_id"],
            old["user_message"],
            old["mode"],
            recovered_from_run_id=old["id"],
        )
        await self.checkpoints.clone(old["id"], run["id"])
        task = asyncio.create_task(self.execute_run(run, checkpoint["state"]))
        self.running[run["id"]] = task
        try:
            result = await task
        finally:
            self.running.pop(run["id"], None)
        return {"recovered_from_run_id": old["id"], "run": run, "result": result}

    async def trace(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        run = (await self.get(params))["run"]
        rows = await self.db.fetchall("SELECT data_json FROM events WHERE run_id=? ORDER BY sequence", (run["id"],))
        events = [loads(r["data_json"], {}) for r in rows]
        usage_summary = summarize_run_usage(events, self.run_budget)
        usage = usage_summary["main_agent"]
        model_event = next(
            (e for e in events if e.get("type") in {"assistant_message", "context_prepared"}),
            None,
        )
        trace = {
            "run_id": run["id"],
            "conversation_id": run["conversation_id"],
            "status": run["status"],
            "started_at": run["started_at"] or run["created_at"],
            "completed_at": run["completed_at"],
            "provider": model_event.get("provider") if model_event else settings.provider,
            "model": model_event.get("model") if model_event else settings.model,
            "steps": max((e.get("step") or 0 for e in events), default=0),
            "stop_reason": run["stop_reason"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "event_count": len(events),
        }
        visible_events = (
            events
            if params.get("include_model_requests") is True
            else [
                {key: value for key, value in event.items() if key != "request"}
                if event.get("type") == "model_request"
                else event
                for event in events
            ]
        )
        return {"trace": trace, "events": visible_events, "usage_summary": usage_summary}
