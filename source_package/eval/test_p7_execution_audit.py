from __future__ import annotations

from eval.p7_execution_audit import execution_audit
from eval.trace_dataset import AttemptRecord


def _record(
    case_id: str,
    status: str,
    error: str | None = None,
    outcome: str | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        case_id=case_id,
        attempt=1,
        trace_id=f"p7-{case_id.lower()}-a01",
        status=status,
        trace_path=f"eval/results/{case_id}.jsonl",
        cache_path=f"eval/results/{case_id}.cache.jsonl",
        base_commit="frozen",
        started_at="2026-07-17T00:00:00+00:00",
        finished_at="2026-07-17T00:01:00+00:00",
        terminal_state="S10_DONE" if status == "succeeded" else None,
        transition_sequence=("T20",) if status == "succeeded" else ("T11",),
        message_count=1,
        error_type=None if status == "succeeded" else "EvaluationRunError",
        error_message=error,
        outcome=outcome or ("completed" if status == "succeeded" else "system_error"),
    )


def _row(case_id: str, *, event: str, mismatch: bool, terminal: bool) -> dict:
    return {
        "case": {"case_id": case_id},
        "terminal_state": "S10_DONE" if terminal else None,
        "messages": [
            {
                "agent": "verification",
                "role": "produce",
                "payload": {
                    "type": "sql_result",
                    "content": {
                        "event": event,
                        "routing_family_mismatch": mismatch,
                    },
                },
            }
        ],
    }


def test_execution_audit_counts_failures_sandbox_and_routing_without_filtering() -> None:
    dataset = (
        _row("E2E-001", event="query_completed", mismatch=False, terminal=True),
        _row("E2E-002", event="sandbox_rejected", mismatch=True, terminal=False),
    )
    records = (
        _record("E2E-001", "succeeded"),
        _record(
            "E2E-002",
            "failed",
            "sandbox_rejected",
            outcome="safe_rejected",
        ),
    )

    report = execution_audit(dataset, records)

    assert report["attempts"] == 2
    assert report["succeeded_attempts"] == 1
    assert report["failed_attempts"] == 1
    assert report["success_rate"] == "0.500000"
    assert report["failure_types"] == {"sandbox_rejected": 1}
    assert report["outcomes"] == {
        "completed": 1,
        "external_unavailable": 0,
        "safe_rejected": 1,
        "system_error": 0,
    }
    assert report["outcome_case_ids"] == {
        "completed": ["E2E-001"],
        "external_unavailable": [],
        "safe_rejected": ["E2E-002"],
        "system_error": [],
    }
    assert report["system_error_attempts"] == 0
    assert report["sandbox_rejected"] == 1
    assert report["sandbox_case_ids"] == ["E2E-002"]
    assert report["routing_family_mismatch"] == 1
    assert report["routing_observations"] == 2
    assert report["routing_family_mismatch_rate"] == "0.500000"
    assert report["routing_mismatch_case_ids"] == ["E2E-002"]
