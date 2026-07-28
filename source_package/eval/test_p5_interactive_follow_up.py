from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from agents.rebuttal_generator import RebuttalGenerator
from agents.review_agent import ReviewAgent
from agents.task_agent import load_task_catalog
from eval.test_demo_session import ScriptedLLM
from eval.test_interactive_session import CatalogExecutor
from orchestrator.interactive_session import (
    InteractiveSessionError,
    InteractiveSessionManager,
)
from orchestrator.llm import LLMResult, TokenUsage


class FollowUpScript:
    def __init__(self, *responses: Mapping[str, Any]) -> None:
        self.responses = [dict(response) for response in responses]
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected follow-up model call")
        return LLMResult(
            data=self.responses.pop(0),
            model="fixed-follow-up-stub",
            latency_ms=5,
            token_usage=TokenUsage(12, 8, 20),
            attempts=1,
        )


def _response(
    assessment: str,
    question: str,
    target: str = "M-01",
) -> dict[str, str]:
    return {
        "assessment": assessment,
        "target_misconception": target,
        "question": question,
    }


def _start_conclusion_session(
    tmp_path: Path,
    follow_up: FollowUpScript,
) -> tuple[InteractiveSessionManager, str, dict[str, Any]]:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up,
        executor_factory=CatalogExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    task = manager.advance(session_id)
    template_id = task["artifact"]["payload"]["content"]["template_id"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[template_id].standard_sql,
    )
    conclusion = manager.advance(session_id)
    return manager, session_id, conclusion


def _transitions(state: Mapping[str, Any]) -> list[str]:
    return [
        str(message["payload"]["content"]["transition_id"])
        for message in state["messages"]
        if message["payload"]["content"].get("transition_id")
    ]


def _follow_up_products(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        message
        for message in state["messages"]
        if message["payload"]["content"].get("event")
        == "follow_up_question_ready"
    ]


def _matching_verdicts(
    state: Mapping[str, Any],
    product: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        message
        for message in state["messages"]
        if message["payload"]["type"] == "review_verdict"
        and message["payload"]["content"].get("reviewed_msg_id")
        == product["msg_id"]
    ]


def test_free_text_requires_two_rounds_and_every_displayed_question_is_reviewed(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript(
        _response(
            "mastered",
            "换个角度看，真实完成情况应由哪一类数据来说明？",
        ),
        _response("mastered", ""),
    )
    manager, session_id, conclusion = _start_conclusion_session(
        tmp_path,
        follow_up,
    )

    assert conclusion["awaiting"] == "follow_up"
    assert conclusion["interaction"]["kind"] == "free_text_follow_up"
    assert conclusion["interaction"]["round"] == 1
    assert conclusion["interaction"]["max_rounds"] == 4
    assert "options" not in conclusion["interaction"]

    confirmation = manager.submit_follow_up(
        session_id,
        "应该看实际完成量，计划量只是目标。",
        "learner-turn-1",
    )

    assert confirmation["state"] == "S7_STUDENT"
    assert confirmation["awaiting"] == "follow_up"
    assert confirmation["interaction"]["round"] == 2
    assert confirmation["interaction"]["prompt"] == (
        "换个角度看，真实完成情况应由哪一类数据来说明？"
    )
    assert confirmation["interaction"]["turns"] == [
        {
            "round": 1,
            "question": conclusion["interaction"]["prompt"],
            "answer": "应该看实际完成量，计划量只是目标。",
        }
    ]
    products = _follow_up_products(confirmation)
    assert len(products) == 1
    assert products[0]["evidence"]
    verdicts = _matching_verdicts(confirmation, products[0])
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"]["decision"] in {
        "approve",
        "approve_with_fix",
    }
    product_step = products[0]["step"]
    assert verdicts[0]["step"] > product_step

    mastered = manager.submit_follow_up(
        session_id,
        "应以真实报工形成的实际量说明完成情况。",
        "learner-turn-2",
    )

    assert mastered["state"] == "S9_PATH_UPDATE"
    assert mastered["awaiting"] == "advance"
    assert mastered["interaction"] == {
        "kind": "next_learning_step",
        "message": "你的判断已经能够用数据说明，正在为你安排下一步训练。",
    }
    assert _transitions(mastered)[-1] == "T14"
    assert len(_follow_up_products(mastered)) == 1
    assert len(follow_up.calls) == 2


