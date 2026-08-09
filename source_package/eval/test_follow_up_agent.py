from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import pytest

from agents.domain_config import load_domain_config
from agents.follow_up_agent import (
    FollowUpAgent,
    FollowUpGenerationError,
    normalize_learner_input,
    reviewed_answer_confirmation,
    reviewed_answer_correction,
)
from agents.task_agent import TaskAgent, load_task_catalog
from agents.validate_message import validate_message
from orchestrator.llm import LLMResult, TokenUsage


class FollowUpLLM:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected follow-up model call")
        return LLMResult(
            data=self.responses.pop(0),
            model="fixed-follow-up-stub",
            latency_ms=7,
            token_usage=TokenUsage(20, 10, 30),
            attempts=1,
        )


def _task_agent(domain_id: str = "production_progress") -> TaskAgent:
    return TaskAgent(
        f"trace-{domain_id}",
        catalog=load_task_catalog(
            domain_config=load_domain_config(domain_id),
        ),
        clock=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
    )


def _current_task(task_agent: TaskAgent, template_id: str) -> dict[str, Any]:
    return task_agent.generate(template_id)


def test_follow_up_agent_builds_a_grounded_reviewable_probe() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": "对照计划量与实际完成量，你会用哪个口径说明真实进度？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent(
        "trace-production_progress",
        llm_call=llm,
        clock=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
    )

    turn = agent.generate(
        student_answer="计划量就是已经完成的数量。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
    )
    product = turn.product
    assert product is not None

    assert turn.assessment == "needs_support"
    assert turn.diagnosed_misconception == "M-01"
    assert turn.next_target_misconception == "M-01"
    assert product["agent"] == "task"
    assert product["role"] == "probe"
    assert product["payload"]["type"] == "quiz_set"
    content = product["payload"]["content"]
    assert content["event"] == "follow_up_question_ready"
    assert content["question"] == "对照计划量与实际完成量，你会用哪个口径说明真实进度？"
    assert content["follow_up_round"] == 2
    assert content["max_follow_up_rounds"] == 4
    assert content["target_misconception"] == "M-01"
    assert content["evidence_refs"] == ["M-01:1"]
    assert product["probe"] == {
        "wrong_attempts": 1,
        "questions": [content["question"]],
        "target_misconception": "M-01",
    }
    assert [item["ref"] for item in product["evidence"]] == ["M-01:1"]
    assert product["claims"] == []
    assert product["model"] == "fixed-follow-up-stub"
    assert product["token_usage"] == {
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "total_tokens": 30,
    }
    assert validate_message(
        {
            "msg_id": "trace-production_progress-001",
            "step": 1,
            **product,
        }
    ) == []
    assert llm.calls[0]["temperature"] == 0.1
    assert set(
        llm.calls[0]["json_schema"]["properties"][
            "diagnosed_misconception"
        ]["enum"]
    ) == {"M-01", "M-02", "M-03", "M-04", "M-05", "UNKNOWN"}
    assert set(
        llm.calls[0]["json_schema"]["properties"][
            "next_target_misconception"
        ]["enum"]
    ) == {
        "M-01",
        "M-02",
        "M-03",
        "M-04",
        "M-05",
        "UNKNOWN",
        "NO_NEXT_TARGET",
    }


def test_repeated_misconception_routes_to_supported_related_target() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-04",
            "question": "分别换一个汇总层级后，你会怎样判断两种结果是否可直接比较？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer="我仍然把计划量当作已经完成的数量。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=3,
        max_rounds=4,
        probed_misconceptions=("M-01",),
    )

    assert turn.diagnosed_misconception == "M-01"
    assert turn.next_target_misconception == "M-04"
    assert turn.product is not None
    assert turn.product["payload"]["content"]["target_misconception"] == "M-04"
    assert [item["ref"] for item in turn.product["evidence"]] == [
        "M-04:1",
        "M-04:2",
    ]


