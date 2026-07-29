from __future__ import annotations

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
from eval.test_p5_interactive_follow_up import FollowUpScript, _response
from orchestrator.interactive_session import (
    FollowUpReviewPolicy,
    InteractiveSessionManager,
    _feedback_for_re_verdict,
)


def _verdict(
    product: Mapping[str, Any],
    decision: str,
    *,
    role: str = "verdict",
    rule_id: str = "R-02",
    reason: str = "INTERNAL-REASON-DO-NOT-LEAK",
    reviewed_msg_id: str | None = None,
) -> dict[str, Any]:
    hits = []
    evidence = []
    if decision == "reject":
        hits = [
            {
                "rule_id": rule_id,
                "reason": reason,
                "evidence_ref": str(product["msg_id"]),
            }
        ]
        evidence = [
            {
                "kind": "review_rule",
                "ref": rule_id,
                "quote": reason,
            }
        ]
    return {
        "trace_id": product["trace_id"],
        "agent": "review",
        "role": role,
        "payload": {
            "type": "review_verdict",
            "content": {
                "event": "review_complete",
                "reviewed_payload_type": product["payload"]["type"],
                "reviewed_msg_id": (
                    reviewed_msg_id
                    if reviewed_msg_id is not None
                    else product["msg_id"]
                ),
            },
        },
        "evidence": evidence,
        "claims": [],
        "verdict": {
            "decision": decision,
            "rule_hits": hits,
            "difficulty_action": "none",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _start_session(
    tmp_path: Path,
    follow_up: FollowUpScript,
    policy: FollowUpReviewPolicy,
) -> tuple[InteractiveSessionManager, str]:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up,
        executor_factory=CatalogExecutor,
        follow_up_review_policy=policy,
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
    state = manager.advance(session_id)
    assert state["awaiting"] == "follow_up"
    return manager, session_id


def _run_three_candidate_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: FollowUpReviewPolicy,
    *,
    first_rule: str = "R-02",
) -> tuple[dict[str, Any], FollowUpScript, list[str]]:
    questions = (
        "第一版证据约束追问？",
        "第二版证据约束追问？",
        "第三版证据约束追问？",
    )
    follow_up = FollowUpScript(
        *(_response("needs_support", question) for question in questions)
    )
    original_review = ReviewAgent.review
    original_re_review = ReviewAgent.re_review
    original_generate = RebuttalGenerator.generate
    review_calls = 0
    model_attempts: list[str] = []

    def scripted_review(
        self: ReviewAgent,
        product: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal review_calls
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_review(self, product, **kwargs)
        review_calls += 1
        if review_calls <= 2:
            rule_id = first_rule if review_calls == 1 else "R-02"
            return _verdict(
                product,
                "reject",
                rule_id=rule_id,
                reason=f"INTERNAL-{rule_id}-REASON-{review_calls}",
            )
        return _verdict(product, "approve")

    def scripted_re_review(
        self: ReviewAgent,
        product: Mapping[str, Any],
        original: Mapping[str, Any],
        rebuttal: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_re_review(
                self,
                product,
                original,
                rebuttal,
                **kwargs,
            )
        rule_id = str(original["verdict"]["rule_hits"][0]["rule_id"])
        return _verdict(
            product,
            "reject",
            role="re_verdict",
            rule_id=rule_id,
            reason=f"AUDITED-{rule_id}-REASON",
        )

    def model_rebuttal_attempt(
        self: RebuttalGenerator,
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_generate(self, product, verdict)
        model_attempts.append(str(verdict["msg_id"]))
        return self.deterministic_concede(product, verdict)

    monkeypatch.setattr(ReviewAgent, "review", scripted_review)
    monkeypatch.setattr(ReviewAgent, "re_review", scripted_re_review)
    monkeypatch.setattr(RebuttalGenerator, "generate", model_rebuttal_attempt)
    manager, session_id = _start_session(tmp_path, follow_up, policy)

    state = manager.submit_follow_up(
        session_id,
        "计划数量可以直接当作实际完成数量。",
        "innovation-a-turn",
    )
    assert state["interaction"]["prompt"] == questions[-1]
    return state, follow_up, model_attempts


@pytest.mark.parametrize("budget", [0, 1, 4])
@pytest.mark.parametrize("mode", ["generic", "mapped"])
def test_follow_up_review_policy_accepts_only_frozen_ablation_values(
    budget: int,
    mode: str,
) -> None:
    policy = FollowUpReviewPolicy(
        rebuttal_budget=budget,
        feedback_mode=mode,
    )
    assert policy.rebuttal_budget == budget
    assert policy.feedback_mode == mode


@pytest.mark.parametrize("budget", [-1, 2, 5, True])
def test_follow_up_review_policy_rejects_unapproved_budgets(budget: Any) -> None:
    with pytest.raises(ValueError, match="rebuttal_budget"):
        FollowUpReviewPolicy(rebuttal_budget=budget)


def test_a1_budget_is_transaction_wide_and_feedback_is_audited_and_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, follow_up, model_attempts = _run_three_candidate_review(
        tmp_path,
        monkeypatch,
        FollowUpReviewPolicy(rebuttal_budget=1, feedback_mode="mapped"),
    )

    assert len(model_attempts) == 1
    trace_roles = [message["role"] for message in state["messages"]]
    assert trace_roles.count("rebuttal") == 2
    assert trace_roles.count("re_verdict") == 2
    generation_inputs = [json.loads(call["user"]) for call in follow_up.calls]
    assert generation_inputs[0]["review_feedback"] == []
    assert generation_inputs[1]["review_feedback"] == [
        "让问题与所给专业材料之间的支持关系更明确。"
    ]
    assert generation_inputs[2]["review_feedback"] == [
        "让问题与所给专业材料之间的支持关系更明确。"
    ]
    encoded = json.dumps(generation_inputs[1:], ensure_ascii=False)
    assert "INTERNAL-R-02" not in encoded
    assert "AUDITED-R-02" not in encoded
    assert "R-02" not in encoded
    assert "rule_hits" not in encoded


def test_a0_switch_preserves_per_cycle_rebuttal_and_generic_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, follow_up, model_attempts = _run_three_candidate_review(
        tmp_path,
        monkeypatch,
        FollowUpReviewPolicy(rebuttal_budget=4, feedback_mode="generic"),
    )

    assert len(model_attempts) == 2
    generation_inputs = [json.loads(call["user"]) for call in follow_up.calls]
    assert generation_inputs[1]["review_feedback"] == [
        "上一版问题未通过专业审核，请重新组织。"
    ]
    assert generation_inputs[2]["review_feedback"] == [
        "上一版问题未通过专业审核，请重新组织。"
    ]


def test_hard_rule_regeneration_does_not_consume_the_soft_rule_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, follow_up, model_attempts = _run_three_candidate_review(
        tmp_path,
        monkeypatch,
        FollowUpReviewPolicy(rebuttal_budget=1, feedback_mode="mapped"),
        first_rule="R-04",
    )

    assert len(model_attempts) == 1
    assert [message["role"] for message in state["messages"]].count("rebuttal") == 1
    generation_inputs = [json.loads(call["user"]) for call in follow_up.calls]
    assert generation_inputs[1]["review_feedback"] == [
        "上一版问题未通过专业审核，请重新组织。"
    ]
    assert generation_inputs[2]["review_feedback"] == [
        "让问题与所给专业材料之间的支持关系更明确。"
    ]


def test_feedback_is_committed_only_for_matching_audited_re_verdict() -> None:
    product = {
        "trace_id": "trace",
        "msg_id": "trace-product-001",
        "payload": {"type": "quiz_set"},
    }
    matching = _verdict(
        product,
        "reject",
        role="re_verdict",
        rule_id="R-03",
    )
    matching["msg_id"] = "trace-review-001"
    cross_wired = {
        **matching,
        "payload": {
            **matching["payload"],
            "content": {
                **matching["payload"]["content"],
                "reviewed_msg_id": "another-product",
            },
        },
    }

    assert _feedback_for_re_verdict(matching, product) == (
        "保持学习目标和既定难度档不变，调整问题的表达、铺垫和认知负荷。",
    )
    assert _feedback_for_re_verdict(cross_wired, product) == ()


def test_deterministic_concede_reuses_full_product_verdict_association_check() -> None:
    product = {
        "trace_id": "trace-shared-validation",
        "msg_id": "trace-shared-validation-001",
        "agent": "task",
        "role": "probe",
        "payload": {"type": "quiz_set", "content": {"question": "问题"}},
        "evidence": [],
    }
    verdict = _verdict(product, "reject")
    verdict["msg_id"] = "trace-shared-validation-002"
    generator = RebuttalGenerator("trace-shared-validation")

    conceded = generator.deterministic_concede(product, verdict)
    assert conceded["payload"]["content"]["concede"] is True

    cross_wired = {
        **verdict,
        "payload": {
            **verdict["payload"],
            "content": {
                **verdict["payload"]["content"],
                "reviewed_msg_id": "another-product",
            },
        },
    }
    with pytest.raises(ValueError, match="association"):
        generator.deterministic_concede(product, cross_wired)

    empty_reject = {
        **verdict,
        "verdict": {
            **verdict["verdict"],
            "rule_hits": [],
        },
    }
    with pytest.raises(ValueError, match="rule_hits"):
        generator.deterministic_concede(product, empty_reject)
