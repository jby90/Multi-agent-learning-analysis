from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from agents.kb_loader import Difficulty, KnowledgeChunk, require_valid_chunks
from agents.retriever import BM25Retriever, Retriever, tokenize


ROOT = Path(__file__).resolve().parents[1]
CHUNK_DIR = ROOT / "agents" / "knowledge_base" / "chunks"


def chunk(
    chunk_id: str,
    knowledge_point: str,
    difficulty: Difficulty = "basic",
    *,
    prerequisites: tuple[str, ...] = (),
    body: str = "相同正文",
    learning_goal: str = "掌握目标",
    common_mistakes: tuple[str, ...] = (),
    applicable_processes: tuple[str, ...] = ("YCL",),
) -> KnowledgeChunk:
    return KnowledgeChunk(
        path=Path(f"{chunk_id}.md"),
        chunk_id=chunk_id,
        knowledge_point=knowledge_point,
        difficulty=difficulty,
        prerequisites=prerequisites,
        learning_goal=learning_goal,
        common_mistakes=common_mistakes,
        applicable_processes=applicable_processes,
        body=body,
        sentences=(body,),
        metadata={},
    )


def ids(chunks: tuple[KnowledgeChunk, ...]) -> tuple[str, ...]:
    return tuple(item.chunk_id for item in chunks)


def test_tokenizer_is_nfkc_casefolded_and_uses_han_unigrams_and_bigrams() -> None:
    assert tokenize("ＡＢＣ 完成率") == (
        "abc",
        "完",
        "成",
        "率",
        "完成",
        "成率",
    )


def test_index_includes_every_approved_field() -> None:
    chunks = (
        chunk("KB-001", "目标", body="正文甲"),
        chunk("KB-002", "目标", learning_goal="学习乙"),
        chunk("KB-003", "目标", common_mistakes=("错误丙",)),
        chunk("KB-004", "目标", applicable_processes=("流程丁",)),
    )
    retriever = BM25Retriever(chunks)

    for keyword, expected in (
        ("正文甲", "KB-001"),
        ("学习乙", "KB-002"),
        ("错误丙", "KB-003"),
        ("流程丁", "KB-004"),
    ):
        assert ids(retriever.retrieve("目标", None, (keyword,)))[0] == expected


def test_difficulty_is_exact_and_none_does_not_filter() -> None:
    chunks = (
        chunk("KB-001", "目标", "basic"),
        chunk("KB-002", "目标", "applied"),
    )
    retriever = BM25Retriever(chunks)

    assert ids(retriever.retrieve("目标", "basic", ())) == ("KB-001",)
    assert ids(retriever.retrieve("目标", None, ())) == ("KB-001", "KB-002")


def test_difficulty_filter_never_falls_back() -> None:
    retriever = BM25Retriever((chunk("KB-001", "目标", "advanced"),))

    assert retriever.retrieve("目标", "basic", ()) == ()


def test_retrieval_appends_only_unique_direct_prerequisites_target_first() -> None:
    chunks = (
        chunk(
            "KB-TARGET",
            "目标",
            prerequisites=("KB-P2", "KB-P1", "KB-P2"),
        ),
        chunk("KB-P1", "前置一", prerequisites=("KB-GRAND",)),
        chunk("KB-P2", "前置二"),
        chunk("KB-GRAND", "传递前置"),
    )
    retriever = BM25Retriever(chunks)

    assert ids(retriever.retrieve("目标", "basic", ())) == (
        "KB-TARGET",
        "KB-P2",
        "KB-P1",
    )


def test_prerequisite_chunks_cannot_substitute_for_a_missing_target() -> None:
    retriever = BM25Retriever(
        (
            chunk("KB-P1", "前置一"),
            chunk("KB-P2", "前置二", prerequisites=("KB-P1",)),
        )
    )

    assert retriever.retrieve("不存在", "basic", ()) == ()


