"""Independent deterministic reviewers for data safety and readability."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agents.quality_protocol import (
    DeterministicAxisReviewResult,
    ReviewRuleHit,
)
from coordination.artifacts import ArtifactEnvelope


AxisEvaluator = Callable[
    [Mapping[str, Any]],
    tuple[Sequence[Mapping[str, str]], int],
]


class _DeterministicReviewAgent:
    agent_id: str
    allowed_rule_ids: frozenset[str]

    def __init__(self, trace_id: str, evaluator: AxisEvaluator) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        if not callable(evaluator):
            raise ValueError("evaluator must be callable")
        self.trace_id = trace_id
        self._evaluator = evaluator

    def review(self, artifact: ArtifactEnvelope) -> DeterministicAxisReviewResult:
        hits, checks = self._evaluator(artifact.materialize())
        normalized = tuple(ReviewRuleHit.from_mapping(hit) for hit in hits)
        if any(hit.rule_id not in self.allowed_rule_ids for hit in normalized):
            raise ValueError(f"{self.agent_id} returned a rule outside its authority")
        return DeterministicAxisReviewResult(
            agent_id=self.agent_id,  # type: ignore[arg-type]
            artifact_id=artifact.artifact_id,
            contract_id=artifact.contract_id,
            hits=normalized,
            checks=checks,
        )


class DataSafetyReviewAgent(_DeterministicReviewAgent):
    agent_id = "data_safety_review"
    allowed_rule_ids = frozenset({"R-01", "R-05"})


class ReadabilityReviewAgent(_DeterministicReviewAgent):
    agent_id = "readability_review"
    allowed_rule_ids = frozenset({"R-06"})