def test_follow_up_rejects_model_target_that_disagrees_with_routing() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-05",
            "question": "你会怎样区分不同业务口径下的结果？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    with pytest.raises(FollowUpGenerationError, match="deterministic routing"):
        agent.generate(
            student_answer="我仍然把计划量当作已经完成的数量。",
            current_task=_current_task(task_agent, "T-01"),
            task_agent=task_agent,
            round_index=3,
            max_rounds=4,
            probed_misconceptions=("M-01",),
        )


def test_follow_up_agent_unknown_target_uses_only_current_task_evidence() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "unknown",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "UNKNOWN",
            "question": "你判断真实进度时，最需要先核对哪一类数据？",
        }
    )
    task_agent = _task_agent()
    current_task = _current_task(task_agent, "T-01")
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer="我还不确定。",
        current_task=current_task,
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
    )
    product = turn.product
    assert product is not None

    assert product["payload"]["content"]["target_misconception"] == "UNKNOWN"
    assert product["evidence"] == current_task["evidence"]
    assert product["payload"]["content"]["evidence_refs"] == [
        item["ref"] for item in current_task["evidence"]
    ]


def test_follow_up_agent_rejects_a_cross_domain_misconception() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": "你会先核对哪个字段来判断当天完成情况？",
        }
    )
    task_agent = _task_agent("first_segment")
    agent = FollowUpAgent("trace-first_segment", llm_call=llm)

    with pytest.raises(FollowUpGenerationError, match="current domain"):
        agent.generate(
            student_answer="看计划数。",
            current_task=_current_task(task_agent, "T-FS02"),
            task_agent=task_agent,
            round_index=2,
            max_rounds=4,
        )


def test_follow_up_agent_builds_a_grounded_first_segment_probe() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-FS01",
            "next_target_misconception": "M-FS01",
            "question": "比较当日实际数和完成率时，你会怎样避免把两种口径混在一起？",
        }
    )
    task_agent = _task_agent("first_segment")
    agent = FollowUpAgent("trace-first_segment", llm_call=llm)

    turn = agent.generate(
        student_answer="我把当日实际数和完成率当成同一个口径了。",
        current_task=_current_task(task_agent, "T-FS02"),
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
    )

    product = turn.product
    assert product is not None
    assert turn.diagnosed_misconception == "M-FS01"
    assert turn.next_target_misconception == "M-FS01"
    assert product["payload"]["content"]["evidence_refs"] == ["M-FS01:1"]
    assert [item["ref"] for item in product["evidence"]] == ["M-FS01:1"]
    assert validate_message(
        {
            "msg_id": "trace-first-segment-001",
            "step": 1,
            **product,
        }
    ) == []


@pytest.mark.parametrize(
    ("question", "reason"),
    (
        ("请读取 2026 年的计划量，你会怎样判断？", "ungrounded number"),
        ("请说明 msg_id 对学习判断有什么影响？", "engineering"),
        ("先看计划量吗？再看实际量吗？", "exactly one question"),
        (
            "计划1855.06≠实际1156.87，混用即结论反转，对吗？",
            "answer",
        ),
    ),
)
def test_follow_up_agent_rejects_unsafe_or_ungrounded_questions(
    question: str,
    reason: str,
) -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": question,
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    with pytest.raises(FollowUpGenerationError, match=reason):
        agent.generate(
            student_answer="我需要再想想。",
            current_task=_current_task(task_agent, "T-01"),
            task_agent=task_agent,
            round_index=2,
            max_rounds=4,
        )


def test_mastered_turn_after_the_minimum_does_not_generate_an_unused_question() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "mastered",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "NO_NEXT_TARGET",
            "question": "",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer="实际完成量才能说明已经做了多少。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=3,
        max_rounds=4,
        completion_allowed=True,
    )

    assert turn.assessment == "mastered"
    assert turn.diagnosed_misconception == "M-01"
    assert turn.next_target_misconception is None
    assert turn.product is None


