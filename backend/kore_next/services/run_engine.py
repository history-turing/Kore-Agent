from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..checkpoint import CheckpointStore
from ..computer import ComputerClient
from ..computer_guard import ComputerStagnationGuard
from ..config import Settings
from ..context import ContextDecision, ContextManager
from ..context_offload import ContextOffloadStore
from ..evidence import EvidenceStore
from ..mcp import McpManager, McpToolError
from ..memory import MemoryService
from ..model import ModelClient
from ..model.types import ModelReply
from ..resources import ResourceAuthority
from ..rpc import RpcFault
from ..runtime_policy import RepeatedToolCallGuard, RunBudget, RuntimePolicyError
from ..tasks import TaskService
from ..tools import ToolError, ToolExecutor, ToolProjection
from ..tools.policies import CONCURRENT_READ_TOOLS, is_mutating_tool, tool_policy
from ..util import dumps, now, uid
from ..workspace import WorkspaceContext, WorkspaceError, resolve_workspace
from .approvals import ApprovalService
from .artifacts import ArtifactService
from .conversations import ConversationService
from .model_settings import ModelSettingsService
from .run_postprocessing import RunPostProcessor
from .runs import RunStore
from .runtime_projections import order_tool_messages
from .tool_router import ServiceToolRouter

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class StepMetrics:
    prepared_tokens: int
    system_tokens: int
    tool_tokens: int
    history_tokens: int
    tool_schema_tokens: int


@dataclass(frozen=True)
class ToolCallOutcome:
    plan_task_id: str | None
    tool_message: dict[str, Any] | None
    result_event: dict[str, Any] | None


