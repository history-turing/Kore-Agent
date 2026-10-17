from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .database import Database
from .util import now

Summarizer = Callable[[str, str | None], Awaitable[str]]


@dataclass(frozen=True)
class ContextDecision:
    messages: list[dict[str, Any]]
    estimated_tokens: int
    context_window: int
    max_input_tokens: int
    compacted: bool
    summary_updated: bool
    dropped_messages: int
    working_input_budget: int | None = None
    tool_result_budget_tokens: int | None = None
    compaction_stage: str = "none"
    summary_validated: bool = False
    original_estimated_tokens: int | None = None
    original_tool_result_tokens: int | None = None
    prepared_tool_result_tokens: int | None = None


class TokenEstimator:
    """Fast deterministic estimator used before provider-specific tokenization."""

    _word = re.compile(r"[A-Za-z0-9_]+|[^\s]", re.UNICODE)

    def count_text(self, text: str | None) -> int:
        if not text:
            return 0
        ascii_count = sum(1 for char in text if ord(char) < 128)
        non_ascii = len(text) - ascii_count
        return max(1, ascii_count // 4 + non_ascii + 4)

    def count_message(self, message: dict[str, Any]) -> int:
        total = 6 + self.count_text(str(message.get("content") or ""))
        total += self.count_text(str(message.get("reasoning") or ""))
        if message.get("tool_calls"):
            total += self.count_text(json.dumps(message["tool_calls"], ensure_ascii=False))
        return total

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        return sum(self.count_message(message) for message in messages) + 8


MODEL_WINDOWS = {
    "qwen": 131_072,
    "claude": 200_000,
    "gpt-4.1": 128_000,
    "gpt-5": 128_000,
    "deepseek": 64_000,
}


def context_window(provider: str, model: str) -> int:
    name = model.lower()
    for marker, window in MODEL_WINDOWS.items():
        if marker in name:
            return window
    if provider == "anthropic":
        return 200_000
    return 64_000


class ContextManager:
    def __init__(self, db: Database, *, tool_result_chars: int = 16_000, reserve_tokens: int = 8_192):
        self.db = db
        self.tool_result_chars = tool_result_chars
        self.reserve_tokens = reserve_tokens
        self.estimator = TokenEstimator()

    async def prepare(
        self,
        *,
        conversation_id: str,
        history: list[dict[str, Any]],
        provider: str,
        model: str,
        base_system: str,
        core_memory: str = "",
        memory_index: str = "",
        task_context: str = "",
        skill_catalog: str = "",
        summarizer: Summarizer | None = None,
    ) -> ContextDecision:
        window = context_window(provider, model)
        # Leave room for the model's reply and keep a smaller working target for
        # tool schemas and estimation error before trimming conversation history.
        max_input = max(8_192, min(window - self.reserve_tokens, int(window * 0.78)))
        working_input = max(4_096, int(max_input * 0.86))
        tool_result_budget = max(2_048, int(working_input * 0.25))
        summary_key = f"conversation_summary:{conversation_id}"
        saved = await self.db.get_json(summary_key, None)
        previous_summary = saved.get("summary") if isinstance(saved, dict) else None

        system_parts = [base_system]
        if core_memory.strip():
            system_parts.append("Core memory (stable facts and preferences):\n" + core_memory.strip())
        if memory_index.strip():
            system_parts.append("Long-term memory index. Use memory_read when details are needed:\n" + memory_index.strip())
        if task_context.strip():
            system_parts.append("Active task state:\n" + task_context.strip())
        if skill_catalog.strip():
            system_parts.append("Available skills. Use skill_read before following one:\n" + skill_catalog.strip())
        if previous_summary:
            system_parts.append("Rolling conversation summary:\n" + previous_summary)

        per_tool_chars = min(self.tool_result_chars, max(4_000, tool_result_budget * 3))
        normalized = [self._reduce_message(message, char_limit=per_tool_chars) for message in history]
        messages = [{"role": "system", "content": "\n\n".join(system_parts)}, *normalized]
        total = self.estimator.count_messages(messages)
        original_total = self.estimator.count_messages([messages[0], *history])
        original_tool_results = sum(
            self.estimator.count_message(message)
            for message in history
            if message.get("role") == "tool"
        )
        normalized_tool_results = sum(
            self.estimator.count_message(message)
            for message in normalized
            if message.get("role") == "tool"
        )
        if total <= working_input:
            return ContextDecision(
                messages,
                total,
                window,
                max_input,
                False,
                False,
                0,
                working_input,
                tool_result_budget,
                "none",
                bool(previous_summary),
                original_total,
                original_tool_results,
                normalized_tool_results,
            )

        # Preserve the recent exchange and summarize only the older prefix.
        keep_from = self._suffix_start(normalized, max(4_096, working_input // 2))
        older, recent = normalized[:keep_from], normalized[keep_from:]
        summary_updated = False
        if older:
            source = self._summary_source(older)
            if summarizer is not None:
                try:
                    generated = (await summarizer(source, previous_summary)).strip()
                except Exception:
                    generated = self._fallback_summary(source, previous_summary)
            else:
                generated = self._fallback_summary(source, previous_summary)
            if not self._valid_summary(generated, source):
                generated = self._fallback_summary(source, previous_summary)
            if generated:
                previous_summary = generated
                await self.db.set_json(
                    summary_key,
                    {
                        "summary": generated,
                        "covered_messages": len(older),
                        "updated_at": now(),
                        "provider": provider,
                        "model": model,
                    },
                )
                summary_updated = True

        compact_system = list(system_parts)
        compact_system = [part for part in compact_system if not part.startswith("Rolling conversation summary:")]
        if previous_summary:
            compact_system.append("Rolling conversation summary:\n" + previous_summary)
        messages = [{"role": "system", "content": "\n\n".join(compact_system)}, *recent]

        while len(messages) > 3 and self.estimator.count_messages(messages) > working_input:
            self._drop_first_round(messages)
        total = self.estimator.count_messages(messages)
        if total > max_input:
            raise ValueError(f"prepared context exceeds input budget: {total} > {max_input}")
        return ContextDecision(
            messages,
            total,
            window,
            max_input,
            True,
            summary_updated,
            len(older),
            working_input,
            tool_result_budget,
            "summary_then_history",
            bool(previous_summary),
            original_total,
            original_tool_results,
            sum(
                self.estimator.count_message(message)
                for message in messages
                if message.get("role") == "tool"
            ),
        )

    async def prepare_step(
        self,
        *,
        messages: list[dict[str, Any]],
        provider: str,
        model: str,
        tool_schema_tokens: int = 0,
        summarizer: Summarizer | None = None,
    ) -> ContextDecision:
        """Keep a growing tool loop within its input budget before each request.

        The returned messages are the model-facing projection. The conversation
        and tool-result records remain untouched so they can still be inspected.
        """
        if not messages or messages[0].get("role") != "system":
            raise ValueError("step context requires a leading system message")
        window = context_window(provider, model)
        max_input = max(8_192, min(window - self.reserve_tokens, int(window * 0.78)))
        working_input = max(4_096, int(max_input * 0.86))
        available = working_input - tool_schema_tokens
        if available <= 0:
            raise ValueError("tool schemas exceed the working input budget")
        original_total = self.estimator.count_messages(messages) + tool_schema_tokens
        original_tool_results = sum(
            self.estimator.count_message(item) for item in messages if item.get("role") == "tool"
        )
        per_tool_chars = min(self.tool_result_chars, max(1_000, int(available * 0.4) * 3))
        normalized = [messages[0], *(
            self._reduce_message(item, char_limit=per_tool_chars) for item in messages[1:]
        )]
        stage = "tool_results" if normalized != messages else "none"
        if self.estimator.count_messages(normalized) > available:
            base_system, marker, previous_summary = str(normalized[0].get("content") or "").partition(
                "\n\nRun context summary:\n"
            )
            if not marker:
                previous_summary = ""
            history = normalized[1:]
            keep_from = self._suffix_start(history, max(1_024, available // 2))
            older, recent = history[:keep_from], history[keep_from:]
            if not older:
                raise ValueError("current tool round exceeds the input budget")
            source = self._summary_source(older)
            if len(source) > 24_000:
                source = source[:12_000] + "\n...[older context clipped]...\n" + source[-12_000:]
            try:
                summary = (await summarizer(source, previous_summary or None)).strip() if summarizer else ""
            except Exception:
                summary = ""
            if not self._valid_summary(summary, source):
                summary = self._fallback_summary(source, previous_summary or None)
            # A summary must also leave room for recent messages and tool schemas.
            summary = summary[-min(12_000, max(1_000, available * 2)) :]
            normalized = [
                {**normalized[0], "content": base_system + "\n\nRun context summary:\n" + summary},
                *recent,
            ]
            stage = "step_summary"
        total = self.estimator.count_messages(normalized) + tool_schema_tokens
        if total > max_input:
            raise ValueError(f"step context exceeds input budget: {total} > {max_input}")
        return ContextDecision(
            messages=normalized,
            estimated_tokens=total,
            context_window=window,
            max_input_tokens=max_input,
            compacted=stage != "none",
            summary_updated=stage == "step_summary",
            dropped_messages=len(messages) - len(normalized),
            working_input_budget=working_input,
            tool_result_budget_tokens=max(2_048, int(working_input * 0.25)),
            compaction_stage=stage,
            summary_validated=stage == "step_summary",
            original_estimated_tokens=original_total,
            original_tool_result_tokens=original_tool_results,
            prepared_tool_result_tokens=sum(
                self.estimator.count_message(item) for item in normalized if item.get("role") == "tool"
            ),
        )

    def _reduce_message(self, message: dict[str, Any], *, char_limit: int | None = None) -> dict[str, Any]:
        item = dict(message)
        content = item.get("content")
        limit = char_limit or self.tool_result_chars
        if item.get("role") == "tool" and isinstance(content, str) and len(content) > limit:
            # Keep both ends of long tool output, where headers and summaries
            # often appear, while limiting its model-facing size.
            head = content[: limit // 2]
            tail = content[-limit // 2 :]
            item["content"] = f"{head}\n\n[tool output compacted: {len(content)} characters]\n\n{tail}"
        return item

    def _suffix_start(self, messages: list[dict[str, Any]], target_tokens: int) -> int:
        used = 0
        start = len(messages)
        for index in range(len(messages) - 1, -1, -1):
            cost = self.estimator.count_message(messages[index])
            if used + cost > target_tokens and start < len(messages):
                break
            used += cost
            start = index
        # A provider cannot accept a tool result without its preceding call.
        while start < len(messages) and messages[start].get("role") == "tool":
            start += 1
        return start

    def _drop_first_round(self, messages: list[dict[str, Any]]) -> None:
        if len(messages) <= 3:
            return
        del messages[1]
        # Keep assistant tool calls and their tool results atomic. Drop through the
        # next user boundary instead of leaving malformed provider history behind.
        while len(messages) > 2 and messages[1].get("role") != "user":
            del messages[1]

    @staticmethod
    def _summary_source(messages: list[dict[str, Any]]) -> str:
        chunks = []
        for message in messages:
            role = str(message.get("role", "unknown")).upper()
            content = str(message.get("content") or "")
            if len(content) > 4_000:
                content = content[:2_000] + "\n...[truncated]...\n" + content[-2_000:]
            chunks.append(f"{role}: {content}")
        return "\n\n".join(chunks)

    @staticmethod
    def _fallback_summary(source: str, previous: str | None) -> str:
        prefix = (previous.strip() + "\n\n") if previous else ""
        clipped = source if len(source) <= 12_000 else source[:6_000] + "\n...[summary source clipped]...\n" + source[-6_000:]
        return (prefix + clipped)[-18_000:]

    @staticmethod
    def _valid_summary(summary: str, source: str) -> bool:
        if not summary.strip() or len(summary) > 24_000:
            return False
        source_terms = {part.lower() for part in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}", source)}
        summary_terms = {part.lower() for part in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}", summary)}
        if not source_terms:
            return True
        return bool(source_terms & summary_terms)
