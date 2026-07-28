"""Independent R-02 evidence and factual-support review agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agents.quality_protocol import EvidenceReviewResult, ReviewRuleHit
from coordination.artifacts import ArtifactEnvelope
from orchestrator.llm import LLMResult


EvidenceEvaluator = Callable[
    [Mapping[str, Any]],
    tuple[tuple[dict[str, str], ...], tuple[LLMResult, ...], int],
]


class EvidenceReviewAgent:
    """Own one auditable quality responsibility and return a typed result."""

    agent_id = "evidence_review"
    rule_id = "R-02"

    def __init__(self, trace_id: str, evaluator: EvidenceEvaluator) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        if not callable(evaluator):
            raise ValueError("evaluator must be callable")
        self.trace_id = trace_id
        self._evaluator = evaluator

    def review(self, artifact: ArtifactEnvelope) -> EvidenceReviewResult:
        hits, llm_results, checks = self._evaluator(artifact.materialize())
        normalized_hits = tuple(ReviewRuleHit.from_mapping(hit) for hit in hits)
        if any(hit.rule_id != self.rule_id for hit in normalized_hits):
            raise ValueError("evidence review agent returned a non-R-02 hit")
        return EvidenceReviewResult(
            agent_id=self.agent_id,
            artifact_id=artifact.artifact_id,
            contract_id=artifact.contract_id,
            hits=normalized_hits,
            llm_results=tuple(llm_results),
            checks=checks,
        )
