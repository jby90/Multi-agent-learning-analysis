from __future__ import annotations

import json

from eval.metrics.adaptation import adaptation_report
from eval.metrics.coverage import coverage_report
from eval.metrics.hallucination import hallucination_report
from eval.metrics.summary import refusal_report
from eval.trace_dataset import canonical_json


TRACE_ID = "p7-e2e-001-a01"
CORE_POINTS = ("计划量与实际量口径", "完成率计算")


def _message(
    step: int,
    *,
    agent: str = "knowledge",
    role: str = "produce",
    payload_type: str = "lecture_note",
    content: dict | None = None,
    claims: list[dict] | None = None,
    evidence: list[dict] | None = None,
    verdict: dict | None = None,
) -> dict:
    value = {
        "trace_id": TRACE_ID,
        "msg_id": f"{TRACE_ID}-{step:03d}",
        "step": step,
        "agent": agent,
        "role": role,
        "payload": {"type": payload_type, "content": content or {}},
        "claims": claims or [],
        "evidence": evidence or [],
        "timestamp": "2026-07-17T00:00:00+00:00",
    }
    if verdict is not None:
        value["verdict"] = verdict
    return value


def _dataset(*messages: dict, manual_review: bool = True) -> tuple[dict, ...]:
    return (
        {
            "case": {
                "case_id": "E2E-001",
                "profile_id": "planner_new",
                "manual_review": manual_review,
            },
            "trace_id": TRACE_ID,
            "messages": list(messages),
        },
    )


def test_claim_without_evidence_is_linked_to_message_and_claim() -> None:
    report = hallucination_report(
        _dataset(
            _message(
                1,
                claims=[{"text": "计划量不是实际量", "kind": "fact"}],
            )
        )
    )

    assert report["denominator_claims"] == 1
    assert report["numerator_events"] == 1
    assert report["rate"] == "1.000000"
    assert report["events"] == [
        {
            "case_id": "E2E-001",
            "trace_id": TRACE_ID,
            "msg_id": f"{TRACE_ID}-001",
            "claim_index": 0,
            "claim_text": "计划量不是实际量",
            "event_type": "claim_without_evidence",
            "evidence_ref": None,
        }
    ]


def test_data_conclusion_percentage_matches_sql_decimal() -> None:
    claim = "查询结果显示完成率为62.36%。"
    report = hallucination_report(
        _dataset(
            _message(
                1,
                agent="verification",
                payload_type="sql_result",
                content={
                    "event": "query_completed",
                    "question": "H2601在2025-05的完成率是多少？",
                    "rows": [{"complete_rate": "0.6236"}],
                    "row_count": 1,
                },
                claims=[{"text": claim, "kind": "data_conclusion"}],
                evidence=[
                    {
                        "kind": "sql_query",
                        "ref": "sql-01",
                        "quote": json.dumps(
                            {"rows": [{"complete_rate": "0.6236"}]},
                            ensure_ascii=False,
                        ),
                        "supports_claim": claim,
                    }
                ],
            )
        )
    )

    assert report["denominator_claims"] == 1
    assert report["numerator_events"] == 0
    assert report["rate"] == "0.000000"


