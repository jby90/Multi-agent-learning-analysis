"""Reproducible coordination evidence derived from one canonical live session."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_STAGE_LABELS = {
    "evidence-bundle": "\u4e09\u6e90\u8bc1\u636e\u68c0\u7d22",
    "resource-generation": "\u4e09\u8def\u8d44\u6e90\u751f\u6210",
    "quality-review-axes": "\u56db\u7ef4\u8d28\u91cf\u5ba1\u6838",
}


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(round(value)), 0)


def _stage_evidence(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    completed: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        branches = details.get("branches")
        stage_id = details.get("stage_id")
        correlation_id = details.get("correlation_id")
        if (
            details.get("aggregation") != "deterministic"
            or not isinstance(stage_id, str)
            or not stage_id.strip()
            or not isinstance(correlation_id, str)
            or not correlation_id.strip()
            or not isinstance(branches, list)
            or not branches
        ):
            continue
        elapsed = _non_negative_int(
            details.get("parallel_elapsed_ms", details.get("elapsed_ms"))
        )
        normalized_branches = [
            {
                "branch_id": str(branch.get("branch_id", "")),
                "required": bool(branch.get("required", True)),
                "status": str(branch.get("status", "failed")),
                "elapsed_ms": _non_negative_int(branch.get("elapsed_ms")),
            }
            for branch in branches
            if isinstance(branch, Mapping)
        ]
        if not normalized_branches:
            continue
        serial_estimate = _non_negative_int(details.get("serial_estimate_ms"))
        if serial_estimate == 0:
            serial_estimate = sum(branch["elapsed_ms"] for branch in normalized_branches)
        saved = max(serial_estimate - elapsed, 0)
        speedup = round(serial_estimate / elapsed, 3) if elapsed > 0 else 1.0
        completed[(stage_id, correlation_id)] = {
            "stage_id": stage_id,
            "label": _STAGE_LABELS.get(stage_id, stage_id),
            "correlation_id": correlation_id,
            "fan_out": len(normalized_branches),
            "parallel_elapsed_ms": elapsed,
            "paired_serial_estimate_ms": serial_estimate,
            "saved_ms": saved,
            "speedup": speedup,
            "succeeded": all(
                branch["status"] == "succeeded"
                for branch in normalized_branches
                if branch["required"]
            ),
            "branches": normalized_branches,
        }
    return list(completed.values())


def _token_totals(messages: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for message in messages:
        usage = message.get("token_usage")
        if not isinstance(usage, Mapping):
            continue
        prompt = _non_negative_int(usage.get("prompt_tokens"))
        completion = _non_negative_int(usage.get("completion_tokens"))
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        totals["total_tokens"] += _non_negative_int(usage.get("total_tokens")) or prompt + completion
    return totals


def build_coordination_evidence(
    events: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build paired parallel evidence without claiming an unexecuted baseline.

    The paired serial estimate is the sum of branch wall times from the exact
    same fork/join execution. This isolates orchestration savings while keeping
    inputs, outputs, and provider conditions paired.
    """

    stages = _stage_evidence(events)
    serial_total = sum(stage["paired_serial_estimate_ms"] for stage in stages)
    parallel_total = sum(stage["parallel_elapsed_ms"] for stage in stages)
    branch_total = sum(len(stage["branches"]) for stage in stages)
    branch_succeeded = sum(
        branch["status"] == "succeeded"
        for stage in stages
        for branch in stage["branches"]
    )
    review_decisions = [
        event
        for event in events
        if event.get("agent") == "review"
        and event.get("activity") == "quality_gate"
        and event.get("status") in {"approved", "waiting"}
    ]
    first_pass = sum(
        event.get("status") == "approved"
        and isinstance(event.get("details"), Mapping)
        and event["details"].get("cycle") == 1
        for event in review_decisions
    )
    debates = sum(
        event.get("agent") == "review"
        and event.get("activity") in {"bounded_debate", "follow_up_debate"}
        and event.get("status") == "debating"
        for event in events
    )
    local_regenerations = sum(
        event.get("agent") == "review"
        and event.get("activity") == "deterministic_rejection_route"
        for event in events
    )
    return {
        "schema_version": 1,
        "measurement_basis": "same_run_branch_wall_time_sum",
        "baseline_label": "\u540c\u6279\u5206\u652f\u4e32\u884c\u8017\u65f6\u4f30\u7b97",
        "stages": stages,
        "summary": {
            "completed_parallel_stages": len(stages),
            "max_fan_out": max((stage["fan_out"] for stage in stages), default=0),
            "parallel_elapsed_ms": parallel_total,
            "paired_serial_estimate_ms": serial_total,
            "saved_ms": max(serial_total - parallel_total, 0),
            "speedup": round(serial_total / parallel_total, 3) if parallel_total > 0 else 1.0,
            "branch_success_rate": round(branch_succeeded / branch_total, 4) if branch_total else 0.0,
        },
        "quality_flow": {
            "review_decisions": len(review_decisions),
            "first_pass_approvals": first_pass,
            "first_pass_rate": round(first_pass / len(review_decisions), 4) if review_decisions else 0.0,
            "debate_triggers": debates,
            "local_regenerations": local_regenerations,
        },
        "token_usage": _token_totals(messages),
    }
