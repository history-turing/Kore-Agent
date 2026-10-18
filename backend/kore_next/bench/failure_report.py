"""Join benchmark run diagnostics with official SWE-bench outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_summary(path: Path) -> list[dict[str, Any]]:
    target = path / "summary.json" if path.is_dir() else path
    if not target.is_file():
        raise SystemExit(f"Summary not found: {target}")
    data = json.loads(target.read_text(encoding="utf-8"))
    return list(data.get("results") or [])


def _load_eval(path: Path) -> dict[str, Any]:
    target = path
    if path.is_dir():
        reports = sorted(path.glob("*.json"))
        if not reports:
            raise SystemExit(f"No evaluation report in {path}")
        target = reports[0]
    return json.loads(target.read_text(encoding="utf-8"))


def build_report(
    summaries: list[dict[str, Any]],
    evaluation: dict[str, Any],
) -> list[dict[str, Any]]:
    resolved = set(evaluation.get("resolved_ids") or [])
    unresolved = set(evaluation.get("unresolved_ids") or [])
    empty = set(evaluation.get("empty_patch_ids") or [])
    rows = []
    for result in summaries:
        instance_id = str(result.get("instance_id"))
        if instance_id in resolved:
            outcome = "resolved"
        elif instance_id in unresolved:
            outcome = "unresolved"
        elif instance_id in empty:
            outcome = "empty_patch"
        else:
            outcome = "not_evaluated"
        rows.append(
            {
                "instance_id": instance_id,
                "outcome": outcome,
                "failure_class": result.get("failure_class"),
                "stop_reason": result.get("stop_reason"),
                "status": result.get("status"),
                "patch_bytes": result.get("patch_bytes"),
                "steps": result.get("steps"),
                "model_calls": result.get("model_calls"),
                "tokens": result.get("tokens"),
                "first_edit_step": result.get("first_edit_step"),
                "blocked_tool_calls": result.get("blocked_tool_calls"),
                "tool_seconds": result.get("tool_seconds"),
                "final_message": str(result.get("final_message") or "")[:500],
            }
        )
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Explain each SWE-bench failure")
    parser.add_argument("--summary", action="append", required=True, help="Run directory or summary.json")
    parser.add_argument("--eval", required=True, help="Official evaluation report JSON or directory")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    summaries: list[dict[str, Any]] = []
    for raw in args.summary:
        summaries.extend(_load_summary(Path(raw).expanduser()))
    rows = build_report(summaries, _load_eval(Path(args.eval).expanduser()))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    for row in rows:
        print(
            f"{row['instance_id']:32s} {row['outcome']:12s} {str(row['failure_class']):16s} "
            f"stop={str(row['stop_reason']):16s} calls={row['model_calls']} tokens={row['tokens']} "
            f"first_edit={row['first_edit_step']} blocked={row['blocked_tool_calls']}"
        )


if __name__ == "__main__":
    main()
