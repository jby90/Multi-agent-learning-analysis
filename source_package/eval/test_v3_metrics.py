from __future__ import annotations

from copy import deepcopy

import pytest

from eval.v3_metrics import (
    FinalReviewIncomplete,
    build_adaptation_nodes,
    build_coverage_cells,
    build_fact_units,
    compute_v3_metrics,
)
from eval.v3_recompute import render_markdown


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
    assert [row["effective_adaptation"] for row in rows] == [1, 0]


def test_rebuttal_sequence_with_spaced_arrows_keeps_both_real_nodes():
    run = _run()
    run["messages"] = run["messages"][:3] + [
        _message(
            4,
            role="system",
            payload_type="control",
            content={"event": "learner_follow_up_submitted", "answer": "错误但有依据"},
            agent="system",
        ),
        _message(
            5,
            role="produce",
            payload_type="quiz_set",
            content={"event": "follow_up_question_ready", "difficulty": "basic"},
            agent="task",
        ),
        _message(
            6,
            role="probe",
            payload_type="control",
            content={"event": "learner_follow_up_assessed", "assessment": "needs_support"},
            agent="diagnosis",
        ),
        _message(
            7,
            role="system",
            payload_type="control",
            content={"event": "learner_follow_up_submitted", "answer": "YCL最低，完成率0.6236"},
            agent="system",
        ),
        _message(
            8,
            role="probe",
            payload_type="control",
            content={"event": "learner_follow_up_assessed", "assessment": "mastered"},
            agent="diagnosis",
        ),
    ]
    gold = {
        **_gold(),
        "预期适配序列": (
            "initial_route(basic) → wrong → targeted_followup/rebuttal "
            "→ corrected → keep"
        ),
        "预期适配节点数": 3,
    }

    rows = build_adaptation_nodes([run], [gold], mode="AUTO_PRELIMINARY")
    post_answer_rows = [row for row in rows if row["node_type"].startswith("post_answer")]

    assert [row["actual_action"] for row in post_answer_rows] == [
        "targeted_followup",
        "keep",
    ]
    assert [row["node_success"] for row in post_answer_rows] == [1, 1]
    assert [row["effective_adaptation"] for row in post_answer_rows] == [1, 0]


def test_step_down_uses_real_downstream_difficulty_and_artifact():
    run = _run()
    run["messages"][0]["payload"]["content"]["selected_difficulty"] = "applied"
    run["messages"][0]["payload"]["content"]["knowledge_point_plan"][0][
        "initial_difficulty"
    ] = "applied"
    run["messages"][1]["payload"]["content"]["difficulty"] = "applied"
    run["messages"] = run["messages"][:3] + [
        _message(
            4,
            role="system",
            payload_type="control",
            content={"event": "learner_follow_up_submitted", "answer": "第一次错误"},
            agent="system",
        ),
        _message(
            5,
            role="produce",
            payload_type="quiz_set",
            content={"event": "follow_up_question_ready", "difficulty": "applied"},
            agent="task",
        ),
        _message(
            6,
            role="probe",
            payload_type="control",
            content={"event": "learner_follow_up_assessed", "assessment": "needs_support"},
            agent="diagnosis",
        ),
        _message(
            7,
            role="system",
            payload_type="control",
            content={"event": "learner_follow_up_submitted", "answer": "第二次错误"},
            agent="system",
        ),
        _message(
            8,
            role="probe",
            payload_type="control",
            content={"event": "learner_follow_up_assessed", "assessment": "needs_support"},
            agent="diagnosis",
        ),
        _message(
            9,
            role="system",
            payload_type="control",
            content={"action": "state_transition", "difficulty_action": "step_down"},
            agent="system",
        ),
        _message(
            10,
            role="system",
            payload_type="control",
            content={
                "event": "learning_contract_ready",
                "learning_contract": {"difficulty": "basic"},
            },
            agent="system",
        ),
        _message(
            11,
            role="produce",
            payload_type="lecture_note",
            content={"event": "product_ready", "difficulty": "basic"},
            agent="knowledge",
        ),
        _message(
            12,
            role="system",
            payload_type="control",
            content={"event": "learner_follow_up_submitted", "answer": "补学后答对"},
            agent="system",
        ),
        _message(
            13,
            role="probe",
            payload_type="control",
            content={"event": "learner_follow_up_assessed", "assessment": "mastered"},
            agent="diagnosis",
        ),
    ]
    gold = {
        **_gold(),
        "预期初始难度": "applied",
        "预期最终难度": "basic",
        "预期适配序列": (
            "initial_route(applied) → wrong → targeted_followup/rebuttal → wrong "
            "→ step_down → remedial_correct"
        ),
        "预期适配节点数": 4,
    }

    rows = build_adaptation_nodes([run], [gold], mode="AUTO_PRELIMINARY")
    step_down = next(row for row in rows if row["actual_action"] == "step_down")
    final_keep = next(
        row
        for row in rows
        if row["actual_action"] == "keep" and row["node_type"] == "post_answer_3"
    )

    assert step_down["before_state_json"] == {"difficulty": "applied"}
    assert step_down["after_state_json"] == {"difficulty": "basic"}
    assert step_down["actual_downstream_difficulty"] == "basic"
    assert step_down["downstream_artifact_id"] == "trace-a-011"
    assert step_down["node_success"] == 1
    assert step_down["effective_adaptation"] == 1
    assert final_keep["before_state_json"] == {"difficulty": "basic"}
    assert final_keep["after_state_json"] == {"difficulty": "basic"}
    assert final_keep["node_success"] == 1


