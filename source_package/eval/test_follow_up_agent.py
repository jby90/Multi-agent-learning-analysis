from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from agents.domain_config import load_domain_config
from agents.follow_up_agent import (
    FollowUpAgent,
    FollowUpGenerationError,
    normalize_learner_input,
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
            "target_misconception": "M-01",
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
    assert turn.target_misconception == "M-01"
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
            "target_misconception"
        ]["enum"]
    ) == {"M-01", "M-02", "M-03", "M-04", "M-05", "UNKNOWN"}


def test_follow_up_agent_unknown_target_uses_only_current_task_evidence() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "unknown",
            "target_misconception": "UNKNOWN",
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
            "target_misconception": "M-01",
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
            "target_misconception": "M-FS01",
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
    assert turn.target_misconception == "M-FS01"
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
            "target_misconception": "M-01",
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
            "target_misconception": "M-01",
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
    assert turn.target_misconception == "M-01"
    assert turn.product is None


def test_mastered_turn_discards_an_unneeded_model_question() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "mastered",
            "target_misconception": "M-03",
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
    assert turn.target_misconception == "M-03"
    assert turn.product is None


def test_terminal_round_never_generates_a_fifth_question() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "target_misconception": "M-01",
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
