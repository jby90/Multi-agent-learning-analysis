from eval.v3_adaptation_opportunity_audit import audit_adaptation_opportunities


def test_frozen_gold_exposes_structural_adaptation_ceiling():
    gold = [
        {"case_id": "A", "预期适配节点数": 2, "预期适配序列": "initial_route(basic) → learner_correct → keep"},
        {"case_id": "B", "预期适配节点数": 3, "预期适配序列": "initial_route(applied) → wrong → targeted_followup → corrected → keep"},
        {"case_id": "C", "预期适配节点数": 2, "预期适配序列": "initial_route(applied) → learner_correct → step_up"},
    ]

    result = audit_adaptation_opportunities(gold, seed_count=2)

    assert result["total_required_nodes"] == 14
    assert result["expected_keep_nodes"] == 4
    assert result["maximum_effective_nodes"] == 10
    assert result["structural_ceiling_percentage"] == 71.4286
    assert result["nodes_required_for_85_percent"] == 12
    assert result["impossible_gap_to_85_percent"] == 2


def test_spaced_arrows_and_rebuttal_alternatives_do_not_create_blank_nodes():
    gold = [
        {
            "case_id": "A",
            "预期适配节点数": 3,
            "预期适配序列": "initial_route(applied) → wrong → targeted_followup/rebuttal → corrected → keep",
        }
    ]

    result = audit_adaptation_opportunities(gold, seed_count=1)

    assert result["total_required_nodes"] == 3
    assert result["expected_keep_nodes"] == 1
    assert result["maximum_effective_nodes"] == 2
    assert result["sequence_parse_errors"] == []


def test_remedial_correct_terminal_node_is_a_real_keep_decision():
    gold = [
        {
            "case_id": "A",
            "预期适配节点数": 4,
            "预期适配序列": (
                "initial_route(applied) → wrong → targeted_followup/rebuttal → "
                "wrong → step_down → remedial_correct"
            ),
        }
    ]

    result = audit_adaptation_opportunities(gold, seed_count=1)

    assert result["total_required_nodes"] == 4
    assert result["expected_keep_nodes"] == 1
    assert result["maximum_effective_nodes"] == 3
