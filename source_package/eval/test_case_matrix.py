from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import pytest

from eval.case_matrix import (
    APPROVED_SAMPLE_IDS,
    MATRIX_PATH,
    PRETEST_PATTERNS,
    load_case_matrix,
)


CORE_POINTS = {
    "三道工序与传导关系",
    "计划量与实际量口径",
    "完成率计算",
    "偏差率与风险等级",
    "月度聚合方法",
    "异常识别标准",
    "传导时滞分析",
    "异常衰减规律",
    "责任单元定位",
    "跨工序归因方法",
}


def test_approved_matrix_has_exact_ids_and_quotas() -> None:
    cases = load_case_matrix()

    assert [case.case_id for case in cases] == [
        f"E2E-{index:03d}" for index in range(1, 51)
    ]
    assert Counter(case.profile_id for case in cases) == {
        "planner_new": 17,
        "craft_engineer": 17,
        "line_leader": 16,
    }
    assert Counter(case.knowledge_point for case in cases) == {
        point: 5 for point in CORE_POINTS
    }
    assert Counter(case.pretest_pattern for case in cases) == {
        "A5": 10,
        "A4": 8,
        "A3": 8,
        "A2": 8,
        "A1": 8,
        "A0": 8,
    }
    assert Counter(case.learning_path for case in cases) == {
        "direct_correct": 18,
        "rebuttal_corrected": 24,
        "second_wrong_step_down": 8,
    }
    assert [case.case_id for case in cases if case.manual_review] == list(
        APPROVED_SAMPLE_IDS
    )


def test_answers_are_derived_from_approved_patterns() -> None:
    for case in load_case_matrix():
        assert dict(case.answers) == dict(PRETEST_PATTERNS[case.pretest_pattern])


def test_approved_matrix_covers_all_existing_task_templates() -> None:
    assert {case.task_template_id for case in load_case_matrix()} == {
        *(f"T-{index:02d}" for index in range(1, 11)),
        "T-08-DECAY",
    }


def test_deviation_risk_cases_use_their_own_template_without_changing_t05() -> None:
    cases = load_case_matrix()

    assert {
        case.task_template_id
        for case in cases
        if case.knowledge_point == "偏差率与风险等级"
    } == {"T-10"}
    assert {
        case.task_template_id
        for case in cases
        if case.knowledge_point == "异常识别标准"
    } == {"T-05"}


def test_propagation_lag_cases_use_a_dedicated_unshared_template() -> None:
    cases = load_case_matrix()

    propagation_cases = [
        case for case in cases if case.knowledge_point == "传导时滞分析"
    ]
    assert len(propagation_cases) == 5
    assert {case.task_template_id for case in propagation_cases} == {"T-07"}
    assert all(
        case.task_template_id != "T-07"
        for case in cases
        if case.knowledge_point != "传导时滞分析"
    )


def test_decay_cases_use_their_own_dedicated_template_without_moving_attribution() -> None:
    cases = load_case_matrix()

    decay_cases = [
        case for case in cases if case.knowledge_point == "异常衰减规律"
    ]
    attribution_cases = [
        case for case in cases if case.knowledge_point == "跨工序归因方法"
    ]
    assert [case.case_id for case in decay_cases] == [
        f"E2E-{index:03d}" for index in range(36, 41)
    ]
    assert {case.task_template_id for case in decay_cases} == {"T-08-DECAY"}
    assert [case.case_id for case in attribution_cases] == [
        f"E2E-{index:03d}" for index in range(46, 51)
    ]
    assert {case.task_template_id for case in attribution_cases} == {"T-08", "T-09"}
    assert all(
        case.task_template_id != "T-08-DECAY"
        for case in cases
        if case.knowledge_point != "异常衰减规律"
    )


def test_direct_cases_have_no_misconception_and_probe_cases_do() -> None:
    for case in load_case_matrix():
        if case.learning_path == "direct_correct":
            assert case.misconception_id is None
        else:
            assert case.misconception_id in {f"M-{index:02d}" for index in range(1, 6)}


def _write_changed_matrix(tmp_path: Path, change: object) -> Path:
    raw = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_matrix_rejects_duplicate_case_id(tmp_path: Path) -> None:
    path = _write_changed_matrix(
        tmp_path,
        lambda rows: rows[1].update(case_id=rows[0]["case_id"]),
    )

    with pytest.raises(ValueError, match="case IDs must be exactly"):
        load_case_matrix(path)


def test_matrix_rejects_answer_pattern_drift(tmp_path: Path) -> None:
    path = _write_changed_matrix(
        tmp_path,
        lambda rows: rows[0]["answers"].update({"PT-1": "D"}),
    )

    with pytest.raises(ValueError, match="answers do not match"):
        load_case_matrix(path)


def test_matrix_rejects_unknown_field(tmp_path: Path) -> None:
    path = _write_changed_matrix(
        tmp_path,
        lambda rows: rows[0].update({"handwritten_output": "forbidden"}),
    )

    with pytest.raises(ValueError, match="fields do not match"):
        load_case_matrix(path)
