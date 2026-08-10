from eval.continuous_learning_supplement import build_cases, validate_cases


def test_continuous_learning_supplement_has_complete_matrix():
    summary = validate_cases(build_cases())

    assert summary["valid"] is True
    assert summary["case_count"] == 40
    assert summary["knowledge_point_count"] == 10
    assert summary["change_required_nodes"] == 60
    assert summary["keep_control_nodes"] == 10


def test_gold_fields_are_isolated_from_production_inputs():
    cases = build_cases()

    assert all(case["route_mode"] == "production" for case in cases)
    assert all("template_id" not in case for case in cases)
    assert all(
        set(case["production_runtime_must_not_read"])
        == {"gold_knowledge_point", "expected_adaptation_nodes", "misconception_id"}
        for case in cases
    )


def test_keep_controls_do_not_inflate_continuous_adaptation_denominator():
    cases = build_cases()
    keep_controls = [case for case in cases if not case["change_required"]]

    assert len(keep_controls) == 10
    assert all(
        case["expected_adaptation_nodes"][0]["expected_actions"] == ["keep"]
        for case in keep_controls
    )
