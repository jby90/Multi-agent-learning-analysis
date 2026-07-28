"""Run and persist the approved 40-case by 3-run Text2SQL benchmark."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

from eval.text2sql_regression import (
    CASE_PATH,
    RegressionCase,
    RegressionReport,
    assert_accuracy_gates,
    benchmark_payload,
    evaluate_three_runs,
    load_cases,
    routing_audit_errors,
)
from orchestrator.runtime import build_verification_agent


BASELINE_COMMIT = "text2sql-baseline"
EXPECTED_CASE_COUNT = 40
ROUTED_LEVERS = frozenset(
    {
        "family_routing",
        "context_cache_implicit",
        "context_cache_explicit",
        "connection_warmup",
    }
)


def current_git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _live_answer(case: RegressionCase, run_index: int) -> Mapping[str, Any]:
    trace_id = f"eval-{case.case_id.lower()}-run-{run_index + 1}"
    draft = build_verification_agent(trace_id).answer(case.question)
    content = draft["payload"]["content"]
    print(
        f"LIVE run={run_index + 1} case={case.case_id} "
        f"expected={case.family} actual={content.get('family')} "
        f"event={content.get('event')} latency_ms={draft.get('latency_ms')}",
        flush=True,
    )
    return draft


def run_live_benchmark(
    *,
    lever: str,
    baseline_commit: str,
    answer: Callable[[RegressionCase, int], Mapping[str, Any]] = _live_answer,
) -> tuple[RegressionReport, dict[str, Any]]:
    cases = load_cases(CASE_PATH)
    if len(cases) != EXPECTED_CASE_COUNT:
        raise ValueError(
            f"expected {EXPECTED_CASE_COUNT} Text2SQL cases, got {len(cases)}"
        )
    report = evaluate_three_runs(cases, answer)
    payload = benchmark_payload(
        report,
        lever=lever,
        baseline_commit=baseline_commit,
        implementation_commit=current_git_commit(),
        measured_at=datetime.now(timezone.utc).isoformat(),
        extra={"case_count": len(cases), "run_count": 3},
    )
    return report, payload


def write_payload(output: Path, payload: Mapping[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the approved Text2SQL latency benchmark"
    )
    parser.add_argument("--lever", required=True)
    parser.add_argument("--baseline-commit", default=BASELINE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    report, payload = run_live_benchmark(
        lever=args.lever,
        baseline_commit=args.baseline_commit,
    )
    write_payload(args.output, payload)
    if args.lever in ROUTED_LEVERS:
        audit_errors = routing_audit_errors(report.outcomes)
        if audit_errors:
            raise AssertionError(
                "routing audit failed:\n" + "\n".join(audit_errors)
            )
    print(f"per_run_accuracy={report.per_run_accuracy}")
    print(
        "three_run_consistent_correctness="
        f"{report.three_run_consistent_correctness:.4f}"
    )
    print(f"family_consistency={report.family_consistency:.4f}")
    print(f"per_family_accuracy={report.per_family_accuracy}")
    print(
        "latency_ms="
        f"median={report.latency_summary.median_ms} "
        f"p95={report.latency_summary.p95_ms} "
        f"max={report.latency_summary.max_ms}"
    )
    assert_accuracy_gates(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
