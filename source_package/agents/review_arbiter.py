"""Deterministic, model-free reducer for specialist review agents."""

from __future__ import annotations

from agents.quality_protocol import (
    DeterministicAxisReviewResult,
    EvidenceReviewResult,
    PedagogyReviewResult,
    ReviewArbitration,
)


class DeterministicReviewArbiter:
    agent_id = "review"

    def decide(
        self,
        evidence: EvidenceReviewResult,
        pedagogy: PedagogyReviewResult,
        data_safety: DeterministicAxisReviewResult,
        readability: DeterministicAxisReviewResult,
    ) -> ReviewArbitration:
        reviews = (evidence, pedagogy, data_safety, readability)
        if len({review.artifact_id for review in reviews}) != 1:
            raise ValueError("specialist reviews refer to different artifacts")
        if len({review.contract_id for review in reviews}) != 1:
            raise ValueError("specialist reviews refer to different contracts")
        hits = (
            evidence.hits
            + (() if pedagogy.hit is None else (pedagogy.hit,))
            + data_safety.hits
            + readability.hits
        )
        if not hits:
            decision = "approve"
        elif (
            len(hits) == 1
            and hits[0].rule_id == "R-03"
            and pedagogy.difficulty_gap == 1
        ):
            decision = "approve_with_fix"
        else:
            decision = "reject"
        return ReviewArbitration(
            hits=hits,
            decision=decision,
            difficulty_action=pedagogy.difficulty_action,
            difficulty_gap=pedagogy.difficulty_gap,
        )
