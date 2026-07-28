from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from coordination.contracts import LearningContract


def _profile() -> dict[str, object]:
    return {
        "profile_id": "welder-01",
        "title": "焊接工艺工程师",
        "background": "熟悉现场工艺，正在学习数字化分析",
        "strengths": ["工艺判断", "现场经验"],
        "gaps_prior": ["SQL", "指标口径"],
        "lecture_style": "案例优先",
    }


def _diagnosis(*, difficulty: str = "applied") -> dict[str, object]:
    return {
        "payload": {
            "type": "profile_assessment",
            "content": {
                "profile_id": "welder-01",
                "blind_spots": ["计划量与实际完成量", "SQL 聚合"],
                "hit_misconceptions": ["M-01"],
                "difficulty": difficulty,
            },
        }
    }


def _contract() -> LearningContract:
    return LearningContract.from_diagnosis(
        profile=_profile(),
        diagnosis=_diagnosis(),
        domain_id="first_segment",
        domain_package_sha256="a" * 64,
    )


def test_learning_contract_is_content_addressed_and_immutable() -> None:
    first = _contract()
    second = _contract()

    assert first.contract_id == second.contract_id
    assert first.as_dict() == second.as_dict()
    with pytest.raises(FrozenInstanceError):
        first.difficulty = "advanced"  # type: ignore[misc]


def test_learning_contract_rejects_context_drift() -> None:
    contract = _contract()

    with pytest.raises(ValueError, match="profile"):
        contract.validate_context(
            profile={**_profile(), "profile_id": "another-learner"},
            diagnosis=_diagnosis(),
        )
    with pytest.raises(ValueError, match="difficulty"):
        contract.validate_context(
            profile=_profile(),
            diagnosis=_diagnosis(difficulty="advanced"),
        )


def test_learning_path_update_may_advance_difficulty() -> None:
    contract = _contract()
    contract.validate_context(
        profile=_profile(),
        diagnosis={
            "payload": {
                "type": "learning_path_update",
                "content": {"difficulty": "advanced"},
            }
        },
    )


def test_contract_control_event_is_auditable() -> None:
    draft = _contract().control_draft("trace-contract")
    content = draft["payload"]["content"]  # type: ignore[index]

    assert draft["timestamp"]
    assert content["event"] == "learning_contract_ready"  # type: ignore[index]
    assert content["learning_contract"]["contract_id"].startswith("lc-")  # type: ignore[index]
