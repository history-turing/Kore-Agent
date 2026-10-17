"""Build a run's usage report from persisted model-call events.

Completion and budget events can repeat the same usage payload. Only one event
per main-model step is counted, so opening an old trace cannot inflate totals.
"""

from __future__ import annotations

from typing import Any

from .runtime_policy import RunBudget

USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
CACHE_FIELDS = (
    "cached_input_tokens",
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
)
AUXILIARY_ROLES = ("context_summary", "memory_reflection", "memory_maintenance", "skill_learning")


def _empty_usage() -> dict[str, int | None]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "model_calls": 0,
        **{field: None for field in CACHE_FIELDS},
    }


def _aggregate_usages(raw_usages: list[dict[str, Any]]) -> dict[str, int | None]:
    total = _empty_usage()
    total["model_calls"] = len(raw_usages)
    for raw in raw_usages:
        for field in USAGE_FIELDS:
            value = int(raw.get(field) or 0)
            if field == "total_tokens" and value == 0:
                value = int(raw.get("input_tokens") or 0) + int(raw.get("output_tokens") or 0)
            total[field] = int(total[field] or 0) + value
    for field in CACHE_FIELDS:
        if raw_usages and all(raw.get(field) is not None for raw in raw_usages):
            total[field] = sum(int(raw[field]) for raw in raw_usages)
    return total


def summarize_run_usage(events: list[dict[str, Any]], budget: RunBudget) -> dict[str, Any]:
    auxiliary_calls: dict[str, list[dict[str, Any]]] = {role: [] for role in AUXILIARY_ROLES}
    main_by_step: dict[int, dict[str, Any]] = {}
    inherited_usage: dict[str, Any] = {}
    inherited_model_calls = 0
    for event in events:
        if event.get("type") == "run_recovered":
            inherited_usage = event.get("inherited_usage") or {}
            inherited_model_calls = int(event.get("inherited_model_calls") or 0)
        elif event.get("type") == "assistant_message" and isinstance(event.get("usage"), dict):
            main_by_step[int(event.get("step") or 0)] = event["usage"]
        elif event.get("type") == "run_budget_exceeded" and isinstance(event.get("usage"), dict):
            main_by_step.setdefault(int(event.get("step") or 0), event["usage"])
        elif event.get("type") == "auxiliary_model_completed" and isinstance(event.get("usage"), dict):
            role = event.get("role")
            if role in auxiliary_calls:
                auxiliary_calls[role].append(event["usage"])

    main = _aggregate_usages(list(main_by_step.values()))
    auxiliary = {role: _aggregate_usages(calls) for role, calls in auxiliary_calls.items()}
    provider_total = _aggregate_usages([*main_by_step.values(), *(call for calls in auxiliary_calls.values() for call in calls)])

    budget_usage = {
        field: int(main[field] or 0) + int(inherited_usage.get(field) or 0)
        for field in USAGE_FIELDS
    }
    decision = budget.evaluate(budget_usage, int(main["model_calls"] or 0) + inherited_model_calls)
    budget_info = budget.event_fields(decision)
    for event in events:
        if event.get("type") in {"run_budget_warning", "run_budget_finalizing", "run_budget_exceeded"}:
            for key in tuple(budget_info):
                if key in event:
                    budget_info[key] = event[key]

    return {
        "main_agent": main,
        **auxiliary,
        "provider_total": provider_total,
        "budget": budget_info,
    }
