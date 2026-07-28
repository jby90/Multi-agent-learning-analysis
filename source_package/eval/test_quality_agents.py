from __future__ import annotations

from typing import Any, Mapping, Sequence

import pytest

from agents.evidence_review_agent import EvidenceReviewAgent
from agents.pedagogy_review_agent import PedagogyReviewAgent
from agents.quality_protocol import (
    EvidenceReviewResult,
    PedagogyReviewResult,
    ReviewRuleHit,
)
from agents.review_arbiter import DeterministicReviewArbiter
from coordination.artifacts import ArtifactEnvelope


def _artifact() -> ArtifactEnvelope:
    return ArtifactEnvelope.from_message(
        {
            "msg_id": "product-1",
            "trace_id": "trace-quality",
            "agent": "knowledge",
            "role": "produce",
            "payload": {"type": "lecture_note", "content": {"difficulty": "basic"}},
            "claims": [],
            "evidence": [],
        },
        contract_id="lc-quality",
    )


def test_specialist_agents_return_distinct_typed_results() -> None:
    evidence_inputs: list[Mapping[str, Any]] = []
    pedagogy_inputs: list[Mapping[str, Any]] = []

    def evidence_evaluator(product: Mapping[str, Any]):
        evidence_inputs.append(product)
        return (), (), 3

    def pedagogy_evaluator(
        product: Mapping[str, Any],
        report: Mapping[str, Any] | None,
        profile: Mapping[str, Any] | None,
        learned: Sequence[str],
    ):
        del report, profile, learned
        pedagogy_inputs.append(product)
        return None, None, "keep", 0

    artifact = _artifact()
    evidence = EvidenceReviewAgent("trace-quality", evidence_evaluator).review(artifact)
    pedagogy = PedagogyReviewAgent("trace-quality", pedagogy_evaluator).review(
        artifact,
        learning_report=None,
        student_profile=None,
        learned_knowledge_points=(),
    )

    assert evidence.agent_id == "evidence_review"
    assert pedagogy.agent_id == "pedagogy_review"
    assert evidence.checks == 3
    assert evidence_inputs[0] is not pedagogy_inputs[0]
    assert evidence.artifact_id == pedagogy.artifact_id == "product-1"


def _evidence_result(*hits: ReviewRuleHit) -> EvidenceReviewResult:
    return EvidenceReviewResult(
        agent_id="evidence_review",
        artifact_id="product-1",
        contract_id="lc-quality",
        hits=hits,
        llm_results=(),
        checks=1,
    )


def _pedagogy_result(
    hit: ReviewRuleHit | None = None,
    *,
    gap: int | None = 0,
) -> PedagogyReviewResult:
    return PedagogyReviewResult(
        agent_id="pedagogy_review",
        artifact_id="product-1",
        contract_id="lc-quality",
        hit=hit,
        llm_result=None,
        difficulty_action="step_down" if hit else "keep",
        difficulty_gap=gap,
    )


def test_model_free_arbiter_applies_canonical_decision_table() -> None:
    arbiter = DeterministicReviewArbiter()
    r03 = ReviewRuleHit("R-03", "difficulty gap", "diagnosis-1")
    r02 = ReviewRuleHit("R-02", "unsupported fact", "kb-1")

    assert arbiter.decide(_evidence_result(), _pedagogy_result()).decision == "approve"
    assert (
        arbiter.decide(_evidence_result(), _pedagogy_result(r03, gap=1)).decision
        == "approve_with_fix"
    )
    assert (
        arbiter.decide(_evidence_result(r02), _pedagogy_result(r03, gap=1)).decision
        == "reject"
    )


def test_arbiter_rejects_cross_artifact_results() -> None:
    pedagogy = _pedagogy_result()
    mismatched = PedagogyReviewResult(
        agent_id=pedagogy.agent_id,
        artifact_id="another-product",
        contract_id=pedagogy.contract_id,
        hit=pedagogy.hit,
        llm_result=pedagogy.llm_result,
        difficulty_action=pedagogy.difficulty_action,
        difficulty_gap=pedagogy.difficulty_gap,
    )

    with pytest.raises(ValueError, match="different artifacts"):
        DeterministicReviewArbiter().decide(_evidence_result(), mismatched)
