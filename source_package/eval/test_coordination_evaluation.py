from __future__ import annotations

from coordination.evaluation import build_coordination_evidence


def test_coordination_evidence_deduplicates_stage_and_uses_paired_branch_times() -> None:
    completion = {
        "sequence": 8,
        "agent": "knowledge",
        "activity": "parallel_resource_generation",
        "status": "done",
        "details": {
            "stage_id": "resource-generation",
            "correlation_id": "eb-1",
            "aggregation": "deterministic",
            "parallel_elapsed_ms": 120,
            "branches": [
                {"branch_id": "knowledge", "required": True, "status": "succeeded", "elapsed_ms": 115},
                {"branch_id": "practice", "required": False, "status": "succeeded", "elapsed_ms": 70},
                {"branch_id": "assessment", "required": False, "status": "succeeded", "elapsed_ms": 65},
            ],
        },
    }
    result = build_coordination_evidence([completion, dict(completion)], [])

    assert len(result["stages"]) == 1
    assert result["stages"][0]["paired_serial_estimate_ms"] == 250
    assert result["stages"][0]["parallel_elapsed_ms"] == 120
    assert result["stages"][0]["saved_ms"] == 130
    assert result["summary"]["speedup"] == 2.083
    assert result["summary"]["branch_success_rate"] == 1.0


def test_coordination_evidence_reports_quality_routes_and_tokens() -> None:
    events = [
        {"agent": "review", "activity": "quality_gate", "status": "approved", "details": {"cycle": 1}},
        {"agent": "review", "activity": "quality_gate", "status": "waiting", "details": {"cycle": 1}},
        {"agent": "review", "activity": "bounded_debate", "status": "debating"},
        {"agent": "review", "activity": "deterministic_rejection_route", "status": "blocked"},
    ]
    messages = [
        {"token_usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        {"token_usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
    ]

    result = build_coordination_evidence(events, messages)

    assert result["quality_flow"] == {
        "review_decisions": 2,
        "first_pass_approvals": 1,
        "first_pass_rate": 0.5,
        "debate_triggers": 1,
        "local_regenerations": 1,
    }
    assert result["token_usage"]["total_tokens"] == 21
