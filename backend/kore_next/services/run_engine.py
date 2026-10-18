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
from ..evidence import EvidenceStore
from ..mcp import McpManager, McpToolError
from ..memory import MemoryService
from ..model import ModelClient
from ..model.types import ModelReply
from ..rpc import RpcFault
from ..runtime_policy import RepeatedToolCallGuard, RunBudget, RuntimePolicyError
from ..tasks import TaskService
from ..tools import ToolExecutor
from ..util import dumps, now, uid
from .approvals import ApprovalService
from .artifacts import ArtifactService
from .conversations import ConversationService
from .model_settings import ModelSettingsService
from .run_postprocessing import RunPostProcessor
from .runs import RunStore
from .tool_router import ServiceToolRouter

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class StepMetrics:
    prepared_tokens: int
    system_tokens: int
    tool_tokens: int
    history_tokens: int
    tool_schema_tokens: int


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
                history = (await self.conversations.get({"conversation_id": run["conversation_id"]}))["messages"]
                prepared_history = self.history_for_model(history)
                base_system = self.system_prompt(run["mode"])
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
            tools = ToolExecutor(
                settings,
                self.approvals.request,
                self.artifacts.publish,
                self.memory.create,
                self._mcp_manager(),
                self._computer(),
                self.tool_router,
            )
            if recovery_state:
                tools.activated_tools.update(recovery_state.get("activated_tools", []))
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
            if phase in {"assistant_ready", "tools_running"}:
                resume_step = start_step
                for index in range(tool_cursor, len(pending_tool_calls)):
                    call = pending_tool_calls[index]
                    plan_task_id, _ = await self._execute_tool_call(
                        run,
                        tools,
                        call,
                        resume_step,
                        model_messages,
                        plan_task_id,
                        tool_cursor=index,
                        pending_tool_calls=pending_tool_calls,
                        total=total,
                        model_calls=model_calls,
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
                base_tools, extra_tools = tools.tool_definitions(run["mode"])
                if force_final:
                    base_tools = []
                    extra_tools = []
                estimator = self.context.estimator
                tool_schema_tokens = (
                    estimator.count_text(dumps([*base_tools, *extra_tools])) if base_tools or extra_tools else 0
                )
                final_instruction = {
                    "role": "system",
                    "content": (
                        "The run budget is closing. Return the best truthful final answer now. "
                        "Do not call tools. State incomplete work explicitly."
                    ),
                }
                step_context = await self.context.prepare_step(
                    messages=model_messages,
                    provider=str(model_config.get("provider") or settings.provider),
                    model=str(model_config.get("model") or settings.model),
                    tool_schema_tokens=tool_schema_tokens
                    + (estimator.count_message(final_instruction) if force_final else 0),
                    summarizer=lambda source, previous: self.post_processor.summarize_context(
                        source, previous, run=run
                    ),
                )
                model_messages = step_context.messages
                request_messages = [*model_messages, final_instruction] if force_final else model_messages
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
                for index, call in enumerate(reply.tool_calls):
                    plan_task_id, _ = await self._execute_tool_call(
                        run,
                        tools,
                        call,
                        steps,
                        model_messages,
                        plan_task_id,
                        tool_cursor=index,
                        pending_tool_calls=reply.tool_calls,
                        total=total,
                        model_calls=model_calls,
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
        final: str | None = None,
    ) -> dict[str, Any]:
        state = {
            "phase": phase,
            "model_messages": model_messages,
            "usage": total,
            "next_step": next_step,
            "plan_task_id": plan_task_id,
            "activated_tools": sorted(tools.activated_tools),
            "model_calls": model_calls,
            "pending_tool_calls": pending_tool_calls,
            "tool_cursor": tool_cursor,
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

    async def _execute_tool_call(
        self,
        run: dict[str, Any],
        tools: ToolExecutor,
        call: dict[str, Any],
        step: int,
        model_messages: list[dict[str, Any]],
        plan_task_id: str | None,
        *,
        tool_cursor: int,
        pending_tool_calls: list[dict[str, Any]],
        total: dict[str, int],
        model_calls: int,
    ) -> tuple[str | None, int]:
        await self.runs.event(
            run,
            "tool_call",
            step=step,
            message=None,
            tool_call=call,
            tool_result=None,
            usage=None,
        )
        execution = await self.runs.begin_tool(run, call, step)
        if execution.get("status") in {"succeeded", "failed"}:
            return plan_task_id, tool_cursor + 1
        if execution.get("status") == "unknown":
            raise RpcFault(
                -32002,
                "Tool outcome is unknown; manual reconciliation is required",
                {"run_id": run["id"], "tool_call_id": call["id"]},
            )
        started = asyncio.get_running_loop().time()
        try:
            output = await tools.execute(run["id"], call["name"], call["arguments"], call["id"], mode=run["mode"])
            success, error = True, None
        except McpToolError as exc:
            output, success, error = exc.payload, False, str(exc)
        except Exception as exc:
            output, success, error = "", False, str(exc)
        output_text = output if isinstance(output, str) else dumps(output)
        # Repeated computer failures and unchanged observations stop
        # the run before another ineffective action is attempted.
        self.computer_guard.record(
            run_id=run["id"],
            tool_name=call["name"],
            success=success,
            output=output_text,
            error=error,
        )
        duration_ms = round((asyncio.get_running_loop().time() - started) * 1000)
        if success and call["name"] == "task_create":
            try:
                task_payload = json.loads(output_text)
                plan_task_id = task_payload.get("task", {}).get("id") or plan_task_id
            except json.JSONDecodeError:
                pass
        tool_message = {
            "role": "tool",
            "content": output_text or error,
            "name": call["name"],
            "tool_call_id": call["id"],
        }
        next_cursor = tool_cursor + 1
        model_messages.append(tool_message)
        async with self.runs.transaction() as conn:
            await self.runs.finish_tool(
                conn,
                run["id"],
                call["id"],
                success=success,
                output=output_text,
                error=error,
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
                    "tool_call_id": call["id"],
                    "tool_name": call["name"],
                    "success": success,
                    "output": output_text,
                    "error": error,
                    "duration_ms": duration_ms,
                },
                usage=None,
            )
            await self.checkpoints.save(
                run_id=run["id"],
                conversation_id=run["conversation_id"],
                sequence=step,
                state=self._checkpoint_state(
                    phase="tools_running",
                    model_messages=model_messages,
                    total=total,
                    next_step=step,
                    plan_task_id=plan_task_id,
                    tools=tools,
                    model_calls=model_calls,
                    pending_tool_calls=pending_tool_calls,
                    tool_cursor=next_cursor,
                ),
                through_event_seq=result_event["event_seq"],
                digest=result_event["runtime_digest"],
                conn=conn,
            )
        await self.runs.broadcast_event(result_event)
        await self.evidence.record(
            run["id"],
            "tool_result",
            {
                "tool_call_id": call["id"],
                "tool_name": call["name"],
                "arguments": call["arguments"],
                "success": success,
                "output": output_text,
                "error": error,
                "duration_ms": duration_ms,
                "task_id": plan_task_id,
                "task_step": step,
            },
        )
        return plan_task_id, next_cursor

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
    def system_prompt(mode: str) -> str:
        common = (
            "You are KoreAgent Next, a reliable local AI agent. Work toward the user's actual result, "
            "use tools when useful, keep durable task state current, publish deliverables, and never "
            "claim an action succeeded without evidence. "
            "Use tool_catalog to discover and activate optional capabilities before calling them."
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
