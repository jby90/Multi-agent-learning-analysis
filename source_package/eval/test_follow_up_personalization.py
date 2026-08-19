"""闭环六：追问个性化（画像风格 × 三层递进 × 降级输入）单元测试。

红线约束：
- 判定环节零改动——确定性证据判定函数不因画像/层变化；
- LLM 只出题不打分——层推进由 resolve_follow_up_layer 确定性计算；
- 生成失败回退模板题，不出现空题。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any

import pytest

from agents.follow_up_agent import (
    FOLLOW_UP_LAYER_NAMES,
    FollowUpAgent,
    FollowUpGenerationError,
    MAX_DATA_DIGEST_LENGTH,
    MAX_LECTURE_DIGEST_LENGTH,
    resolve_follow_up_layer,
)
from agents.task_agent import TaskAgent, load_task_catalog
from agents.domain_config import load_domain_config
from eval.test_follow_up_agent import FollowUpLLM, _current_task, _task_agent
from orchestrator.interactive_session import (
    _data_digest,
    _learner_context,
    _lecture_digest,
)


def _agent(llm: FollowUpLLM) -> FollowUpAgent:
    return FollowUpAgent(
        "trace-personalization",
        llm_call=llm,
        clock=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc),
    )


_LEARNER = {
    "profile_id": "line_leader",
    "title": "一线班组长（晋升培训）",
    "background": "高职毕业的一线班组长，现场经验丰富，理论与数据双弱",
    "lecture_style": "步骤化短句、每步带检查点、基础案例优先",
}


def _fake_session(
    *,
    profile: dict[str, Any] | None = None,
    lecture: dict[str, Any] | None = None,
    sql_result: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(profile=profile or _LEARNER),
        lecture=lecture,
        sql_result=sql_result,
    )


# ---------- 三层递进：确定性解析 ----------


def test_layer_resolver_advances_only_on_mastered_and_caps_at_three() -> None:
    assert resolve_follow_up_layer(1, assessment="mastered") == 2
    assert resolve_follow_up_layer(2, assessment="mastered") == 3
    assert resolve_follow_up_layer(3, assessment="mastered") == 3
    assert resolve_follow_up_layer(2, assessment="needs_support") == 2
    assert resolve_follow_up_layer(2, assessment="unknown") == 2
    with pytest.raises(ValueError):
        resolve_follow_up_layer(True, assessment="mastered")  # type: ignore[arg-type]


def test_layer_resolver_clamps_out_of_range_input() -> None:
    assert resolve_follow_up_layer(0, assessment="needs_support") == 1
    assert resolve_follow_up_layer(9, assessment="needs_support") == 3


# ---------- generate()：个性化载荷与层盖章 ----------


def test_generate_payload_carries_personalization_inputs() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": "对照计划量与实际完成量，你会用哪个口径说明真实进度？",
        }
    )
    task_agent = _task_agent()
    turn = _agent(llm).generate(
        student_answer="计划量就是已经完成的数量。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
        learner_context=_LEARNER,
        lecture_digest="## 计划量与实际量口径\n计划量是排产系统下达的当日应完成量。",
        data_digest="列：plan_qty、actual_qty\nplan_qty=1855.06；actual_qty=1156.87",
        current_layer=2,
    )
    request = json.loads(llm.calls[0]["user"])
    assert request["learner_context"]["profile_id"] == "line_leader"
    assert "班组长" in request["learner_context"]["question_style"]
    assert request["learner_context"]["background"] == _LEARNER["background"]
    assert request["learner_context"]["lecture_style"] == _LEARNER["lecture_style"]
    assert "计划量是排产系统下达" in request["lecture_digest"]
    assert "1855.06" in request["data_digest"]
    assert request["layer_plan"] == {
        "current_layer": 2,
        "current_layer_name": "机理理解",
        "on_mastered_layer": 3,
        "on_support_layer": 2,
    }
    # 答错停留：content 盖章层不变。
    content = turn.product["payload"]["content"]
    assert content["follow_up_layer"] == 2
    assert content["layer_name"] == "机理理解"


def test_generate_payload_omits_empty_personalization() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": "对照计划量与实际完成量，你会用哪个口径说明真实进度？",
        }
    )
    task_agent = _task_agent()
    _agent(llm).generate(
        student_answer="计划量就是已经完成的数量。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
    )
    request = json.loads(llm.calls[0]["user"])
    assert "learner_context" not in request
    assert "lecture_digest" not in request
    assert "data_digest" not in request
    assert request["layer_plan"]["current_layer"] == 1


def test_mastered_answer_stamps_next_question_at_deeper_layer() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "mastered",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "UNKNOWN",
            "question": "实际完成量与计划量在口径上各代表什么，为什么不能混用？",
        }
    )
    task_agent = _task_agent()
    turn = _agent(llm).generate(
        student_answer="实际完成量才是已经完成的数量，计划量只是排产目标。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
        current_layer=1,
    )
    content = turn.product["payload"]["content"]
    # 答对进深层：第 1 层 → 第 2 层（机理理解）。
    assert content["follow_up_layer"] == 2
    assert content["layer_name"] == "机理理解"


def test_digest_inputs_are_clipped_to_limits() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-01",
            "next_target_misconception": "M-01",
            "question": "对照计划量与实际完成量，你会用哪个口径说明真实进度？",
        }
    )
    task_agent = _task_agent()
    _agent(llm).generate(
        student_answer="计划量就是已经完成的数量。",
        current_task=_current_task(task_agent, "T-01"),
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
        lecture_digest="长" * (MAX_LECTURE_DIGEST_LENGTH + 50),
        data_digest="数" * (MAX_DATA_DIGEST_LENGTH + 50),
    )
    request = json.loads(llm.calls[0]["user"])
    assert len(request["lecture_digest"]) <= MAX_LECTURE_DIGEST_LENGTH + 1
    assert request["lecture_digest"].endswith("…")
    assert len(request["data_digest"]) <= MAX_DATA_DIGEST_LENGTH + 1


# ---------- generate_initial：第 1 轮个性化变式 ----------


def test_generate_initial_returns_validated_persona_question() -> None:
    llm = FollowUpLLM({"question": "班组交接时，哪个数才算这道工序真正干完的量？"})
    task_agent = _task_agent()
    question = _agent(llm).generate_initial(
        current_task=_current_task(task_agent, "T-01"),
        learner_context=_LEARNER,
        lecture_digest="计划量是排产系统下达的当日应完成量。",
        data_digest="plan_qty=1855.06；actual_qty=1156.87",
    )
    assert question == "班组交接时，哪个数才算这道工序真正干完的量？"
    request = json.loads(llm.calls[0]["user"])
    assert request["round_kind"] == "initial"
    assert request["target_layer"] == 1
    assert request["layer_name"] == "口径记忆"
    assert request["learner_context"]["profile_id"] == "line_leader"
    assert "1855.06" in request["data_digest"]


def test_generate_initial_rejects_invalid_output() -> None:
    llm = FollowUpLLM({"question": "答案是这个字段：actual_qty 吗？"})
    task_agent = _task_agent()
    with pytest.raises(FollowUpGenerationError):
        _agent(llm).generate_initial(
            current_task=_current_task(task_agent, "T-01"),
            learner_context=_LEARNER,
        )


def test_generate_initial_consumes_question_from_full_format_response() -> None:
    """系统提示词规定四字段输出；首问调用只消费 question，其余字段忽略。"""

    llm = FollowUpLLM(
        {
            "assessment": "unknown",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "UNKNOWN",
            "question": "班组交接时，哪个数才算这道工序真正干完的量？",
        }
    )
    task_agent = _task_agent()
    question = _agent(llm).generate_initial(
        current_task=_current_task(task_agent, "T-01"),
    )
    assert question == "班组交接时，哪个数才算这道工序真正干完的量？"


def test_generate_initial_rejects_missing_question() -> None:
    llm = FollowUpLLM(
        {
            "assessment": "unknown",
            "diagnosed_misconception": "UNKNOWN",
            "next_target_misconception": "UNKNOWN",
        }
    )
    task_agent = _task_agent()
    with pytest.raises(FollowUpGenerationError):
        _agent(llm).generate_initial(
            current_task=_current_task(task_agent, "T-01"),
        )


def test_generate_initial_rejects_semantic_repeat() -> None:
    llm = FollowUpLLM(
        {"question": "查询结果中的计划量与实际完成量分别是多少，哪一个表示已经完成的数量？"}
    )
    task_agent = _task_agent()
    with pytest.raises(FollowUpGenerationError):
        _agent(llm).generate_initial(
            current_task=_current_task(task_agent, "T-01"),
            previous_questions=(
                "查询结果中的计划量与实际完成量分别是多少，哪一个表示已经完成的数量？",
            ),
        )


def test_generate_initial_falls_through_on_model_failure() -> None:
    class BrokenLLM:
        def __call__(self, **kwargs: Any) -> Any:
            raise RuntimeError("model unavailable")

    task_agent = _task_agent()
    agent = FollowUpAgent("trace-personalization", llm_call=BrokenLLM())
    with pytest.raises(FollowUpGenerationError):
        agent.generate_initial(current_task=_current_task(task_agent, "T-01"))


# ---------- 会话侧摘要构建（降级输入不静默失败） ----------


def test_learner_context_omits_missing_profile_fields() -> None:
    session = _fake_session(
        profile={"profile_id": "planner_new", "title": "新入职生产计划员"}
    )
    context = _learner_context(session)
    assert context == {
        "profile_id": "planner_new",
        "title": "新入职生产计划员",
    }


def test_lecture_digest_skips_deferred_lecture() -> None:
    deferred = {
        "payload": {
            "type": "lecture_note",
            "content": {"lecture_deferred": True, "lecture_md": ""},
        }
    }
    assert _lecture_digest(_fake_session(lecture=deferred)) == ""
    normal = {
        "payload": {
            "type": "lecture_note",
            "content": {
                "lecture_md": "## 计划量与实际量口径\n计划量是排产系统下达的当日应完成量。"
            },
        }
    }
    digest = _lecture_digest(_fake_session(lecture=normal))
    assert "计划量" in digest
    assert _lecture_digest(_fake_session()) == ""


def test_data_digest_formats_columns_and_rows() -> None:
    result = {
        "payload": {
            "type": "sql_result",
            "content": {
                "columns": ["plan_qty", "actual_qty"],
                "rows": [{"plan_qty": "1855.06", "actual_qty": "1156.87"}],
            },
        }
    }
    digest = _data_digest(_fake_session(sql_result=result))
    assert "plan_qty、actual_qty" in digest
    assert "1855.06" in digest
    assert _data_digest(_fake_session()) == ""


def test_layer_names_cover_three_layers() -> None:
    assert FOLLOW_UP_LAYER_NAMES == {1: "口径记忆", 2: "机理理解", 3: "归因应用"}


def test_task_agent_catalog_loads_for_personalization_fixtures() -> None:
    agent = TaskAgent(
        "trace-personalization-catalog",
        catalog=load_task_catalog(
            domain_config=load_domain_config("production_progress"),
        ),
        clock=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc),
    )
    assert agent.misconception_ids