def test_follow_up_model_receives_the_active_question_and_reviewed_rows() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "mastered",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "M-02",
            "question": "是否还需要检查后续月份？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)
    current_question = "查询结果中另外两道工序的完成率分别是多少？"

    turn = agent.generate(
        student_answer="AZTP完成率为0.9149，ZZTP完成率为1.0249。",
        current_task=_current_task(task_agent, "T-03"),
        task_agent=task_agent,
        current_question=current_question,
        round_index=3,
        max_rounds=4,
        completion_allowed=True,
    )

    request = json.loads(llm.calls[0]["user"])
    assert request["current_question"] == current_question
    assert request["current_evidence_rows"] == [
        {"complete_rate": "0.9149", "process_code": "AZTP"},
        {"complete_rate": "0.6236", "process_code": "YCL"},
        {"complete_rate": "1.0249", "process_code": "ZZTP"},
    ]
    assert request["answer_requirements"] == {
        "operation": "compare_entities",
        "required_fields": ["process_code", "complete_rate"],
        "required_reasoning": ["comparison"],
    }
    assert turn.assessment == "mastered"
    assert turn.diagnosed_misconception == "UNKNOWN"
    assert turn.next_target_misconception is None
    assert turn.product is None


@pytest.mark.parametrize(
    "student_answer",
    (
        "YCL完成率最低 0.6236",
        "YCL 0.6236",
        "YCL 62.36%",
    ),
)
def test_reviewed_extreme_field_and_value_override_an_unknown_false_negative(
    student_answer: str,
) -> None:
    llm = FollowUpLLM(
        {
            "assessment": "unknown",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "UNKNOWN",
            "question": "查询结果中另外两道工序的完成率分别是多少？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer=student_answer,
        current_task=_current_task(task_agent, "T-03"),
        task_agent=task_agent,
        current_question=(
            "根据刚才的三道工序结果，哪一道工序完成率最低，"
            "你依据的数值是什么？"
        ),
        round_index=2,
        max_rounds=4,
    )

    assert turn.assessment == "mastered"
    assert turn.diagnosed_misconception == "UNKNOWN"


@pytest.mark.parametrize(
    ("student_answer", "expected_value"),
    (
        ("YCL 0.6236", "0.6236"),
        ("YCL完成率为62.36%", "0.6236"),
    ),
)
def test_reviewed_extreme_confirmation_explains_the_bound_field_and_value(
    student_answer: str,
    expected_value: str,
) -> None:
    current_task = _current_task(_task_agent(), "T-03")

    confirmation = reviewed_answer_confirmation(
        question=(
            "根据刚才的三道工序结果，哪一道工序完成率最低，"
            "你依据的数值是什么？"
        ),
        answer=student_answer,
        evidence=current_task["evidence"],
    )

    assert confirmation == (
        f"已核验：YCL 的完成率为 {expected_value}，与查询结果中的最低值一致。"
    )


@pytest.mark.parametrize(
    "student_answer",
    (
        "YCL最低",
        "YCL完成率最低 0.9149",
        "AZTP完成率最低 0.6236",
        "0.6236",
    ),
)
def test_reviewed_extreme_guard_does_not_accept_incomplete_or_mismatched_evidence(
    student_answer: str,
) -> None:
    llm = FollowUpLLM(
        {
            "assessment": "unknown",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "UNKNOWN",
            "question": "查询结果中另外两道工序的完成率分别是多少？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer=student_answer,
        current_task=_current_task(task_agent, "T-03"),
        task_agent=task_agent,
        current_question=(
            "根据刚才的三道工序结果，哪一道工序完成率最低，"
            "你依据的数值是什么？"
        ),
        round_index=2,
        max_rounds=4,
    )

    assert turn.assessment == "unknown"


def test_model_cannot_master_an_extreme_answer_without_the_reviewed_value() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "mastered",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "NO_NEXT_TARGET",
            "question": "",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer="YCL完成率最低。",
        current_task=_current_task(task_agent, "T-03"),
        task_agent=task_agent,
        current_question=(
            "根据刚才的三道工序结果，哪一道工序完成率最低，"
            "你依据的数值是什么？"
        ),
        round_index=3,
        max_rounds=4,
        completion_allowed=True,
    )

    assert turn.assessment == "unknown"
    assert turn.next_target_misconception is not None
    assert turn.product is not None


def test_model_cannot_master_an_extreme_answer_with_an_ungrounded_percent_claim() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "mastered",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "NO_NEXT_TARGET",
            "question": "",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer=(
            "YCL完成率最低，为0.6236；但我判断完成率为9999%，"
            "所以所有工序均正常完成。"
        ),
        current_task=_current_task(task_agent, "T-03"),
        task_agent=task_agent,
        current_question=(
            "根据刚才的三道工序结果，哪一道工序完成率最低，"
            "你依据的数值是什么？"
        ),
        round_index=3,
        max_rounds=4,
        completion_allowed=True,
    )

    assert turn.assessment == "unknown"
    assert turn.next_target_misconception is not None
    assert turn.product is not None


