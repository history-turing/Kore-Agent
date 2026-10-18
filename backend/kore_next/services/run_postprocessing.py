from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..memory import MemoryService
from ..model import ModelClient
from ..skill_learning import SkillLearningService
from ..tasks import TaskService
from .model_settings import ModelSettingsService
from .runs import RunStore


class RunPostProcessor:
    """Context summaries, auxiliary model usage, reflection, and plan task extraction."""

    def __init__(
        self,
        *,
        settings: Callable[[], Settings],
        model_settings: ModelSettingsService,
        runs: RunStore,
        memory: MemoryService,
        tasks: TaskService,
        skills: Callable[[], Awaitable[list[dict[str, Any]]]],
        skill_learning: SkillLearningService,
    ):
        self._settings = settings
        self.model_settings = model_settings
        self.runs = runs
        self.memory = memory
        self.tasks = tasks
        self.skills = skills
        self.skill_learning = skill_learning

    async def summarize_context(self, source: str, previous: str | None, *, run: dict[str, Any] | None = None) -> str:
        config = await self.model_settings.role_config("summary")
        if config is None:
            return (previous or "") + "\n" + source[-12_000:]
        prompt = (
            "Create a compact factual rolling summary for continuing this conversation. "
            "Preserve user goals, constraints, decisions, unresolved issues, file paths, "
            "tool outcomes and next steps. Do not add facts.\n\n"
            f"PREVIOUS SUMMARY:\n{previous or '(none)'}\n\nNEW HISTORY:\n{source}"
        )
        reply = await ModelClient(self._settings(), config).complete([{"role": "user", "content": prompt}], [], [])
        if run is not None:
            await self.record_auxiliary_usage(run, "context_summary", reply.usage, config)
        return reply.content or source[-12_000:]

    async def record_auxiliary_usage(
        self, run: dict[str, Any], role: str, usage: dict[str, int], config: dict[str, Any]
    ) -> None:
        settings = self._settings()
        await self.runs.event(
            run,
            "auxiliary_model_completed",
            role=role,
            message=None,
            tool_call=None,
            tool_result=None,
            usage=usage,
            provider=config.get("provider") or settings.provider,
            model=config.get("model") or settings.model,
        )

    async def run(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        settings = self._settings()
        try:
            await self.runs.event(run, "post_run_started", message=None, tool_call=None, tool_result=None, usage=None)
            config = await self.model_settings.role_config("reflection")
            memories: list[dict[str, Any]] = []
            if (
                config is not None
                and result.get("final_message", {}).get("content")
                and self.memory.should_reflect(run["user_message"], result["final_message"]["content"])
            ):

                async def reflect_model(prompt: str) -> str:
                    reply = await ModelClient(settings, config).complete([{"role": "user", "content": prompt}], [], [])
                    await self.record_auxiliary_usage(run, "memory_reflection", reply.usage, config)
                    return reply.content or "{}"

                memories = await self.memory.reflect(
                    user_text=run["user_message"],
                    assistant_text=result["final_message"]["content"],
                    model=reflect_model,
                )
            maintenance_config = await self.model_settings.role_config("maintenance")

            async def maintenance_model(prompt: str) -> str:
                assert maintenance_config is not None
                reply = await ModelClient(settings, maintenance_config).complete(
                    [{"role": "user", "content": prompt}], [], []
                )
                await self.record_auxiliary_usage(run, "memory_maintenance", reply.usage, maintenance_config)
                return reply.content or "{}"

            await self.memory.maintain(maintenance_model if maintenance_config is not None else None)
            distill_config = await self.model_settings.role_config("skill_learning")

            async def distill_model(prompt: str) -> str:
                if distill_config is None:
                    return "{}"
                reply = await ModelClient(settings, distill_config).complete(
                    [{"role": "user", "content": prompt}], [], []
                )
                await self.record_auxiliary_usage(run, "skill_learning", reply.usage, distill_config)
                return reply.content or "{}"

            candidate = await self.skill_learning.learn(
                run,
                model=distill_model if distill_config is not None else None,
                existing_skills=await self.skills(),
            )
            await self.runs.event(
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
            await self.runs.event(
                run,
                "post_run_failed",
                message=None,
                tool_call=None,
                tool_result=None,
                usage=None,
                error={"type": type(exc).__name__, "message": str(exc)},
            )

    async def create_plan_task(self, run: dict[str, Any], content: str) -> dict[str, Any]:
        steps = []
        for line in content.splitlines():
            clean = line.strip()
            if clean.startswith(("- ", "* ")):
                steps.append({"title": clean[2:].strip()})
            elif len(clean) > 3 and clean[0].isdigit() and clean[1:3] in {". ", "、"}:
                steps.append({"title": clean[3:].strip()})
        if not steps:
            steps = [{"title": "按已确认的计划完成目标", "description": content[:4_000]}]
        return await self.tasks.create(
            title=run["user_message"][:80],
            description=content,
            goal=run["user_message"],
            steps=steps[:30],
            conversation_id=run["conversation_id"],
            run_id=run["id"],
            status="pending",
        )
