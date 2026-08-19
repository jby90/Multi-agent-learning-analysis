from __future__ import annotations

from collections import Counter
from dataclasses import asdict

from eval.v4_cases import load_formal_cases, load_gold_standard
from eval.v4_route_gate import audit_routes


def test_v4_inputs_are_isolated_from_route_gold() -> None:
    cases = load_formal_cases()
    forbidden = {
        "knowledge_point",
        "template_id",
        "target_knowledge_point",
        "expected_knowledge_point",
        "expected_template_id",
    }
    assert len(cases) == 50
    assert all(not (set(asdict(case)) & forbidden) for case in cases)


def test_v4_gold_is_ten_points_by_five_scenarios() -> None:
    gold = load_gold_standard()
    points = Counter(row["target_knowledge_point"] for row in gold.values())
    scripts = Counter(row["learner_script_id"] for row in gold.values())
    assert len(points) == 10
    assert set(points.values()) == {5}
    assert len(scripts) == 5
    assert set(scripts.values()) == {10}


def test_v4_route_gate_passes_all_cases_and_thirty_cells() -> None:
    report = audit_routes()
    assert report["status"] == "passed"
    assert report["passed_count"] == 50
    assert report["coverage_cell_count"] == 30
