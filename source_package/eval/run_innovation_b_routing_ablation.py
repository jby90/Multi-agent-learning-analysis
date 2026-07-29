"""Reproducible B0/B1 routing ablation over one real reachable history."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.follow_up_agent import (
    _responsibility_scope,
    deterministic_follow_up_route,
)
from agents.misconception_relations import (
    RelationRoute,
    RoutingPolicy,
    default_relation_index,
    default_relation_support_points,
    select_relation_route,
)
from agents.task_agent import TaskAgent
from orchestrator.demo_session import _payload_content


_ALLOWED_IDS = ("M-01", "M-02", "M-03", "M-04", "M-05")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_and_points(
    source: str,
    route: RelationRoute | None,
) -> tuple[str, tuple[str, ...]]:
    if route is None:
        return source, ()
    return route.target, route.route_support_points


def _fixed_review_stub(
    task_agent: TaskAgent,
    target: str,
) -> dict[str, Any]:
    product = task_agent.counter_evidence(target)
    evidence = product.get("evidence")
    probe = product.get("probe")
    approved = (
        isinstance(evidence, list)
        and bool(evidence)
        and isinstance(probe, Mapping)
        and probe.get("target_misconception") == target
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("ref"), str)
            and str(item["ref"]).startswith(f"{target}:")
            for item in evidence
        )
    )
    return {
        "stub": "fixed-evidence-bound-review-v1",
        "decision": "approve" if approved else "reject",
        "target": target,
        "evidence_refs": [
            str(item["ref"])
            for item in evidence or []
            if isinstance(item, Mapping) and isinstance(item.get("ref"), str)
        ],
    }


def run_ablation() -> dict[str, Any]:
    task_agent = TaskAgent("innovation-b-ablation")
    if tuple(task_agent.misconception_ids) != _ALLOWED_IDS:
        raise RuntimeError("production misconception vocabulary changed")
    task = task_agent.generate("T-02")
    scope = _responsibility_scope(_payload_content(task))
    if set(scope) != {"完成率计算", "计划量与实际量口径"}:
        raise RuntimeError("T-02 responsibility scope changed")
    relation_index = default_relation_index(_ALLOWED_IDS)
    allowed_points = default_relation_support_points(_ALLOWED_IDS)

    fixed_answer_diagnosis_stub = {
        "stub_id": "answer-diagnosis-fixed-v1",
        "rounds": [
            {
                "round": 2,
                "answer": "计划量就是已经完成的数量。",
                "assessment": "needs_support",
                "diagnosed_misconception": "M-01",
            },
            {
                "round": 3,
                "answer": "我仍然把计划量当作完成量。",
                "assessment": "needs_support",
                "diagnosed_misconception": "M-01",
            },
            {
                "round": 4,
                "answer": "我仍无法区分完成率相关口径。",
                "assessment": "needs_support",
                "diagnosed_misconception": "M-04",
            },
        ],
    }
    initial_probed = ("M-01",)
    first_routes = {
        mode: select_relation_route(
            "M-01",
            responsibility_scope=scope,
            probed=initial_probed,
            covered_relation_points=(),
            relation_index=relation_index,
            allowed_ids=_ALLOWED_IDS,
            allowed_support_points=allowed_points,
            policy=RoutingPolicy(mode),
        )
        for mode in ("total_support", "marginal_support")
    }
    if first_routes["total_support"] != first_routes["marginal_support"]:
        raise RuntimeError("B0/B1 diverged before the frozen history prefix")
    approved_route = first_routes["marginal_support"]
    if approved_route is None or approved_route.target != "M-04":
        raise RuntimeError("real M-01 to M-04 prefix is no longer reachable")
    prefix_review = _fixed_review_stub(task_agent, approved_route.target)
    if prefix_review["decision"] != "approve":
        raise RuntimeError("reachable prefix did not pass the fixed review gate")
    approval_ledger = [
        {
            "round": 3,
            "source": "M-01",
            "target": "M-04",
            "route_support_points": list(approved_route.route_support_points),
            "review_stub": prefix_review["stub"],
            "review_decision": prefix_review["decision"],
            "evidence_refs": prefix_review["evidence_refs"],
            "backend_committed": True,
        }
    ]
    covered = approved_route.route_support_points
    probed = ("M-01", "M-04")
    divergence_routes = {
        mode: select_relation_route(
            "M-04",
            responsibility_scope=scope,
            probed=probed,
            covered_relation_points=covered,
            relation_index=relation_index,
            allowed_ids=_ALLOWED_IDS,
            allowed_support_points=allowed_points,
            policy=RoutingPolicy(mode),
        )
        for mode in ("total_support", "marginal_support")
    }
    b0_target, b0_points = _target_and_points(
        "M-04",
        divergence_routes["total_support"],
    )
    b1_target, b1_points = _target_and_points(
        "M-04",
        divergence_routes["marginal_support"],
    )
    history_prefix = {
        "template_id": "T-02",
        "responsibility_scope": list(scope),
        "answer_diagnosis_stub": fixed_answer_diagnosis_stub,
        "probed_misconceptions": list(probed),
        "covered_relation_points": list(covered),
        "coverage_sources": approval_ledger,
        "next_round": 4,
        "next_diagnosis": "M-04",
    }
    b0_review = _fixed_review_stub(task_agent, b0_target)
    b1_review = _fixed_review_stub(task_agent, b1_target)
    b0_zero_gain = int(
        divergence_routes["total_support"] is not None
        and divergence_routes["total_support"].marginal_gain == 0
    )
    b1_zero_gain = int(
        divergence_routes["marginal_support"] is not None
        and divergence_routes["marginal_support"].marginal_gain == 0
    )
    b0_distinct = len(set(covered) | set(b0_points))
    b1_distinct = len(set(covered) | set(b1_points))
    b0_repeated = len(set(covered) & set(b0_points))
    b1_repeated = len(set(covered) & set(b1_points))
    repeated_routes = {
        mode: select_relation_route(
            "M-04",
            responsibility_scope=scope,
            probed=probed,
            covered_relation_points=covered,
            relation_index=relation_index,
            allowed_ids=_ALLOWED_IDS,
            allowed_support_points=allowed_points,
            policy=RoutingPolicy(mode),
        )
        for mode in ("total_support", "marginal_support")
    }
    routing_deterministic = repeated_routes == divergence_routes
    cross_domain_targets = sum(
        target not in _ALLOWED_IDS for target in (b0_target, b1_target)
    )
    out_of_scope_targets = sum(
        point not in scope for point in (*b0_points, *b1_points)
    )
    unfrozen_evidence_targets = sum(
        review["decision"] != "approve" for review in (b0_review, b1_review)
    )
    unapproved_coverage_points = sum(
        not source["backend_committed"] or source["review_decision"] != "approve"
        for source in approval_ledger
    )
    terminal_target, terminal_points = deterministic_follow_up_route(
        assessment="needs_support",
        diagnosed_misconception="M-04",
        completion_allowed=True,
        terminal_round=True,
        probed_misconceptions=probed,
        covered_relation_points=covered,
        responsibility_scope=scope,
        relation_index=relation_index,
        allowed_targets=_ALLOWED_IDS,
        allowed_support_points=allowed_points,
        routing_policy=RoutingPolicy("marginal_support"),
    )
    fourth_round_unclosed = int(
        terminal_target is not None or bool(terminal_points)
    )
    adopted = (
        b0_target != b1_target
        and b1_distinct >= b0_distinct
        and b1_zero_gain < b0_zero_gain
        and b1_repeated < b0_repeated
        and routing_deterministic
        and cross_domain_targets == 0
        and out_of_scope_targets == 0
        and unfrozen_evidence_targets == 0
        and unapproved_coverage_points == 0
        and fourth_round_unclosed == 0
        and prefix_review["decision"] == "approve"
        and b0_review["decision"] == "approve"
        and b1_review["decision"] == "approve"
    )
    return {
        "schema_version": "innovation-b-routing-ablation-v1",
        "input_contract": {
            "source": "formal-domain-assets-and-fixed-stubs-only",
            "accepts_temporary_relations": False,
            "accepts_arbitrary_covered_sets": False,
            "uses_monkeypatch": False,
            "uses_post_divergence_llm_output": False,
        },
        "tasks": [
            {
                "template_id": "T-02",
                "responsibility_scope": list(scope),
                "reachable_history_prefix": history_prefix,
                "history_prefix_sha256": _canonical_hash(history_prefix),
                "b0_target": b0_target,
                "b1_target": b1_target,
                "b0_route_support_points": list(b0_points),
                "b1_route_support_points": list(b1_points),
                "b0_marginal_gain": (
                    divergence_routes["total_support"].marginal_gain
                    if divergence_routes["total_support"] is not None
                    else 0
                ),
                "b1_marginal_gain": (
                    divergence_routes["marginal_support"].marginal_gain
                    if divergence_routes["marginal_support"] is not None
                    else 0
                ),
                "first_divergence": b0_target != b1_target,
                "direct_deterministic_consequence_only": True,
                "b0_review_gate": b0_review,
                "b1_review_gate": b1_review,
            }
        ],
        "summary": {
            "adopt_marginal_support": adopted,
            "different_route_support_points": {
                "b0": b0_distinct,
                "b1": b1_distinct,
            },
            "zero_gain_relation_hops": {
                "b0": b0_zero_gain,
                "b1": b1_zero_gain,
            },
            "repeated_support_point_contributions": {
                "b0": b0_repeated,
                "b1": b1_repeated,
            },
            "routing_deterministic": routing_deterministic,
            "cross_domain_targets": cross_domain_targets,
            "out_of_scope_targets": out_of_scope_targets,
            "unfrozen_evidence_targets": unfrozen_evidence_targets,
            "unapproved_coverage_points": unapproved_coverage_points,
            "fourth_round_unclosed": fourth_round_unclosed,
            "post_divergence_llm_outputs_compared": 0,
        },
        "claims_boundary": {
            "proves": [
                "real_reachable_first_route_divergence",
                "distinct_route_support_not_lower",
                "strict_zero_gain_hop_reduction",
                "approved_coverage_provenance",
            ],
            "does_not_prove": [
                "learning_outcome_improvement",
                "multi_node_second_domain_gain",
                "cross_industry_generalization",
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/results/innovation_b_routing_ablation.json"),
    )
    args = parser.parse_args()
    result = run_ablation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
