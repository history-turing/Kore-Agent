from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .agent import ModelClient, ToolExecutor
from .checkpoint import CheckpointStore
from .computer import ComputerClient
from .computer_guard import ComputerStagnationGuard
from .config import Settings
from .context import ContextDecision, ContextManager
from .database import Database
from .evidence import EvidenceStore
from .mcp import McpManager, McpToolError
from .memory import MemoryService
from .permissions import PermissionRuleStore
from .run_insights import summarize_run_usage
from .runtime_policy import RepeatedToolCallGuard, RunBudget, RunBudgetConfig, RuntimePolicyError
from .secrets import SecretStore
from .skill_learning import SkillLearningService
from .tasks import TaskService
from .util import dumps, loads, now, uid

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]


class RpcFault(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def required(params: dict[str, Any], key: str) -> Any:
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RpcFault(-32602, f"Missing or invalid parameter: {key}")
    return value


class Application:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        self.db = Database(self.settings.database)
        self.secrets = SecretStore(self.settings.data_dir / "settings")
        self.mcp_manager = McpManager(self.settings.workspace_dir)
        self.computer = ComputerClient(self.settings, self._store_observation)
        self.context_manager = ContextManager(self.db)
        self.checkpoints = CheckpointStore(self.db)
        self.memory_service = MemoryService(self.db)
        self.task_service = TaskService(self.db)
        self.evidence = EvidenceStore(self.db)
        self.skill_learning = SkillLearningService(self.db)
        self.permission_rules = PermissionRuleStore(self.db, signing_key=self.secrets.permission_signing_key())
        self.computer_guard = ComputerStagnationGuard()
        self.run_budget = RunBudget(
            RunBudgetConfig(
                warning_tokens=self.settings.run_warning_tokens,
                finalization_tokens=self.settings.run_finalization_tokens,
                hard_tokens=self.settings.run_hard_tokens,
                warning_model_calls=self.settings.run_warning_model_calls,
                finalization_model_calls=self.settings.run_finalization_model_calls,
                hard_model_calls=self.settings.run_hard_model_calls,
            )
        )
        self.broadcast: Broadcaster = self._noop
        self.approval_waiters: dict[str, asyncio.Future[bool]] = {}
        self.running: dict[str, asyncio.Task[Any]] = {}
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self.triggering_automations: set[str] = set()
        self.automation_task: asyncio.Task[Any] | None = None

    async def _noop(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def start(self) -> None:
        await self.db.initialize()
        await self._migrate_user_skills()
        await self.skill_learning.scan_completed_runs(existing_skills=await self._skills(), limit=100)
        mcp = await self._mcp()
        await self.mcp_manager.start(mcp["servers"])
        await self.computer.start()
        await self._restore_automation_runs()
        self.automation_task = asyncio.create_task(self._automation_loop())

    async def close(self) -> None:
        if self.automation_task:
            self.automation_task.cancel()
        for task in self.running.values():
            task.cancel()
        for task in self.background_tasks:
            task.cancel()
        for approval_id, waiter in list(self.approval_waiters.items()):
            if not waiter.done():
                waiter.set_result(False)
            try:
                await self._cancel_approval(approval_id)
            except Exception:
                pass
        await asyncio.gather(*self.running.values(), *self.background_tasks, return_exceptions=True)
        await self.mcp_manager.close()
        await self.computer.close()

    def methods(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
        return {
            "system.info": self.system_info,
            "system.restart": self.system_restart,
            "model_settings.get": self.model_settings_get,
            "model_settings.update": self.model_settings_update,
            "model_settings.test": self.model_settings_test,
            "conversation.list": self.conversation_list,
            "conversation.get": self.conversation_get,
            "conversation.search": self.conversation_search,
            "conversation.create": self.conversation_create,
            "conversation.send": self.conversation_send,
            "conversation.rename": self.conversation_rename,
            "conversation.delete": self.conversation_delete,
            "run.list": self.run_list,
            "run.get": self.run_get,
            "run.cancel": self.run_cancel,
            "run.interrupt": self.run_interrupt,
            "run.recover": self.run_recover,
            "trace.get": self.trace_get,
            "automation.list": self.automation_list,
            "automation.get": self.automation_get,
            "automation.create": self.automation_create,
            "automation.pause": self.automation_pause,
            "automation.resume": self.automation_resume,
            "automation.cancel": self.automation_cancel,
            "approval.list": self.approval_list,
            "approval.get": self.approval_get,
            "approval.approve": self.approval_approve,
            "approval.deny": self.approval_deny,
            "artifact.list": self.artifact_list,
            "artifact.get": self.artifact_get,
            "memory.list": self.memory_list,
            "task.list": self.task_list,
            "task.get": self.task_get,
            "task.plan_accept": self.task_plan_accept,
            "task.plan_reject": self.task_plan_reject,
            "computer.status": self.computer_status,
            "computer.request_permission": self.computer_request_permission,
            "computer.latest_observation": self.computer_latest_observation,
            "extension.list": self.extension_list,
            "extension.import.preview": self.extension_import_preview,
            "extension.import.apply": self.extension_import_apply,
            "skill.install": self.skill_install,
            "skill.set_enabled": self.skill_set_enabled,
            "skill.delete": self.skill_delete,
            "mcp.add": self.mcp_add,
            "mcp.set_enabled": self.mcp_set_enabled,
            "mcp.delete": self.mcp_delete,
        }

    async def system_info(self, params: dict[str, Any]) -> dict[str, Any]:
        legacy_rules = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM permission_rules WHERE "
            "json_extract(matcher_json, '$.version') IS NULL OR json_extract(matcher_json, '$.version')!=2"
        )
        return {
            "name": "KoreAgent Next",
            "version": "0.1.0",
            "provider": self.settings.provider,
            "model": self.settings.model,
            "database": str(self.settings.database),
            "workspace": str(self.settings.workspace_dir),
            "platform": __import__("sys").platform,
            "rpc_methods": sorted(self.methods()),
            "legacy_permission_rules_needing_review": legacy_rules["n"] if legacy_rules else 0,
            "execution_capabilities": {
                "workspace_sandbox": "macos-seatbelt" if sys.platform == "darwin" else "unavailable",
                "shell_workspace": sys.platform == "darwin",
                "mcp_workspace": sys.platform == "darwin",
                "host_process": True,
            },
        }

    async def system_restart(self, params: dict[str, Any]) -> dict[str, bool]:
        active = [rid for rid, task in self.running.items() if not task.done()]
        if active:
            raise RpcFault(-32001, "Host restart is blocked by active runs", {"run_ids": active})
        await self.mcp_manager.close()
        await self.computer.close()
        self.mcp_manager = McpManager(self.settings.workspace_dir)
        mcp = await self._mcp()
        await self.mcp_manager.start(mcp["servers"])
        self.computer = ComputerClient(self.settings, self._store_observation)
        await self.computer.start()
        return {"accepted": True}

    async def _model_view(self) -> dict[str, Any]:
        saved = await self.db.get_json("model_settings", {})
        default_provider = saved.get("default_provider", self.settings.provider)
        known = {item.get("provider"): item for item in saved.get("providers", [])}
        providers = []
        labels = {
            "openai": "OpenAI",
            "qwen": "Qwen",
            "deepseek": "DeepSeek",
            "anthropic": "Anthropic",
        }
        for name in labels:
            item = known.get(name, {})
            configured = bool(self.secrets.get(name) or (name == self.settings.provider and self.settings.api_key))
            providers.append(
                {
                    "provider": name,
                    "label": labels[name],
                    "model": item.get("model") or (self.settings.model if name == self.settings.provider else ""),
                    "base_url": item.get("base_url") or (self.settings.base_url if name == self.settings.provider else None),
                    "api_style": item.get(
                        "api_style",
                        "anthropic_messages" if name == "anthropic" else "chat_completions",
                    ),
                    "configured": configured,
                    "key_source": self.secrets.source(name) if self.secrets.get(name) else ("environment" if configured else "none"),
                }
            )
        role_default = {"enabled": True, "inherit_main": True, "provider": None, "model": None}
        roles = {key: saved.get(key, role_default) for key in ("reflection", "maintenance", "summary", "skill_learning")}
        return {
            "default_provider": default_provider,
            "providers": providers,
            **roles,
            "active_provider": default_provider,
            "active_model": next((p["model"] for p in providers if p["provider"] == default_provider), ""),
            "active_roles": {
                "main": {
                    "enabled": True,
                    "provider": default_provider,
                    "model": next((p["model"] for p in providers if p["provider"] == default_provider), ""),
                },
                **{
                    k: {
                        "enabled": v["enabled"],
                        "provider": default_provider if v.get("inherit_main", True) else v.get("provider"),
                        "model": (
                            next((p["model"] for p in providers if p["provider"] == default_provider), "")
                            if v.get("inherit_main", True)
                            else v.get("model")
                        ),
                    }
                    for k, v in roles.items()
                },
            },
            "restart_required": any(
                item.get("enabled", True) and item["name"] not in self.mcp_manager.servers for item in (await self._mcp())["servers"]
            ),
            "restart_supported": True,
            "restart_blocked_by_run_ids": [rid for rid, task in self.running.items() if not task.done()],
            "can_restart": not any(not task.done() for task in self.running.values()),
        }

    async def model_settings_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._model_view()

    async def model_settings_update(self, params: dict[str, Any]) -> dict[str, Any]:
        allowed = {"openai", "qwen", "deepseek", "anthropic"}
        provider = required(params, "default_provider")
        if provider not in allowed or not isinstance(params.get("providers"), list):
            raise RpcFault(-32602, "Invalid model settings")
        clean = []
        for item in params["providers"]:
            if item.get("provider") not in allowed:
                raise RpcFault(-32602, "Unsupported provider")
            row = dict(item)
            secret = row.pop("api_key", None)
            if secret:
                self.secrets.set(row["provider"], str(secret))
            clean.append(row)
        saved = {"default_provider": provider, "providers": clean}
        for key in ("reflection", "maintenance", "summary", "skill_learning"):
            saved[key] = params.get(key, {"enabled": True, "inherit_main": True, "provider": None, "model": None})
        await self.db.set_json("model_settings", saved)
        return await self._model_view()

    async def model_settings_test(self, params: dict[str, Any]) -> dict[str, Any]:
        started = asyncio.get_running_loop().time()
        config = dict(params)
        provider = str(config.get("provider") or self.settings.provider)
        saved = await self.db.get_json("model_settings", {})
        provider_config = next((p for p in saved.get("providers", []) if p.get("provider") == provider), {})
        base_url = str(config.get("base_url") or provider_config.get("base_url") or self.settings.base_url)
        official_hosts = {
            "openai": {"api.openai.com"},
            "qwen": {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"},
            "deepseek": {"api.deepseek.com"},
            "anthropic": {"api.anthropic.com"},
        }
        endpoint = urlsplit(base_url)
        official = (
            endpoint.scheme == "https"
            and endpoint.hostname in official_hosts.get(provider, set())
            and endpoint.username is None and endpoint.password is None
            and endpoint.port in {None, 443}
        )
        if not config.get("api_key"):
            if not official:
                return {
                    "success": False, "provider": provider,
                    "model": config.get("model", self.settings.model), "duration_ms": 0,
                    "error": "已保存的 API Key 仅能发送到提供商官方 HTTPS 端点；自定义地址测试请单独输入 Key。",
                }
            config["api_key"] = self.secrets.get(provider) or (self.settings.api_key if provider == self.settings.provider else "")
        config["base_url"] = base_url
        client = ModelClient(self.settings, config)
        try:
            await client.complete([{"role": "user", "content": "Reply with OK."}])
            success = True
        except (httpx.HTTPError, KeyError, ValueError):
            success = False
        return {
            "success": success,
            "provider": config.get("provider", self.settings.provider),
            "model": config.get("model", self.settings.model),
            "duration_ms": round((asyncio.get_running_loop().time() - started) * 1000),
        }

    def _conversation(self, row: Any, count: int = 0) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": count,
        }

    async def conversation_list(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(params.get("limit", 50)), 200))
        rows = await self.db.fetchall(
            "SELECT c.*,COUNT(m.id) AS n FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id "
            "GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
        return {"conversations": [self._conversation(r, r["n"]) for r in rows]}

    async def conversation_create(self, params: dict[str, Any]) -> dict[str, Any]:
        stamp, cid = now(), uid()
        title = str(params.get("title") or "新对话").strip()[:120]
        await self.db.execute(
            "INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",
            (cid, title, stamp, stamp),
        )
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id=?", (cid,))
        return {"conversation": self._conversation(row)}

    async def conversation_get(self, params: dict[str, Any]) -> dict[str, Any]:
        cid = str(required(params, "conversation_id"))
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id=?", (cid,))
        if not row:
            raise RpcFault(-32000, "Conversation not found")
        msgs = await self.db.fetchall("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,))
        messages = [
            {
                "role": m["role"],
                "content": m["content"],
                "name": m["name"],
                "tool_call_id": m["tool_call_id"],
                "tool_calls": loads(m["tool_calls_json"], []),
                "reasoning": m["reasoning"],
            }
            for m in msgs
        ]
        return {"conversation": self._conversation(row, len(messages)), "messages": messages}

    async def conversation_search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(required(params, "query")).strip()
        if not query:
            raise RpcFault(-32602, "Search query is empty")
        limit = max(1, min(int(params.get("limit", 20)), 100))
        if len(query) >= 3:
            rows = await self.db.fetchall(
                "SELECT m.id AS message_id,m.conversation_id,c.title,m.role,m.content,m.created_at "
                "FROM messages_fts JOIN messages AS m ON m.id=messages_fts.rowid "
                "JOIN conversations AS c ON c.id=m.conversation_id "
                "WHERE messages_fts MATCH ? AND m.role IN ('user','assistant') "
                "ORDER BY m.id DESC LIMIT ?",
                ('"' + query.replace('"', '""') + '"', limit),
            )
        else:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = await self.db.fetchall(
                "SELECT m.id AS message_id,m.conversation_id,c.title,m.role,m.content,m.created_at "
                "FROM messages AS m JOIN conversations AS c ON c.id=m.conversation_id "
                "WHERE m.content LIKE ? ESCAPE '\\' AND m.role IN ('user','assistant') "
                "ORDER BY m.id DESC LIMIT ?",
                (f"%{escaped}%", limit),
            )
        return {"matches": [{**dict(row), "content": str(row["content"] or "")[:1_500]} for row in rows]}

    async def conversation_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        cid, title = (
            str(required(params, "conversation_id")),
            str(required(params, "title")).strip()[:120],
        )
        await self._require_conversation(cid)
        await self.db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (title, now(), cid))
        return (await self.conversation_get({"conversation_id": cid})) | {"messages": []}

    async def conversation_delete(self, params: dict[str, Any]) -> dict[str, bool]:
        cid = str(required(params, "conversation_id"))
        await self._require_conversation(cid)
        await self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        return {"deleted": True}

    async def _require_conversation(self, cid: str) -> Any:
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id=?", (cid,))
        if not row:
            raise RpcFault(-32000, "Conversation not found")
        return row

    async def _add_message(self, cid: str, message: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO messages(conversation_id,role,content,name,tool_call_id,tool_calls_json,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                cid,
                message["role"],
                message.get("content"),
                message.get("name"),
                message.get("tool_call_id"),
                dumps(message.get("tool_calls", [])),
                message.get("reasoning"),
                now(),
            ),
        )
        await self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))

    async def _new_run(self, cid: str | None, text: str, mode: str, **extra: Any) -> dict[str, Any]:
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
        await self.db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                run[k]
                for k in (
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
            ),
        )
        return run

    async def _save_run(self, run: dict[str, Any]) -> None:
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
        await self.broadcast("run.status", {"run_id": run["id"], "status": run["status"]})

    async def _event(self, run: dict[str, Any], kind: str, **data: Any) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT COALESCE(MAX(sequence),0)+1 AS n FROM events WHERE run_id=?", (run["id"],))
        event = {
            "event_id": uid(),
            "run_id": run["id"],
            "conversation_id": run["conversation_id"],
            "sequence": row["n"],
            "type": kind,
            "event_time": now(),
            "step": data.pop("step", None),
            "provider": self.settings.provider,
            "model": self.settings.model,
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
        broadcast_event = {key: value for key, value in event.items() if key != "request"} if kind == "model_request" else event
        await self.broadcast("agent.event", broadcast_event)
        return event

    async def conversation_send(self, params: dict[str, Any]) -> dict[str, Any]:
        cid, content = str(required(params, "conversation_id")), str(required(params, "content"))
        mode = str(params.get("mode", "normal"))
        if mode not in {"normal", "plan"}:
            raise RpcFault(-32602, "mode must be normal or plan")
        await self._require_conversation(cid)
        await self._add_message(cid, {"role": "user", "content": content})
        run = await self._new_run(cid, content, mode)
        task = asyncio.create_task(self._execute_run(run))
        self.running[run["id"]] = task
        try:
            result = await task
        finally:
            self.running.pop(run["id"], None)
        return {
            "conversation_id": cid,
            "content": result["final_message"].get("content"),
            "run": run,
            "result": result,
            "plan_task_id": result.get("plan_task_id"),
        }

    async def _active_model_config(self) -> dict[str, Any]:
        saved = await self.db.get_json("model_settings", {})
        provider = saved.get("default_provider", self.settings.provider)
        item = next((p for p in saved.get("providers", []) if p.get("provider") == provider), {})
        return {"provider": provider, **item, "api_key": self.secrets.get(provider)}

    async def _execute_run(self, run: dict[str, Any], recovery_state: dict[str, Any] | None = None) -> dict[str, Any]:
        run.update(status="running", started_at=now(), updated_at=now())
        await self._save_run(run)
        await self._event(run, "agent_started", message=None, tool_call=None, tool_result=None, usage=None)
        try:
            model_config = await self._active_model_config()
            model = ModelClient(self.settings, model_config)
            if recovery_state:
                # Recovery resumes from model-facing messages and cumulative usage,
                # so the new run inherits its predecessor's budget and tool state.
                context_decision: ContextDecision | None = None
                model_messages = list(recovery_state.get("model_messages", []))
                total = dict(recovery_state.get("usage", {}))
                start_step = int(recovery_state.get("next_step", 1))
                plan_task_id = recovery_state.get("plan_task_id")
                model_calls = int(recovery_state.get("model_calls", max(0, start_step - 1)))
                await self._event(
                    run,
                    "run_recovered",
                    step=start_step,
                    message=None,
                    tool_call=None,
                    tool_result=None,
                    usage=None,
                    inherited_usage=total,
                    inherited_model_calls=model_calls,
                    recovered_from_run_id=run.get("recovered_from_run_id"),
                )
            else:
                history = (await self.conversation_get({"conversation_id": run["conversation_id"]}))["messages"]
                prepared_history = self._history_for_model(history)
                base_system = self._system_prompt(run["mode"])
                decision = await self.context_manager.prepare(
                    conversation_id=run["conversation_id"],
                    history=prepared_history,
                    provider=str(model_config.get("provider") or self.settings.provider),
                    model=str(model_config.get("model") or self.settings.model),
                    base_system=base_system,
                    core_memory=await self.memory_service.render_core(),
                    memory_index=await self.memory_service.index(),
                    task_context=await self.task_service.active_context(run["conversation_id"]),
                    skill_catalog=self._skill_catalog(await self._skills()),
                    summarizer=lambda source, previous: self._summarize_context(source, previous, run=run),
                )
                context_decision = decision
                model_messages = decision.messages
                total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                start_step = 1
                plan_task_id = None
                model_calls = 0
            tools = ToolExecutor(
                self.settings,
                self._request_approval,
                self.publish_artifact,
                self.remember,
                self.mcp_manager,
                self.computer,
                self,
            )
            if recovery_state:
                tools.activated_tools.update(recovery_state.get("activated_tools", []))
            final = ""
            steps = start_step - 1
            budget_warning_emitted = False
            repeated_calls = RepeatedToolCallGuard()
            for steps in range(start_step, self.settings.max_steps + 1):
                # Check before each request and again after usage arrives; a model
                # response can cross the limit even when the request was allowed.
                budget_decision = self.run_budget.evaluate(total, model_calls)
                if budget_decision.exceeded:
                    await self._event(
                        run,
                        "run_budget_exceeded",
                        step=steps,
                        message=None,
                        tool_call=None,
                        tool_result=None,
                        usage=None,
                        **self.run_budget.event_fields(budget_decision),
                    )
                    raise RuntimePolicyError(
                        f"Run budget exceeded by {budget_decision.reason}",
                        stop_reason="run_budget",
                    )
                if budget_decision.should_warn and not budget_warning_emitted:
                    budget_warning_emitted = True
                    await self._event(
                        run,
                        "run_budget_warning",
                        step=steps,
                        message=None,
                        tool_call=None,
                        tool_result=None,
                        usage=None,
                        **self.run_budget.event_fields(budget_decision),
                    )
                force_final = budget_decision.should_finalize
                if force_final:
                    await self._event(
                        run,
                        "run_budget_finalizing",
                        step=steps,
                        message=None,
                        tool_call=None,
                        tool_result=None,
                        usage=None,
                        **self.run_budget.event_fields(budget_decision),
                    )
                base_tools, extra_tools = tools.tool_definitions(run["mode"])
                if force_final:
                    base_tools = []
                    extra_tools = []
                estimator = self.context_manager.estimator
                tool_schema_tokens = estimator.count_text(dumps([*base_tools, *extra_tools])) if base_tools or extra_tools else 0
                final_instruction = {
                    "role": "system",
                    "content": (
                        "The run budget is closing. Return the best truthful final answer now. "
                        "Do not call tools. State incomplete work explicitly."
                    ),
                }
                step_context = await self.context_manager.prepare_step(
                    messages=model_messages,
                    provider=str(model_config.get("provider") or self.settings.provider),
                    model=str(model_config.get("model") or self.settings.model),
                    tool_schema_tokens=tool_schema_tokens + (estimator.count_message(final_instruction) if force_final else 0),
                    summarizer=lambda source, previous: self._summarize_context(source, previous, run=run),
                )
                model_messages = step_context.messages
                request_messages = [*model_messages, final_instruction] if force_final else model_messages
                system_tokens = sum(estimator.count_message(item) for item in request_messages if item.get("role") == "system")
                tool_tokens = sum(estimator.count_message(item) for item in request_messages if item.get("role") == "tool")
                history_tokens = sum(
                    estimator.count_message(item)
                    for item in request_messages
                    if item.get("role") not in {"system", "tool"}
                ) + 8
                prepared_tokens = system_tokens + tool_tokens + history_tokens + tool_schema_tokens
                initial_context = step_context
                model_window = step_context.context_window
                max_input_tokens = step_context.max_input_tokens
                working_input_budget = step_context.working_input_budget
                await self._event(
                    run,
                    "context_prepared",
                    step=steps,
                    message=None,
                    tool_call=None,
                    tool_result=None,
                    usage=None,
                    provider=model_config.get("provider") or self.settings.provider,
                    model=model_config.get("model") or self.settings.model,
                    context={
                        "estimated_tokens": prepared_tokens,
                        "original_estimated_tokens": (
                            initial_context.original_estimated_tokens + tool_schema_tokens
                            if initial_context and initial_context.original_estimated_tokens is not None
                            else prepared_tokens
                        ),
                        "prepared_input_tokens": prepared_tokens,
                        "context_window": model_window,
                        "max_input_tokens": max_input_tokens,
                        "working_input_budget": working_input_budget,
                        "tool_result_budget_tokens": (
                            step_context.tool_result_budget_tokens
                        ),
                        "tool_schema_tokens_estimated": tool_schema_tokens,
                        "breakdown": {
                            "system": system_tokens,
                            "messages": history_tokens,
                            "tool_results": tool_tokens,
                            "tool_schemas": tool_schema_tokens,
                        },
                        "tool_result_tokens_before": (
                            initial_context.original_tool_result_tokens if initial_context else tool_tokens
                        ),
                        "tool_result_tokens_after": (
                            initial_context.prepared_tool_result_tokens if initial_context else tool_tokens
                        ),
                        "compacted": bool(initial_context.compacted) or bool(
                            steps == start_step and context_decision and context_decision.compacted
                        ),
                        "compaction_stage": initial_context.compaction_stage if initial_context.compacted else (
                            context_decision.compaction_stage if steps == start_step and context_decision else "none"
                        ),
                        "summary_validated": bool(initial_context and initial_context.summary_validated),
                        "summary_updated": bool(initial_context and initial_context.summary_updated),
                        "dropped_messages": initial_context.dropped_messages if initial_context else 0,
                    },
                )
                # Save the input boundary before calling the model. An interrupted
                # run can then replay from this step without losing its context.
                await self.checkpoints.save(
                    run_id=run["id"],
                    conversation_id=run["conversation_id"],
                    sequence=steps,
                    state={
                        "model_messages": model_messages,
                        "usage": total,
                        "next_step": steps,
                        "plan_task_id": plan_task_id,
                        "activated_tools": sorted(tools.activated_tools),
                        "model_calls": model_calls,
                    },
                )
                request = {
                    "messages": request_messages,
                    "tools": [*base_tools, *extra_tools],
                    "provider": model_config.get("provider") or self.settings.provider,
                    "model": model_config.get("model") or self.settings.model,
                }
                await self._event(
                    run,
                    "model_request",
                    step=steps,
                    request=request,
                    request_sha256=hashlib.sha256(dumps(request).encode("utf-8")).hexdigest(),
                )

                # Deltas update the live UI; the assembled assistant message below
                # is the durable conversation and trace record.
                async def on_text_delta(delta: str, step: int = steps) -> None:
                    await self.broadcast(
                        "agent.event",
                        {
                            "event_id": uid(),
                            "run_id": run["id"],
                            "conversation_id": run["conversation_id"],
                            "type": "assistant_delta",
                            "step": step,
                            "delta": delta,
                            "event_time": now(),
                        },
                    )

                async def on_reasoning_delta(delta: str, step: int = steps) -> None:
                    await self.broadcast(
                        "agent.event",
                        {
                            "event_id": uid(),
                            "run_id": run["id"],
                            "conversation_id": run["conversation_id"],
                            "type": "reasoning_delta",
                            "step": step,
                            "delta": delta,
                            "event_time": now(),
                        },
                    )

                reply = await model.complete(
                    request_messages,
                    extra_tools,
                    base_tools,
                    on_text_delta=on_text_delta,
                    on_reasoning_delta=on_reasoning_delta,
                )
                model_calls += 1
                for key in total:
                    total[key] += reply.usage.get(key, 0)
                after_call_budget = self.run_budget.evaluate(total, model_calls)
                if after_call_budget.exceeded:
                    await self._event(
                        run,
                        "run_budget_exceeded",
                        step=steps,
                        message=None,
                        tool_call=None,
                        tool_result=None,
                        usage=reply.usage,
                        **self.run_budget.event_fields(after_call_budget),
                    )
                    raise RuntimePolicyError(
                        f"Run budget exceeded by {after_call_budget.reason}",
                        stop_reason="run_budget",
                    )
                assistant = {
                    "role": "assistant",
                    "content": reply.content,
                    "reasoning": reply.reasoning,
                    "tool_calls": reply.tool_calls,
                }
                await self._add_message(run["conversation_id"], assistant)
                await self._event(
                    run,
                    "assistant_message",
                    step=steps,
                    message=assistant,
                    tool_call=None,
                    tool_result=None,
                    usage=reply.usage,
                    provider=model_config.get("provider") or self.settings.provider,
                    model=model_config.get("model") or self.settings.model,
                )
                model_messages.append(
                    {
                        "role": "assistant",
                        "content": reply.content or "",
                        **(
                            {
                                "tool_calls": [
                                    {
                                        "id": c["id"],
                                        "type": "function",
                                        "function": {
                                            "name": c["name"],
                                            "arguments": dumps(c["arguments"]),
                                        },
                                    }
                                    for c in reply.tool_calls
                                ]
                            }
                            if reply.tool_calls
                            else {}
                        ),
                    }
                )
                if not reply.tool_calls:
                    final = reply.content or ""
                    break
                if force_final:
                    raise RuntimePolicyError(
                        "Model attempted a tool call while the run budget was finalizing",
                        stop_reason="run_budget",
                    )
                repeated_calls.observe(reply.tool_calls)
                for call in reply.tool_calls:
                    await self._event(
                        run,
                        "tool_call",
                        step=steps,
                        message=None,
                        tool_call=call,
                        tool_result=None,
                        usage=None,
                    )
                    started = asyncio.get_running_loop().time()
                    try:
                        output = await tools.execute(run["id"], call["name"], call["arguments"], call["id"], mode=run["mode"])
                        success, error = True, None
                    except McpToolError as exc:
                        output, success, error = exc.payload, False, str(exc)
                    except Exception as exc:
                        output, success, error = "", False, str(exc)
                    # Repeated computer failures and unchanged observations stop
                    # the run before another ineffective action is attempted.
                    self.computer_guard.record(
                        run_id=run["id"],
                        tool_name=call["name"],
                        success=success,
                        output=output,
                        error=error,
                    )
                    duration_ms = round((asyncio.get_running_loop().time() - started) * 1000)
                    if success and call["name"] == "task_create":
                        try:
                            task_payload = json.loads(output)
                            plan_task_id = task_payload.get("task", {}).get("id") or plan_task_id
                        except json.JSONDecodeError:
                            pass
                    tool_message = {
                        "role": "tool",
                        "content": output or error,
                        "name": call["name"],
                        "tool_call_id": call["id"],
                    }
                    await self._add_message(run["conversation_id"], tool_message)
                    model_messages.append(tool_message)
                    await self._event(
                        run,
                        "tool_result",
                        step=steps,
                        message=None,
                        tool_call=None,
                        tool_result={
                            "tool_call_id": call["id"],
                            "tool_name": call["name"],
                            "success": success,
                            "output": output,
                            "error": error,
                            "duration_ms": duration_ms,
                        },
                        usage=None,
                    )
                    await self.evidence.record(
                        run["id"],
                        "tool_result",
                        {
                            "tool_call_id": call["id"],
                            "tool_name": call["name"],
                            "arguments": call["arguments"],
                            "success": success,
                            "output": output,
                            "error": error,
                            "duration_ms": duration_ms,
                            "task_id": plan_task_id,
                            "task_step": steps,
                        },
                    )
                await self.checkpoints.save(
                    run_id=run["id"],
                    conversation_id=run["conversation_id"],
                    sequence=steps,
                    state={
                        "model_messages": model_messages,
                        "usage": total,
                        "next_step": steps + 1,
                        "plan_task_id": plan_task_id,
                        "activated_tools": sorted(tools.activated_tools),
                        "model_calls": model_calls,
                    },
                )
            else:
                raise RuntimePolicyError(
                    f"Maximum step limit ({self.settings.max_steps}) reached",
                    stop_reason="max_steps",
                )
            run.update(status="completed", updated_at=now(), completed_at=now(), stop_reason="completed")
            await self._save_run(run)
            final_message = {
                "role": "assistant",
                "content": final,
                "reasoning": None,
                "tool_calls": [],
            }
            result = {
                "run_id": run["id"],
                "final_message": final_message,
                "messages": [],
                "steps": steps,
                "stop_reason": "completed",
                "usage": total,
                "error": None,
                "plan_task_id": plan_task_id,
            }
            if run["mode"] == "plan" and not plan_task_id:
                task = await self._create_plan_task(run, final)
                plan_task_id = task["id"]
                result["plan_task_id"] = plan_task_id
                final_message["content"] = final + "\n\n计划已保存，等待你确认后执行。"
            await self._event(
                run,
                "agent_completed",
                message=final_message,
                tool_call=None,
                tool_result=None,
                usage=total,
                result=result,
                stop_reason="completed",
            )
            post_run = asyncio.create_task(self._post_run(run, result))
            self._track_background(post_run)
            return result
        except asyncio.CancelledError:
            run.update(status="cancelled", updated_at=now(), completed_at=now(), stop_reason="cancelled")
            await self._save_run(run)
            raise
        except Exception as exc:
            stop_reason = exc.stop_reason if isinstance(exc, RuntimePolicyError) else "error"
            run.update(
                status="failed",
                updated_at=now(),
                completed_at=now(),
                error=str(exc),
                stop_reason=stop_reason,
            )
            await self._save_run(run)
            await self._event(
                run,
                "agent_failed",
                message=None,
                tool_call=None,
                tool_result=None,
                usage=None,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise RpcFault(-32603, "Agent run failed", {"run_id": run["id"], "message": str(exc)}) from exc
        finally:
            self.computer_guard.clear(run["id"])
            await self.computer.end_run(run["id"])

    def _history_for_model(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for message in history:
            item = {"role": message["role"], "content": message.get("content") or ""}
            if message.get("tool_calls"):
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": dumps(call["arguments"])},
                    }
                    for call in message["tool_calls"]
                ]
            if message.get("tool_call_id"):
                item["tool_call_id"] = message["tool_call_id"]
            if message.get("name"):
                item["name"] = message["name"]
            result.append(item)
        return result

    @staticmethod
    def _system_prompt(mode: str) -> str:
        common = (
            "You are KoreAgent Next, a reliable local AI agent. Work toward the user's actual result, use tools when useful, "
            "keep durable task state current, publish deliverables, and never claim an action succeeded without evidence. "
            "Use tool_catalog to discover and activate optional capabilities before calling them."
        )
        if mode == "plan":
            return common + (
                " You are in PLAN MODE. Inspect and research only. Do not perform side effects. "
                "Create exactly one pending Task with task_create, including concrete ordered steps, constraints, and success criteria."
            )
        return common

    @staticmethod
    def _skill_catalog(skills: list[dict[str, Any]]) -> str:
        selected: dict[str, dict[str, Any]] = {}
        for skill in sorted(skills, key=lambda item: item.get("scope") != "project"):
            if skill.get("enabled"):
                selected.setdefault(skill["name"], skill)
        lines = [
            f"- {skill['name']} ({skill.get('scope', 'project')}): {skill.get('description', '')}"
            for skill in selected.values()
        ]
        return "\n".join(lines)[:6_000]

    async def _summarize_context(
        self, source: str, previous: str | None, *, run: dict[str, Any] | None = None
    ) -> str:
        config = await self._role_model_config("summary")
        if config is None:
            return (previous or "") + "\n" + source[-12_000:]
        prompt = (
            "Create a compact factual rolling summary for continuing this conversation. Preserve user goals, constraints, "
            "decisions, unresolved issues, file paths, tool outcomes and next steps. Do not add facts.\n\n"
            f"PREVIOUS SUMMARY:\n{previous or '(none)'}\n\nNEW HISTORY:\n{source}"
        )
        reply = await ModelClient(self.settings, config).complete([{"role": "user", "content": prompt}], [], [])
        if run is not None:
            await self._record_auxiliary_usage(run, "context_summary", reply.usage, config)
        return reply.content or source[-12_000:]

    async def _record_auxiliary_usage(
        self, run: dict[str, Any], role: str, usage: dict[str, int], config: dict[str, Any]
    ) -> None:
        await self._event(
            run,
            "auxiliary_model_completed",
            role=role,
            message=None,
            tool_call=None,
            tool_result=None,
            usage=usage,
            provider=config.get("provider") or self.settings.provider,
            model=config.get("model") or self.settings.model,
        )

    async def _role_model_config(self, role: str) -> dict[str, Any] | None:
        saved = await self.db.get_json("model_settings", {})
        role_settings = saved.get(role, {"enabled": True, "inherit_main": True})
        if role_settings.get("enabled", True) is False:
            return None
        main = await self._active_model_config()
        if role_settings.get("inherit_main", True):
            return main
        provider = role_settings.get("provider")
        item = next((entry for entry in saved.get("providers", []) if entry.get("provider") == provider), {})
        return {"provider": provider, **item, "model": role_settings.get("model") or item.get("model"), "api_key": self.secrets.get(provider)}

    def _track_background(self, task: asyncio.Task[Any]) -> None:
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _post_run(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        try:
            await self._event(run, "post_run_started", message=None, tool_call=None, tool_result=None, usage=None)
            config = await self._role_model_config("reflection")
            memories: list[dict[str, Any]] = []
            if (
                config is not None
                and result.get("final_message", {}).get("content")
                and self.memory_service.should_reflect(run["user_message"], result["final_message"]["content"])
            ):

                async def reflect_model(prompt: str) -> str:
                    reply = await ModelClient(self.settings, config).complete([{"role": "user", "content": prompt}], [], [])
                    await self._record_auxiliary_usage(run, "memory_reflection", reply.usage, config)
                    return reply.content or "{}"

                memories = await self.memory_service.reflect(
                    user_text=run["user_message"],
                    assistant_text=result["final_message"]["content"],
                    model=reflect_model,
                )
            maintenance_config = await self._role_model_config("maintenance")

            async def maintenance_model(prompt: str) -> str:
                assert maintenance_config is not None
                reply = await ModelClient(self.settings, maintenance_config).complete([{"role": "user", "content": prompt}], [], [])
                await self._record_auxiliary_usage(run, "memory_maintenance", reply.usage, maintenance_config)
                return reply.content or "{}"

            await self.memory_service.maintain(maintenance_model if maintenance_config is not None else None)
            distill_config = await self._role_model_config("skill_learning")

            async def distill_model(prompt: str) -> str:
                if distill_config is None:
                    return "{}"
                reply = await ModelClient(self.settings, distill_config).complete([{"role": "user", "content": prompt}], [], [])
                await self._record_auxiliary_usage(run, "skill_learning", reply.usage, distill_config)
                return reply.content or "{}"

            candidate = await self.skill_learning.learn(
                run,
                model=distill_model if distill_config is not None else None,
                existing_skills=await self._skills(),
            )
            await self._event(
                run,
                "post_run_completed",
                message=None,
                tool_call=None,
                tool_result=None,
                usage=None,
                post_run={
                    "memories_created_or_updated": [item["id"] for item in memories],
                    "skill_candidate": candidate,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._event(
                run,
                "post_run_failed",
                message=None,
                tool_call=None,
                tool_result=None,
                usage=None,
                error={"type": type(exc).__name__, "message": str(exc)},
            )

    async def _create_plan_task(self, run: dict[str, Any], content: str) -> dict[str, Any]:
        steps = []
        for line in content.splitlines():
            clean = line.strip()
            if clean.startswith(("- ", "* ")):
                steps.append({"title": clean[2:].strip()})
            elif len(clean) > 3 and clean[0].isdigit() and clean[1:3] in {". ", "、"}:
                steps.append({"title": clean[3:].strip()})
        if not steps:
            steps = [{"title": "按已确认的计划完成目标", "description": content[:4_000]}]
        return await self.task_service.create(
            title=run["user_message"][:80],
            description=content,
            goal=run["user_message"],
            steps=steps[:30],
            conversation_id=run["conversation_id"],
            run_id=run["id"],
            status="pending",
        )

    def _run(self, row: Any) -> dict[str, Any]:
        return dict(row)

    async def run_list(self, params: dict[str, Any]) -> dict[str, Any]:
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
        return {"runs": [self._run(r) for r in rows]}

    async def run_get(self, params: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM runs WHERE id=?", (str(required(params, "run_id")),))
        if not row:
            raise RpcFault(-32000, "Run not found")
        return {"run": self._run(row)}

    async def run_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        rid = str(required(params, "run_id"))
        task = self.running.get(rid)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RpcFault):
                pass
        return await self.run_get({"run_id": rid})

    async def run_interrupt(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self.run_cancel(params)
        if result["run"]["status"] == "cancelled":
            await self.db.execute(
                "UPDATE runs SET status='interrupted',stop_reason='interrupted' WHERE id=?",
                (result["run"]["id"],),
            )
        return await self.run_get(params)

    async def run_recover(self, params: dict[str, Any]) -> dict[str, Any]:
        old = (await self.run_get(params))["run"]
        if old["status"] not in {"failed", "cancelled", "interrupted"}:
            raise RpcFault(-32001, "Run is not recoverable")
        checkpoint = await self.checkpoints.get(old["id"])
        if checkpoint is None:
            raise RpcFault(-32001, "Run has no recoverable checkpoint")
        run = await self._new_run(
            old["conversation_id"],
            old["user_message"],
            old["mode"],
            recovered_from_run_id=old["id"],
        )
        await self.checkpoints.clone(old["id"], run["id"])
        task = asyncio.create_task(self._execute_run(run, checkpoint["state"]))
        self.running[run["id"]] = task
        try:
            result = await task
        finally:
            self.running.pop(run["id"], None)
        return {"recovered_from_run_id": old["id"], "run": run, "result": result}

    async def trace_get(self, params: dict[str, Any]) -> dict[str, Any]:
        run = (await self.run_get(params))["run"]
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
            "provider": model_event.get("provider") if model_event else self.settings.provider,
            "model": model_event.get("model") if model_event else self.settings.model,
            "steps": max((e.get("step") or 0 for e in events), default=0),
            "stop_reason": run["stop_reason"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "event_count": len(events),
        }
        visible_events = events if params.get("include_model_requests") is True else [
            {key: value for key, value in event.items() if key != "request"} if event.get("type") == "model_request" else event
            for event in events
        ]
        return {"trace": trace, "events": visible_events, "usage_summary": usage_summary}

    async def _request_approval(self, run_id: str, call_id: str, arguments: dict[str, Any], reason: str) -> bool:
        stamp, aid = now(), uid()
        tool_name = str(arguments.get("tool_name") or ("computer" if reason.startswith("执行电脑操作") else "tool"))
        try:
            conversation_id = (await self.run_get({"run_id": run_id}))["run"].get("conversation_id")
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
        if self.settings.auto_approve:
            return True
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
            "available_scopes": ["once", "run", "conversation", "global"] if self.permission_rules.signing_key else ["once"],
        }
        await self.db.execute("INSERT INTO approvals VALUES(?,?,?,?)", (aid, dumps(approval), "pending", stamp))
        future = asyncio.get_running_loop().create_future()
        self.approval_waiters[aid] = future
        await self.broadcast("approval.required", approval)
        try:
            run_record = (await self.run_get({"run_id": run_id}))["run"]
            await self._event(
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
            self.approval_waiters.pop(aid, None)

    async def _cancel_approval(self, approval_id: str) -> None:
        try:
            approval = (await self.approval_get({"approval_id": approval_id}))["approval"]
        except RpcFault:
            return
        if approval.get("status") != "pending":
            return
        approval.update(status="cancelled", resolved_at=now())
        await self.db.execute(
            "UPDATE approvals SET data_json=?,status=? WHERE id=?",
            (dumps(approval), "cancelled", approval_id),
        )
        await self.broadcast("approval.resolved", approval)

    async def approval_list(self, params: dict[str, Any]) -> dict[str, Any]:
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

    async def approval_get(self, params: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT data_json FROM approvals WHERE id=?", (str(required(params, "approval_id")),))
        if not row:
            raise RpcFault(-32000, "Approval not found")
        return {"approval": loads(row["data_json"], {})}

    async def _resolve_approval(self, aid: str, decision: bool, *, scope: str = "once") -> dict[str, Any]:
        approval = (await self.approval_get({"approval_id": aid}))["approval"]
        if approval["status"] != "pending":
            raise RpcFault(-32001, "Approval is already resolved")
        if scope not in {"once", "run", "conversation", "global"}:
            raise RpcFault(-32602, "scope must be once, run, conversation, or global")
        if scope != "once" and self.permission_rules.signing_key is None:
            raise RpcFault(-32001, "Persistent approval rules require an OS-protected signing key")
        rule = None
        if scope != "once":
            # Persistent decisions are bound to the exact tool arguments and the
            # chosen scope; an edited command must be approved separately.
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
            )
        approval.update(
            status="approved" if decision else "denied",
            resolved_at=now(),
            resolution_scope=scope,
            created_rule_id=rule["id"] if rule else None,
        )
        await self.db.execute(
            "UPDATE approvals SET data_json=?,status=? WHERE id=?",
            (dumps(approval), approval["status"], aid),
        )
        waiter = self.approval_waiters.get(aid)
        if waiter and not waiter.done():
            waiter.set_result(decision)
        await self.broadcast("approval.resolved", approval)
        if approval.get("run_id"):
            try:
                run_record = (await self.run_get({"run_id": approval["run_id"]}))["run"]
                await self._event(
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

    async def approval_approve(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._resolve_approval(
            str(required(params, "approval_id")),
            True,
            scope=str(params.get("scope") or "once"),
        )

    async def approval_deny(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._resolve_approval(
            str(required(params, "approval_id")),
            False,
            scope=str(params.get("scope") or "once"),
        )

    async def publish_artifact(self, *, path: Path, title: str, description: str | None, run_id: str) -> dict[str, Any]:
        path = path.resolve()
        path.relative_to(self.settings.workspace_dir.resolve())
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("Artifact exceeds the 100 MiB limit")
        aid, stamp = uid(), now()
        managed = self.settings.data_dir / "artifacts" / aid / path.name
        managed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, managed)
        data = managed.read_bytes()
        run = (await self.run_get({"run_id": run_id}))["run"]
        artifact = {
            "id": aid,
            "kind": "file",
            "title": title,
            "description": description,
            "filename": path.name,
            "mime_type": mimetypes.guess_type(path.name)[0],
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "run_id": run_id,
            "conversation_id": run["conversation_id"],
            "task_id": None,
            "source_url": None,
            "created_at": stamp,
            "storage_path": str(managed),
        }
        await self.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (aid, dumps(artifact), stamp))
        public = {k: v for k, v in artifact.items() if k != "storage_path"}
        await self.broadcast("artifact.created", public)
        return public

    async def publish_url_artifact(self, *, url: str, title: str, description: str | None, run_id: str) -> dict[str, Any]:
        from urllib.parse import urlsplit

        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Artifact URL must use http or https")
        aid, stamp = uid(), now()
        run = (await self.run_get({"run_id": run_id}))["run"]
        artifact = {
            "id": aid,
            "kind": "url",
            "title": title.strip() or url,
            "description": description,
            "filename": None,
            "mime_type": None,
            "size_bytes": 0,
            "sha256": None,
            "run_id": run_id,
            "conversation_id": run["conversation_id"],
            "task_id": None,
            "source_url": url.strip(),
            "created_at": stamp,
            "storage_path": None,
        }
        await self.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (aid, dumps(artifact), stamp))
        public = {key: value for key, value in artifact.items() if key != "storage_path"}
        await self.broadcast("artifact.created", public)
        return public

    async def artifact_list(self, params: dict[str, Any]) -> dict[str, Any]:
        clauses: list[str] = []
        values: list[Any] = []
        for key in ("run_id", "conversation_id"):
            if params.get(key):
                clauses.append(f"json_extract(data_json, '$.{key}')=?")
                values.append(str(params[key]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(params.get("limit", 50)), 500)))
        rows = await self.db.fetchall(
            f"SELECT data_json FROM artifacts{where} ORDER BY created_at DESC LIMIT ?",
            tuple(values),
        )
        items = [loads(r["data_json"], {}) for r in rows]
        return {"artifacts": [{k: v for k, v in a.items() if k != "storage_path"} for a in items]}

    async def artifact_get(self, params: dict[str, Any]) -> dict[str, Any]:
        aid = str(params.get("id") or params.get("artifact_id") or "")
        row = await self.db.fetchone("SELECT data_json FROM artifacts WHERE id=?", (aid,))
        if not row:
            raise RpcFault(-32000, "Artifact not found")
        return {"artifact": {k: v for k, v in loads(row["data_json"], {}).items() if k != "storage_path"}}

    async def artifact_path(self, aid: str) -> Path | None:
        row = await self.db.fetchone("SELECT data_json FROM artifacts WHERE id=?", (aid,))
        if not row:
            return None
        storage_path = loads(row["data_json"], {}).get("storage_path")
        if not storage_path:
            return None
        path = Path(storage_path).resolve()
        try:
            path.relative_to((self.settings.data_dir / "artifacts").resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    async def task_list(self, params: dict[str, Any]) -> dict[str, Any]:
        tasks = await self.task_service.list(
            limit=max(1, min(int(params.get("limit", 100)), 500)),
            status=params.get("status"),
            conversation_id=params.get("conversation_id"),
        )
        return {"tasks": tasks, "count": len(tasks)}

    async def task_get(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"task": await self.task_service.get(str(required(params, "task_id")))}
        except KeyError as exc:
            raise RpcFault(-32000, "Task not found") from exc

    async def _task_decision(self, tid: str, accepted: bool) -> dict[str, Any]:
        try:
            return {"task": await self.task_service.decide(tid, accepted)}
        except KeyError as exc:
            raise RpcFault(-32000, "Task not found") from exc
        except ValueError as exc:
            raise RpcFault(-32001, str(exc)) from exc

    async def task_plan_accept(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._task_decision(str(required(params, "task_id")), True)

    async def task_plan_reject(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._task_decision(str(required(params, "task_id")), False)

    async def memory_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.memory_service.list()

    async def remember(self, *, title: str, summary: str, content: str) -> dict[str, Any]:
        return await self.memory_service.create(title=title, summary=summary, content=content)

    async def automation_list(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT data_json FROM automations ORDER BY updated_at DESC")
        return {"automations": [loads(r["data_json"], {}) for r in rows]}

    async def automation_get(self, params: dict[str, Any]) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT data_json FROM automations WHERE id=?",
            (str(required(params, "automation_id")),),
        )
        if not row:
            raise RpcFault(-32000, "Automation not found")
        return {"automation": loads(row["data_json"], {})}

    async def automation_create(self, params: dict[str, Any]) -> dict[str, Any]:
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
        next_at = self._next_schedule(schedule, stamp)
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

    def _next_schedule(self, schedule: dict[str, Any], base: str) -> str | None:
        from datetime import UTC, datetime, timedelta
        from zoneinfo import ZoneInfo

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
        from croniter import croniter

        try:
            timezone = ZoneInfo(str(schedule.get("timezone") or "UTC"))
        except Exception as exc:
            raise RpcFault(-32602, "timezone must be a valid IANA timezone") from exc
        local_base = parse(base).astimezone(timezone)
        next_local = croniter(schedule["cron_expr"], local_base).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=timezone)
        return next_local.astimezone(UTC).isoformat().replace("+00:00", "Z")

    async def _automation_state(self, aid: str, status: str) -> dict[str, Any]:
        item = (await self.automation_get({"automation_id": aid}))["automation"]
        allowed = {"active": {"paused", "cancelled"}, "paused": {"active", "cancelled"}}
        if status not in allowed.get(item["status"], set()):
            raise RpcFault(-32001, f"Cannot change automation from {item['status']} to {status}")
        item["status"], item["updated_at"] = status, now()
        if status == "active":
            item["next_run_at"] = self._next_schedule(item["schedule"], item["updated_at"])
        elif status != "completed":
            item["next_run_at"] = None
        await self.db.execute(
            "UPDATE automations SET data_json=?,status=?,next_run_at=?,updated_at=? WHERE id=?",
            (dumps(item), status, item["next_run_at"], item["updated_at"], aid),
        )
        return {"automation": item}

    async def automation_pause(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._automation_state(str(required(params, "automation_id")), "paused")

    async def automation_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._automation_state(str(required(params, "automation_id")), "active")

    async def automation_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._automation_state(str(required(params, "automation_id")), "cancelled")

    async def _automation_loop(self) -> None:
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
                    if item["id"] in self.triggering_automations:
                        continue
                    self.triggering_automations.add(item["id"])
                    task = asyncio.create_task(self._trigger_automation(item))
                    self._track_background(task)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(2)

    async def _trigger_automation(self, item: dict[str, Any]) -> None:
        try:
            run = await self._claim_automation_occurrence(item["id"])
            if run is None:
                return
            task = asyncio.create_task(self._execute_run(run))
            self.running[run["id"]] = task
            task.add_done_callback(lambda _task, run_id=run["id"]: self.running.pop(run_id, None))
        finally:
            self.triggering_automations.discard(item["id"])

    async def _claim_automation_occurrence(self, automation_id: str) -> dict[str, Any] | None:
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
            previous = await (await db.execute(
                "SELECT 1 FROM runs WHERE source='automation' AND source_id=? AND status IN ('pending','running') LIMIT 1",
                (automation_id,),
            )).fetchone()
            if previous:
                if item["schedule"]["kind"] != "once":
                    item["next_run_at"] = self._next_schedule(item["schedule"], stamp)
                    item["updated_at"] = stamp
                    await db.execute(
                        "UPDATE automations SET data_json=?,next_run_at=?,updated_at=? WHERE id=?",
                        (dumps(item), item["next_run_at"], stamp, automation_id),
                    )
                    await db.commit()
                else:
                    await db.rollback()
                return None
            seen = await (await db.execute(
                "SELECT 1 FROM automation_occurrences WHERE automation_id=? AND scheduled_for=?",
                (automation_id, scheduled_for),
            )).fetchone()
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
                "INSERT INTO messages(conversation_id,role,content,name,tool_call_id,tool_calls_json,reasoning,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (cid, "user", item["prompt"], None, None, "[]", None, stamp),
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, cid))
            rid = uid()
            run = {
                "id": rid, "conversation_id": cid, "status": "pending", "user_message": item["prompt"],
                "created_at": stamp, "started_at": None, "updated_at": stamp, "completed_at": None,
                "error": None, "stop_reason": None, "recovered_from_run_id": None,
                "source": "automation", "source_id": automation_id, "scheduled_for": scheduled_for,
                "triggered_at": stamp, "mode": "normal",
            }
            await db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(run[key] for key in (
                    "id", "conversation_id", "status", "user_message", "created_at", "started_at", "updated_at",
                    "completed_at", "error", "stop_reason", "recovered_from_run_id", "source", "source_id",
                    "scheduled_for", "triggered_at", "mode",
                )),
            )
            trigger_id = hashlib.sha256(f"{automation_id}\0{scheduled_for}".encode()).hexdigest()
            await db.execute(
                "INSERT INTO automation_occurrences VALUES(?,?,?,?,?,?)",
                (trigger_id, automation_id, scheduled_for, rid, "pending", stamp),
            )
            item["last_run_at"], item["last_run_id"] = stamp, rid
            if item["schedule"]["kind"] == "once":
                item["status"], item["next_run_at"] = "completed", None
            else:
                item["next_run_at"] = self._next_schedule(item["schedule"], stamp)
            item["updated_at"] = stamp
            await db.execute(
                "UPDATE automations SET data_json=?,status=?,next_run_at=?,updated_at=? WHERE id=?",
                (dumps(item), item["status"], item["next_run_at"], stamp, automation_id),
            )
            await db.commit()
            return run

    async def _restore_automation_runs(self) -> None:
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
            task = asyncio.create_task(self._execute_run(run))
            self.running[run["id"]] = task
            task.add_done_callback(lambda _task, run_id=run["id"]: self.running.pop(run_id, None))

    async def computer_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.computer.status()

    async def computer_request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("permission") not in {"accessibility", "screen_recording"}:
            raise RpcFault(-32602, "Invalid permission")
        try:
            return await self.computer.permission(str(params["permission"]))
        except RuntimeError as exc:
            raise RpcFault(-32001, str(exc)) from exc

    async def computer_latest_observation(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.db.get_json("computer_latest_observation", {"run_id": None, "event_time": None, "observation": None})

    async def _store_observation(self, observation: dict[str, Any]) -> None:
        await self.db.set_json("computer_latest_observation", observation)

    async def tool_call(self, name: str, arguments: dict[str, Any], *, run_id: str, mode: str) -> Any:
        run = (await self.run_get({"run_id": run_id}))["run"]
        conversation_id = run.get("conversation_id")
        if name == "history_search":
            query = str(required(arguments, "query"))
            limit = max(1, min(int(arguments.get("limit", 20)), 100))
            rows = await self.db.fetchall(
                "SELECT id,role,content,name,created_at FROM messages WHERE conversation_id=? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (conversation_id, f"%{query}%", limit),
            )
            return {"matches": [dict(row) for row in rows]}
        if name == "session_search":
            return await self.conversation_search(arguments)
        if name == "history_read":
            offset = max(0, int(arguments.get("offset", 0)))
            limit = max(1, min(int(arguments.get("limit", 50)), 200))
            rows = await self.db.fetchall(
                "SELECT id,role,content,name,tool_call_id,reasoning,created_at FROM messages WHERE conversation_id=? ORDER BY id LIMIT ? OFFSET ?",
                (conversation_id, limit, offset),
            )
            return {"messages": [dict(row) for row in rows], "offset": offset}
        if name == "memory_list":
            data = await self.memory_service.list()
            status = arguments.get("status")
            if status in {"active", "archived"}:
                return {"memories": data[status], "core": data["core"]}
            return data
        if name == "memory_read":
            try:
                return {"memory": await self.memory_service.get(str(required(arguments, "memory_id")))}
            except KeyError as exc:
                raise ValueError("memory not found") from exc
        if name == "memory_create":
            memory = await self.memory_service.create(
                title=str(required(arguments, "title")),
                summary=str(required(arguments, "summary")),
                content=str(required(arguments, "content")),
            )
            return {"memory": memory}
        if name == "memory_update":
            memory_id = str(required(arguments, "memory_id"))
            values = {key: arguments[key] for key in ("title", "summary", "content") if key in arguments}
            try:
                return {"memory": await self.memory_service.update(memory_id, **values)}
            except KeyError as exc:
                raise ValueError("memory not found") from exc
        if name == "memory_archive":
            try:
                return {
                    "memory": await self.memory_service.archive(str(required(arguments, "memory_id")), str(arguments.get("reason") or "agent_tool"))
                }
            except KeyError as exc:
                raise ValueError("memory not found") from exc
        if name == "core_memory_update":
            entries = await self.memory_service.update_core(str(required(arguments, "key")), str(required(arguments, "value")))
            return {"entries": entries}
        if name == "core_memory_remove":
            return {"entries": await self.memory_service.remove_core(str(required(arguments, "key")))}
        if name == "task_create":
            task = await self.task_service.create(
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
                return {"task": await self.task_service.update(task_id, changes, run_id=run_id)}
            except KeyError as exc:
                raise ValueError("task not found") from exc
        if name == "task_get":
            try:
                return {"task": await self.task_service.get(str(required(arguments, "task_id")))}
            except KeyError as exc:
                raise ValueError("task not found") from exc
        if name == "task_list":
            tasks = await self.task_service.list(limit=max(1, min(int(arguments.get("limit", 100)), 500)), status=arguments.get("status"))
            return {"tasks": tasks, "count": len(tasks)}
        if name in {"skill_read", "skill_resource_read"}:
            skill_name = str(required(arguments, "name"))
            scope = arguments.get("scope")
            skill = next(
                (
                    item
                    for item in sorted(await self._skills(), key=lambda value: value.get("scope") != "project")
                    if item.get("name") == skill_name and item.get("enabled") and (scope is None or item.get("scope") == scope)
                ),
                None,
            )
            if not skill:
                raise ValueError("skill not found or disabled")
            root = Path(skill["location"]).parent.resolve()
            resource = Path(skill["location"]).resolve() if name == "skill_read" else (root / str(required(arguments, "path"))).resolve()
            try:
                resource.relative_to(root)
            except ValueError as exc:
                raise PermissionError("skill resource path escapes the skill directory") from exc
            if not resource.is_file():
                raise ValueError("skill resource not found")
            content = resource.read_text(encoding="utf-8")
            budget = 12_000 if name == "skill_read" else 20_000
            return {
                "skill": skill_name, "path": str(resource.relative_to(root)),
                "content": content[:budget], "truncated": len(content) > budget,
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
            "automation_create": self.automation_create,
            "automation_list": self.automation_list,
            "automation_get": self.automation_get,
            "automation_pause": self.automation_pause,
            "automation_resume": self.automation_resume,
            "automation_cancel": self.automation_cancel,
        }
        if name in automation_methods:
            return await automation_methods[name](arguments)
        raise ValueError(f"Unsupported service tool: {name}")

    async def _skills(self) -> list[dict[str, Any]]:
        skills = list(await self.db.get_json("skills", []))
        known = {(item.get("name"), item.get("scope")) for item in skills}
        for scope, root in (("project", self.settings.data_dir / "skills" / "project"), ("user", self.settings.user_skills_dir)):
            if not root.is_dir():
                continue
            for folder in root.iterdir():
                if not folder.is_dir() or folder.is_symlink() or (folder.name, scope) in known:
                    continue
                skill_file = folder / "SKILL.md"
                if not skill_file.is_file() or skill_file.is_symlink() or skill_file.stat().st_size > 2 * 1024 * 1024:
                    continue
                metadata, _ = self._parse_skill_document(skill_file.read_text(encoding="utf-8"))
                skills.append({
                    "name": folder.name, "description": metadata.get("description", ""),
                    "scope": scope, "location": str(skill_file), "enabled": True, "discovered": True,
                })
        for skill in skills:
            root = Path(str(skill.get("location") or "")).parent
            if root.is_dir():
                skill["resources"] = [
                    {"path": str(path.relative_to(root)).replace("\\", "/"), "size_bytes": path.stat().st_size}
                    for path in root.rglob("*") if path.is_file() and not path.is_symlink()
                ][:100]
        return skills

    async def _migrate_user_skills(self) -> None:
        saved = await self.db.get_json("skills", [])
        changed = False
        old_root = (self.settings.data_dir / "skills" / "user").resolve()
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
            destination = self.settings.user_skills_dir / str(item["name"])
            if not destination.exists():
                shutil.copytree(source.parent, destination)
            item["location"] = str(destination / "SKILL.md")
            changed = True
        if changed:
            await self.db.set_json("skills", saved)

    async def _mcp(self) -> dict[str, Any]:
        return await self.db.get_json("mcp", {"servers": []})

    async def _save_mcp(self, data: dict[str, Any]) -> None:
        await self.db.set_json("mcp", data)
        path = self.settings.data_dir / "mcp.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def extension_list(self, params: dict[str, Any]) -> dict[str, Any]:
        mcp = await self._mcp()
        skills = await self._skills()
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
            metadata, instructions = self._parse_skill_document(content)
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
                "config_path": str(self.settings.data_dir / "mcp.json"),
                "error": None,
                "restart_required": any(item.get("enabled", True) and item["name"] not in self.mcp_manager.servers for item in mcp["servers"]),
                "servers": [self._mcp_view(item) for item in mcp["servers"]],
            },
        }

    async def extension_import_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = str(required(params, "input"))
        scope = str(params.get("skill_scope", "project"))
        return {"plan": self._extension_plan(raw, scope)}

    async def extension_import_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("confirmed") is not True:
            raise RpcFault(-32602, "confirmed must be true")
        raw = str(required(params, "input"))
        scope = str(params.get("skill_scope", "project"))
        plan = self._extension_plan(raw, scope)
        expected = plan["fingerprint"]
        if params.get("fingerprint") != expected:
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
                temporary_root = Path(tempfile.mkdtemp(prefix="koreagent-import-", dir=self.settings.data_dir))
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
            skill_files = self._discover_skill_files(checkout)
            if not skill_files:
                raise RpcFault(-32602, "No SKILL.md was found in the extension source")
            imported = []
            for skill_file in skill_files:
                imported.append(await self._install_skill_tree(skill_file, scope, raw))
            return {
                "skills": [{"name": item["name"], "scope": item["scope"], "source": raw} for item in imported],
                "mcp_servers": [],
                "restart_required": False,
            }
        finally:
            if temporary_root:
                shutil.rmtree(temporary_root, ignore_errors=True)

    def _extension_plan(self, raw: str, scope: str) -> dict[str, Any]:
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
    def _discover_skill_files(root: Path) -> list[Path]:
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

    async def _install_skill_tree(self, skill_file: Path, scope: str, source: str) -> dict[str, Any]:
        content = skill_file.read_text(encoding="utf-8")
        metadata, instructions = self._parse_skill_document(content)
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
            target = destination / child.name
            self._copy_skill_resource(child, target, root=skill_file.parent)
        return result["skill"]

    @classmethod
    def _copy_skill_resource(cls, source: Path, target: Path, *, root: Path) -> None:
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
                cls._copy_skill_resource(child, target / child.name, root=root)

    @staticmethod
    def _parse_skill_document(content: str) -> tuple[dict[str, str], str]:
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

    async def skill_install(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(required(params, "name")).strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
            raise RpcFault(-32602, "Invalid skill name")
        scope = str(params.get("scope", "project"))
        if scope not in {"project", "user"}:
            raise RpcFault(-32602, "Skill scope must be project or user")
        description = str(params.get("description", ""))
        folder = (self.settings.user_skills_dir if scope == "user" else self.settings.data_dir / "skills" / "project") / name
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{required(params, 'instructions')}\n",
            encoding="utf-8",
        )
        skills = [s for s in await self._skills() if not (s["name"] == name and s["scope"] == scope)]
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
        skills = await self._skills()
        item = next((s for s in skills if s["name"] == name and s["scope"] == scope), None)
        if not item:
            raise RpcFault(-32000, "Skill not found")
        item["enabled"] = bool(params.get("enabled"))
        await self.db.set_json("skills", skills)
        return {"skill": item}

    async def skill_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        name, scope = str(required(params, "name")), str(required(params, "scope"))
        skills = await self._skills()
        selected = next((s for s in skills if s["name"] == name and s["scope"] == scope), None)
        if not selected:
            raise RpcFault(-32000, "Skill not found")
        path = Path(selected["location"])
        shutil.rmtree(path.parent, ignore_errors=True)
        await self.db.set_json("skills", [s for s in skills if s is not selected])
        return {"deleted": True}

    def _mcp_view(self, item: dict[str, Any]) -> dict[str, Any]:
        runtime = self.mcp_manager.servers.get(item["name"])
        state = "restart_required" if item.get("enabled", True) else "stopped"
        error = None
        tool_names: list[str] = []
        if runtime:
            error = runtime.error
            state = "failed" if error else "running"
            tool_names = [tool.get("name", "") for tool in runtime.tools]
        sandbox = item.get("sandbox") or {}
        filesystem = sandbox.get("filesystem", "workspace")
        network = sandbox.get("network", "denied")
        host_mode = filesystem == "host" and network == "allowed"
        native_sandbox = sys.platform == "darwin" and not host_mode
        supported = host_mode or (
            native_sandbox and network in {"allowed", "denied"}
            and filesystem in {"workspace", "readonly"}
        )
        supported = supported and not any(sandbox.get(key) for key in ("readable_roots", "writable_roots", "allowed_domains"))
        if item.get("enabled", True) and not supported and not runtime:
            state = "unavailable"
            error = "所选沙箱策略在此平台不可用，进程不会启动"
        return {
            **item,
            "env_names": sorted(item.get("env", {}).keys()),
            "state": state,
            "tool_names": tool_names,
            "error": error,
            "started_at": runtime.started_at if runtime else None,
            "last_call_at": runtime.last_call_at if runtime else None,
            "restart_count": runtime.restart_count if runtime else 0,
            "sandboxed": native_sandbox,
            "sandbox_backend": "macos-seatbelt" if native_sandbox and supported else ("host" if host_mode and supported else "unavailable"),
        }

    async def mcp_add(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(required(params, "name"))
        command = str(required(params, "command"))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RpcFault(-32602, "Invalid MCP server name")
        data = await self._mcp()
        if any(s["name"] == name for s in data["servers"]):
            raise RpcFault(-32001, "MCP server already exists")
        item = {
            "name": name,
            "command": command,
            "args": list(params.get("args", [])),
            "cwd": params.get("cwd"),
            "enabled": bool(params.get("enabled", True)),
            "permission": params.get("permission", "human_approval"),
            "env": dict(params.get("env", {})),
            "startup_timeout_seconds": float(params.get("startup_timeout_seconds", 20)),
            "call_timeout_seconds": float(params.get("call_timeout_seconds", 60)),
            "sandbox": params.get(
                "sandbox",
                {
                    "filesystem": "workspace",
                    "network": "denied",
                    "readable_roots": [],
                    "writable_roots": [],
                    "allowed_domains": [],
                },
            ),
        }
        data["servers"].append(item)
        await self._save_mcp(data)
        return {
            "server": self._mcp_view(item),
            "restart_required": True,
            "config_path": str(self.settings.data_dir / "mcp.json"),
        }

    async def mcp_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        data, name = await self._mcp(), str(required(params, "name"))
        item = next((s for s in data["servers"] if s["name"] == name), None)
        if not item:
            raise RpcFault(-32000, "MCP server not found")
        item["enabled"] = bool(params.get("enabled"))
        await self._save_mcp(data)
        return {"server": self._mcp_view(item)}

    async def mcp_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        data, name = await self._mcp(), str(required(params, "name"))
        before = len(data["servers"])
        data["servers"] = [s for s in data["servers"] if s["name"] != name]
        if len(data["servers"]) == before:
            raise RpcFault(-32000, "MCP server not found")
        await self._save_mcp(data)
        return {"deleted": True, "restart_required": True}
