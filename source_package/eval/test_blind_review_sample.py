from __future__ import annotations

import json

import pytest

from eval.blind_review_sample import build_blind_sample, render_blind_form


PROFILES = {
    "planner_new": {
        "profile_id": "planner_new",
        "title": "新入职生产计划员",
        "background": "会 SQL，不懂船舶工序口径。",
        "strengths": ["SQL 基础"],
        "gaps_prior": ["工序口径"],
        "lecture_style": "重讲口径",
        "difficulty_start": "basic",
    },
    "craft_engineer": {
        "profile_id": "craft_engineer",
        "title": "转岗工艺工程师",
        "background": "懂工艺，不熟数据工具。",
        "strengths": ["工艺知识"],
        "gaps_prior": ["数据工具"],
        "lecture_style": "重讲数据工具",
        "difficulty_start": "applied",
    },
    "line_leader": {
        "profile_id": "line_leader",
        "title": "一线班组长",
        "background": "现场熟，理论弱。",
        "strengths": ["现场经验"],
        "gaps_prior": ["完成率计算"],
        "lecture_style": "步骤化短句",
        "difficulty_start": "basic",
    },
}


def _matrix() -> list[dict]:
    rows: list[dict] = []
    serial = 1
    for profile_id in PROFILES:
        for index in range(3):
            rows.append(
                {
                    "case_id": f"E2E-{serial:03d}",
                    "profile_id": profile_id,
                    "knowledge_point": f"知识点-{index + 1}",
                }
            )
            serial += 1
    return rows


def _product(trace_id: str, step: int, text: str, *, final: bool = False) -> dict:
    return {
        "msg_id": f"{trace_id}-{step:03d}",
        "trace_id": trace_id,
        "step": step,
        "agent": "task",
        "role": "produce",
        "payload": {
            "type": "practice_guide",
            "content": {
                "event": "product_ready",
                "question": text,
                "guide_md": text,
                "guide_intro": "请结合岗位场景完成。",
                "difficulty": "advanced" if final else "basic",
                "difficulty_gap": 1,
                "template_id": "T-09",
                "standard_stem": "技术母题",
                "contextualized_stem": text,
                "llm_latency_ms": 123,
            },
        },
        "model": "qwen3-235b-a22b",
        "token_usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
        "verdict": {"decision": "approve"},
    }


def _dataset() -> tuple[dict, ...]:
    rows = []
    for case in _matrix():
        trace_id = case["case_id"].lower()
        rows.append(
            {
                "case": dict(case),
                "trace_id": trace_id,
                "messages": [
                    _product(trace_id, 1, "早期产物"),
                    _product(trace_id, 2, f"{case['case_id']} 最终岗位题", final=True),
                ],
            }
        )
    return tuple(rows)


def test_blind_sample_is_balanced_deterministic_and_uses_last_product() -> None:
    first = build_blind_sample(_dataset(), _matrix(), PROFILES, size=6)
    second = build_blind_sample(_dataset(), _matrix(), PROFILES, size=6)

    assert first == second
    assert len(first) == 6
    assert {row["blind_id"] for row in first} == {
        "BR-001",
        "BR-002",
        "BR-003",
        "BR-004",
        "BR-005",
        "BR-006",
    }
    assert sorted(row["learner_profile"]["title"] for row in first).count(
        "新入职生产计划员"
    ) == 2
    assert all(
        "最终岗位题" in json.dumps(row["resource"]["content"], ensure_ascii=False)
        for row in first
    )


def test_blind_sample_skips_traces_without_teacher_visible_resources() -> None:
    dataset = [dict(row) for row in _dataset()]
    for index in (0, 3, 6):
        dataset[index] = {**dataset[index], "messages": []}

    sample = build_blind_sample(tuple(dataset), _matrix(), PROFILES, size=6)

    assert len(sample) == 6
    assert sorted(row["learner_profile"]["title"] for row in sample).count(
        "新入职生产计划员"
    ) == 2
    assert all(row["resource"]["content"] for row in sample)


def test_blind_sample_uses_last_visible_product_before_empty_template_fallback() -> None:
    dataset = [dict(row) for row in _dataset()]
    trace_id = dataset[0]["trace_id"]
    empty_fallback = {
        "msg_id": f"{trace_id}-003",
        "trace_id": trace_id,
        "step": 3,
        "agent": "task",
        "role": "produce",
        "payload": {
            "type": "quiz_set",
            "content": {
                "event": "product_ready",
                "payload_type": "quiz_set",
                "generated_by": "template_fallback",
            },
        },
    }
    dataset[0] = {
        **dataset[0],
        "messages": [*dataset[0]["messages"], empty_fallback],
    }

    sample = build_blind_sample(tuple(dataset), _matrix(), PROFILES, size=6)

    assert "最终岗位题" in json.dumps(
        sample[0]["resource"]["content"], ensure_ascii=False
    )


def test_blind_material_omits_duplicate_task_display_fields() -> None:
    sample = build_blind_sample(_dataset(), _matrix(), PROFILES, size=6)

    for row in sample:
        assert set(row["resource"]["content"]) == {"guide_md", "guide_intro"}


@pytest.mark.parametrize(
    "forbidden",
    [
        "msg_id",
        "trace_id",
        "verdict",
        "model",
        "difficulty",
        "difficulty_gap",
        "token_usage",
        "profile_id",
        "template_id",
        "llm_latency_ms",
        "standard_stem",
    ],
)
def test_blind_material_removes_system_judgment(forbidden: str) -> None:
    material = json.dumps(
        build_blind_sample(_dataset(), _matrix(), PROFILES, size=6),
        ensure_ascii=False,
    )

    assert forbidden not in material


def test_blind_form_has_empty_rating_slots_and_no_source_ids() -> None:
    sample = build_blind_sample(_dataset(), _matrix(), PROFILES, size=6)
    form = render_blind_form(sample, source_sha256="a" * 64)

    assert "P7 教师盲评表" in form
    assert form.count("结论（适配/不适配）：") == 6
    assert "E2E-" not in form
    assert "trace_id" not in form
    assert "a" * 64 in form


def test_blind_size_must_split_evenly_across_three_profiles() -> None:
    with pytest.raises(ValueError, match="divisible by three"):
        build_blind_sample(_dataset(), _matrix(), PROFILES, size=5)
