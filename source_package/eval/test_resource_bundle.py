from __future__ import annotations

import pytest

from coordination.resource_bundle import ResourceBundle


EVIDENCE_ID = "eb-shared-evidence"


def product(payload_type: str, *, difficulty: str = "basic") -> dict:
    return {
        "payload": {
            "type": payload_type,
            "content": {
                "event": "product_ready",
                "difficulty": difficulty,
                "evidence_bundle_ref": EVIDENCE_ID,
            },
        },
        "evidence": [],
    }


def test_resource_bundle_is_stable_and_declares_three_independent_branches() -> None:
    products = {
        "knowledge": product("lecture_note"),
        "practice": product("practice_guide"),
        "assessment": product("quiz_set"),
    }

    first = ResourceBundle.build(
        contract_id="lc-resource",
        evidence_bundle_id=EVIDENCE_ID,
        products=products,
    )
    second = ResourceBundle.build(
        contract_id="lc-resource",
        evidence_bundle_id=EVIDENCE_ID,
        products=products,
    )

    assert first.bundle_id == second.bundle_id
    assert first.bundle_id.startswith("rb-")
    assert [branch.branch_id for branch in first.branches] == [
        "knowledge",
        "practice",
        "assessment",
    ]
    assert all(branch.status == "ready" for branch in first.branches)


def test_resource_bundle_allows_optional_single_branch_retry() -> None:
    bundle = ResourceBundle.build(
        contract_id="lc-resource",
        evidence_bundle_id=EVIDENCE_ID,
        products={
            "knowledge": product("lecture_note"),
            "practice": product("quiz_set"),
            "assessment": None,
        },
    )

    assessment = bundle.branches[-1]
    assert assessment.branch_id == "assessment"
    assert assessment.status == "unavailable"
    assert assessment.required is False


def test_resource_bundle_rejects_cross_evidence_or_wrong_branch_type() -> None:
    wrong_evidence = product("quiz_set")
    wrong_evidence["payload"]["content"]["evidence_bundle_ref"] = "eb-other"
    with pytest.raises(ValueError, match="shared evidence bundle"):
        ResourceBundle.build(
            contract_id="lc-resource",
            evidence_bundle_id=EVIDENCE_ID,
            products={
                "knowledge": product("lecture_note"),
                "practice": product("quiz_set"),
                "assessment": wrong_evidence,
            },
        )
    with pytest.raises(ValueError, match="invalid payload type"):
        ResourceBundle.build(
            contract_id="lc-resource",
            evidence_bundle_id=EVIDENCE_ID,
            products={
                "knowledge": product("lecture_note"),
                "practice": product("quiz_set"),
                "assessment": product("practice_guide"),
            },
        )
