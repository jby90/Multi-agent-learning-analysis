from __future__ import annotations

from agents.diagnosis_agent import DiagnosisAgent
from agents.diagnostic_router import (
    DiagnosticRouter,
    ProbeResult,
    probes_for_diagnosis,
)


ALL_CORRECT = {
    "PT-1": "B",
    "PT-2": "B",
    "PT-3": "C",
    "PT-4": "B",
    "PT-5": "C",
}


def test_v3_router_is_deterministic_and_records_complete_route_evidence() -> None:
    router = DiagnosticRouter()
    probes = (
        ProbeResult("DP-02-B", True),
        ProbeResult("DP-02-A", False),
    )

    first = router.route("planner_new", ALL_CORRECT, probes)
    second = router.route("planner_new", ALL_CORRECT, probes)

    assert first == second
    selected = first["knowledge_point_plan"][0]
    assert selected == {
        "plan_item_id": "PLAN-001",
        "knowledge_point": "偏差率与风险等级",
        "mastery_status": "needs_training",
        "evidence_source": "diagnostic_probe",
        "evidence_ids": ["DP-02-A"],
        "priority": 100,
        "initial_difficulty": "applied",
        "route_reason": "应用校准探针答错，需从应用档建立稳定掌握。",
        "prerequisites": ["计划量与实际量口径", "完成率计算"],
    }


def test_correct_pretest_answer_is_never_marked_as_a_blind_spot() -> None:
    router = DiagnosticRouter()
    answers = dict(ALL_CORRECT)
    answers["PT-2"] = "A"

    result = router.route("line_leader", answers, ())
    planned = {item["knowledge_point"] for item in result["knowledge_point_plan"]}

    assert "完成率计算" in planned
    assert "计划量与实际量口径" not in planned


def test_all_ten_points_are_reachable_only_from_real_pretest_or_probe_evidence() -> None:
    router = DiagnosticRouter()
    reached: set[str] = set()

    for question_id in ALL_CORRECT:
        answers = dict(ALL_CORRECT)
        answers[question_id] = "A" if ALL_CORRECT[question_id] != "A" else "D"
        reached.add(router.route("planner_new", answers, ())["selected_knowledge_point"])

    for probe_id in ("DP-01-B", "DP-02-B", "DP-03-B", "DP-04-B", "DP-05-B"):
        reached.add(
            router.route(
                "planner_new",
                ALL_CORRECT,
                (ProbeResult(probe_id, False),),
            )["selected_knowledge_point"]
        )

    assert reached == set(router.core_knowledge_points)


def test_diagnosis_agent_uses_probe_evidence_without_target_or_template_injection() -> None:
    content = DiagnosisAgent("trace-v3-probe").assess(
        "planner_new",
        ALL_CORRECT,
        probe_results=[{"probe_id": "DP-03-B", "is_correct": False}],
    )["payload"]["content"]

    assert content["selected_knowledge_point"] == "异常衰减规律"
    assert content["selected_difficulty"] == "basic"
    assert content["knowledge_point_plan"][0]["evidence_ids"] == ["DP-03-B"]
    assert "template_id" not in content


def test_router_rejects_more_than_two_probes_per_session() -> None:
    router = DiagnosticRouter()

    try:
        router.route(
            "planner_new",
            ALL_CORRECT,
            (
                ProbeResult("DP-01-B", True),
                ProbeResult("DP-01-A", True),
                ProbeResult("DP-02-B", False),
            ),
        )
    except ValueError as exc:
        assert "at most two" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("three probes must be rejected")


def test_profile_experience_selects_probe_without_creating_a_blind_spot() -> None:
    router = DiagnosticRouter()

    probes = probes_for_diagnosis(
        "planner_new",
        ALL_CORRECT,
        ("handled_responsibility_handoffs",),
    )
    preliminary = router.route(
        "planner_new",
        ALL_CORRECT,
        (),
        experience_tags=("handled_responsibility_handoffs",),
    )

    assert [item["probe_id"] for item in probes] == ["DP-04-B", "DP-04-A"]
    assert preliminary["recommended_probe_knowledge_point"] == "责任单元定位"
    assert "责任单元定位" not in {
        item["knowledge_point"]
        for item in preliminary["knowledge_point_plan"]
        if item["mastery_status"] == "needs_training"
    }


def test_probe_failure_not_experience_tag_creates_the_selected_blind_spot() -> None:
    router = DiagnosticRouter()

    result = router.route(
        "planner_new",
        ALL_CORRECT,
        (ProbeResult("DP-04-B", False),),
        experience_tags=("handled_responsibility_handoffs",),
    )

    assert result["selected_knowledge_point"] == "责任单元定位"
    assert result["selected_difficulty"] == "basic"
    assert result["knowledge_point_plan"][0]["evidence_source"] == "diagnostic_probe"
