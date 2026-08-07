from __future__ import annotations

import json

from eval.v3_cases import INPUT_PATH, load_formal_cases, load_gold_standard


def test_v3_formal_inputs_are_50_production_routes_without_target_injection() -> None:
    cases = load_formal_cases()

    assert len(cases) == 50
    assert {case.route_mode for case in cases} == {"production"}
    assert all(len(case.diagnostic_probe_answers) <= 2 for case in cases)
    assert all(len(case.experience_tags) <= 1 for case in cases)
    raw = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(raw, ensure_ascii=False)
    assert '"knowledge_point"' not in serialized
    assert '"template_id"' not in serialized
    assert '"expected_' not in serialized


def test_v3_gold_is_separate_and_covers_the_same_case_ids() -> None:
    cases = load_formal_cases()
    gold = load_gold_standard()

    assert set(gold) == {case.case_id for case in cases}
    assert all("目标知识点" in row for row in gold.values())
    assert all("标准SQL" in row for row in gold.values())
