from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.domain_config import load_domain_config
from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.knowledge_agent import KnowledgeAgent
from agents.review_agent import _r02_formula_hit
from agents.semantic_claims import build_claim_plan, enforce_claim_plan
from orchestrator.llm import LLMResult, TokenUsage


ROOT = Path(__file__).resolve().parents[1]
CHUNK_DIR = ROOT / "agents" / "knowledge_base" / "chunks"
PROFILE = {
    "profile_id": "line_leader",
    "role": "一线班组长",
    "strength": "现场操作",
    "gap": "理论与数据基础",
}


def _chunks_by_id() -> dict[str, KnowledgeChunk]:
    return {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}


class _StaticRetriever:
    def __init__(self, chunks: tuple[KnowledgeChunk, ...]) -> None:
        self.chunks = chunks

    def retrieve(
        self,
        knowledge_point: str,
        difficulty: str | None,
        keywords: tuple[str, ...],
    ) -> tuple[KnowledgeChunk, ...]:
        return self.chunks


class _FixedLLM:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        return LLMResult(
            data=deepcopy(self.data),
            model="fixed-stub",
            latency_ms=1,
            token_usage=TokenUsage(10, 5, 15),
            attempts=1,
        )


def test_production_claim_plan_repairs_reversed_and_ambiguous_formulas() -> None:
    chunks = _chunks_by_id()
    plan = build_claim_plan(
        load_domain_config("production_progress"),
        "计划量与实际量口径",
        (chunks["KB-002"],),
    )

    assert [item.invariant.invariant_id for item in plan] == [
        "PP-COMPLETE-RATE-001"
    ]
    result = enforce_claim_plan(
        (
            "完成率通过计划量除以实际量计算。"
            "计算计划量和实际量的比值作为完成率。"
            "正确警示：不能用计划量除以实际量。"
        ),
        plan,
    )

    assert "完成率通过实际量÷计划量计算" in result.text
    assert "计算实际量÷计划量作为完成率" in result.text
    assert "不能用计划量除以实际量" in result.text
    assert result.checked == 2
    assert result.repaired == 2
    assert result.failures == ()


def test_correct_formula_is_left_unchanged() -> None:
    chunks = _chunks_by_id()
    plan = build_claim_plan(
        load_domain_config("production_progress"),
        "完成率计算",
        (chunks["KB-003"], chunks["KB-002"]),
    )
    source = "完成率 = 实际量 ÷ 计划量。"

    result = enforce_claim_plan(source, plan)

    assert result.text == source
    assert result.repaired == 0
    assert result.failures == ()


def test_second_domain_blocks_an_invented_formula() -> None:
    chunks = _chunks_by_id()
    plan = build_claim_plan(
        load_domain_config("first_segment"),
        "完成率字段口径",
        (chunks["SEC-003"],),
    )

    result = enforce_claim_plan(
        "完成率等于完成数除以计划数，可以自行计算。",
        plan,
    )

    assert result.text == (
        "完成率的计算公式、分子分母、聚合方式和判定阈值，"
        "raw未明确，以现场口径为准。"
    )
    assert result.repaired == 1
    assert result.failures == ()


def test_e2e_010_lecture_is_repaired_before_review() -> None:
    chunks = _chunks_by_id()
    llm = _FixedLLM(
        {
            "lecture_md": (
                "## 核心概念\n"
                "- 完成率：计划量与实际量的比值。\n\n"
                "## 小结\n"
                "完成率通过计划量除以实际量计算，用于判断生产状态。"
            ),
            "claims": [
                {
                    "text": "问做得怎么样要计算两者的比值。",
                    "kind": "speculation",
                }
            ],
            "coverage": ["计划量与实际量口径"],
        }
    )
    agent = KnowledgeAgent(
        trace_id="e2e-010-formula-regression",
        retriever=_StaticRetriever((chunks["KB-002"],)),
        llm_call=llm,
        clock=lambda: datetime(2026, 7, 29, tzinfo=timezone.utc),
        domain_config=load_domain_config("production_progress"),
    )

    draft = agent.generate(
        knowledge_point="计划量与实际量口径",
        student_profile=PROFILE,
        learning_report_summary="混淆计划量与实际量",
    )

    lecture = draft["payload"]["content"]["lecture_md"]
    assert "计划量除以实际量" not in lecture
    assert "计划量与实际量的比值" not in lecture
    assert "实际量÷计划量" in lecture
    assert draft["payload"]["content"]["semantic_validation"] == {
        "checked": 2,
        "repaired": 2,
        "failures": [],
        "invariant_ids": ["PP-COMPLETE-RATE-001"],
    }
    assert _r02_formula_hit(draft) is None
    assert "semantic_claim_plan" in llm.calls[0]["user"]
