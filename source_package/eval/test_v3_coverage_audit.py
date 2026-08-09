from __future__ import annotations

from eval.v3_coverage_audit import audit_coverage_cells


def _message(step, *, role, payload_type, content, profile_ref=""):
    message = {
        "msg_id": f"trace-{step:03d}",
        "step": step,
        "role": role,
        "payload": {"type": payload_type, "content": content},
        "claims": [],
        "evidence": [],
    }
    if profile_ref:
        message["student_profile_ref"] = profile_ref
    return message


def test_audit_distinguishes_missing_resource_from_profile_schema_location():
    diagnosis = _message(
        1,
        role="produce",
        payload_type="profile_assessment",
        content={
            "selected_knowledge_point": "KP",
            "knowledge_point_plan": [{
                "knowledge_point": "KP",
                "evidence_ids": ["DP-1"],
                "route_reason": "probe failed",
            }],
        },
    )
    lecture = _message(
        2,
        role="produce",
        payload_type="lecture_note",
        content={
            "knowledge_point": "KP",
            "difficulty": "basic",
            "student_profile_ref": "profile-a",
        },
    )
    review = _message(
        3,
        role="verdict",
        payload_type="review_result",
        content={"reviewed_msg_id": lecture["msg_id"]},
    )
    review["verdict"] = {"decision": "approve", "rule_hits": []}
    run = {
        "seed_id": "seed_A",
        "case_id": "E2E-001",
        "profile_id": "profile-a",
        "messages": [diagnosis, lecture, review],
    }
    cell = {
        "coverage_cell_id": "CC-1",
        "seed_id": "seed_A",
        "case_id": "E2E-001",
        "knowledge_point": "KP",
        "difficulty": "basic",
        "route_reachable": 1,
        "route_evidence_valid": 1,
        "learning_contract_match": 0,
        "profile_resource_match": 0,
        "lecture_pass": 1,
        "practice_pass": 0,
        "quiz_pass": 0,
        "feedback_pass": 0,
        "review_pass": 0,
        "evidence_pass": 0,
        "cell_pass": 0,
    }

    report = audit_coverage_cells([run], [], [cell])

    row = report["cells"][0]
    assert row["profile_binding"]["classification"] == "schema_location_mismatch"
    assert row["resources"]["practice_guide"]["status"] == "not_generated"
    assert row["resources"]["quiz_set"]["status"] == "not_generated"
    assert "practice_guide:not_generated" in row["failure_causes"]
    assert report["failure_cause_counts"]["schema_location_mismatch"] == 1