def test_real_corpus_retrieves_kb_and_sec_by_exact_knowledge_point() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(
        retriever.retrieve(
            "工序枚举与命名边界", "basic", ("raw", "工序枚举")
        )
    ) == ("SEC-001",)
    assert ids(
        retriever.retrieve(
            "三道工序与传导关系", "basic", ("托盘", "上下游")
        )
    ) == ("KB-001",)
    assert retriever.retrieve("工序枚举与命名边界", "applied", ()) == ()


def test_real_corpus_retrieves_completion_rate_by_each_exact_difficulty() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(retriever.retrieve("完成率计算", "basic", ())) == (
        "KB-003",
        "KB-002",
    )
    assert ids(retriever.retrieve("完成率计算", "applied", ())) == (
        "KB-003-A",
        "KB-002",
        "KB-003",
    )
    assert ids(retriever.retrieve("完成率计算", "advanced", ())) == (
        "KB-003-B",
        "KB-003",
        "KB-003-A",
        "KB-009",
    )


def test_real_corpus_retrieves_plan_actual_by_each_exact_difficulty() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(retriever.retrieve("计划量与实际量口径", "basic", ())) == (
        "KB-002",
    )
    assert ids(retriever.retrieve("计划量与实际量口径", "applied", ())) == (
        "KB-002-A",
        "KB-002",
    )
    assert ids(retriever.retrieve("计划量与实际量口径", "advanced", ())) == (
        "KB-002-B",
        "KB-002",
        "KB-002-A",
    )


def test_real_corpus_retrieves_process_propagation_by_each_exact_difficulty() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(retriever.retrieve("三道工序与传导关系", "basic", ())) == (
        "KB-001",
    )
    assert ids(retriever.retrieve("三道工序与传导关系", "applied", ())) == (
        "KB-001-A",
        "KB-001",
    )
    assert ids(retriever.retrieve("三道工序与传导关系", "advanced", ())) == (
        "KB-001-B",
        "KB-001",
        "KB-001-A",
    )


def test_real_corpus_retrieves_deviation_risk_by_each_exact_difficulty() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(retriever.retrieve("偏差率与风险等级", "basic", ())) == (
        "KB-004",
        "KB-003",
    )
    assert ids(retriever.retrieve("偏差率与风险等级", "applied", ())) == (
        "KB-004-A",
        "KB-003",
        "KB-004",
    )
    assert ids(retriever.retrieve("偏差率与风险等级", "advanced", ())) == (
        "KB-004-B",
        "KB-004",
        "KB-004-A",
    )


def test_real_corpus_retrieves_monthly_aggregation_by_each_exact_difficulty() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(retriever.retrieve("月度聚合方法", "basic", ())) == ("KB-005",)
    assert ids(retriever.retrieve("月度聚合方法", "applied", ())) == (
        "KB-005-A",
        "KB-005",
    )
    assert ids(retriever.retrieve("月度聚合方法", "advanced", ())) == (
        "KB-005-B",
        "KB-005",
        "KB-005-A",
    )


def test_real_corpus_retrieves_anomaly_identification_by_each_exact_difficulty() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(retriever.retrieve("异常识别标准", "basic", ())) == (
        "KB-006",
        "KB-003",
    )
    assert ids(retriever.retrieve("异常识别标准", "applied", ())) == (
        "KB-006-A",
        "KB-006",
    )
    assert ids(retriever.retrieve("异常识别标准", "advanced", ())) == (
        "KB-006-B",
        "KB-006",
        "KB-006-A",
    )


def test_real_corpus_retrieves_propagation_lag_by_each_exact_difficulty() -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    assert ids(retriever.retrieve("传导时滞分析", "basic", ())) == (
        "KB-007",
        "KB-001",
        "KB-006",
    )
    assert ids(retriever.retrieve("传导时滞分析", "applied", ())) == (
        "KB-007-A",
        "KB-007",
        "KB-001-A",
    )
    assert ids(retriever.retrieve("传导时滞分析", "advanced", ())) == (
        "KB-007-B",
        "KB-007",
        "KB-007-A",
        "KB-001-B",
    )