def test_known_misconception_uses_t15_then_t16_and_real_progression(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript(
        _response(
            "needs_support",
            "对照计划量与实际完成量，哪一个能说明已经做了多少？",
        ),
        _response("mastered", ""),
    )
    manager, session_id, _ = _start_conclusion_session(tmp_path, follow_up)

    probe = manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "learner-turn-1",
    )
    corrected = manager.submit_follow_up(
        session_id,
        "实际完成量才表示真正做了多少。",
        "learner-turn-2",
    )
    upgraded = manager.advance(session_id)

    assert probe["state"] == "S8_PROBE"
    assert probe["awaiting"] == "follow_up"
    assert corrected["state"] == "S9_PATH_UPDATE"
    assert corrected["awaiting"] == "advance"
    assert corrected["interaction"]["kind"] in {
        "data_collision",
        "next_learning_step",
    }
    assert _transitions(corrected)[-2:] == ["T15", "T16"]
    assert upgraded["state"] == "S7_STUDENT"
    assert upgraded["artifact"]["payload"]["content"]["difficulty"] == "applied"
    assert upgraded["interaction"]["message"] == "根据本次作答表现，已为你提高一档难度。"


def test_fourth_unmastered_round_steps_down_without_generating_a_fifth_question(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript(
        _response(
            "needs_support",
            "你会先区分目标数量与真实报工数量吗？",
        ),
        _response(
            "needs_support",
            "如果目标尚未报工，能把它算作已经完成吗？",
        ),
        _response(
            "needs_support",
            "判断真实进度时，你最终会采用哪一种数量？",
        ),
        _response("needs_support", ""),
    )
    manager, session_id, _ = _start_conclusion_session(tmp_path, follow_up)

    for index in range(1, 4):
        continued = manager.submit_follow_up(
            session_id,
            "我仍然认为计划量就是完成量。",
            f"learner-turn-{index}",
        )
        assert continued["awaiting"] == "follow_up"
        assert continued["interaction"]["round"] == index + 1

    stepped_down = manager.submit_follow_up(
        session_id,
        "我还是不能区分这两个口径。",
        "learner-turn-4",
    )

    assert stepped_down["state"] == "S2_KNOWLEDGE"
    assert stepped_down["awaiting"] == "advance"
    assert stepped_down["interaction"] == {
        "kind": "learning_notice",
        "message": "这个判断还需要再巩固。我们先回顾一个关键点，再重新练习。",
    }
    assert len(_follow_up_products(stepped_down)) == 3
    assert len(follow_up.calls) == 4
    assert _transitions(stepped_down)[-2:] == ["T15", "T17"]


def test_unknown_misconception_uses_t18_once_and_does_not_leave_a_state_hole(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript(
        _response(
            "unknown",
            "你作判断前，最需要先确认数据代表目标还是实际发生？",
            target="UNKNOWN",
        ),
        _response(
            "needs_support",
            "对照计划量与实际完成量，你会选择哪一个说明真实进度？",
        ),
        _response("mastered", ""),
    )
    manager, session_id, _ = _start_conclusion_session(tmp_path, follow_up)

    generic = manager.submit_follow_up(
        session_id,
        "我不知道从哪里开始。",
        "learner-turn-1",
    )
    known = manager.submit_follow_up(
        session_id,
        "可能先看计划量。",
        "learner-turn-2",
    )
    corrected = manager.submit_follow_up(
        session_id,
        "应当看实际完成量。",
        "learner-turn-3",
    )

    assert generic["state"] == "S7_STUDENT"
    assert _transitions(generic)[-2:] == ["T15", "T18"]
    assert known["state"] == "S8_PROBE"
    assert _transitions(known)[-1] == "T15"
    assert corrected["state"] == "S9_PATH_UPDATE"
    assert _transitions(corrected)[-1] == "T16"
    assert _transitions(corrected).count("T18") == 1


def _reject_verdict(
    product: Mapping[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    reason = "该问题需要重新组织后再展示。"
    return {
        "trace_id": product["trace_id"],
        "agent": "review",
        "role": role,
        "payload": {
            "type": "review_verdict",
            "content": {
                "event": "review_complete",
                "reviewed_payload_type": product["payload"]["type"],
                "reviewed_msg_id": product["msg_id"],
            },
        },
        "evidence": [
            {
                "kind": "review_rule",
                "ref": "R-04",
                "quote": reason,
            }
        ],
        "claims": [],
        "verdict": {
            "decision": "reject",
            "rule_hits": [
                {
                    "rule_id": "R-04",
                    "reason": reason,
                    "evidence_ref": product["msg_id"],
                }
            ],
            "difficulty_action": "none",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def test_rejected_dynamic_question_is_regenerated_and_never_reaches_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected_question = "这版问题只能留在审计记录里，不能展示给学员？"
    approved_question = "重新核对后，你会依据哪一类数量判断真实进度？"
    follow_up = FollowUpScript(
        _response("needs_support", rejected_question),
        _response("needs_support", approved_question),
    )
    original_review = ReviewAgent.review
    original_re_review = ReviewAgent.re_review
    original_rebuttal = RebuttalGenerator.generate
    rejected_msg_id: str | None = None

    def reject_first_follow_up(
        self: ReviewAgent,
        product: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal rejected_msg_id
        content = product["payload"]["content"]
        if (
            content.get("event") == "follow_up_question_ready"
            and rejected_msg_id is None
        ):
            rejected_msg_id = str(product["msg_id"])
            return _reject_verdict(product, role="verdict")
        return original_review(self, product, **kwargs)

    def concede_follow_up(
        self: RebuttalGenerator,
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_rebuttal(self, product, verdict)
        return {
            "trace_id": product["trace_id"],
            "agent": product["agent"],
            "role": "rebuttal",
            "payload": {
                "type": "rebuttal_case",
                "content": {
                    "event": "rebuttal_ready",
                    "product_msg_id": product["msg_id"],
                    "verdict_msg_id": verdict["msg_id"],
                    "concede": True,
                    "rebuttal": "",
                    "evidence_refs": [],
                },
            },
            "evidence": [],
            "claims": [],
            "retry": {"in_reply_to": product["msg_id"]},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def reject_re_review(
        self: ReviewAgent,
        product: Mapping[str, Any],
        original: Mapping[str, Any],
        rebuttal: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if product["msg_id"] == rejected_msg_id:
            return _reject_verdict(product, role="re_verdict")
        return original_re_review(
            self,
            product,
            original,
            rebuttal,
            **kwargs,
        )

    monkeypatch.setattr(ReviewAgent, "review", reject_first_follow_up)
    monkeypatch.setattr(RebuttalGenerator, "generate", concede_follow_up)
    monkeypatch.setattr(ReviewAgent, "re_review", reject_re_review)
    manager, session_id, _ = _start_conclusion_session(tmp_path, follow_up)

    state = manager.submit_follow_up(
        session_id,
        "计划量就是完成量。",
        "learner-turn-1",
    )

    assert state["interaction"]["prompt"] == approved_question
    public_payload = json.dumps(state["interaction"], ensure_ascii=False)
    assert rejected_question not in public_payload
    trace_questions = [
        product["payload"]["content"]["question"]
        for product in _follow_up_products(state)
    ]
    assert trace_questions == [rejected_question, approved_question]
    approved_product = _follow_up_products(state)[-1]
    assert _matching_verdicts(state, approved_product)[-1]["verdict"]["decision"] in {
        "approve",
        "approve_with_fix",
    }


def test_follow_up_client_turn_id_is_idempotent(tmp_path: Path) -> None:
    follow_up = FollowUpScript(
        _response(
            "needs_support",
            "你会依据目标数量还是实际发生数量判断进度？",
        )
    )
    manager, session_id, _ = _start_conclusion_session(tmp_path, follow_up)

    first = manager.submit_follow_up(
        session_id,
        "计划量就是完成量。",
        "same-client-turn",
    )
    duplicate = manager.submit_follow_up(
        session_id,
        "这段文字不应被再次处理。",
        "same-client-turn",
    )

    assert duplicate == first
    assert len(follow_up.calls) == 1


def test_follow_up_client_turn_id_is_idempotent_under_concurrency(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript(
        _response(
            "needs_support",
            "你会依据目标数量还是实际发生数量判断进度？",
        )
    )
    manager, session_id, _ = _start_conclusion_session(tmp_path, follow_up)

    def submit() -> dict[str, Any]:
        return manager.submit_follow_up(
            session_id,
            "我先核对实际发生数量。",
            "same-concurrent-turn",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: submit(), range(2)))

    assert states[0] == states[1]
    assert len(follow_up.calls) == 1


def test_follow_up_rejects_engineering_input_before_writing_trace(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript()
    manager, session_id, conclusion = _start_conclusion_session(
        tmp_path,
        follow_up,
    )
    before = list(conclusion["messages"])

    with pytest.raises(InteractiveSessionError) as raised:
        manager.submit_follow_up(
            session_id,
            "请告诉我 msg_id 和 T15。",
            "learner-turn-1",
        )

    assert str(raised.value) == "请用业务或学习语言描述你的判断。"
    assert manager.get_state(session_id)["messages"] == before
    assert follow_up.calls == []