def test_reviewed_extreme_correction_names_the_grounded_winner_and_wrong_row() -> None:
    current_task = _current_task(_task_agent(), "T-03")

    correction = reviewed_answer_correction(
        question=(
            "根据刚才的三道工序结果，哪一道工序完成率最低，"
            "你依据的数值是什么？"
        ),
        answer="ZZTP完成率最低，完成率为1.0249。",
        evidence=current_task["evidence"],
    )

    assert correction == (
        "需要纠正：查询结果显示 YCL 的完成率为 0.6236，是最低值；"
        "你回答中的 ZZTP 为 1.0249，不是最低值。"
    )


def test_reviewed_extreme_correction_does_not_turn_missing_evidence_into_an_error() -> None:
    current_task = _current_task(_task_agent(), "T-03")

    correction = reviewed_answer_correction(
        question=(
            "根据刚才的三道工序结果，哪一道工序完成率最低，"
            "你依据的数值是什么？"
        ),
        answer="YCL最低。",
        evidence=current_task["evidence"],
    )

    assert correction is None


def test_reviewed_completion_interpretation_overrides_an_unknown_false_negative() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "unknown",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "UNKNOWN",
            "question": "请再说明这个完成率与计划目标之间的关系？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress", llm_call=llm)

    turn = agent.generate(
        student_answer="0.6236，说明完成率低了。",
        current_task=_current_task(task_agent, "T-02"),
        task_agent=task_agent,
        current_question=(
            "根据刚才的查询结果，该工序的完成率是多少，"
            "这个数值说明了怎样的完成情况？"
        ),
        round_index=2,
        max_rounds=4,
    )

    assert turn.assessment == "mastered"
    assert turn.diagnosed_misconception == "UNKNOWN"


def test_completion_rate_template_fallback_stays_within_single_value_evidence() -> None:
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress")
    current_task = _current_task(task_agent, "T-02")
    expected = (
        "查询结果中的完成率数值是多少？",
        "请复述查询结果给出的完成率数值？",
        "查询结果给出的完成率具体是多少？",
    )
    previous_questions: list[str] = []

    for round_index, expected_question in enumerate(expected, start=2):
        turn = agent.deterministic_fallback(
            current_task=current_task,
            round_index=round_index,
            previous_questions=tuple(previous_questions),
        )
        assert turn.product is not None
        question = turn.product["payload"]["content"]["question"]
        assert question == expected_question
        assert "工序" not in question
        assert "计划" not in question
        assert "换算" not in question
        assert "说明" not in question
        previous_questions.append(question)


def test_high_risk_count_fallback_uses_the_template_output_contract() -> None:
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress")
    current_task = _current_task(task_agent, "T-10-A")

    turn = agent.deterministic_fallback(
        current_task=current_task,
        round_index=2,
    )

    assert turn.product is not None
    question = turn.product["payload"]["content"]["question"]
    assert question == "查询结果中WSA与WSB的高风险记录数分别是多少？"
    assert "完成率" not in question


def test_deterministic_fallback_skips_a_question_already_used_in_history() -> None:
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress")
    current_task = _current_task(task_agent, "T-02")
    first = agent.deterministic_fallback(
        current_task=current_task,
        round_index=2,
    )
    assert first.product is not None
    first_question = first.product["payload"]["content"]["question"]

    second = agent.deterministic_fallback(
        current_task=current_task,
        round_index=2,
        previous_questions=(first_question,),
    )
    assert second.product is not None
    assert second.product["payload"]["content"]["question"] != first_question


