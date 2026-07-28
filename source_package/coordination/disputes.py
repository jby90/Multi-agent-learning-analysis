"""Deterministic routing for review rejects and bounded specialist debate."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from coordination.contracts import QualityPolicy


DisputeRoute = Literal["publish", "targeted_debate", "local_regeneration"]


@dataclass(frozen=True, slots=True)
class DisputeIssue:
    rule_id: str
    reason: str
    evidence_ref: str

    def as_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "reason": self.reason,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True, slots=True)
class DisputePlan:
    route: DisputeRoute
    issues: tuple[DisputeIssue, ...]
    specialist_agents: tuple[str, ...]
    hard_veto_rules: tuple[str, ...]

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(issue.rule_id for issue in self.issues)

    def as_event_details(self) -> dict[str, Any]:
        return {
            "dispute_route": self.route,
            "rule_ids": list(self.rule_ids),
            "specialist_agents": list(self.specialist_agents),
            "hard_veto_rules": list(self.hard_veto_rules),
            "issues": [issue.as_dict() for issue in self.issues],
        }


_RULE_SPECIALISTS = {
    "R-02": "evidence_review",
    "R-03": "pedagogy_review",
}


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def plan_review_dispute(
    verdict_message: Mapping[str, Any],
    *,
    policy: QualityPolicy | None = None,
) -> DisputePlan:
    """Route canonical review output without asking a model to choose control flow."""

    if not isinstance(verdict_message, Mapping):
        raise ValueError("verdict_message must be a mapping")
    verdict = verdict_message.get("verdict")
    if not isinstance(verdict, Mapping):
        raise ValueError("verdict_message.verdict must be a mapping")
    decision = verdict.get("decision")
    if decision not in {"approve", "approve_with_fix", "reject"}:
        raise ValueError("review decision is not canonical")
    if decision != "reject":
        return DisputePlan("publish", (), (), ())

    raw_hits = verdict.get("rule_hits")
    if not isinstance(raw_hits, list) or not raw_hits:
        raise ValueError("reject verdict must contain rule_hits")
    issues = tuple(
        DisputeIssue(
            rule_id=_required_string(hit.get("rule_id"), "rule_hit.rule_id"),
            reason=_required_string(hit.get("reason"), "rule_hit.reason"),
            evidence_ref=_required_string(
                hit.get("evidence_ref"), "rule_hit.evidence_ref"
            ),
        )
        for hit in raw_hits
        if isinstance(hit, Mapping)
    )
    if len(issues) != len(raw_hits):
        raise ValueError("every rule hit must be a mapping")

    active_policy = policy or QualityPolicy()
    hard_rules = frozenset(active_policy.hard_veto_rules)
    debatable_rules = frozenset(active_policy.debatable_rules)
    rule_ids = frozenset(issue.rule_id for issue in issues)
    matched_hard = tuple(
        rule_id for rule_id in active_policy.hard_veto_rules if rule_id in rule_ids
    )
    unknown_rules = rule_ids - hard_rules - debatable_rules
    if matched_hard or unknown_rules:
        # Unknown rules fail closed and cannot acquire debate authority implicitly.
        closed_rules = matched_hard + tuple(sorted(unknown_rules))
        return DisputePlan("local_regeneration", issues, (), closed_rules)

    specialists = tuple(
        specialist
        for rule_id in active_policy.debatable_rules
        if rule_id in rule_ids
        for specialist in (_RULE_SPECIALISTS.get(rule_id),)
        if specialist is not None
    )
    if not specialists:
        return DisputePlan("local_regeneration", issues, (), tuple(sorted(rule_ids)))
    return DisputePlan("targeted_debate", issues, specialists, ())
