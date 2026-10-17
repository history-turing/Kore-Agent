from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointStore
from .computer import ComputerClient
from .computer_guard import ComputerStagnationGuard
from .config import Settings
from .context import ContextManager
from .database import Database
from .evidence import EvidenceStore
from .mcp import McpManager
from .memory import MemoryService
from .model import ModelClient
from .permissions import PermissionRuleStore
from .rpc import RpcFault, required
from .runtime_policy import RunBudget, RunBudgetConfig
from .secrets import SecretStore
from .services.approvals import ApprovalService
from .services.artifacts import ArtifactService
from .services.automations import AutomationService
from .services.conversations import ConversationService
from .services.extensions import ExtensionService
from .services.mcp_settings import McpSettingsService
from .services.model_settings import ModelSettingsService
from .services.run_engine import RunEngine
from .services.run_postprocessing import RunPostProcessor
from .services.runs import RunStore
from .services.skills import SkillService
from .services.tool_router import ServiceToolRouter
from .skill_learning import SkillLearningService
from .tasks import TaskService

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]

__all__ = ["Application", "RpcFault", "required"]


def _build_model_client(settings: Settings, config: dict[str, Any]) -> ModelClient:
    return ModelClient(settings, config)


class Application:
    """Composition root and stable JSON-RPC facade.

    Feature behavior lives in ``kore_next.services``. Public methods remain on
    this class so the RPC contract and existing callers stay stable.
    """

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

        self.conversations = ConversationService(self.db)
        self.model_settings = ModelSettingsService(
            self.db,
            self.secrets,
            settings=lambda: self.settings,
            runtime_servers=lambda: self.mcp_manager.servers,
            running_tasks=lambda: self.running,
            model_client_factory=_build_model_client,
        )
        self.runs = RunStore(
            self.db,
            self.checkpoints,
            self.run_budget,
            settings=lambda: self.settings,
            broadcast=lambda: self.broadcast,
            running=self.running,
            execute_run=lambda run, state=None: self._execute_run(run, state),
        )
        self.approvals = ApprovalService(
            self.db,
            self.permission_rules,
            settings=lambda: self.settings,
            broadcast=lambda: self.broadcast,
            get_run=self.runs.get,
            event=self.runs.event,
            waiters=self.approval_waiters,
        )
        self.artifacts = ArtifactService(
            self.db,
            settings=lambda: self.settings,
            broadcast=lambda: self.broadcast,
            get_run=self.runs.get,
        )
        self.skill_service = SkillService(
            self.db,
            self.skill_learning,
            settings=lambda: self.settings,
        )
        self.mcp_settings = McpSettingsService(
            self.db,
            settings=lambda: self.settings,
            mcp_manager=lambda: self.mcp_manager,
        )
        self.extensions = ExtensionService(
            self.db,
            self.skill_learning,
            settings=lambda: self.settings,
            skills=self.skill_service,
            mcp_settings=self.mcp_settings,
        )
        self.automations = AutomationService(
            self.db,
            self.runs,
            execute_run=lambda run: self._execute_run(run),
            track_background=self._track_background,
            running=self.running,
            triggering=self.triggering_automations,
            schedule_next=lambda schedule, base: self._next_schedule(schedule, base),
        )
        self.tool_router = ServiceToolRouter(
            self.db,
            self.conversations,
            self.runs,
            self.memory_service,
            self.task_service,
            self.evidence,
            self.extensions,
            self.automations,
            self.artifacts,
        )
        self.run_post_processor = RunPostProcessor(
            settings=lambda: self.settings,
            model_settings=self.model_settings,
            runs=self.runs,
            memory=self.memory_service,
            tasks=self.task_service,
            skills=self.extensions.skills,
            skill_learning=self.skill_learning,
        )
        self.run_engine = RunEngine(
            settings=lambda: self.settings,
            model_settings=self.model_settings,
            conversations=self.conversations,
            runs=self.runs,
            context=self.context_manager,
            checkpoints=self.checkpoints,
            memory=self.memory_service,
            tasks=self.task_service,
            evidence=self.evidence,
            skills=self.extensions.skills,
            post_processor=self.run_post_processor,
            run_budget=self.run_budget,
            computer_guard=self.computer_guard,
            computer=lambda: self.computer,
            mcp_manager=lambda: self.mcp_manager,
            approvals=self.approvals,
            artifacts=self.artifacts,
            tool_router=self.tool_router,
            broadcast=lambda: self.broadcast,
            post_run=lambda run, result: self._post_run(run, result),
            track_background=self._track_background,
        )

    async def _noop(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def start(self) -> None:
        await self.db.initialize()
        await self.extensions.migrate_user_skills()
        await self.skill_learning.scan_completed_runs(existing_skills=await self.extensions.skills(), limit=100)
        mcp = await self.extensions.mcp()
        await self.mcp_manager.start(mcp["servers"])
        await self.computer.start()
        await self.automations.restore()
        self.automation_task = asyncio.create_task(self.automations.loop())

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
                await self.approvals.cancel(approval_id)
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
            "platform": sys.platform,
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
        mcp = await self.extensions.mcp()
        await self.mcp_manager.start(mcp["servers"])
        self.computer = ComputerClient(self.settings, self._store_observation)
        await self.computer.start()
        return {"accepted": True}

    async def model_settings_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.model_settings.view()

    async def model_settings_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.model_settings.update(params)

    async def model_settings_test(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.model_settings.test(params)

    async def conversation_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.conversations.list(params)

    async def conversation_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.conversations.get(params)

    async def conversation_search(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.conversations.search(params)

    async def conversation_create(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.conversations.create(params)

    async def conversation_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.conversations.rename(params)

    async def conversation_delete(self, params: dict[str, Any]) -> dict[str, bool]:
        return await self.conversations.delete(params)

    async def conversation_send(self, params: dict[str, Any]) -> dict[str, Any]:
        cid, content = str(required(params, "conversation_id")), str(required(params, "content"))
        mode = str(params.get("mode", "normal"))
        if mode not in {"normal", "plan"}:
            raise RpcFault(-32602, "mode must be normal or plan")
        await self.conversations.require(cid)
        await self.conversations.add_message(cid, {"role": "user", "content": content})
        run = await self.runs.create(cid, content, mode)
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

    async def run_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.runs.list(params)

    async def run_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.runs.get(params)

    async def run_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.runs.cancel(params)

    async def run_interrupt(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.runs.interrupt(params)

    async def run_recover(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.runs.recover(params)

    async def trace_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.runs.trace(params)

    async def automation_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.automations.list(params)

    async def automation_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.automations.get(params)

    async def automation_create(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.automations.create(params)

    async def automation_pause(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.automations.pause(params)

    async def automation_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.automations.resume(params)

    async def automation_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.automations.cancel(params)

    async def approval_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.approvals.list(params)

    async def approval_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.approvals.get(params)

    async def approval_approve(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.approvals.approve(params)

    async def approval_deny(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.approvals.deny(params)

    async def artifact_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.artifacts.list(params)

    async def artifact_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.artifacts.get(params)

    async def artifact_path(self, aid: str) -> Path | None:
        return await self.artifacts.path(aid)

    async def publish_artifact(self, *, path: Path, title: str, description: str | None, run_id: str) -> dict[str, Any]:
        return await self.artifacts.publish(path=path, title=title, description=description, run_id=run_id)

    async def publish_url_artifact(self, *, url: str, title: str, description: str | None, run_id: str) -> dict[str, Any]:
        return await self.artifacts.publish_url(url=url, title=title, description=description, run_id=run_id)

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

    async def extension_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.extension_list(params)

    async def extension_import_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.import_preview(params)

    async def extension_import_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.import_apply(params)

    async def skill_install(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.skill_install(params)

    async def skill_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.skill_set_enabled(params)

    async def skill_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.skill_delete(params)

    async def mcp_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.mcp_add(params)

    async def mcp_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.mcp_set_enabled(params)

    async def mcp_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.extensions.mcp_delete(params)

    async def tool_call(self, name: str, arguments: dict[str, Any], *, run_id: str, mode: str) -> Any:
        return await self.tool_router.tool_call(name, arguments, run_id=run_id, mode=mode)

    def _track_background(self, task: asyncio.Task[Any]) -> None:
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _execute_run(self, run: dict[str, Any], recovery_state: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.run_engine.execute(run, recovery_state)

    async def _post_run(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        await self.run_engine.run_post_processing(run, result)

    async def _add_message(self, cid: str, message: dict[str, Any]) -> None:
        await self.conversations.add_message(cid, message)

    async def _new_run(self, cid: str | None, text: str, mode: str, **extra: Any) -> dict[str, Any]:
        return await self.runs.create(cid, text, mode, **extra)

    async def _save_run(self, run: dict[str, Any]) -> None:
        await self.runs.save(run)

    async def _event(self, run: dict[str, Any], kind: str, **data: Any) -> dict[str, Any]:
        return await self.runs.event(run, kind, **data)

    async def _request_approval(self, run_id: str, call_id: str, arguments: dict[str, Any], reason: str) -> bool:
        return await self.approvals.request(run_id, call_id, arguments, reason)

    async def _cancel_approval(self, approval_id: str) -> None:
        await self.approvals.cancel(approval_id)

    async def _resolve_approval(self, aid: str, decision: bool, *, scope: str = "once") -> dict[str, Any]:
        return await self.approvals.resolve(aid, decision, scope=scope)

    async def _skills(self) -> list[dict[str, Any]]:
        return await self.extensions.skills()

    async def _migrate_user_skills(self) -> None:
        await self.extensions.migrate_user_skills()

    @staticmethod
    def _skill_catalog(skills: list[dict[str, Any]]) -> str:
        return RunEngine.skill_catalog(skills)

    @staticmethod
    def _system_prompt(mode: str) -> str:
        return RunEngine.system_prompt(mode)

    @staticmethod
    def _history_for_model(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return RunEngine.history_for_model(history)

    async def _summarize_context(self, source: str, previous: str | None, *, run: dict[str, Any] | None = None) -> str:
        return await self.run_engine.summarize_context(source, previous, run=run)

    async def _role_model_config(self, role: str) -> dict[str, Any] | None:
        return await self.model_settings.role_config(role)

    async def _record_auxiliary_usage(self, run: dict[str, Any], role: str, usage: dict[str, int], config: dict[str, Any]) -> None:
        await self.run_engine.record_auxiliary_usage(run, role, usage, config)

    async def _create_plan_task(self, run: dict[str, Any], content: str) -> dict[str, Any]:
        return await self.run_engine.create_plan_task(run, content)

    async def _claim_automation_occurrence(self, automation_id: str) -> dict[str, Any] | None:
        return await self.automations.claim_occurrence(automation_id)

    async def _restore_automation_runs(self) -> None:
        await self.automations.restore()

    def _next_schedule(self, schedule: dict[str, Any], base: str) -> str | None:
        return self.automations.compute_next_schedule(schedule, base)