def test_deterministic_fallback_targets_the_missing_evidence_field() -> None:
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress")

    turn = agent.deterministic_fallback(
        current_task=_current_task(task_agent, "T-03"),
        round_index=3,
        required_evidence_fields=("process_code",),
    )

    assert turn.product is not None
    question = turn.product["payload"]["content"]["question"]
    assert "工序" in question


def test_deterministic_fallback_preserves_required_correction_target() -> None:
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress")

    turn = agent.deterministic_fallback(
        current_task=_current_task(task_agent, "T-05-A"),
        round_index=4,
        task_agent=task_agent,
        required_target="M-03",
    )

    assert turn.assessment == "unknown"
    assert turn.diagnosed_misconception == "M-03"
    assert turn.next_target_misconception == "M-03"
    assert turn.route_support_points == ()


def test_plan_actual_correction_uses_only_reviewed_scalar_fields() -> None:
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress")
    current_task = _current_task(task_agent, "T-03-A")
    expected = (
        "查询结果中的计划量和实际完成量分别是多少？",
        "查询结果给出的计划量数值是多少？",
        "查询结果给出的实际完成量数值是多少？",
    )
    previous_questions: list[str] = []

    for round_index, expected_question in enumerate(expected, start=2):
        turn = agent.deterministic_fallback(
            current_task=current_task,
            round_index=round_index,
            previous_questions=tuple(previous_questions),
            task_agent=task_agent,
            required_target="M-01",
        )
        assert turn.product is not None
        question = turn.product["payload"]["content"]["question"]
        assert question == expected_question
        assert "业务含义" not in question
        assert "区别" not in question
        previous_questions.append(question)


def test_generate_passes_missing_evidence_fields_to_the_model() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": "三道工序中哪一道完成率最低，你依据的工序和值是什么？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress", llm_call=llm)

    agent.generate(
        student_answer="只写了0.6236。",
        current_task=_current_task(task_agent, "T-02"),
        task_agent=task_agent,
        round_index=2,
        required_evidence_fields=("process_code",),
    )

    request = json.loads(llm.calls[0]["user"])
    assert request["required_evidence_fields"] == ["process_code"]


def test_generate_rejects_a_superficially_rephrased_history_question() -> None:
    repeated = "三道工序中哪一道完成率最低，你依据的工序和值是什么？"
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": f"请问{repeated}",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production-progress", llm_call=llm)

    with pytest.raises(FollowUpGenerationError, match="repeats history"):
        agent.generate(
            student_answer="我还不能确认最低工序。",
            current_task=_current_task(task_agent, "T-03"),
            task_agent=task_agent,
            round_index=2,
            previous_questions=(repeated,),
        )


def test_reviewed_ship_extreme_is_preserved_when_the_model_is_unavailable() -> None:
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress")
    current_task = _current_task(task_agent, "T-01-A")
    question = (
        "根据刚才的船号对比，哪一艘船的完成率最低，"
        "你依据的数值是什么？"
    )

    continued = agent.deterministic_fallback(
        current_task=current_task,
        round_index=2,
        student_answer="H2601 0.6236",
        current_question=question,
    )
    assert continued.assessment == "mastered"
    assert continued.product is not None
    assert continued.model == "deterministic-reviewed-answer"

    completed = agent.deterministic_fallback(
        current_task=current_task,
        round_index=3,
        student_answer="H2601 62.36%",
        current_question=question,
        completion_allowed=True,
    )
    assert completed.assessment == "mastered"
    assert completed.product is None


def test_advanced_delay_fallback_uses_template_specific_questions() -> None:
    task_agent = _task_agent()
    current_task = _current_task(task_agent, "T-07-B")
    agent = FollowUpAgent("trace-production_progress")

    questions = []
    for round_index in (2, 3, 4):
        turn = agent.deterministic_fallback(
            current_task=current_task,
            round_index=round_index,
        )
        assert turn.product is not None
        content = turn.product["payload"]["content"]
        assert content["standard_stem"] == (
            current_task["payload"]["content"]["standard_stem"]
        )
        questions.append(content["question"])

    assert questions == [
        "查询结果中YCL在2025-05、ZZTP在2025-06和AZTP在2025-07的完成率分别是多少？",
        "三道工序的完成率低点分别出现在哪个月？",
        "只依据当前月度表，四态候选应归为哪一种状态？",
    ]


