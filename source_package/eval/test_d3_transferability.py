from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.validate_message import validate_message


ROOT = Path(__file__).resolve().parents[1]
# 优化3 后：交付 traces 已换新录制；测试改用前端稳定夹具（旧轨迹副本）
KB_TRACE = (
    ROOT
    / "frontend"
    / "src"
    / "test"
    / "fixtures"
    / "traces"
    / "demo-planner_new-20260716133542.jsonl"
)
SEC_TRACE = ROOT / "traces" / "demo-planner_new-20260717-d3-sec-02.jsonl"


def load_trace(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def knowledge_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        message
        for message in messages
        if message.get("agent") == "knowledge"
        and message.get("role") == "produce"
    ]
    # 真实交互会话中每个难度单元产出一节微课；契约校验取首节（初始档）。
    assert matches
    return matches[0]


def transition_ids(messages: list[dict[str, Any]]) -> list[str]:
    return [
        message["payload"]["content"]["transition_id"]
        for message in messages
        if message.get("agent") == "system"
        and message.get("role") == "system"
        and "transition_id" in message["payload"]["content"]
    ]


def test_sec_trace_is_protocol_valid_grounded_and_review_approved() -> None:
    messages = load_trace(SEC_TRACE)

    assert len(messages) == 9
    assert [errors for message in messages if (errors := validate_message(message))] == []
    assert transition_ids(messages) == ["T01", "T02", "T03", "T04"]

    profile = messages[1]["payload"]["content"]
    assert profile["profile"]["profile_id"] == "planner_new"
    assert "工序枚举与命名边界" in profile["knowledge_dimensions"]

    diagnosis = messages[3]["payload"]["content"]
    assert diagnosis["event"] == "diagnosis_ready"
    assert diagnosis["difficulty"] == "basic"
    assert diagnosis["pretest_score"] == {"correct": 3, "total": 5, "rate": 0.6}

    knowledge = knowledge_message(messages)
    content = knowledge["payload"]["content"]
    assert content["event"] == "product_ready"
    assert content["knowledge_point"] == "工序枚举与命名边界"
    assert content["knowledge_point_match"] is True
    assert content["retrieved_chunk_ids"] == ["SEC-001"]
    assert content["quote_validation"] == {
        "checked": 4,
        "passed": 4,
        "failed": 0,
        "failures": [],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }
    assert {evidence["ref"] for evidence in knowledge["evidence"]} == {"SEC-001"}
    assert {claim["kind"] for claim in knowledge["claims"]} == {"fact"}

    review = messages[7]
    assert review["agent"] == "review"
    assert review["payload"]["content"]["reviewed_msg_id"] == knowledge["msg_id"]
    assert review["verdict"] == {
        "decision": "approve",
        "rule_hits": [],
        "difficulty_action": "keep",
    }
    assert not {
        "quiz_set",
        "practice_guide",
        "sql_result",
    }.intersection(message["payload"]["type"] for message in messages)


def test_kb_and_sec_traces_use_the_same_knowledge_message_contract() -> None:
    kb = knowledge_message(load_trace(KB_TRACE))
    sec = knowledge_message(load_trace(SEC_TRACE))

    assert (kb["agent"], kb["role"], kb["payload"]["type"]) == (
        sec["agent"],
        sec["role"],
        sec["payload"]["type"],
    ) == ("knowledge", "produce", "lecture_note")
    assert kb["student_profile_ref"] == sec["student_profile_ref"] == "planner_new"
    assert kb["payload"]["content"]["event"] == "product_ready"
    assert sec["payload"]["content"]["event"] == "product_ready"
    assert kb["payload"]["content"]["retrieved_chunk_ids"] == ["KB-001"]
    assert sec["payload"]["content"]["retrieved_chunk_ids"] == ["SEC-001"]
    assert kb["payload"]["content"]["knowledge_point"] == "三道工序与传导关系"
    assert sec["payload"]["content"]["knowledge_point"] == "工序枚举与命名边界"
    assert kb["payload"]["content"]["lecture_md"] != sec["payload"]["content"][
        "lecture_md"
    ]
