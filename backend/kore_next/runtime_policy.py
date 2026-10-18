from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class RuntimePolicyError(RuntimeError):
    def __init__(self, message: str, *, stop_reason: str) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason


@dataclass(frozen=True)
class BudgetDecision:
    status: str
    reason: str | None
    chargeable_tokens: int
    model_calls: int

    @property
    def should_warn(self) -> bool:
        return self.status == "warning"

    @property
    def should_finalize(self) -> bool:
        return self.status == "finalizing"

    @property
    def exceeded(self) -> bool:
        return self.status == "exceeded"


@dataclass(frozen=True)
class BudgetPhase:
    """A coarse execution phase used to steer the model before it runs out."""

    name: str
    progress: float
    model_calls_remaining: int
    instruction: str


@dataclass(frozen=True)
class RunBudgetConfig:
    warning_tokens: int = 80_000
    finalization_tokens: int = 100_000
    hard_tokens: int = 120_000
    warning_model_calls: int = 16
    finalization_model_calls: int = 20
    hard_model_calls: int = 24

    def __post_init__(self) -> None:
        if not (0 < self.warning_tokens < self.finalization_tokens < self.hard_tokens):
            raise ValueError("token budget thresholds must increase")
        if not (0 < self.warning_model_calls < self.finalization_model_calls < self.hard_model_calls):
            raise ValueError("model call budget thresholds must increase")


class RunBudget:
    def __init__(self, config: RunBudgetConfig | None = None) -> None:
        self.config = config or RunBudgetConfig()

    def evaluate(self, usage: dict[str, int], model_calls: int) -> BudgetDecision:
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        if tokens >= self.config.hard_tokens or model_calls >= self.config.hard_model_calls:
            reason = "tokens" if tokens >= self.config.hard_tokens else "model_calls"
            return BudgetDecision("exceeded", reason, tokens, model_calls)
        if tokens >= self.config.finalization_tokens or model_calls >= self.config.finalization_model_calls:
            reason = "tokens" if tokens >= self.config.finalization_tokens else "model_calls"
            return BudgetDecision("finalizing", reason, tokens, model_calls)
        if tokens >= self.config.warning_tokens or model_calls >= self.config.warning_model_calls:
            reason = "tokens" if tokens >= self.config.warning_tokens else "model_calls"
            return BudgetDecision("warning", reason, tokens, model_calls)
        return BudgetDecision("normal", None, tokens, model_calls)

    def phase(self, usage: dict[str, int], model_calls: int) -> BudgetPhase:
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        token_progress = tokens / max(1, self.config.hard_tokens)
        call_progress = model_calls / max(1, self.config.hard_model_calls)
        progress = min(1.0, max(token_progress, call_progress))
        remaining = max(0, self.config.hard_model_calls - model_calls)
        if progress >= 0.9:
            name = "finalize"
            instruction = (
                "Budget phase: finalize. Return the best truthful final answer now. "
                "Do not call tools; state any incomplete work explicitly."
            )
        elif progress >= 0.7:
            name = "verify"
            instruction = (
                f"Budget phase: verify ({remaining} model calls left). "
                "Run one targeted test or reproduction for the changed area, then return the best verified patch."
            )
        elif progress >= 0.4:
            name = "implement"
            instruction = (
                f"Budget phase: implement ({remaining} model calls left). "
                "Write the smallest plausible patch now. Use targeted reads and "
                "avoid broad searches or full test suites."
            )
        else:
            name = "explore"
            instruction = (
                f"Budget phase: explore ({remaining} model calls left). "
                "Locate the relevant code with targeted searches and begin the "
                "smallest patch by about 40% of the budget. Avoid full test suites "
                "and whole-repository scans."
            )
        return BudgetPhase(name, round(progress, 3), remaining, instruction)

    def event_fields(self, decision: BudgetDecision) -> dict[str, Any]:
        return {
            "run_budget_status": decision.status,
            "run_budget_reason": decision.reason,
            "run_budget_chargeable_tokens": decision.chargeable_tokens,
            "run_budget_model_calls": decision.model_calls,
            "run_budget_warning_tokens": self.config.warning_tokens,
            "run_budget_finalization_tokens": self.config.finalization_tokens,
            "run_budget_hard_tokens": self.config.hard_tokens,
            "run_budget_warning_model_calls": self.config.warning_model_calls,
            "run_budget_finalization_model_calls": self.config.finalization_model_calls,
            "run_budget_hard_model_calls": self.config.hard_model_calls,
        }


class RepeatedToolCallGuard:
    """Stops the model after three identical consecutive tool rounds."""

    def __init__(self, limit: int = 3) -> None:
        self.limit = limit
        self._last_signature: str | None = None
        self._count = 0

    def observe(self, calls: list[dict[str, Any]]) -> None:
        normalized = [
            {
                "name": str(call.get("name") or ""),
                "arguments": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
            }
            for call in calls
        ]
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        signature = hashlib.sha256(encoded.encode()).hexdigest()
        if signature == self._last_signature:
            self._count += 1
        else:
            self._last_signature = signature
            self._count = 1
        if self._count >= self.limit:
            names = ", ".join(item["name"] for item in normalized) or "unknown"
            raise RuntimePolicyError(
                f"Repeated tool call loop detected: {names}",
                stop_reason="repeated_tool_call",
            )