def test_applied_three_process_fallback_never_uses_an_unresolved_process_reference() -> None:
    task_agent = _task_agent()
    current_task = _current_task(task_agent, "T-03-A")
    agent = FollowUpAgent("trace-production_progress")

    turn = agent.deterministic_fallback(
        current_task=current_task,
        round_index=4,
    )

    assert turn.product is not None
    question = turn.product["payload"]["content"]["question"]
    assert "另外两道工序" not in question
    assert "每个月完成率最低的工序" in question
    assert "因果" not in question
    assert all(month in question for month in ("2025-05", "2025-06", "2025-07"))


def test_generic_three_process_fallback_only_requests_fields_in_the_active_evidence() -> None:
    task_agent = _task_agent()
    current_task = _current_task(task_agent, "T-08-DECAY-A")
    agent = FollowUpAgent("trace-production_progress")

    turn = agent.deterministic_fallback(
        current_task=current_task,
        round_index=3,
    )

    assert turn.product is not None
    question = turn.product["payload"]["content"]["question"]
    assert question == "查询结果中AZTP在2025-06的完成率是多少？"

    final_turn = agent.deterministic_fallback(
        current_task=current_task,
        round_index=4,
    )
    assert final_turn.product is not None
    assert final_turn.product["payload"]["content"]["question"] == (
        "查询结果中AZTP在2025-07的完成率是多少？"
    )


@pytest.mark.parametrize("domain_id", ("production_progress", "first_segment"))
def test_every_template_has_three_distinct_fallback_questions(
    domain_id: str,
) -> None:
    task_agent = _task_agent(domain_id)
    catalog = load_task_catalog(domain_config=load_domain_config(domain_id))
    agent = FollowUpAgent(f"trace-{domain_id}")

    for template_id in catalog.templates:
        current_task = _current_task(task_agent, template_id)
        questions = []
        for round_index in (2, 3, 4):
            turn = agent.deterministic_fallback(
                current_task=current_task,
                round_index=round_index,
            )
            assert turn.product is not None
            questions.append(turn.product["payload"]["content"]["question"])
        assert len(set(questions)) == 3, template_id
        assert not any(
            unresolved in question
            for question in questions
            for unresolved in ("另外两道工序", "其他船号", "其他责任单元")
        ), template_id


def test_mastered_turn_discards_an_unneeded_model_question() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "mastered",
            "diagnosed_misconception": "M-03",
            "next_target_misconception": "NO_NEXT_TARGET",
            "question": "是否还要继续追问？",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer="五月是显著的单期偏低信号，但不能当作随机噪声忽略。",
        current_task=_current_task(task_agent, "T-05-A"),
        task_agent=task_agent,
        round_index=3,
        max_rounds=4,
        completion_allowed=True,
    )

    assert turn.assessment == "mastered"
    assert turn.next_target_misconception is None
    assert turn.product is None


def test_terminal_round_never_generates_a_fifth_question() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "NO_NEXT_TARGET",
            "question": "",
        }
    )
    task_agent = _task_agent()
    agent = FollowUpAgent("trace-production_progress", llm_call=llm)

    turn = agent.generate(
        student_answer="我仍然把计划量当作完成量。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=4,
        max_rounds=4,
        completion_allowed=True,
        terminal_round=True,
    )

    assert turn.assessment == "needs_support"
    assert turn.product is None


@pytest.mark.parametrize(
    "raw",
    (
        "",
        " ",
        "对",
        "\u200b\u200d",
        "T15",
        "m-01",
        "msgId",
        "rule_hits",
        "HTTP 409",
        "/api/sessions/abc",
        "orchestrator",
        "x" * 501,
    ),
)
def test_learner_input_validation_fails_closed(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_learner_input(raw)


def test_learner_input_validation_accepts_business_language_and_nfkc() -> None:
    assert normalize_learner_input("  应以实际完成量判断。  ") == "应以实际完成量判断。"
    assert normalize_learner_input("完成率是４２．３１％。") == "完成率是42.31%。"
