from __future__ import annotations

from kore_next.runtime_policy import RunBudget, RunBudgetConfig


def budget() -> RunBudget:
    return RunBudget(
        RunBudgetConfig(
            warning_model_calls=5,
            finalization_model_calls=9,
            hard_model_calls=10,
        )
    )


def test_budget_phase_progression():
    run_budget = budget()
    assert run_budget.phase({}, 0).name == "explore"
    assert run_budget.phase({}, 4).name == "implement"
    assert run_budget.phase({}, 7).name == "verify"
    assert run_budget.phase({}, 9).name == "finalize"


def test_budget_phase_instruction_is_actionable():
    run_budget = budget()
    implement = run_budget.phase({}, 4)
    assert "smallest plausible patch" in implement.instruction
    assert implement.model_calls_remaining == 6
    verify = run_budget.phase({}, 7)
    assert "targeted test" in verify.instruction


def test_token_pressure_can_advance_phase():
    run_budget = budget()
    phase = run_budget.phase({"input_tokens": 50_000, "output_tokens": 50_000}, 0)
    assert phase.name == "verify"
    assert phase.progress >= 0.8
