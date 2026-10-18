from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..checkpoint import CheckpointStore
from ..config import Settings
from ..database import Database
from ..rpc import RpcFault, required
from ..run_insights import summarize_run_usage
from ..runtime_policy import RunBudget
from ..util import dumps, loads, now, uid
from .runtime_events import RuntimeEventStore
from .runtime_projections import project_model_messages

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


async def insert_run(db: SqlExecutor, run: dict[str, Any], conn: Any | None = None) -> None:
    placeholders = ",".join("?" for _ in RUN_FIELDS)
    sql = f"INSERT INTO runs VALUES({placeholders})"
    args = tuple(run[field] for field in RUN_FIELDS)
    if conn is None:
        await db.execute(sql, args)
    else:
        await db.execute_on(conn, sql, args)


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
        self.runtime_event_store = RuntimeEventStore(db)

    def transaction(self) -> Any:
        return self.db.transaction()

    async def create(
        self, cid: str | None, text: str, mode: str, *, conn: Any | None = None, **extra: Any
    ) -> dict[str, Any]:
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
            "turn_id": extra.get("turn_id") or rid,
            "mode": mode,
        }
        await insert_run(self.db, run, conn)
        return run

    async def save(self, run: dict[str, Any], *, conn: Any | None = None) -> None:
        sql = "UPDATE runs SET status=?,started_at=?,updated_at=?,completed_at=?,error=?,stop_reason=? WHERE id=?"
        args = (
            run["status"],
            run["started_at"],
            run["updated_at"],
            run["completed_at"],
            run["error"],
            run["stop_reason"],
            run["id"],
        )
        if conn is None:
            await self.db.execute(sql, args)
        else:
            await self.db.execute_on(conn, sql, args)
        if run.get("source") == "automation":
            occurrence_sql = "UPDATE automation_occurrences SET state=? WHERE run_id=?"
            if conn is None:
                await self.db.execute(occurrence_sql, (run["status"], run["id"]))
            else:
                await self.db.execute_on(conn, occurrence_sql, (run["status"], run["id"]))
        await self._broadcast()("run.status", {"run_id": run["id"], "status": run["status"]})

    async def event(self, run: dict[str, Any], kind: str, *, conn: Any | None = None, **data: Any) -> dict[str, Any]:
        if conn is not None:
            return await self._event_on(conn, run, kind, **data)
        async with self.db.transaction() as transaction:
            event = await self._event_on(transaction, run, kind, **data)
        await self.broadcast_event(event)
        return event

    async def _event_on(self, conn: Any, run: dict[str, Any], kind: str, **data: Any) -> dict[str, Any]:
        settings = self._settings()
        step = data.pop("step", None)
        invocation_id = data.pop("invocation_id", None)
        row = await self.db.fetchone_on(
            conn,
            "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM events WHERE run_id=?",
            (run["id"],),
        )
        event = {
            "event_id": uid(),
            "run_id": run["id"],
            "conversation_id": run["conversation_id"],
            "sequence": row["n"],
            "type": kind,
            "event_time": now(),
            "step": step,
            "provider": settings.provider,
            "model": settings.model,
            **data,
        }
        if invocation_id or step is not None:
            event["invocation_id"] = invocation_id or f"{run['id']}:{step}"
        runtime_event = await self.runtime_event_store.append(conn, run=run, event=event)
        event["event_seq"] = runtime_event["event_seq"]
        event["runtime_digest"] = runtime_event["digest"]
        await self.db.execute_on(
            conn,
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
        return event

    async def broadcast_event(self, event: dict[str, Any]) -> None:
        # Keep the full model request in the durable log without flooding the UI.
        kind = str(event.get("type") or "")
        broadcast_event = (
            {key: value for key, value in event.items() if key != "request"} if kind == "model_request" else event
        )
        await self._broadcast()("agent.event", broadcast_event)

    async def runtime_events(self, run_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return await self.runtime_event_store.list(run_id, after_seq=after_seq, limit=limit)

    async def verify_events(self, run_id: str) -> dict[str, Any]:
        return await self.runtime_event_store.verify(run_id)

    async def begin_tool(self, run: dict[str, Any], call: dict[str, Any], step: int) -> dict[str, Any]:
        """Persist the dispatch boundary before a tool can cause side effects."""
        arguments_hash = hashlib.sha256(dumps(call.get("arguments") or {}).encode("utf-8")).hexdigest()
        async with self.db.transaction() as conn:
            await self.db.execute_on(
                conn,
                "INSERT OR IGNORE INTO tool_executions("
                "run_id,call_id,step,tool_name,arguments_hash,status,started_at"
                ") VALUES(?,?,?,?,?,'dispatched',?)",
                (
                    run["id"],
                    call["id"],
                    step,
                    call["name"],
                    arguments_hash,
                    now(),
                ),
            )
            row = await self.db.fetchone_on(
                conn,
                "SELECT * FROM tool_executions WHERE run_id=? AND call_id=?",
                (run["id"], call["id"]),
            )
        return dict(row) if row else {}

    async def finish_tool(
        self,
        conn: Any,
        run_id: str,
        call_id: str,
        *,
        success: bool,
        output: str,
        error: str | None,
    ) -> None:
        await self.db.execute_on(
            conn,
            "UPDATE tool_executions SET status=?,output_hash=?,error=?,finished_at=? WHERE run_id=? AND call_id=?",
            (
                "succeeded" if success else "failed",
                hashlib.sha256(output.encode("utf-8")).hexdigest(),
                error,
                now(),
                run_id,
                call_id,
            ),
        )

    async def tool_executions(self, run_id: str, *, step: int | None = None) -> dict[str, dict[str, Any]]:
        sql = "SELECT * FROM tool_executions WHERE run_id=?"
        values: list[Any] = [run_id]
        if step is not None:
            sql += " AND step=?"
            values.append(step)
        rows = await self.db.fetchall(sql, tuple(values))
        return {str(row["call_id"]): dict(row) for row in rows}

    async def recover_stale_runs(self) -> int:
        """Mark runs left behind by a previous host process as interrupted."""
        rows = await self.db.fetchall("SELECT id FROM runs WHERE status='running'")
        if not rows:
            return 0
        stamp = now()
        await self.db.execute(
            "UPDATE runs SET status='interrupted',stop_reason='host_restart',updated_at=? WHERE status='running'",
            (stamp,),
        )
        await self.db.execute(
            "UPDATE automation_occurrences SET state='interrupted' WHERE run_id IN "
            "(SELECT id FROM runs WHERE status='interrupted' AND stop_reason='host_restart')"
        )
        return len(rows)

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
            run = (await self.get({"run_id": result["run"]["id"]}))["run"]
            await self.event(
                run,
                "run_interrupted",
                message=None,
                tool_call=None,
                tool_result=None,
                usage=None,
            )
        return await self.get(params)

    async def recover(self, params: dict[str, Any]) -> dict[str, Any]:
        old = (await self.get(params))["run"]
        if old["status"] not in {"failed", "cancelled", "interrupted"}:
            raise RpcFault(-32001, "Run is not recoverable")
        checkpoint = await self.checkpoints.get(old["id"])
        if checkpoint is None:
            raise RpcFault(-32001, "Run has no recoverable checkpoint")
        active = await self.db.fetchone(
            "SELECT id FROM runs WHERE recovered_from_run_id=? AND status IN ('pending','running') LIMIT 1",
            (old["id"],),
        )
        if active:
            raise RpcFault(-32001, "Run recovery is already in progress", {"run_id": active["id"]})
        state = dict(checkpoint["state"])
        state["recovered_from_run_id"] = old["id"]
        through_event_seq = int(checkpoint.get("through_event_seq") or 0)
        covered_digest = checkpoint.get("digest")
        state["snapshot_source_run_id"] = old["id"]
        state["snapshot_source_event_seq"] = through_event_seq
        state["snapshot_source_digest"] = covered_digest
        if through_event_seq:
            verification = await self.runtime_event_store.verify(old["id"], through_event_seq=through_event_seq)
            if not verification["valid"] or (
                checkpoint.get("digest") and verification.get("digest") != checkpoint["digest"]
            ):
                raise RpcFault(
                    -32003,
                    "Run checkpoint does not match its event log",
                    {"run_id": old["id"], **verification},
                )
            tail = await self.runtime_event_store.list(old["id"], after_seq=through_event_seq)
            if tail:
                through_event_seq = int(tail[-1]["event_seq"])
                covered_digest = tail[-1]["digest"]
                state["model_messages"] = [
                    *state.get("model_messages", []),
                    *project_model_messages(tail),
                ]
                last = tail[-1]
                if last["type"] == "assistant_message":
                    message = last.get("message") or {}
                    calls = message.get("tool_calls") or []
                    state["pending_tool_calls"] = calls
                    state["tool_cursor"] = 0
                    state["phase"] = "assistant_ready" if calls else "final_ready"
                    if not calls:
                        state["final"] = message.get("content") or ""
        phase = str(state.get("phase") or "model_pending")
        if phase == "tool_unknown":
            raise self._unknown_tool_fault(old["id"], state)
        if phase == "blocked":
            raise RpcFault(-32001, "Run stopped by runtime policy and cannot be resumed")
        if phase in {"assistant_ready", "tools_running"}:
            pending = list(state.get("pending_tool_calls") or [])
            step = int(state.get("next_step") or 1)
            executions = await self.tool_executions(old["id"], step=step)
            cursor = int(state.get("tool_cursor") or 0)
            for execution in executions.values():
                if execution["status"] in {"dispatched", "unknown"}:
                    await self._mark_tool_unknown(old, execution["call_id"])
                    raise self._unknown_tool_fault(old["id"], {"tool_call_id": execution["call_id"]})
            for index, call in enumerate(pending):
                execution = executions.get(str(call.get("id")))
                if execution and execution["status"] in {"succeeded", "failed"}:
                    cursor = max(cursor, index + 1)
            state["tool_cursor"] = cursor
        run = await self.create(
            old["conversation_id"],
            old["user_message"],
            old["mode"],
            recovered_from_run_id=old["id"],
            source=old.get("source"),
            source_id=old.get("source_id"),
            scheduled_for=old.get("scheduled_for"),
            triggered_at=old.get("triggered_at"),
            turn_id=old.get("turn_id") or old["id"],
        )
        await self.checkpoints.save(
            run_id=run["id"],
            conversation_id=checkpoint["conversation_id"],
            sequence=checkpoint["sequence"],
            state=state,
            # The new run has its own event sequence; its initial snapshot is
            # inherited materialized state, not a prefix of the new log.
            through_event_seq=0,
            digest=None,
        )
        task = asyncio.create_task(self.execute_run(run, state))
        self.running[run["id"]] = task
        try:
            result = await task
        finally:
            self.running.pop(run["id"], None)
        return {"recovered_from_run_id": old["id"], "run": run, "result": result}

    async def _mark_tool_unknown(self, run: dict[str, Any], call_id: Any) -> None:
        await self.db.execute(
            "UPDATE runs SET status='interrupted',stop_reason='tool_unknown',updated_at=? WHERE id=?",
            (now(), run["id"]),
        )
        if call_id:
            await self.db.execute(
                "UPDATE tool_executions SET status='unknown',finished_at=? WHERE run_id=? AND call_id=?",
                (now(), run["id"], str(call_id)),
            )

    @staticmethod
    def _unknown_tool_fault(run_id: str, details: dict[str, Any]) -> RpcFault:
        return RpcFault(
            -32002,
            "Tool outcome is unknown; manual reconciliation is required",
            {"run_id": run_id, **details},
        )

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