def test_coverage_cell_requires_the_complete_closed_loop():
    cells = build_coverage_cells([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    assert len(cells) == 1
    assert cells[0]["route_reachable"] == 1
    assert cells[0]["lecture_pass"] == 1
    assert cells[0]["learning_contract_match"] == 0
    assert cells[0]["profile_resource_match"] == 0
    assert cells[0]["cell_pass"] == 0


def test_coverage_contract_gate_accepts_the_matching_contract_before_a_step_up():
    run = _run()
    run["messages"].extend([
        _message(
            7,
            role="system",
            payload_type="control",
            content={
                "event": "learning_contract_ready",
                "learning_contract": {
                    "target_knowledge_points": ["三道工序与传导关系"],
                    "difficulty": "basic",
                },
            },
            agent="system",
        ),
        _message(
            8,
            role="system",
            payload_type="control",
            content={
                "event": "learning_contract_ready",
                "learning_contract": {
                    "target_knowledge_points": ["三道工序与传导关系"],
                    "difficulty": "applied",
                },
            },
            agent="system",
        ),
    ])

    cell = build_coverage_cells([run], [_gold()], mode="AUTO_PRELIMINARY")[0]

    assert cell["learning_contract_match"] == 1


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
            "coverage_cell_id": cells[0]["coverage_cell_id"],
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
    assert result["metrics"]["profile_resource_difficulty_adaptation_accuracy"] == {
        "numerator": 1,
        "denominator": 1,
        "percentage": 100.0,
        "denominator_zero": False,
    }
    assert result["metrics"]["effective_automatic_adaptation_rate"] == {
        "numerator": 1,
        "denominator": 2,
        "percentage": 50.0,
        "denominator_zero": False,
    }


def test_coverage_cells_keep_seed_a_and_seed_b_independent():
    seed_a = _run()
    seed_b = deepcopy(seed_a)
    seed_b["run_id"] = "RUN-B"
    seed_b["seed_id"] = "seed_B"
    seed_b["session_id"] = "session-b"

    cells = build_coverage_cells([seed_a, seed_b], [_gold()], mode="AUTO_PRELIMINARY")

    assert len(cells) == 2
    assert {row["seed_id"] for row in cells} == {"seed_A", "seed_B"}
    assert len({row["coverage_cell_id"] for row in cells}) == 2


def test_final_hallucination_label_requires_an_eligible_rule_hit():
    facts = build_fact_units([_run()], mode="AUTO_PRELIMINARY")
    nodes = build_adaptation_nodes([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    cells = build_coverage_cells([_run()], [_gold()], mode="AUTO_PRELIMINARY")
    review = {
        "fact_units": [{
            "content_unit_id": facts[0]["content_unit_id"],
            "reviewer_a": "HALLUCINATION",
            "reviewer_b": "HALLUCINATION",
            "rule_hits": [],
        }],
        "adaptation_transactions": [{
            "first_gen_transaction_id": row["first_gen_transaction_id"],
            "reviewer_a_mismatch": 0,
            "reviewer_b_mismatch": 0,
        } for row in nodes if row["first_gen_transaction_id"]],
        "coverage_cells": [{
            "coverage_cell_id": cells[0]["coverage_cell_id"],
            "case_id": "E2E-001",
            "reviewer_a_pass": 0,
            "reviewer_b_pass": 0,
        }],
    }

    with pytest.raises(FinalReviewIncomplete, match="requires an R-01/R-02/R-04/R-05/R-06 rule hit"):
        compute_v3_metrics(
            facts,
            nodes,
            cells,
            mode="FINAL_HUMAN_REVIEWED",
            human_review=review,
        )


def test_markdown_renders_zero_denominator_as_not_applicable():
    report = {
        "mode": "AUTO_PRELIMINARY",
        "combined": {
            "metrics": {
                "hallucination_interception_rate": {
                    "numerator": 0,
                    "denominator": 0,
                    "percentage": 0.0,
                    "denominator_zero": True,
                }
            }
        },
        "by_seed": {},
    }

    assert "| 幻觉拦截率（辅助） | 0 | 0 | N/A |" in render_markdown(report)