@pytest.mark.parametrize(
    ("knowledge_point", "expected_by_difficulty"),
    (
        (
            "异常衰减规律",
            (
                ("KB-008", "KB-007"),
                ("KB-008-A", "KB-008", "KB-007-A"),
                ("KB-008-B", "KB-008", "KB-008-A", "KB-001-B"),
            ),
        ),
        (
            "责任单元定位",
            (
                ("KB-009", "KB-005"),
                ("KB-009-A", "KB-009", "KB-005-A"),
                ("KB-009-B", "KB-009", "KB-009-A", "KB-001-B"),
            ),
        ),
        (
            "跨工序归因方法",
            (
                ("KB-010", "KB-009", "KB-007", "KB-008"),
                ("KB-010-A", "KB-010", "KB-007-A", "KB-008-A", "KB-009-A"),
                ("KB-010-B", "KB-010", "KB-010-A", "KB-001-B"),
            ),
        ),
    ),
)
def test_real_corpus_retrieves_remaining_transmission_points_by_exact_difficulty(
    knowledge_point: str,
    expected_by_difficulty: tuple[tuple[str, ...], ...],
) -> None:
    retriever = BM25Retriever(require_valid_chunks(CHUNK_DIR))

    for difficulty, expected_ids in zip(
        ("basic", "applied", "advanced"), expected_by_difficulty, strict=True
    ):
        assert ids(retriever.retrieve(knowledge_point, difficulty, ())) == expected_ids


def test_chinese_keywords_distinguish_chunks_with_the_same_knowledge_point() -> None:
    chunks = (
        chunk("KB-001", "聚合方法", body="使用加权比值汇总"),
        chunk("KB-002", "聚合方法", body="使用日均平均观察"),
        chunk("KB-003", "聚合方法", body="仅作对照说明"),
    )
    retriever = BM25Retriever(chunks)

    assert ids(retriever.retrieve("聚合方法", None, ("加权比值",)))[0] == "KB-001"
    assert ids(retriever.retrieve("聚合方法", None, ("日均平均",)))[0] == "KB-002"


def test_ties_use_chunk_id_and_ignore_constructor_order() -> None:
    chunks = (
        chunk("KB-003", "目标", body="相同"),
        chunk("KB-001", "目标", body="相同"),
    )

    forward = BM25Retriever(chunks).retrieve("目标", None, ("未命中",))
    reverse = BM25Retriever(tuple(reversed(chunks))).retrieve(
        "目标", None, ("未命中",)
    )

    assert ids(forward) == ("KB-001", "KB-003")
    assert ids(reverse) == ("KB-001", "KB-003")


def test_empty_filter_returns_empty_and_top_three_is_stable() -> None:
    retriever = BM25Retriever(
        tuple(chunk(f"KB-{index:03d}", "目标") for index in range(1, 6))
    )

    assert retriever.retrieve("不存在", None, ()) == ()
    outputs = [
        ids(retriever.retrieve("目标", None, ("零分词",))) for _ in range(3)
    ]
    assert outputs == [("KB-001", "KB-002", "KB-003")] * 3


@pytest.mark.parametrize(
    ("knowledge_point", "difficulty", "keywords"),
    (
        ("", None, ()),
        ("   ", None, ()),
        ("目标", "expert", ()),
        ("目标", None, cast(tuple[str, ...], (1,))),
    ),
)
def test_invalid_query_values_fail_before_scoring(
    knowledge_point: str,
    difficulty: str | None,
    keywords: tuple[str, ...],
) -> None:
    retriever = BM25Retriever((chunk("KB-001", "目标"),))

    with pytest.raises(ValueError):
        retriever.retrieve(knowledge_point, difficulty, keywords)


def test_retriever_protocol_is_runtime_checkable() -> None:
    class FakeRetriever:
        def retrieve(
            self,
            knowledge_point: str,
            difficulty: str | None,
            keywords: tuple[str, ...],
        ) -> tuple[KnowledgeChunk, ...]:
            return ()

    assert isinstance(FakeRetriever(), Retriever)
