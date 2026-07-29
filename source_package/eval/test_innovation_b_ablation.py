from __future__ import annotations

from eval.run_innovation_b_routing_ablation import run_ablation


def test_innovation_b_ablation_is_reproducible_and_passes_every_adoption_gate() -> None:
    first = run_ablation()
    second = run_ablation()

    assert first == second
    assert first["summary"]["adopt_marginal_support"] is True
    task = first["tasks"][0]
    assert task["first_divergence"] is True
    assert task["b0_target"] == "M-05"
    assert task["b1_target"] == "M-04"
    assert task["b0_marginal_gain"] == 0
    assert task["b1_route_support_points"] == []
    assert task["direct_deterministic_consequence_only"] is True
    assert task["b0_review_gate"]["decision"] == "approve"
    assert task["b1_review_gate"]["decision"] == "approve"

    summary = first["summary"]
    assert summary["different_route_support_points"]["b1"] >= summary[
        "different_route_support_points"
    ]["b0"]
    assert summary["zero_gain_relation_hops"] == {"b0": 1, "b1": 0}
    assert summary["repeated_support_point_contributions"] == {
        "b0": 1,
        "b1": 0,
    }
    assert summary["routing_deterministic"] is True
    assert summary["cross_domain_targets"] == 0
    assert summary["out_of_scope_targets"] == 0
    assert summary["unfrozen_evidence_targets"] == 0
    assert summary["unapproved_coverage_points"] == 0
    assert summary["fourth_round_unclosed"] == 0
    assert summary["post_divergence_llm_outputs_compared"] == 0


def test_innovation_b_ablation_coverage_has_a_real_approved_source() -> None:
    history = run_ablation()["tasks"][0]["reachable_history_prefix"]

    assert history["covered_relation_points"] == [
        "计划量与实际量口径",
        "完成率计算",
    ]
    assert history["coverage_sources"] == [
        {
            "round": 3,
            "source": "M-01",
            "target": "M-04",
            "route_support_points": [
                "计划量与实际量口径",
                "完成率计算",
            ],
            "review_stub": "fixed-evidence-bound-review-v1",
            "review_decision": "approve",
            "evidence_refs": ["M-04:1", "M-04:2"],
            "backend_committed": True,
        }
    ]
