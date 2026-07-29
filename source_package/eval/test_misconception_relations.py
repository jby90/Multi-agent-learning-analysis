from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from agents.knowledge_scope import CHUNK_DIRECTORY
from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.misconception_relations import (
    MisconceptionRelation,
    build_relation_index,
    default_relation_index,
    rank_related_targets,
)


PRODUCTION_IDS = ("M-01", "M-02", "M-03", "M-04", "M-05")
SYNTHETIC_IDS = ("M-Z", "M-Q", "M-A")


def synthetic_chunk(
    chunk_id: str,
    knowledge_point: str,
    common_mistakes: tuple[str, ...],
) -> KnowledgeChunk:
    return KnowledgeChunk(
        path=Path(f"{chunk_id}.md"),
        chunk_id=chunk_id,
        knowledge_point=knowledge_point,
        difficulty="basic",
        prerequisites=(),
        learning_goal=knowledge_point,
        common_mistakes=common_mistakes,
        applicable_processes=(),
        body="",
        sentences=(),
        metadata={},
    )


def relation(
    index: Mapping[str, tuple[MisconceptionRelation, ...]],
    source: str,
    target: str,
) -> tuple[int, int, tuple[str, ...]]:
    found = next(item for item in index[source] if item.target == target)
    return (
        found.knowledge_point_support,
        found.chunk_support,
        found.support_points,
    )


def test_real_chunks_produce_the_approved_weighted_relations() -> None:
    chunks = require_valid_chunks(CHUNK_DIRECTORY)

    index = build_relation_index(chunks, PRODUCTION_IDS)

    assert relation(index, "M-01", "M-04") == (
        2,
        4,
        ("计划量与实际量口径", "完成率计算"),
    )
    assert relation(index, "M-02", "M-05") == (
        2,
        4,
        ("三道工序与传导关系", "跨工序归因方法"),
    )
    assert relation(index, "M-01", "M-05") == (1, 1, ("完成率计算",))
    assert relation(index, "M-04", "M-05") == (1, 1, ("完成率计算",))
    assert index["M-03"] == ()


def test_index_is_symmetric_immutable_and_excludes_out_of_domain_ids() -> None:
    chunks = require_valid_chunks(CHUNK_DIRECTORY)

    index = build_relation_index(chunks, PRODUCTION_IDS)

    assert relation(index, "M-04", "M-01") == relation(index, "M-01", "M-04")
    assert isinstance(index, Mapping)
    with pytest.raises(TypeError):
        index["M-01"] = ()  # type: ignore[index]
    assert build_relation_index(chunks, ("M-FS01",))["M-FS01"] == ()


def test_ranking_respects_scope_probed_targets_and_domain_order() -> None:
    index = build_relation_index(require_valid_chunks(CHUNK_DIRECTORY), PRODUCTION_IDS)

    assert rank_related_targets(
        "M-01",
        responsibility_scope=("完成率计算",),
        probed=("M-01",),
        relation_index=index,
    ) == ("M-04", "M-05")
    assert rank_related_targets(
        "M-01",
        responsibility_scope=("完成率计算",),
        probed=("M-01", "M-04"),
        relation_index=index,
    ) == ("M-05",)
    assert rank_related_targets(
        "M-01",
        responsibility_scope=("完成率计算",),
        probed=("M-01",),
        relation_index={
            "M-01": (
                MisconceptionRelation("M-05", 1, 1, ("完成率计算",)),
                MisconceptionRelation("M-04", 2, 4, ("完成率计算",)),
            )
        },
    ) == ("M-04", "M-05")


def test_default_index_cache_is_keyed_by_ordered_domain_vocabulary() -> None:
    assert default_relation_index(("M-FS01",)) == {"M-FS01": ()}


def test_duplicate_mistakes_in_one_chunk_count_once_per_pair() -> None:
    index = build_relation_index(
        (synthetic_chunk("C-duplicate", "重复标注", ("M-Z", "M-Q", "M-Q")),),
        SYNTHETIC_IDS,
    )

    assert relation(index, "M-Z", "M-Q") == (1, 1, ("重复标注",))


def test_synthetic_relations_never_create_self_loops() -> None:
    index = build_relation_index(
        (synthetic_chunk("C-self", "重复标注", ("M-Z", "M-Z", "M-Q")),),
        SYNTHETIC_IDS,
    )

    assert all(
        relation.target != source
        for source, relations in index.items()
        for relation in relations
    )


def test_equal_weight_relations_follow_non_alphabetical_allowed_id_order() -> None:
    index = build_relation_index(
        (
            synthetic_chunk("C-q", "范围甲", ("M-Z", "M-Q")),
            synthetic_chunk("C-a", "范围乙", ("M-Z", "M-A")),
        ),
        SYNTHETIC_IDS,
    )

    assert tuple(relation.target for relation in index["M-Z"]) == ("M-Q", "M-A")
    assert rank_related_targets(
        "M-Z",
        responsibility_scope=("范围甲", "范围乙"),
        probed=("M-Z",),
        relation_index=index,
    ) == ("M-Q", "M-A")
