from __future__ import annotations

import pytest

from eval.v3_metrics import (
    FinalReviewIncomplete,
    build_adaptation_nodes,
    build_coverage_cells,
    build_fact_units,
    compute_v3_metrics,
)


def _message(step: int, *, role: str, payload_type: str, content: dict, **extra):
    return {
        "trace_id": "trace-a",
        "msg_id": f"trace-a-{step:03d}",
        "step": step,
        "agent": extra.pop("agent", "knowledge"),
        "role": role,
        "payload": {"type": payload_type, "content": content},
        "evidence": extra.pop("evidence", []),
        "claims": extra.pop("claims", []),
        **extra,
    }


def _run():
    product = _message(
        2,
        role="produce",
        payload_type="lecture_note",
        content={
            "knowledge_point": "三道工序与传导关系",
            "difficulty": "basic",
            "coverage": ["三道工序与传导关系"],
        },
        claims=[{"kind": "fact", "text": "YCL 是三道工序中的第一道。"}],
        evidence=[{
            "kind": "kb_quote",
            "ref": "KB-1",
            "supports_claim": "YCL 是三道工序中的第一道。",
        }],
    )
    review = _message(
        3,
        role="verdict",
        payload_type="review_result",
        content={"reviewed_msg_id": product["msg_id"]},
        agent="review",
        verdict={"decision": "approve", "rule_hits": []},
    )
    diagnosis = _message(
        1,
        role="produce",
        payload_type="profile_assessment",
        content={
            "selected_knowledge_point": "三道工序与传导关系",
            "selected_difficulty": "basic",
            "knowledge_point_plan": [{
                "knowledge_point": "三道工序与传导关系",
                "evidence_ids": ["DP-01-B"],
                "route_reason": "基础探针答错",
                "initial_difficulty": "basic",
            }],
        },
        agent="diagnosis",
    )
    submission = _message(
        4,
        role="system",
        payload_type="control",
        content={"event": "learner_follow_up_submitted", "answer": "YCL最低，完成率0.6236"},
        agent="system",
    )
    assessment = _message(
        5,
        role="probe",
        payload_type="control",
        content={"event": "learner_follow_up_assessed", "assessment": "mastered"},
        agent="diagnosis",
    )
    path = _message(
        6,
        role="system",
        payload_type="control",
        content={
            "event": "path_updated",
            "difficulty_action": "keep",
            "difficulty": "basic",
        },
        agent="system",
    )
    return {
        "run_id": "RUN-A",
        "seed_id": "seed_A",
        "case_id": "E2E-001",
        "session_id": "session-a",
        "route_mode": "production",
        "messages": [diagnosis, product, review, submission, assessment, path],
    }


def _gold():
    return {
        "case_id": "E2E-001",
        "目标知识点": "三道工序与传导关系",
        "预期初始难度": "basic",
        "预期适配序列": "initial_route(basic) → learner_correct → keep",
        "预期最终难度": "basic",
        "预期适配节点数": 2,
        "覆盖格": "KP-01-BASIC",
        "计入覆盖率": "是",
    }


def test_auto_preliminary_keeps_human_fact_label_pending():
    rows = build_fact_units([_run()], mode="AUTO_PRELIMINARY")
    assert len(rows) == 1
    assert rows[0]["human_label"] == "PENDING_HUMAN"
    assert rows[0]["auto_label"] == "SUPPORTED"
    assert rows[0]["published_final"] == 1


def test_adaptation_nodes_are_merged_with_gold_after_the_run():
    rows = build_adaptation_nodes([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    assert [row["node_type"] for row in rows] == ["initial_route", "post_answer"]
    assert all(row["due_node"] == 1 for row in rows)
    assert all(row["node_success"] == 1 for row in rows)


def test_coverage_cell_requires_the_complete_closed_loop():
    cells = build_coverage_cells([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    assert len(cells) == 1
    assert cells[0]["route_reachable"] == 1
    assert cells[0]["lecture_pass"] == 1
    assert cells[0]["cell_pass"] == 0


def test_final_mode_refuses_missing_double_review_labels():
    facts = build_fact_units([_run()], mode="AUTO_PRELIMINARY")
    nodes = build_adaptation_nodes([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    cells = build_coverage_cells([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    with pytest.raises(FinalReviewIncomplete, match="fact unit"):
        compute_v3_metrics(
            facts,
            nodes,
            cells,
            mode="FINAL_HUMAN_REVIEWED",
            human_review={},
        )


def test_final_mode_uses_agreed_human_labels_without_overwriting_auto():
    facts = build_fact_units([_run()], mode="AUTO_PRELIMINARY")
    nodes = build_adaptation_nodes([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    cells = build_coverage_cells([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    review = {
        "fact_units": [{
            "content_unit_id": facts[0]["content_unit_id"],
            "reviewer_a": "SUPPORTED",
            "reviewer_b": "SUPPORTED",
        }],
        "adaptation_transactions": [{
            "first_gen_transaction_id": row["first_gen_transaction_id"],
            "reviewer_a_mismatch": 0,
            "reviewer_b_mismatch": 0,
        } for row in nodes if row["first_gen_transaction_id"]],
        "coverage_cells": [{
            "case_id": "E2E-001",
            "reviewer_a_pass": 0,
            "reviewer_b_pass": 0,
        }],
    }
    result = compute_v3_metrics(
        facts,
        nodes,
        cells,
        mode="FINAL_HUMAN_REVIEWED",
        human_review=review,
    )
    assert result["mode"] == "FINAL_HUMAN_REVIEWED"
    assert facts[0]["auto_label"] == "SUPPORTED"
    assert facts[0]["human_label"] == "SUPPORTED"
    assert result["metrics"]["final_hallucination_rate"]["numerator"] == 0
