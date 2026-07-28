"""Independent R-03 learner and pedagogy alignment review agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agents.quality_protocol import PedagogyReviewResult, ReviewRuleHit
from coordination.artifacts import ArtifactEnvelope
from orchestrator.llm import LLMResult


PedagogyEvaluator = Callable[
    [
        Mapping[str, Any],
        Mapping[str, Any] | None,
        Mapping[str, Any] | None,
        Sequence[str],
    ],
    tuple[dict[str, str] | None, LLMResult | None, str, int | None],
]


class PedagogyReviewAgent:
    """Judge learning fit independently from factual evidence review."""

    agent_id = "pedagogy_review"
    rule_id = "R-03"

    def __init__(self, trace_id: str, evaluator: PedagogyEvaluator) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        if not callable(evaluator):
            raise ValueError("evaluator must be callable")
        self.trace_id = trace_id
        self._evaluator = evaluator

    def review(
        self,
        artifact: ArtifactEnvelope,
        *,
        learning_report: Mapping[str, Any] | None,
        student_profile: Mapping[str, Any] | None,
        learned_knowledge_points: Sequence[str],
    ) -> PedagogyReviewResult:
        hit, llm_result, action, gap = self._evaluator(
            artifact.materialize(),
            learning_report,
            student_profile,
            learned_knowledge_points,
        )
        normalized_hit = ReviewRuleHit.from_mapping(hit) if hit is not None else None
        if normalized_hit is not None and normalized_hit.rule_id != self.rule_id:
            raise ValueError("pedagogy review agent returned a non-R-03 hit")
        return PedagogyReviewResult(
            agent_id=self.agent_id,
            artifact_id=artifact.artifact_id,
            contract_id=artifact.contract_id,
            hit=normalized_hit,
            llm_result=llm_result,
            difficulty_action=action,
            difficulty_gap=gap,
        )
