from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..database import Database
from ..permissions import PermissionRuleStore
from ..rpc import RpcFault, required
from ..util import dumps, loads, now, uid

GetRun = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
EmitEvent = Callable[..., Awaitable[dict[str, Any]]]
Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]


class ApprovalService:
    """Human approval requests, persistent rules, and waiter coordination."""

    def __init__(
        self,
        db: Database,
        permission_rules: PermissionRuleStore,
        *,
        settings: Callable[[], Settings],
        broadcast: Callable[[], Broadcast],
        get_run: GetRun,
        event: EmitEvent,
        waiters: dict[str, asyncio.Future[bool]],
    ):
        self.db = db
        self.permission_rules = permission_rules
        self._settings = settings
        self._broadcast = broadcast
        self.get_run = get_run
        self.event = event
        self.waiters = waiters

    async def request(self, run_id: str, call_id: str, arguments: dict[str, Any], reason: str) -> bool:
        stamp, aid = now(), uid()
        tool_name = str(arguments.get("tool_name") or ("computer" if reason.startswith("执行电脑操作") else "tool"))
        try:
            conversation_id = (await self.get_run({"run_id": run_id}))["run"].get("conversation_id")
        except RpcFault:
            conversation_id = None
        verdict = await self.permission_rules.evaluate(
            tool_name=tool_name,
            conversation_id=conversation_id,
            run_id=run_id,
            arguments=arguments,
        )
        if verdict.effect == "allow":
            return True
        if verdict.effect == "deny":
            return False
        # Headless benchmark runs have no approval UI. This opt-in keeps an
        # explicit deny rule authoritative while allowing unattended tool use.
        if self._settings().auto_approve:
            return True
        available_matches = ["arguments", "tool"]
        if tool_name == "shell":
            available_matches.insert(1, "command")
        approval = {
            "id": aid,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "arguments": arguments,
            "reason": reason,
            "ui_scope": "desktop" if tool_name.startswith("computer") else "sandbox",
            "status": "pending",
            "created_at": stamp,
            "resolved_at": None,
            "matched_rule_id": None,
            "available_scopes": ["once", "run", "conversation", "global"]
            if self.permission_rules.signing_key
            else ["once"],
            "available_matches": available_matches if self.permission_rules.signing_key else ["arguments"],
        }
        await self.db.execute("INSERT INTO approvals VALUES(?,?,?,?)", (aid, dumps(approval), "pending", stamp))
        future = asyncio.get_running_loop().create_future()
        self.waiters[aid] = future
        await self._broadcast()("approval.required", approval)
        try:
            run_record = (await self.get_run({"run_id": run_id}))["run"]
            await self.event(
                run_record,
                "approval_requested",
                message=None,
                tool_call=None,
                tool_result=None,
                usage=None,
                approval_id=aid,
                tool_name=tool_name,
            )
        except RpcFault:
            pass
        try:
            return await future
        finally:
            self.waiters.pop(aid, None)

    async def cancel(self, approval_id: str) -> None:
        try:
            approval = (await self.get({"approval_id": approval_id}))["approval"]
        except RpcFault:
            return
        if approval.get("status") != "pending":
            return
        approval.update(status="cancelled", resolved_at=now())
        await self.db.execute(
            "UPDATE approvals SET data_json=?,status=? WHERE id=?",
            (dumps(approval), "cancelled", approval_id),
        )
        await self._broadcast()("approval.resolved", approval)

    async def cancel_pending(self) -> int:
        """Cancel approvals whose in-memory waiters did not survive a restart."""
        rows = await self.db.fetchall("SELECT id,data_json FROM approvals WHERE status='pending'")
        for row in rows:
            approval = loads(row["data_json"], {})
            approval.update(status="cancelled", resolved_at=now())
            await self.db.execute(
                "UPDATE approvals SET data_json=?,status=? WHERE id=?",
                (dumps(approval), "cancelled", row["id"]),
            )
        return len(rows)

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        status = params.get("status")
        if status:
            rows = await self.db.fetchall(
                "SELECT data_json FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, int(params.get("limit", 50))),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT data_json FROM approvals ORDER BY created_at DESC LIMIT ?",
                (int(params.get("limit", 50)),),
            )
        return {"approvals": [loads(r["data_json"], {}) for r in rows]}

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT data_json FROM approvals WHERE id=?", (str(required(params, "approval_id")),)
        )
        if not row:
            raise RpcFault(-32000, "Approval not found")
        return {"approval": loads(row["data_json"], {})}

    async def resolve(
        self,
        aid: str,
        decision: bool,
        *,
        scope: str = "once",
        match: str = "arguments",
    ) -> dict[str, Any]:
        approval = (await self.get({"approval_id": aid}))["approval"]
        if approval["status"] != "pending":
            raise RpcFault(-32001, "Approval is already resolved")
        if scope not in {"once", "run", "conversation", "global"}:
            raise RpcFault(-32602, "scope must be once, run, conversation, or global")
        if match not in self.permission_rules.MATCH_MODES:
            raise RpcFault(-32602, "match must be arguments, command, or tool")
        if scope != "once" and self.permission_rules.signing_key is None:
            raise RpcFault(-32001, "Persistent approval rules require an OS-protected signing key")
        if (
            scope != "once"
            and match == "command"
            and not str((approval.get("arguments") or {}).get("command") or "").strip()
        ):
            raise RpcFault(-32602, "Command matching requires a shell command")
        rule = None
        if scope != "once":
            scope_id = {
                "run": str(approval.get("run_id") or ""),
                "conversation": str(approval.get("conversation_id") or ""),
                "global": "*",
            }[scope]
            if not scope_id:
                raise RpcFault(-32602, f"{scope} scope is unavailable for this approval")
            rule = await self.permission_rules.add(
                tool_name=str(approval.get("tool_name") or "tool"),
                effect="allow" if decision else "deny",
                scope=scope,
                scope_id=scope_id,
                arguments=approval.get("arguments") or {},
                description=str(approval.get("reason") or "User decision"),
                match=match,
            )
        approval.update(
            status="approved" if decision else "denied",
            resolved_at=now(),
            resolution_scope=scope,
            resolution_match=match if scope != "once" else "arguments",
            created_rule_id=rule["id"] if rule else None,
        )
        await self.db.execute(
            "UPDATE approvals SET data_json=?,status=? WHERE id=?",
            (dumps(approval), approval["status"], aid),
        )
        waiter = self.waiters.get(aid)
        if waiter and not waiter.done():
            waiter.set_result(decision)
        await self._broadcast()("approval.resolved", approval)
        if approval.get("run_id"):
            try:
                run_record = (await self.get_run({"run_id": approval["run_id"]}))["run"]
                await self.event(
                    run_record,
                    "approval_resolved",
                    message=None,
                    tool_call=None,
                    tool_result=None,
                    usage=None,
                    approval_id=aid,
                    tool_name=approval["tool_name"],
                    approval_status=approval["status"],
                )
            except RpcFault:
                pass
        return {"approval": approval}

    async def approve(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.resolve(
            str(required(params, "approval_id")),
            True,
            scope=str(params.get("scope") or "once"),
            match=str(params.get("match") or "arguments"),
        )

    async def deny(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.resolve(
            str(required(params, "approval_id")),
            False,
            scope=str(params.get("scope") or "once"),
        )
