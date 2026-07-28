"""Typed, immutable messages exchanged by independent quality agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from orchestrator.llm import LLMResult


QualityAgentId = Literal[
    "evidence_review",
    "pedagogy_review",
    "data_safety_review",
    "readability_review",
]


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ReviewRuleHit:
    rule_id: str
    reason: str
    evidence_ref: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReviewRuleHit":
        return cls(
            rule_id=_required_string(value.get("rule_id"), "rule_hit.rule_id"),
            reason=_required_string(value.get("reason"), "rule_hit.reason"),
            evidence_ref=_required_string(
                value.get("evidence_ref"),
                "rule_hit.evidence_ref",
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "reason": self.reason,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True, slots=True)
class EvidenceReviewResult:
    agent_id: Literal["evidence_review"]
    artifact_id: str
    contract_id: str
    hits: tuple[ReviewRuleHit, ...]
    llm_results: tuple[LLMResult, ...]
    checks: int


@dataclass(frozen=True, slots=True)
class PedagogyReviewResult:
    agent_id: Literal["pedagogy_review"]
    artifact_id: str
    contract_id: str
    hit: ReviewRuleHit | None
    llm_result: LLMResult | None
    difficulty_action: str
    difficulty_gap: int | None


@dataclass(frozen=True, slots=True)
class DeterministicAxisReviewResult:
    agent_id: Literal["data_safety_review", "readability_review"]
    artifact_id: str
    contract_id: str
    hits: tuple[ReviewRuleHit, ...]
    checks: int


@dataclass(frozen=True, slots=True)
class ReviewArbitration:
    hits: tuple[ReviewRuleHit, ...]
    decision: Literal["approve", "approve_with_fix", "reject"]
    difficulty_action: str
    difficulty_gap: int | None