def test_data_conclusion_month_label_is_not_split_into_false_numbers() -> None:
    claim = "查询2025-03结果：完成率为62.36%。"
    report = hallucination_report(
        _dataset(
            _message(
                1,
                agent="verification",
                payload_type="sql_result",
                content={
                    "event": "query_completed",
                    "question": "查询H2601YCL3-5月完成率走势",
                    "rows": [
                        {"month_label": "2025-03", "complete_rate": "0.6236"}
                    ],
                    "row_count": 1,
                },
                claims=[{"text": claim, "kind": "data_conclusion"}],
                evidence=[
                    {
                        "kind": "sql_query",
                        "ref": "sql-01",
                        "quote": json.dumps(
                            {
                                "rows": [
                                    {
                                        "month_label": "2025-03",
                                        "complete_rate": "0.6236",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        "supports_claim": claim,
                    }
                ],
            )
        )
    )

    assert report["denominator_claims"] == 1
    assert report["numerator_events"] == 0
    assert report["rate"] == "0.000000"


def test_data_conclusion_wrong_month_label_is_reported_as_one_token() -> None:
    claim = "查询2025-04结果：完成率为62.36%。"
    report = hallucination_report(
        _dataset(
            _message(
                1,
                agent="verification",
                payload_type="sql_result",
                content={
                    "event": "query_completed",
                    "question": "查询H2601YCL3-5月完成率走势",
                    "rows": [
                        {"month_label": "2025-03", "complete_rate": "0.6236"}
                    ],
                    "row_count": 1,
                },
                claims=[{"text": claim, "kind": "data_conclusion"}],
                evidence=[
                    {
                        "kind": "sql_query",
                        "ref": "sql-01",
                        "quote": json.dumps(
                            {
                                "rows": [
                                    {
                                        "month_label": "2025-03",
                                        "complete_rate": "0.6236",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        "supports_claim": claim,
                    }
                ],
            )
        )
    )

    assert report["numerator_events"] == 1
    assert report["events"][0]["unmatched_numbers"] == ["2025-04"]


def test_data_conclusion_numeric_mismatch_names_bad_number() -> None:
    claim = "查询结果显示完成率为52.36%。"
    report = hallucination_report(
        _dataset(
            _message(
                1,
                agent="verification",
                payload_type="sql_result",
                content={
                    "event": "query_completed",
                    "question": "完成率是多少？",
                    "rows": [{"complete_rate": "0.6236"}],
                    "row_count": 1,
                },
                claims=[{"text": claim, "kind": "data_conclusion"}],
                evidence=[
                    {
                        "kind": "sql_query",
                        "ref": "sql-01",
                        "quote": "{}",
                        "supports_claim": claim,
                    }
                ],
            )
        )
    )

    assert report["numerator_events"] == 1
    assert report["events"][0]["event_type"] == "data_conclusion_number_mismatch"
    assert report["events"][0]["unmatched_numbers"] == ["52.36%"]


def test_invalid_sentence_ref_and_r02_are_separate_auditable_events() -> None:
    product_id = f"{TRACE_ID}-001"
    messages = (
        _message(
            1,
            content={
                "quote_validation": {
                    "failures": [
                        {
                            "claim_text": "无效锚点结论",
                            "sentence_ref": [999],
                            "reason": "invalid_sentence_ref",
                        }
                    ]
                }
            },
            claims=[{"text": "无效锚点结论", "kind": "speculation"}],
        ),
        _message(
            2,
            agent="review",
            role="verdict",
            payload_type="review_verdict",
            content={"reviewed_msg_id": product_id},
            verdict={
                "decision": "reject",
                "difficulty_action": "none",
                "rule_hits": [
                    {
                        "rule_id": "R-02",
                        "reason": "引用不支持结论",
                        "evidence_ref": "KB-002",
                    }
                ],
            },
        ),
    )

    report = hallucination_report(_dataset(*messages))

    assert report["denominator_claims"] == 0
    assert report["numerator_events"] == 2
    assert [event["event_type"] for event in report["events"]] == [
        "invalid_sentence_ref",
        "review_r02_unsupported",
    ]
    assert report["events"][1]["reviewed_msg_id"] == product_id


def test_template_fallback_is_reported_but_excluded_from_main_rate() -> None:
    report = hallucination_report(
        _dataset(
            _message(
                1,
                content={"generated_by": "template_fallback"},
                claims=[{"text": "模板结论", "kind": "fact"}],
            )
        )
    )

    assert report["denominator_claims"] == 0
    assert report["numerator_events"] == 0
    assert report["template_fallback_products"] == 1


def _review(
    step: int,
    product_id: str,
    *,
    role: str = "verdict",
    decision: str | None = None,
    rule_hits: list[dict] | None = None,
) -> dict:
    return _message(
        step,
        agent="review",
        role=role,
        payload_type="review_verdict",
        content={"reviewed_msg_id": product_id, "reviewed_payload_type": "quiz_set"},
        verdict={
            "decision": decision
            if decision is not None
            else ("approve" if not rule_hits else "approve_with_fix"),
            "difficulty_action": "keep" if not rule_hits else "step_down",
            "rule_hits": rule_hits or [],
        },
    )


def test_adaptation_uses_latest_verdict_for_each_produced_resource() -> None:
    product = _message(
        1,
        agent="task",
        payload_type="quiz_set",
        content={"difficulty": "basic", "question": "题目"},
    )
    r03 = {
        "rule_id": "R-03",
        "reason": "难度不匹配（difficulty_gap=1）。",
        "evidence_ref": "diagnosis-001",
    }
    report = adaptation_report(
        _dataset(
            product,
            _review(2, product["msg_id"], rule_hits=[r03]),
            _review(3, product["msg_id"], role="re_verdict"),
        )
    )

    assert report["total_products"] == 1
    assert report["matched"] == 1
    assert report["unmatched"] == 0
    assert report["rate"] == "1.000000"
    assert report["items"][0]["verdict_msg_id"] == f"{TRACE_ID}-003"


def test_adaptation_excludes_latest_rejected_product_and_audits_it() -> None:
    product = _message(
        1,
        agent="task",
        payload_type="quiz_set",
        content={"difficulty": "basic", "question": "题目"},
    )
    r02 = {
        "rule_id": "R-02",
        "reason": "引用不支持结论",
        "evidence_ref": "KB-002",
    }
    report = adaptation_report(
        _dataset(
            product,
            _review(2, product["msg_id"], decision="reject", rule_hits=[r02]),
        )
    )

    assert report["candidate_products"] == 1
    assert report["excluded_rejected_products"] == 1
    assert report["total_products"] == 0
    assert report["matched"] == 0
    assert report["unmatched"] == 0
    assert report["rate"] == "0.000000"
    assert report["items"] == []
    assert report["excluded_items"] == [
        {
            "case_id": "E2E-001",
            "profile_id": "planner_new",
            "trace_id": TRACE_ID,
            "product_msg_id": product["msg_id"],
            "payload_type": "quiz_set",
            "verdict_msg_id": f"{TRACE_ID}-002",
            "decision": "reject",
            "rule_ids": ["R-02"],
            "reason": "latest_review_rejected",
        }
    ]


def test_adaptation_excludes_rejected_product_but_keeps_approved_regeneration() -> None:
    rejected_product = _message(
        1,
        agent="task",
        payload_type="quiz_set",
        content={"difficulty": "basic", "question": "初始题目"},
    )
    regenerated_product = _message(
        3,
        agent="task",
        payload_type="quiz_set",
        content={"difficulty": "basic", "question": "重生成题目"},
    )
    r02 = {
        "rule_id": "R-02",
        "reason": "引用不支持结论",
        "evidence_ref": "KB-002",
    }
    report = adaptation_report(
        _dataset(
            rejected_product,
            _review(
                2,
                rejected_product["msg_id"],
                decision="reject",
                rule_hits=[r02],
            ),
            regenerated_product,
            _review(4, regenerated_product["msg_id"]),
        )
    )

    assert report["candidate_products"] == 2
    assert report["excluded_rejected_products"] == 1
    assert report["total_products"] == 1
    assert report["matched"] == 1
    assert report["unmatched"] == 0
    assert [item["product_msg_id"] for item in report["items"]] == [
        regenerated_product["msg_id"]
    ]
    assert [item["product_msg_id"] for item in report["excluded_items"]] == [
        rejected_product["msg_id"]
    ]
    assert report["candidate_products"] == (
        report["total_products"] + report["excluded_rejected_products"]
    )
    assert len(report["items"]) == report["total_products"]


def test_adaptation_keeps_product_when_latest_re_verdict_approves_it() -> None:
    product = _message(
        1,
        agent="task",
        payload_type="quiz_set",
        content={"difficulty": "basic", "question": "题目"},
    )
    r02 = {
        "rule_id": "R-02",
        "reason": "引用不支持结论",
        "evidence_ref": "KB-002",
    }
    report = adaptation_report(
        _dataset(
            product,
            _review(2, product["msg_id"], decision="reject", rule_hits=[r02]),
            _review(3, product["msg_id"], role="re_verdict", decision="approve"),
        )
    )

    assert report["candidate_products"] == 1
    assert report["excluded_rejected_products"] == 0
    assert report["total_products"] == 1
    assert report["matched"] == 1
    assert report["excluded_items"] == []
    assert report["items"][0]["verdict_msg_id"] == f"{TRACE_ID}-003"
    assert report["items"][0]["decision"] == "approve"


def test_adaptation_parses_r03_gap_and_keeps_evidence_chain() -> None:
    product = _message(
        1,
        agent="task",
        payload_type="quiz_set",
        content={"difficulty": "basic", "question": "题目"},
    )
    r03 = {
        "rule_id": "R-03",
        "reason": "画像与产物错配（difficulty_gap=1）。",
        "evidence_ref": "diagnosis-001",
    }
    report = adaptation_report(_dataset(product, _review(2, product["msg_id"], rule_hits=[r03])))

    assert report["candidate_products"] == 1
    assert report["excluded_rejected_products"] == 0
    assert report["total_products"] == 1
    assert report["matched"] == 0
    assert report["unmatched"] == 1
    assert report["excluded_items"] == []
    assert report["items"][0]["difficulty_gap"] == 1
    assert report["items"][0]["evidence_ref"] == "diagnosis-001"


def test_adaptation_keeps_missing_review_visible_as_unmatched() -> None:
    product = _message(
        1,
        agent="task",
        payload_type="quiz_set",
        content={"difficulty": "basic", "question": "题目"},
    )

    report = adaptation_report(_dataset(product))

    assert report["candidate_products"] == 1
    assert report["excluded_rejected_products"] == 0
    assert report["total_products"] == 1
    assert report["matched"] == 0
    assert report["unmatched"] == 1
    assert report["excluded_items"] == []
    assert report["items"][0]["reason"] == "missing_review"
    assert report["candidate_products"] == (
        report["total_products"] + report["excluded_rejected_products"]
    )
    assert len(report["items"]) == report["total_products"]


def test_coverage_requires_exact_coverage_field_hit_and_links_message() -> None:
    report = coverage_report(
        _dataset(
            _message(
                1,
                content={
                    "knowledge_point": "计划量与实际量口径",
                    "coverage": ["计划量与实际量口径", "完成率"],
                    "retrieved_chunk_ids": ["KB-002"],
                },
            )
        ),
        CORE_POINTS,
        ("KB-002", "KB-003"),
    )

    assert report["covered"] == 1
    assert report["total"] == 2
    assert report["rate"] == "0.500000"
    assert report["covered_points"][0]["knowledge_point"] == "计划量与实际量口径"
    assert report["uncovered_points"] == ["完成率计算"]
    assert report["never_retrieved_chunk_ids"] == ["KB-003"]


def test_refusal_rate_is_session_level_and_deterministic() -> None:
    dataset = _dataset(
        _message(
            1,
            content={"event": "knowledge_refused", "refuse_reason": "证据不足"},
        ),
        _message(
            2,
            agent="system",
            role="system",
            payload_type="control",
            content={"student_visible_refusal": True},
        ),
    )
    first = refusal_report(dataset)
    second = refusal_report(dataset)

    assert first["refused_cases"] == 1
    assert first["total_cases"] == 1
    assert first["rate"] == "1.000000"
    assert len(first["events"]) == 2
    assert canonical_json(first) == canonical_json(second)
