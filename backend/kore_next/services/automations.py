from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from ..database import Database
from ..rpc import RpcFault, required
from ..util import dumps, loads, now, uid
from .runs import RunStore, insert_run

ExecuteRun = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ScheduleNext = Callable[[dict[str, Any], str], str | None]


class AutomationService:
    """Scheduled automation lifecycle, occurrence claiming, and restart recovery."""

    def __init__(
        self,
        db: Database,
        runs: RunStore,
        *,
        execute_run: ExecuteRun,
        track_background: Callable[[asyncio.Task[Any]], None],
        running: dict[str, asyncio.Task[Any]],
        triggering: set[str],
        schedule_next: ScheduleNext,
    ):
        self.db = db
        self.runs = runs
        self.execute_run = execute_run
        self.track_background = track_background
        self.running = running
        self.triggering = triggering
        self.schedule_next = schedule_next

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT data_json FROM automations ORDER BY updated_at DESC")
        return {"automations": [loads(r["data_json"], {}) for r in rows]}

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT data_json FROM automations WHERE id=?",
            (str(required(params, "automation_id")),),
        )
        if not row:
            raise RpcFault(-32000, "Automation not found")
        return {"automation": loads(row["data_json"], {})}

    async def create(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(required(params, "kind"))
        if kind not in {"once", "interval", "cron"}:
            raise RpcFault(-32602, "Invalid automation kind")
        stamp, aid = now(), uid()
        schedule = {
            "kind": kind,
            "run_at": params.get("run_at"),
            "interval_seconds": params.get("interval_seconds"),
            "cron_expr": params.get("cron_expr"),
            "timezone": params.get("timezone", "UTC"),
        }
        next_at = self.schedule_next(schedule, stamp)
        item = {
            "id": aid,
            "title": str(required(params, "title")),
            "prompt": str(required(params, "prompt")),
            "conversation_id": params.get("conversation_id"),
            "status": "active",
            "schedule": schedule,
            "next_run_at": next_at,
            "last_run_at": None,
            "last_run_id": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        await self.db.execute(
            "INSERT INTO automations VALUES(?,?,?,?,?)",
            (aid, dumps(item), "active", next_at, stamp),
        )
        return {"automation": item}

    @staticmethod
    def compute_next_schedule(schedule: dict[str, Any], base: str) -> str | None:
        def parse(value: str) -> datetime:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

        if schedule["kind"] == "once":
            run_at = schedule.get("run_at")
            if not run_at:
                raise RpcFault(-32602, "run_at is required")
            try:
                return parse(str(run_at)).isoformat().replace("+00:00", "Z")
            except ValueError as exc:
                raise RpcFault(-32602, "run_at must be an ISO datetime") from exc
        if schedule["kind"] == "interval":
            seconds = int(schedule.get("interval_seconds") or 0)
            if seconds < 1:
                raise RpcFault(-32602, "interval_seconds must be positive")
            return (parse(base) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        if not schedule.get("cron_expr"):
            raise RpcFault(-32602, "cron_expr is required")
        try:
            timezone = ZoneInfo(str(schedule.get("timezone") or "UTC"))
        except Exception as exc:
            raise RpcFault(-32602, "timezone must be a valid IANA timezone") from exc
        local_base = parse(base).astimezone(timezone)
        next_local = croniter(schedule["cron_expr"], local_base).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=timezone)
        return next_local.astimezone(UTC).isoformat().replace("+00:00", "Z")

    async def set_state(self, aid: str, status: str) -> dict[str, Any]:
        item = (await self.get({"automation_id": aid}))["automation"]
        allowed = {"active": {"paused", "cancelled"}, "paused": {"active", "cancelled"}}
        if status not in allowed.get(item["status"], set()):
            raise RpcFault(-32001, f"Cannot change automation from {item['status']} to {status}")
        item["status"], item["updated_at"] = status, now()
        if status == "active":
            item["next_run_at"] = self.schedule_next(item["schedule"], item["updated_at"])
        elif status != "completed":
            item["next_run_at"] = None
        await self.db.execute(
            "UPDATE automations SET data_json=?,status=?,next_run_at=?,updated_at=? WHERE id=?",
            (dumps(item), status, item["next_run_at"], item["updated_at"], aid),
        )
        return {"automation": item}

    async def pause(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.set_state(str(required(params, "automation_id")), "paused")

    async def resume(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.set_state(str(required(params, "automation_id")), "active")

    async def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.set_state(str(required(params, "automation_id")), "cancelled")

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1)
                stamp = now()
                rows = await self.db.fetchall(
                    "SELECT data_json FROM automations WHERE status='active' AND next_run_at<=?",
                    (stamp,),
                )
                for row in rows:
                    item = loads(row["data_json"], {})
                    if item["id"] in self.triggering:
                        continue
                    self.triggering.add(item["id"])
                    task = asyncio.create_task(self.trigger(item))
                    self.track_background(task)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(2)

    async def trigger(self, item: dict[str, Any]) -> None:
        try:
            run = await self.claim_occurrence(item["id"])
            if run is None:
                return
            task = asyncio.create_task(self.execute_run(run))
            self.running[run["id"]] = task
            task.add_done_callback(lambda _task, run_id=run["id"]: self.running.pop(run_id, None))
        finally:
            self.triggering.discard(item["id"])

    async def claim_occurrence(self, automation_id: str) -> dict[str, Any] | None:
        """Claim a due occurrence and create its message/run in one SQLite transaction."""
        async with self.db.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT data_json FROM automations WHERE id=?", (automation_id,))).fetchone()
            if not row:
                await db.rollback()
                return None
            item = loads(row["data_json"], {})
            stamp = now()
            scheduled_for = item.get("next_run_at")
            if item.get("status") != "active" or not scheduled_for or scheduled_for > stamp:
                await db.rollback()
                return None
            previous = await (
                await db.execute(
                    "SELECT 1 FROM runs WHERE source='automation' AND source_id=? "
                    "AND status IN ('pending','running') LIMIT 1",
                    (automation_id,),
                )
            ).fetchone()
            if previous:
                if item["schedule"]["kind"] != "once":
                    item["next_run_at"] = self.schedule_next(item["schedule"], stamp)
                    item["updated_at"] = stamp
                    await db.execute(
                        "UPDATE automations SET data_json=?,next_run_at=?,updated_at=? WHERE id=?",
                        (dumps(item), item["next_run_at"], stamp, automation_id),
                    )
                    await db.commit()
                else:
                    await db.rollback()
                return None
            seen = await (
                await db.execute(
                    "SELECT 1 FROM automation_occurrences WHERE automation_id=? AND scheduled_for=?",
                    (automation_id, scheduled_for),
                )
            ).fetchone()
            # The scheduled timestamp identifies an occurrence across scheduler
            # restarts, preventing a due tick from creating a second run.
            if seen:
                await db.rollback()
                return None
            cid = item.get("conversation_id") or uid()
            if not item.get("conversation_id"):
                await db.execute(
                    "INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (cid, item["title"][:120], stamp, stamp),
                )
                item["conversation_id"] = cid
            await db.execute(
                "INSERT INTO messages(conversation_id,role,content,name,tool_call_id,"
                "tool_calls_json,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (cid, "user", item["prompt"], None, None, "[]", None, stamp),
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, cid))
            rid = uid()
            run = {
                "id": rid,
                "conversation_id": cid,
                "status": "pending",
                "user_message": item["prompt"],
                "created_at": stamp,
                "started_at": None,
                "updated_at": stamp,
                "completed_at": None,
                "error": None,
                "stop_reason": None,
                "recovered_from_run_id": None,
                "source": "automation",
                "source_id": automation_id,
                "scheduled_for": scheduled_for,
                "triggered_at": stamp,
                "mode": "normal",
            }
            await insert_run(db, run)
            trigger_id = hashlib.sha256(f"{automation_id}\0{scheduled_for}".encode()).hexdigest()
            await db.execute(
                "INSERT INTO automation_occurrences VALUES(?,?,?,?,?,?)",
                (trigger_id, automation_id, scheduled_for, rid, "pending", stamp),
            )
            item["last_run_at"], item["last_run_id"] = stamp, rid
            if item["schedule"]["kind"] == "once":
                item["status"], item["next_run_at"] = "completed", None
            else:
                item["next_run_at"] = self.schedule_next(item["schedule"], stamp)
            item["updated_at"] = stamp
            await db.execute(
                "UPDATE automations SET data_json=?,status=?,next_run_at=?,updated_at=? WHERE id=?",
                (dumps(item), item["status"], item["next_run_at"], stamp, automation_id),
            )
            await db.commit()
            return run

    async def restore(self) -> None:
        # A claimed-but-not-started Run is resumed once. A Run that was already
        # executing at a crash is interrupted, never silently repeated.
        await self.db.execute(
            "UPDATE runs SET status='interrupted',stop_reason='host_restart',updated_at=? "
            "WHERE source='automation' AND status='running'",
            (now(),),
        )
        await self.db.execute(
            "UPDATE automation_occurrences SET state='interrupted' WHERE run_id IN "
            "(SELECT id FROM runs WHERE source='automation' AND status='interrupted')"
        )
        rows = await self.db.fetchall("SELECT * FROM runs WHERE source='automation' AND status='pending'")
        for row in rows:
            run = dict(row)
            task = asyncio.create_task(self.execute_run(run))
            self.running[run["id"]] = task
            task.add_done_callback(lambda _task, run_id=run["id"]: self.running.pop(run_id, None))
