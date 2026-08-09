from __future__ import annotations

from eval.route_reachability import audit_route_reachability, render_markdown


def test_v3_2_route_gate_resolves_probe_selection_from_profile_evidence() -> None:
    report = audit_route_reachability()

    assert report["scenario_space"]["profile_count"] == 3
    assert report["scenario_space"]["exhaustive_pretest_scenarios"] == 3072
    assert report["scenario_space"]["formal_case_count"] == 50
    assert report["summary"]["first_unit_reachable_count"] == 10
    assert report["summary"]["unreachable_count"] == 0
    assert report["summary"]["forced_knowledge_point_count"] == 0
    assert report["summary"]["forced_template_id_count"] == 0
    assert report["summary"]["pre_probe_conflicting_group_count"] == 0
    assert report["summary"]["pre_probe_affected_case_count"] == 0
    assert report["summary"]["real_route_reachable_upper_bound"] == 10


def test_every_formal_selection_is_traceable_and_deterministic() -> None:
    report = audit_route_reachability()

    assert report["summary"]["traceable_selection_count"] == 50
    assert report["summary"]["deterministic_case_count"] == 50
    for row in report["formal_case_audit"]:
        assert row["route_evidence_ids"]
        assert row["route_reason"]
        assert row["forced_knowledge_point"] is False
        assert row["forced_template_id"] is False


def test_frozen_probe_matches_pass_the_real_route_gate_without_target_injection() -> None:
    report = audit_route_reachability()

    assert report["summary"]["formal_route_matches"] == 50
    assert all(row["knowledge_point_match"] for row in report["formal_case_audit"])
    assert all(row["difficulty_match"] for row in report["formal_case_audit"])
    assert all(row["template_match"] for row in report["formal_case_audit"])
    assert report["summary"]["status"] == "passed"
    assert report["pre_probe_compatibility"]["compatible"] is True


def test_markdown_report_names_all_acceptance_gates() -> None:
    markdown = render_markdown(audit_route_reachability())

    assert "生产路由可区分上限：10/10" in markdown
    assert "探针选择冲突：0组，影响0例" in markdown
    assert "路由证据可追溯：50/50" in markdown
    assert "强制知识点/模板注入：0/0" in markdown