class RunEngine:
    """The agent execution loop: context, model calls, tools, and finalization."""

    def __init__(
        self,
        *,
        settings: Callable[[], Settings],
        model_settings: ModelSettingsService,
        conversations: ConversationService,
        runs: RunStore,
        context: ContextManager,
        checkpoints: CheckpointStore,
        offload: ContextOffloadStore,
        memory: MemoryService,
        tasks: TaskService,
        evidence: EvidenceStore,
        skills: Callable[[], Awaitable[list[dict[str, Any]]]],
        post_processor: RunPostProcessor,
        run_budget: RunBudget,
        computer_guard: ComputerStagnationGuard,
        computer: Callable[[], ComputerClient],
        mcp_manager: Callable[[], McpManager],
        approvals: ApprovalService,
        artifacts: ArtifactService,
        tool_router: ServiceToolRouter,
        resources: ResourceAuthority,
        broadcast: Callable[[], Broadcast],
        post_run: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]],
        track_background: Callable[[asyncio.Task[Any]], None],
    ):
        self._settings = settings
        self.model_settings = model_settings
        self.conversations = conversations
        self.runs = runs
        self.context = context
        self.checkpoints = checkpoints
        self.offload = offload
        self.memory = memory
        self.tasks = tasks
        self.evidence = evidence
        self.skills = skills
        self.post_processor = post_processor
        self.run_budget = run_budget
        self.computer_guard = computer_guard
        self._computer = computer
        self._mcp_manager = mcp_manager
        self.approvals = approvals
        self.artifacts = artifacts
        self.tool_router = tool_router
        self.resources = resources
        self._broadcast = broadcast
        self.post_run = post_run
        self.track_background = track_background

    async def execute(self, run: dict[str, Any], recovery_state: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self._settings()
        run.update(status="running", started_at=now(), updated_at=now())
        await self.runs.save(run)
        await self.runs.event(run, "agent_started", message=None, tool_call=None, tool_result=None, usage=None)
        try:
            model_config = await self.model_settings.active_config()
            model = ModelClient(settings, model_config)
            conversation = await self.conversations.get({"conversation_id": run["conversation_id"]})
            workspace = await self._resolve_run_workspace(run, conversation["conversation"])
            if recovery_state:
                # Recovery resumes from model-facing messages and cumulative usage,
                # so the new run inherits its predecessor's budget and tool state.
                context_decision: ContextDecision | None = None
                model_messages = list(recovery_state.get("model_messages", []))
                total = dict(recovery_state.get("usage", {}))
                start_step = int(recovery_state.get("next_step", 1))
                plan_task_id = recovery_state.get("plan_task_id")
                model_calls = int(recovery_state.get("model_calls", max(0, start_step - 1)))
                await self.runs.event(
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
                history = conversation["messages"]
                prepared_history = self.history_for_model(history)
                base_system = self.system_prompt(run["mode"], workspace)
                decision = await self.context.prepare(
                    conversation_id=run["conversation_id"],
                    history=prepared_history,
                    provider=str(model_config.get("provider") or settings.provider),
                    model=str(model_config.get("model") or settings.model),
                    base_system=base_system,
                    core_memory=await self.memory.render_core(),
                    memory_index=await self.memory.index(),
                    task_context=await self.tasks.active_context(run["conversation_id"]),
                    skill_catalog=self.skill_catalog(await self.skills()),
                    summarizer=lambda source, previous: self.post_processor.summarize_context(
                        source, previous, run=run
                    ),
                )
                context_decision = decision
                model_messages = decision.messages
                total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                start_step = 1
                plan_task_id = None
                model_calls = 0
            phase = str((recovery_state or {}).get("phase") or "model_pending")
            pending_tool_calls = list((recovery_state or {}).get("pending_tool_calls") or [])
            tool_cursor = int((recovery_state or {}).get("tool_cursor") or 0)
            completed_call_ids = (recovery_state or {}).get("completed_call_ids")
            tools = ToolExecutor(
                settings,
                self.approvals.request,
                self.artifacts.publish,
                self.memory.create,
                self._mcp_manager(),
                self._computer(),
                self.tool_router,
                self.resources,
                workspace,
            )
            if recovery_state:
                activation_steps = recovery_state.get("tool_activation_steps")
                if isinstance(activation_steps, dict):
                    tools.restore_activations(activation_steps)
                else:
                    # Older checkpoints only stored names; make them visible
                    # immediately rather than losing the recovered capability set.
                    tools.restore_activations({name: 0 for name in recovery_state.get("activated_tools", [])})
            final = ""
            steps = start_step - 1
            if phase == "final_ready":
                final = str(recovery_state.get("final") or "")
                return await self._finalize_run(run, final, max(0, start_step - 1), total, plan_task_id)
            if phase == "tool_unknown":
                raise RpcFault(
                    -32002,
                    "Tool outcome is unknown; manual reconciliation is required",
                    {"run_id": run["id"]},
                )
            if phase == "blocked":
                raise RpcFault(-32001, "Run stopped by runtime policy and cannot be resumed")
            if phase == "abandoned":
                raise RpcFault(-32001, "Run was abandoned after an unknown tool outcome")
            if phase in {"assistant_ready", "tools_running"}:
                resume_step = start_step
                projection = tools.project(resume_step, run["mode"])
                calls_to_run = self._pending_calls_to_run(pending_tool_calls, completed_call_ids, tool_cursor)
                plan_task_id = await self._execute_tool_calls(
                    run,
                    tools,
                    calls_to_run,
                    resume_step,
                    model_messages,
                    plan_task_id,
                    projection=projection,
                    order_calls=pending_tool_calls,
                )
                snapshot = await self.runs.runtime_event_store.latest(run["id"])
                await self.checkpoints.save(
                    run_id=run["id"],
                    conversation_id=run["conversation_id"],
                    sequence=resume_step,
                    state=self._checkpoint_state(
                        phase="model_pending",
                        model_messages=model_messages,
                        total=total,
                        next_step=resume_step + 1,
                        plan_task_id=plan_task_id,
                        tools=tools,
                        model_calls=model_calls,
                        pending_tool_calls=[],
                        tool_cursor=0,
                    ),
                    through_event_seq=snapshot["event_seq"],
                    digest=snapshot["digest"],
                )
                steps = resume_step
                start_step = resume_step + 1
            budget_warning_emitted = False
            repeated_calls = RepeatedToolCallGuard()
            first_edit_step: int | None = None
            for steps in range(start_step, settings.max_steps + 1):
                # Check before each request and again after usage arrives; a model
                # response can cross the limit even when the request was allowed.
                budget_decision = self.run_budget.evaluate(total, model_calls)
                if budget_decision.exceeded:
                    await self.runs.event(
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
                    await self.runs.event(
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
                    await self.runs.event(
                        run,
                        "run_budget_finalizing",
                        step=steps,
                        message=None,
                        tool_call=None,
                        tool_result=None,
                        usage=None,
                        **self.run_budget.event_fields(budget_decision),
                    )
                phase = self.run_budget.phase(total, model_calls)
                projection = tools.project(steps, run["mode"])
                base_tools, extra_tools = list(projection.base), list(projection.extra)
                if force_final:
                    base_tools = []
                    extra_tools = []
                estimator = self.context.estimator
                tool_schema_tokens = (
                    estimator.count_text(dumps([*base_tools, *extra_tools])) if base_tools or extra_tools else 0
                )
                guidance_content = phase.instruction
                if phase.name == "implement" and first_edit_step is None:
                    guidance_content += (
                        " No repository edit has been detected yet; write the smallest plausible patch now."
                    )
                guidance = {"role": "system", "content": guidance_content}
                step_context = await self.context.prepare_step(
                    messages=model_messages,
                    provider=str(model_config.get("provider") or settings.provider),
                    model=str(model_config.get("model") or settings.model),
                    tool_schema_tokens=tool_schema_tokens + estimator.count_message(guidance),
                    summarizer=lambda source, previous: self.post_processor.summarize_context(
                        source, previous, run=run
                    ),
                )
                model_messages = step_context.messages
                request_messages = [*model_messages, guidance]
                system_tokens = sum(
                    estimator.count_message(item) for item in request_messages if item.get("role") == "system"
                )
                tool_tokens = sum(
                    estimator.count_message(item) for item in request_messages if item.get("role") == "tool"
                )
                history_tokens = (
                    sum(
                        estimator.count_message(item)
                        for item in request_messages
                        if item.get("role") not in {"system", "tool"}
                    )
                    + 8
                )
                metrics = StepMetrics(
                    prepared_tokens=system_tokens + tool_tokens + history_tokens + tool_schema_tokens,
                    system_tokens=system_tokens,
                    tool_tokens=tool_tokens,
                    history_tokens=history_tokens,
                    tool_schema_tokens=tool_schema_tokens,
                )
                await self._record_context_prepared(
                    run,
                    step_context,
                    metrics,
                    step=steps,
                    start_step=start_step,
                    context_decision=context_decision,
                    model_config=model_config,
                    settings=settings,
                )
                request = {
                    "messages": request_messages,
                    "tools": [*base_tools, *extra_tools],
                    "provider": model_config.get("provider") or settings.provider,
                    "model": model_config.get("model") or settings.model,
                }
                # The checkpoint and request trace share one commit, so a crash
                # can never leave a request without its recovery boundary.
                async with self.runs.transaction() as conn:
                    snapshot = await self.runs.runtime_event_store.latest(run["id"], conn=conn)
                    await self.checkpoints.save(
                        run_id=run["id"],
                        conversation_id=run["conversation_id"],
                        sequence=steps,
                        state=self._checkpoint_state(
                            phase="model_pending",
                            model_messages=model_messages,
                            total=total,
                            next_step=steps,
                            plan_task_id=plan_task_id,
                            tools=tools,
                            model_calls=model_calls,
                            pending_tool_calls=[],
                            tool_cursor=0,
                        ),
                        through_event_seq=snapshot["event_seq"],
                        digest=snapshot["digest"],
                        conn=conn,
                    )
                    request_event = await self.runs.event(
                        run,
                        "model_request",
                        conn=conn,
                        step=steps,
                        request=request,
                        request_sha256=hashlib.sha256(dumps(request).encode("utf-8")).hexdigest(),
                    )
                await self.runs.broadcast_event(request_event)

                reply, model_calls = await self._call_model_step(
                    run,
                    model,
                    request_messages,
                    base_tools,
                    extra_tools,
                    steps,
                    model_config,
                    settings,
                    total,
                    model_calls,
                )
                assistant = {
                    "role": "assistant",
                    "content": reply.content,
                    "reasoning": reply.reasoning,
                    "tool_calls": reply.tool_calls,
                }
                model_messages.append(self._model_assistant_message(reply))
                if force_final and reply.tool_calls:
                    async with self.runs.transaction() as conn:
                        await self.conversations.add_message(run["conversation_id"], assistant, conn=conn)
                        assistant_event = await self.runs.event(
                            run,
                            "assistant_message",
                            conn=conn,
                            step=steps,
                            message=assistant,
                            tool_call=None,
                            tool_result=None,
                            usage=reply.usage,
                            provider=model_config.get("provider") or settings.provider,
                            model=model_config.get("model") or settings.model,
                        )
                        await self.checkpoints.save(
                            run_id=run["id"],
                            conversation_id=run["conversation_id"],
                            sequence=steps,
                            state=self._checkpoint_state(
                                phase="blocked",
                                model_messages=model_messages,
                                total=total,
                                next_step=steps,
                                plan_task_id=plan_task_id,
                                tools=tools,
                                model_calls=model_calls,
                                pending_tool_calls=reply.tool_calls,
                                tool_cursor=0,
                            ),
                            through_event_seq=assistant_event["event_seq"],
                            digest=assistant_event["runtime_digest"],
                            conn=conn,
                        )
                    await self.runs.broadcast_event(assistant_event)
                    raise RuntimePolicyError(
                        "Model attempted a tool call while the run budget was finalizing",
                        stop_reason="run_budget",
                    )
                if not reply.tool_calls:
                    final = reply.content or ""
                    async with self.runs.transaction() as conn:
                        await self.conversations.add_message(run["conversation_id"], assistant, conn=conn)
                        assistant_event = await self.runs.event(
                            run,
                            "assistant_message",
                            conn=conn,
                            step=steps,
                            message=assistant,
                            tool_call=None,
                            tool_result=None,
                            usage=reply.usage,
                            provider=model_config.get("provider") or settings.provider,
                            model=model_config.get("model") or settings.model,
                        )
                        await self.checkpoints.save(
                            run_id=run["id"],
                            conversation_id=run["conversation_id"],
                            sequence=steps,
                            state=self._checkpoint_state(
                                phase="final_ready",
                                model_messages=model_messages,
                                total=total,
                                next_step=steps + 1,
                                plan_task_id=plan_task_id,
                                tools=tools,
                                model_calls=model_calls,
                                pending_tool_calls=[],
                                tool_cursor=0,
                                final=final,
                            ),
                            through_event_seq=assistant_event["event_seq"],
                            digest=assistant_event["runtime_digest"],
                            conn=conn,
                        )
                    await self.runs.broadcast_event(assistant_event)
                    break
                repeated_calls.observe(reply.tool_calls)
                if first_edit_step is None and any(
                    is_mutating_tool(
                        str(call.get("name") or ""),
                        call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                    )
                    for call in reply.tool_calls
                ):
                    first_edit_step = steps
                    run["first_edit_step"] = steps
                    await self.runs.event(
                        run,
                        "first_edit",
                        step=steps,
                        message=None,
                        tool_call=None,
                        tool_result=None,
                        usage=None,
                    )
                async with self.runs.transaction() as conn:
                    await self.conversations.add_message(run["conversation_id"], assistant, conn=conn)
                    assistant_event = await self.runs.event(
                        run,
                        "assistant_message",
                        conn=conn,
                        step=steps,
                        message=assistant,
                        tool_call=None,
                        tool_result=None,
                        usage=reply.usage,
                        provider=model_config.get("provider") or settings.provider,
                        model=model_config.get("model") or settings.model,
                    )
                    await self.checkpoints.save(
                        run_id=run["id"],
                        conversation_id=run["conversation_id"],
                        sequence=steps,
                        state=self._checkpoint_state(
                            phase="assistant_ready",
                            model_messages=model_messages,
                            total=total,
                            next_step=steps,
                            plan_task_id=plan_task_id,
                            tools=tools,
                            model_calls=model_calls,
                            pending_tool_calls=reply.tool_calls,
                            tool_cursor=0,
                        ),
                        through_event_seq=assistant_event["event_seq"],
                        digest=assistant_event["runtime_digest"],
                        conn=conn,
                    )
                await self.runs.broadcast_event(assistant_event)
                plan_task_id = await self._execute_tool_calls(
                    run,
                    tools,
                    reply.tool_calls,
                    steps,
                    model_messages,
                    plan_task_id,
                    projection=projection,
                    order_calls=reply.tool_calls,
                )
                snapshot = await self.runs.runtime_event_store.latest(run["id"])
                await self.checkpoints.save(
                    run_id=run["id"],
                    conversation_id=run["conversation_id"],
                    sequence=steps,
                    state=self._checkpoint_state(
                        phase="model_pending",
                        model_messages=model_messages,
                        total=total,
                        next_step=steps + 1,
                        plan_task_id=plan_task_id,
                        tools=tools,
                        model_calls=model_calls,
                        pending_tool_calls=[],
                        tool_cursor=0,
                    ),
                    through_event_seq=snapshot["event_seq"],
                    digest=snapshot["digest"],
                )
            else:
                raise RuntimePolicyError(
                    f"Maximum step limit ({settings.max_steps}) reached",
                    stop_reason="max_steps",
                )
            return await self._finalize_run(run, final, steps, total, plan_task_id)
        except asyncio.CancelledError:
            run.update(status="cancelled", updated_at=now(), completed_at=now(), stop_reason="cancelled")
            await self.runs.save(run)
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
            await self.runs.save(run)
            await self.runs.event(
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
            await self._computer().end_run(run["id"])

    async def _finalize_run(
        self,
        run: dict[str, Any],
        final: str,
        steps: int,
        total: dict[str, int],
        plan_task_id: str | None,
    ) -> dict[str, Any]:
        run.update(status="completed", updated_at=now(), completed_at=now(), stop_reason="completed")
        await self.runs.save(run)
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
            "first_edit_step": run.get("first_edit_step"),
            "usage": total,
            "error": None,
            "plan_task_id": plan_task_id,
        }
        if run["mode"] == "plan" and not plan_task_id:
            task = await self.post_processor.create_plan_task(run, final)
            plan_task_id = task["id"]
            result["plan_task_id"] = plan_task_id
            final_message["content"] = final + "\n\n计划已保存，等待你确认后执行。"
        await self.runs.event(
            run,
            "agent_completed",
            message=final_message,
            tool_call=None,
            tool_result=None,
            usage=total,
            result=result,
            stop_reason="completed",
        )
        post_run = asyncio.create_task(self.post_run(run, result))
        self.track_background(post_run)
        return result

    async def _resolve_run_workspace(
        self,
        run: dict[str, Any],
        conversation: dict[str, Any],
    ) -> WorkspaceContext:
        workspace_path = run.get("workspace_path") or conversation.get("workspace_path")
        source = run.get("workspace_source") or ("conversation" if workspace_path else "none")
        if source not in {"conversation", "run_override", "legacy_default"}:
            source = "conversation" if workspace_path else "none"
        try:
            workspace = resolve_workspace(workspace_path, source=source)
        except WorkspaceError as exc:
            raise RpcFault(-32001, f"Workspace is unavailable: {exc}") from exc
        if run.get("workspace_path") != workspace.path or run.get("workspace_source") != workspace.source:
            run["workspace_path"] = workspace.path
            run["workspace_source"] = workspace.source
            await self.runs.set_workspace(run["id"], workspace.path, workspace.source)
        return workspace

    async def _call_model_step(
        self,
        run: dict[str, Any],
        model: ModelClient,
        request_messages: list[dict[str, Any]],
        base_tools: list[dict[str, Any]],
        extra_tools: list[dict[str, Any]],
        step: int,
        model_config: dict[str, Any],
        settings: Settings,
        total: dict[str, int],
        model_calls: int,
    ) -> tuple[ModelReply, int]:
        # Deltas update the live UI; the assembled assistant message below
        # is the durable conversation and trace record.
        async def on_text_delta(delta: str, step: int = step) -> None:
            await self._broadcast()(
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

        async def on_reasoning_delta(delta: str, step: int = step) -> None:
            await self._broadcast()(
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
            await self.runs.event(
                run,
                "run_budget_exceeded",
                step=step,
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
        return reply, model_calls

    @staticmethod
    def _checkpoint_state(
        *,
        phase: str,
        model_messages: list[dict[str, Any]],
        total: dict[str, int],
        next_step: int,
        plan_task_id: str | None,
        tools: ToolExecutor,
        model_calls: int,
        pending_tool_calls: list[dict[str, Any]],
        tool_cursor: int,
        completed_call_ids: list[str] | None = None,
        final: str | None = None,
    ) -> dict[str, Any]:
        state = {
            "phase": phase,
            "model_messages": model_messages,
            "usage": total,
            "next_step": next_step,
            "plan_task_id": plan_task_id,
            "activated_tools": sorted(tools.activation_steps),
            "tool_activation_steps": dict(tools.activation_steps),
            "model_calls": model_calls,
            "pending_tool_calls": pending_tool_calls,
            "tool_cursor": tool_cursor,
            "completed_call_ids": completed_call_ids or [],
        }
        if final is not None:
            state["final"] = final
        return state

    @staticmethod
    def _model_assistant_message(reply: ModelReply) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": reply.content or "",
            **(
                {
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": dumps(call["arguments"]),
                            },
                        }
                        for call in reply.tool_calls
                    ]
                }
                if reply.tool_calls
                else {}
            ),
        }

    async def _record_context_prepared(
        self,
        run: dict[str, Any],
        step_context: ContextDecision,
        metrics: StepMetrics,
        *,
        step: int,
        start_step: int,
        context_decision: ContextDecision | None,
        model_config: dict[str, Any],
        settings: Settings,
    ) -> None:
        await self.runs.event(
            run,
            "context_prepared",
            step=step,
            message=None,
            tool_call=None,
            tool_result=None,
            usage=None,
            provider=model_config.get("provider") or settings.provider,
            model=model_config.get("model") or settings.model,
            context={
                "estimated_tokens": metrics.prepared_tokens,
                "original_estimated_tokens": (
                    step_context.original_estimated_tokens + metrics.tool_schema_tokens
                    if step_context.original_estimated_tokens is not None
                    else metrics.prepared_tokens
                ),
                "prepared_input_tokens": metrics.prepared_tokens,
                "context_window": step_context.context_window,
                "max_input_tokens": step_context.max_input_tokens,
                "working_input_budget": step_context.working_input_budget,
                "tool_result_budget_tokens": step_context.tool_result_budget_tokens,
                "tool_schema_tokens_estimated": metrics.tool_schema_tokens,
                "breakdown": {
                    "system": metrics.system_tokens,
                    "messages": metrics.history_tokens,
                    "tool_results": metrics.tool_tokens,
                    "tool_schemas": metrics.tool_schema_tokens,
                },
                "tool_result_tokens_before": (
                    step_context.original_tool_result_tokens
                    if step_context.original_tool_result_tokens is not None
                    else metrics.tool_tokens
                ),
                "tool_result_tokens_after": (
                    step_context.prepared_tool_result_tokens
                    if step_context.prepared_tool_result_tokens is not None
                    else metrics.tool_tokens
                ),
                "compacted": bool(step_context.compacted)
                or bool(step == start_step and context_decision and context_decision.compacted),
                "compaction_stage": step_context.compaction_stage
                if step_context.compacted
                else (context_decision.compaction_stage if step == start_step and context_decision else "none"),
                "summary_validated": bool(step_context.summary_validated),
                "summary_updated": bool(step_context.summary_updated),
                "dropped_messages": step_context.dropped_messages,
            },
        )

    @staticmethod
    def _pending_calls_to_run(
        pending: list[dict[str, Any]],
        completed: Any,
        cursor: int,
    ) -> list[dict[str, Any]]:
        if isinstance(completed, list):
            done = {str(item) for item in completed}
            return [call for call in pending if str(call.get("id")) not in done]
        return pending[max(0, cursor) :]

    @staticmethod
    def _is_concurrent_safe(call: dict[str, Any]) -> bool:
        name = str(call.get("name") or "")
        if name == "http_request":
            arguments = call.get("arguments")
            method = str(arguments.get("method") or "GET").upper() if isinstance(arguments, dict) else "GET"
            return method in {"GET", "HEAD"}
        return name in CONCURRENT_READ_TOOLS

    async def _execute_tool_calls(
        self,
        run: dict[str, Any],
        tools: ToolExecutor,
        calls: list[dict[str, Any]],
        step: int,
        model_messages: list[dict[str, Any]],
        plan_task_id: str | None,
        *,
        projection: ToolProjection,
        order_calls: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if not calls:
            return plan_task_id
        seen_call_ids: set[str] = set()
        if len(calls) > 1 and all(self._is_concurrent_safe(call) for call in calls):
            outcomes: list[ToolCallOutcome | None] = list(
                await asyncio.gather(
                    *(
                        self._execute_tool_call(
                            run,
                            tools,
                            call,
                            step,
                            plan_task_id,
                            projection=projection,
                            seen_call_ids=seen_call_ids,
                        )
                        for call in calls
                    )
                )
            )
        else:
            outcomes = [None] * len(calls)
            for index, call in enumerate(calls):
                outcomes[index] = await self._execute_tool_call(
                    run,
                    tools,
                    call,
                    step,
                    plan_task_id,
                    projection=projection,
                    seen_call_ids=seen_call_ids,
                )
        for outcome in outcomes:
            if outcome is None:
                continue
            if outcome.plan_task_id is not None:
                plan_task_id = outcome.plan_task_id
            if outcome.tool_message is not None:
                model_messages.append(outcome.tool_message)
        if order_calls and len(order_calls) > 1:
            model_messages[:] = order_tool_messages(model_messages, order_calls)
        return plan_task_id

    async def _execute_tool_call(
        self,
        run: dict[str, Any],
        tools: ToolExecutor,
        call: dict[str, Any],
        step: int,
        plan_task_id: str | None,
        *,
        projection: ToolProjection,
        seen_call_ids: set[str],
    ) -> ToolCallOutcome:
        await self.runs.event(
            run,
            "tool_call",
            step=step,
            message=None,
            tool_call=call,
            tool_result=None,
            usage=None,
        )
        started = asyncio.get_running_loop().time()
        call_id = str(call.get("id") or "")
        name = str(call.get("name") or "")
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        policy = tool_policy(name, arguments)
        policy_value = f"{policy.side_effect}/{policy.recovery}"
        idempotency_key = f"{run['id']}:{call_id}"
        prepared = None
        dispatched = False
        output = ""
        success = False
        error: str | None = None
        error_code: str | None = None
        retryable = False
        if not call_id or call_id in seen_call_ids:
            error = "Duplicate or missing tool call id"
            error_code = "duplicate_call_id"
        else:
            seen_call_ids.add(call_id)
            try:
                prepared = await tools.preflight(
                    run["id"],
                    name,
                    arguments,
                    call_id,
                    mode=run["mode"],
                    projection=projection,
                    effective_step=step + 1,
                    arguments_error=call.get("arguments_error"),
                )
            except ToolError as exc:
                error, error_code, retryable = str(exc), exc.code, exc.retryable
            except Exception as exc:
                error, error_code = str(exc), "tool_error"
        if prepared is not None:
            try:
                execution = await self.runs.begin_tool(run, call, step)
            except ToolError as exc:
                prepared = None
                error, error_code, retryable = str(exc), exc.code, exc.retryable
            else:
                if execution.get("status") in {"succeeded", "failed"}:
                    return ToolCallOutcome(plan_task_id, None, None)
                if execution.get("status") == "unknown":
                    raise RpcFault(
                        -32002,
                        "Tool outcome is unknown; manual reconciliation is required",
                        {"run_id": run["id"], "tool_call_id": call_id},
                    )
                dispatched = True
        if prepared is not None:
            try:
                output = await prepared.dispatch()
                success, error, error_code, retryable = True, None, None, False
            except McpToolError as exc:
                output, success, error, error_code = exc.payload, False, str(exc), "mcp_error"
            except ToolError as exc:
                output, success, error, error_code = "", False, str(exc), exc.code
                retryable = exc.retryable
            except Exception as exc:
                output, success, error, error_code = "", False, str(exc), "tool_error"
        output_text = output if isinstance(output, str) else dumps(output)
        catalog_event: dict[str, Any] | None = None
        if success and name == "tool_catalog":
            try:
                payload = json.loads(output_text)
                catalog_event = {
                    "matching_count": payload.get("matching_count", 0),
                    "activated": payload.get("activated", []),
                    "match_names": [item.get("name") for item in payload.get("matches", [])[:8]],
                    "effective_step": payload.get("effective_step"),
                }
            except (json.JSONDecodeError, TypeError, AttributeError):
                catalog_event = None
        output_ref: dict[str, Any] | None = None
        model_output = output_text
        if success and output_text:
            try:
                archived = await self.offload.archive(output_text)
            except Exception:
                archived = None
            if archived:
                output_ref = {
                    "sha256": archived["sha256"],
                    "size_bytes": archived["size_bytes"],
                }
                model_output = self.offload.placeholder(archived)
        if dispatched:
            # Repeated computer failures and unchanged observations stop
            # the run before another ineffective action is attempted.
            self.computer_guard.record(
                run_id=run["id"],
                tool_name=name,
                success=success,
                output=output_text,
                error=error,
            )
        duration_ms = round((asyncio.get_running_loop().time() - started) * 1000)
        if success and name == "task_create":
            try:
                task_payload = json.loads(output_text)
                plan_task_id = task_payload.get("task", {}).get("id") or plan_task_id
            except json.JSONDecodeError:
                pass
        tool_message = {
            "role": "tool",
            "content": model_output or error or "Tool completed with no output.",
            "name": name,
            "tool_call_id": call_id,
        }
        async with self.runs.transaction() as conn:
            if dispatched:
                await self.runs.finish_tool(
                    conn,
                    run["id"],
                    call_id,
                    success=success,
                    output=output_text,
                    error=error,
                    error_code=error_code,
                )
            await self.conversations.add_message(run["conversation_id"], tool_message, conn=conn)
            result_event = await self.runs.event(
                run,
                "tool_result",
                conn=conn,
                step=step,
                message=None,
                tool_call=None,
                tool_result={
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "success": success,
                    "output": model_output,
                    "output_ref": output_ref,
                    "error": error,
                    "error_code": error_code,
                    "retryable": retryable,
                    "dispatched": dispatched,
                    "policy": policy_value,
                    "idempotency_key": idempotency_key,
                    "duration_ms": duration_ms,
                    "catalog": catalog_event,
                },
                usage=None,
            )
        await self.runs.broadcast_event(result_event)
        evidence_task = asyncio.create_task(
            self._record_evidence(
                run["id"],
                result_event["event_seq"],
                {
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "arguments": arguments,
                    "success": success,
                    "output": model_output,
                    "output_ref": output_ref,
                    "error": error,
                    "error_code": error_code,
                    "retryable": retryable,
                    "dispatched": dispatched,
                    "policy": policy_value,
                    "idempotency_key": idempotency_key,
                    "duration_ms": duration_ms,
                    "task_id": plan_task_id,
                    "task_step": step,
                },
            )
        )
        self.track_background(evidence_task)
        return ToolCallOutcome(plan_task_id, tool_message, result_event)

    async def _record_evidence(self, run_id: str, event_seq: int, data: dict[str, Any]) -> None:
        try:
            await self.evidence.record(
                run_id,
                "tool_result",
                data,
                evidence_id=f"ev:{run_id}:{event_seq}",
            )
        except Exception:
            # The runtime event remains the source of truth; startup backfill
            # will retry this derived index if the process exits early.
            return

    @staticmethod
    def history_for_model(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    def system_prompt(mode: str, workspace: WorkspaceContext | None = None) -> str:
        common = (
            "You are KoreAgent Next, a reliable local AI agent. Work toward the user's actual result, "
            "use tools when useful, keep durable task state current, publish deliverables, and never "
            "claim an action succeeded without evidence. "
            "Use tool_catalog to discover and activate optional capabilities before calling them; "
            "newly activated tools become callable on the next model step."
        )
        if workspace is not None and workspace.root is not None:
            common += (
                f" The active workspace is {workspace.root}. Resolve relative paths inside it and do not "
                "assume access outside that root."
            )
        else:
            common += (
                " No workspace is attached. Filesystem and shell tools are unavailable in this run; "
                "ask the user to attach a folder when file access is required."
            )
        if mode == "plan":
            return common + (
                " You are in PLAN MODE. Inspect and research only. Do not perform side effects. "
                "Create exactly one pending Task with task_create, including concrete ordered steps, "
                "constraints, and success criteria."
            )
        return common

    @staticmethod
    def skill_catalog(skills: list[dict[str, Any]]) -> str:
        selected: dict[str, dict[str, Any]] = {}
        for skill in sorted(skills, key=lambda item: item.get("scope") != "project"):
            if skill.get("enabled"):
                selected.setdefault(skill["name"], skill)
        lines = [
            f"- {skill['name']} ({skill.get('scope', 'project')}): {skill.get('description', '')}"
            for skill in selected.values()
        ]
        return "\n".join(lines)[:6_000]
