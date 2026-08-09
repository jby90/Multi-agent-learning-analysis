from __future__ import annotations

from types import MappingProxyType

import pytest

from coordination.evidence_bundle import EvidenceBundle


def bundle() -> EvidenceBundle:
    return EvidenceBundle(
        contract_id="lc-test-contract",
        knowledge_point="完成率计算",
        difficulty="basic",
        sources={
            "knowledge": {
                "chunk_ids": ["KB-003"],
                "difficulty_fallback": False,
            },
            "business_data": {
                "template_id": "T-02",
                "family": "Q3",
                "expected_columns": ["complete_rate"],
            },
            "pedagogy": {
                "profile_id": "line_leader",
                "lecture_style": "步骤化短句",
                "misconceptions": ["M-04"],
            },
        },
    )


def test_evidence_bundle_is_content_addressed_and_deeply_immutable() -> None:
    first = bundle()
    second = bundle()

    assert first.bundle_id == second.bundle_id
    assert first.bundle_id.startswith("eb-")
    assert isinstance(first.sources, MappingProxyType)
    assert first.source("knowledge")["chunk_ids"] == ("KB-003",)
    with pytest.raises(TypeError):
        first.sources["knowledge"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        first.source("knowledge")["chunk_ids"] = ()  # type: ignore[index]


def test_evidence_bundle_binds_all_resource_drafts_to_one_identity() -> None:
    evidence = bundle()
    original = {
        "payload": {
            "type": "lecture_note",
            "content": {"event": "product_ready"},
        }
    }

    bound = evidence.bind(original)

    assert "evidence_bundle_ref" not in original["payload"]["content"]
    assert bound["payload"]["content"]["evidence_bundle_ref"] == evidence.bundle_id
    assert bound["payload"]["content"]["evidence_source_ids"] == [
        "knowledge",
        "business_data",
        "pedagogy",
    ]
    assert bound["student_profile_ref"] == "line_leader"
    assert bound["payload"]["content"]["learning_contract_id"] == (
        evidence.contract_id
    )
    assert bound["payload"]["content"]["knowledge_point"] == (
        evidence.knowledge_point
    )
    assert bound["payload"]["content"]["difficulty"] == evidence.difficulty
    assert bound["payload"]["content"]["lineage_id"].startswith("lin-")
    assert bound["payload"]["content"]["artifact_id"].startswith("art-")
    assert bound["payload"]["content"]["generation_stage"] == "first_generation"


def test_evidence_bundle_rejects_cross_target_resource_draft() -> None:
    evidence = bundle()
    draft = {
        "payload": {
            "type": "quiz_set",
            "content": {
                "knowledge_point": "其他知识点",
                "difficulty": "advanced",
            },
        }
    }

    with pytest.raises(ValueError, match="knowledge_point"):
        evidence.bind(draft)


def test_evidence_bundle_requires_practice_scaffolds_to_match_bound_prerequisites() -> None:
    evidence = EvidenceBundle(
        contract_id="lc-advanced",
        knowledge_point="完成率计算",
        difficulty="advanced",
        sources={
            "knowledge": {
                "chunk_ids": ["KB-003-B", "KB-009"],
                "difficulty_fallback": False,
                "prerequisite_bindings": [
                    {
                        "knowledge_point": "责任单元定位",
                        "chunk_id": "KB-009",
                        "learning_goal": "按执行单元下钻定位异常",
                    }
                ],
            },
            "business_data": {"template_id": "T-02-B"},
            "pedagogy": {
                "profile_id": "line_leader",
                "lecture_style": "步骤化短句",
                "misconceptions": [],
            },
        },
    )
    exact = {
        "payload": {
            "type": "practice_guide",
            "content": {
                "prerequisite_scaffolds": [
                    {
                        "knowledge_point": "责任单元定位",
                        "chunk_id": "KB-009",
                        "learning_goal": "按执行单元下钻定位异常",
                    }
                ]
            },
        }
    }

    bound = evidence.bind(exact)
    assert bound["payload"]["content"]["prerequisite_scaffolds"] == [
        {
            "knowledge_point": "责任单元定位",
            "chunk_id": "KB-009",
            "learning_goal": "按执行单元下钻定位异常",
        }
    ]

    mismatched = {
        "payload": {
            "type": "practice_guide",
            "content": {
                "prerequisite_scaffolds": [
                    {
                        "knowledge_point": "责任单元定位",
                        "chunk_id": "KB-UNKNOWN",
                        "learning_goal": "自行编造的前置要求",
                    }
                ]
            },
        }
    }
    with pytest.raises(ValueError, match="prerequisite_scaffolds"):
        evidence.bind(mismatched)


def test_evidence_bundle_control_message_contains_only_auditable_summary() -> None:
    draft = bundle().control_draft("trace-evidence-bundle")
    content = draft["payload"]["content"]

    assert content["event"] == "evidence_bundle_ready"
    assert content["evidence_bundle"]["sources"]["business_data"] == {
        "template_id": "T-02",
        "family": "Q3",
        "expected_columns": ["complete_rate"],
    }


def test_evidence_bundle_rejects_missing_source_or_unknown_difficulty() -> None:
    sources = dict(bundle().as_dict()["sources"])
    sources.pop("pedagogy")
    with pytest.raises(ValueError, match="sources must contain"):
        EvidenceBundle("lc-test", "完成率计算", "basic", sources)
    with pytest.raises(ValueError, match="difficulty"):
        EvidenceBundle(
            "lc-test",
            "完成率计算",
            "expert",
            bundle().as_dict()["sources"],
        )
