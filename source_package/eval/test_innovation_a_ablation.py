from __future__ import annotations

from eval.run_innovation_a_ablation import run_ablation


def test_innovation_a_ablation_is_deterministic_and_a0_equivalent() -> None:
    first = run_ablation()
    second = run_ablation()

    assert first == second
    assert first["a0_baseline_equivalent"] is True
    groups = first["groups"]
    assert groups["A0"]["model_rebuttal_attempts"] == 2
    assert groups["A1-B"]["model_rebuttal_attempts"] == 1
    assert groups["A1-F"]["model_rebuttal_attempts"] == 2
    assert groups["A1-BF"]["model_rebuttal_attempts"] == 1
    assert {row["re_review_calls"] for row in groups.values()} == {2}
    assert {row["failed_products_published"] for row in groups.values()} == {0}


def test_innovation_a_budget_sensitivity_has_the_expected_upper_bound() -> None:
    sensitivity = run_ablation()["budget_sensitivity"]

    assert sensitivity["0"]["model_rebuttal_attempts"] == 0
    assert sensitivity["0"]["deterministic_concessions"] == 2
    assert sensitivity["1"]["model_rebuttal_attempts"] == 1
    assert sensitivity["1"]["deterministic_concessions"] == 1
    assert sensitivity["4"]["model_rebuttal_attempts"] == 2
    assert sensitivity["4"]["deterministic_concessions"] == 0
    assert {row["re_review_calls"] for row in sensitivity.values()} == {2}
