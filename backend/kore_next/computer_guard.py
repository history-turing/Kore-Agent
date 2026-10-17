from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .runtime_policy import RuntimePolicyError


@dataclass
class _RunState:
    last_failure: str | None = None
    repeated_failures: int = 0
    last_observation: str | None = None
    unchanged_after_actions: int = 0
    action_since_observe: bool = False


class ComputerStagnationGuard:
    def __init__(self, *, failure_limit: int = 3, stagnation_limit: int = 3) -> None:
        self.failure_limit = failure_limit
        self.stagnation_limit = stagnation_limit
        self._states: dict[str, _RunState] = {}

    def record(
        self,
        *,
        run_id: str,
        tool_name: str,
        success: bool,
        output: str,
        error: str | None,
    ) -> None:
        if not tool_name.startswith("computer_"):
            return
        state = self._states.setdefault(run_id, _RunState())
        if not success:
            code = self._failure_code(error)
            state.repeated_failures = state.repeated_failures + 1 if code == state.last_failure else 1
            state.last_failure = code
            if state.repeated_failures >= self.failure_limit:
                raise RuntimePolicyError(
                    f"Computer actions halted after repeated {code} failures; observe or change strategy.",
                    stop_reason="computer_stagnation",
                )
            return
        state.last_failure = None
        state.repeated_failures = 0
        if tool_name != "computer_observe":
            state.action_since_observe = True
            return
        fingerprint = self._observation_fingerprint(output)
        # An identical observation counts as stagnation only after an action;
        # repeated read-only observations are harmless.
        if state.action_since_observe and fingerprint and fingerprint == state.last_observation:
            state.unchanged_after_actions += 1
        else:
            state.unchanged_after_actions = 0
        state.action_since_observe = False
        state.last_observation = fingerprint
        if state.unchanged_after_actions >= self.stagnation_limit:
            raise RuntimePolicyError(
                "Computer actions halted because fresh observations did not change.",
                stop_reason="computer_stagnation",
            )

    def clear(self, run_id: str) -> None:
        self._states.pop(run_id, None)

    @staticmethod
    def _failure_code(error: str | None) -> str:
        value = (error or "computer_error").split(":", 1)[0].strip().lower()
        return value or "computer_error"

    @staticmethod
    def _observation_fingerprint(raw: str) -> str | None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        stable = {
            # Fingerprint structured UI state; screenshot metadata and timestamps
            # must not count as progress when the controls remain unchanged.
            "active_app": payload.get("active_app"),
            "windows": payload.get("windows"),
            "elements": payload.get("elements"),
        }
        encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()
