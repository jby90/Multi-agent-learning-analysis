from __future__ import annotations

from eval.run_semantic_claim_ablation import run_ablation


def test_semantic_claim_ablation_is_reproducible_and_passes_adoption_gate() -> None:
    first = run_ablation()
    second = run_ablation()

    assert first == second
    assert first["claim_plan"] == ["PP-COMPLETE-RATE-001"]
    assert first["design"] == {
        "only_difference": "semantic_claim_preflight_enabled",
        "fixed_input_sha256": first["design"]["fixed_input_sha256"],
        "same_history_prefix": True,
        "same_retrieved_evidence": True,
        "same_model_output": True,
        "post_split_llm_output_used": False,
    }
    assert first["adoption_gate"] == {
        "baseline_reproduces_e2e_010": True,
        "enabled_repairs_without_llm": True,
    }


def test_semantic_claim_ablation_changes_only_the_preflight_outcome() -> None:
    groups = run_ablation()["groups"]
    disabled = groups["H0-disabled"]
    enabled = groups["H1-enabled"]

    assert disabled["r02_formula_hit"] is True
    assert disabled["r02_rule_id"] == "R-02"
    assert enabled["r02_formula_hit"] is False
    assert enabled["r02_rule_id"] is None
    assert disabled["llm_calls"] == enabled["llm_calls"] == 0
    assert enabled["semantic_checked"] == 2
    assert enabled["semantic_repaired"] == 2
