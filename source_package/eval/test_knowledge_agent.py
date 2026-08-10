from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Sequence

from jsonschema import Draft202012Validator
import pytest

from agents.domain_config import active_domain_config
from agents.kb_loader import (
    EvidenceContextGroup,
    KnowledgeChunk,
    TeachingFact,
    TeachingFactCard,
    require_valid_chunks,
)
from agents.knowledge_agent import KNOWLEDGE_OUTPUT_SCHEMA, KnowledgeAgent
from agents.retriever import BM25Retriever
from orchestrator import runtime
from orchestrator.agents_stub import KnowledgeStub, build_stubs, profile_loaded_draft
from orchestrator.bus import MessageBus
from orchestrator.engine import OrchestratorEngine
from orchestrator.llm import LLMResult, TokenUsage
from orchestrator.transitions import State


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "agents" / "prompts" / "knowledge.md"
FIXED_NOW = datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc)
PLANNER_PROFILE = {
    "profile_id": "planner_new",
    "role": "新入职生产计划员",
    "strength": "SQL和数据分析",
    "gap": "船舶工序与口径",
}
EXPECTED_PROMPT = """你是船舶制造岗位培训讲师。根据学员画像与提供的知识切片撰写微课讲义。只输出JSON：
{"lecture_md": "...", "claims": [{"text":"...", "kind":"fact", "chunk_id":"KB-xxx", "sentence_ref":[3]}], "coverage": ["涉及的知识点"]}
规则：
1. 每条确定性专业表述必须列入claims并给出chunk_id与sentence_ref。sentence_ref中的每个序号必须直接选自同一chunk正文已展示的[S1]...[Sn]，序号从1开始；禁止0、负数、越界或自造序号。找不到支撑锚点时不得输出fact，改为speculation且不得携带chunk_id或sentence_ref。
2. 切片中没有的内容禁止写成事实——需要补充说明时用"一般来说""通常"开头并在claims中标kind=speculation。禁止自造任何数值示例（如"计划1000、实际800、完成率80%"这类模型自编的数字）；方法教学使用文字、步骤和判断表述，不用具体数值举例。仅当切片正文提供带[S#]锚点的示例数值时，才可引用，并必须在claims中申报对应sentence_ref；切片未提供数值时一律不写数值。正文中含数字或"必须/始终/等于"类确定性判断的句子须在claims申报，写不进claims的改用"一般来说/建议"表述。
3. 先读取knowledge_point_match。值为false时只能输出{"lecture_md": null, "refuse_reason": "..."}；值为true时可根据切片充分性生成讲义或自主拒答，拒答必须给出非空refuse_reason。成功与拒答两分支互斥：成功禁refuse_reason，拒答禁claims/coverage。不得改讲切片自身知识点，不得用画像或学情摘要补足。
4. 按画像调整：planner_new重讲工序与口径、少讲SQL；craft_engineer重讲数据工具与图表、少讲工艺常识；line_leader步骤化、短句、每步带检查点。
5. 讲义结构：本节目标→核心概念→计算步骤/方法要点→常见错误提醒→小结。常见错误提醒只能来自切片正文已有说明，正文未说明则该节写"参见教师讲解"。
6. [S#]只用于claims选择，禁止出现在lecture_md。
7. semantic_claim_plan中的canonical_claim是当前领域的不可变事实边界：正文涉及对应metric时必须使用canonical_expression；不得交换分子分母、改写成方向不明的“二者相除”，也不得为mode=stored的指标自行推导公式。
[画像JSON + 学情报告摘要 + top-3切片全文]
"""
SUCCESS = {
    "lecture_md": "# 本节目标\n理解完成率",
    "claims": [
        {
            "text": "完成率有固定口径。",
            "kind": "fact",
            "chunk_id": "KB-003",
            "sentence_ref": [1],
        }
    ],
    "coverage": ["完成率计算"],
}
REFUSAL = {"lecture_md": None, "refuse_reason": "切片不足"}
BASELINE_REQUEST_SHA256 = (
    "c01a67fa62ae924d149673bc20e397a7e0990fc5ae63124fb53700b655f0d0ac"
)
BASELINE_MESSAGE_WITHOUT_WALL_LATENCY_SHA256 = (
    "e831f560f6e45607024950edd430f780f2d44fff84d7f8078cc634328fbe364a"
)
TEACHING_FACT_1 = "本项目岗位培训与评测采用的月完成率正常波动区间为88%—103%。"
TEACHING_FACT_2 = (
    "月完成率低于88%时，先作为偏低信号观察，不能仅凭单月数值直接定性；"
    "还须结合持续性或伴随的风险记录。"
)


def make_teaching_fact_card(
    *,
    card_id: str = "GENERIC-RANGE-CARD",
    title: str = "本项目异常识别口径卡",
    facts: tuple[tuple[str, str], ...] = (
        ("TF-GENERIC-001", TEACHING_FACT_1),
        ("TF-GENERIC-002", TEACHING_FACT_2),
    ),
) -> TeachingFactCard:
    return TeachingFactCard(
        schema_version=1,
        card_id=card_id,
        card_version="1.0.0",
        title=title,
        source={
            "type": "project_business_asset",
            "path": "项目业务资产",
            "locator": "异常识别阈值",
            "version": "交付版",
            "git_commit": "不适用",
            "git_blob": "不适用",
            "effective_scope": "岗位培训与评测",
        },
        facts=tuple(TeachingFact(fact_id=fact_id, text=text) for fact_id, text in facts),
    )


def make_chunk(
    chunk_id: str = "KB-003",
    knowledge_point: str = "完成率计算",
    *,
    body: str = "完成率 = 实际量 ÷ 计划量。",
    sentences: tuple[str, ...] | None = None,
    teaching_fact_card: TeachingFactCard | None = None,
    evidence_context_groups: tuple[EvidenceContextGroup, ...] = (),
    prerequisites: tuple[str, ...] = (),
) -> KnowledgeChunk:
    return KnowledgeChunk(
        path=Path(f"{chunk_id}.md"),
        chunk_id=chunk_id,
        knowledge_point=knowledge_point,
        difficulty="basic",
        prerequisites=prerequisites,
        learning_goal="能够正确计算完成率",
        common_mistakes=("M-01",),
        applicable_processes=("YCL",),
        body=body,
        sentences=sentences if sentences is not None else (body,),
        metadata={},
        teaching_fact_card=teaching_fact_card,
        evidence_context_groups=evidence_context_groups,
    )


KB003 = make_chunk()
UNRELATED = make_chunk(
    "KB-900",
    "三道工序与传导关系",
    body="预处理、制作托盘、安装托盘构成上下游关系。",
)
CHUNK_DIR = ROOT / "agents" / "knowledge_base" / "chunks"
CANONICAL_QUOTES = {
    "KB-001": "船舶制造中与托盘相关的三道关键工序按物流顺序为：**钢材预处理（YCL）→ 制作托盘（ZZTP）→ 安装托盘（AZTP）**。",
    "KB-002": "生产进度表中每条日级记录同时携带两个数量字段：**计划量（plan_qty）**是排产系统下达的当日应完成量；**实际量（actual_qty）**是车间实际报工的完成量。",
    "KB-003": "**完成率 = 实际量 ÷ 计划量**。",
    "KB-004": "**偏差率（deviation_rate）= (实际量−计划量)÷计划量**，负值表示欠产，正值表示超产。",
    "KB-005": "生产进度数据是**日级**记录（period_date），月度分析必须先聚合。",
    "KB-006": "判定完成率异常的基准是**正常波动区间**：本数据环境下各船各工序的月完成率正常范围约为**88%—103%**（生产有自然波动，略超100%属正常超额）。",
    "KB-007": "上游异常影响下游可能存在**时滞**：在制品缓冲使传导不一定当月显现。**不能只用同月数据确认或排除传导**——同月正常不能排除后续影响，同月同时异常也不能仅凭月度数据确认即时传导。",
    "KB-008": "（本知识点名为\"异常衰减规律\"，实际指**异常强度沿链变化**；衰减只是其中一种形态，**不存在普适的逐级衰减规律**。）",
    "KB-009": "每条生产记录归属一个**责任单元（workshop_code，车间/工区）**。",
    "KB-010": "跨工序归因**不是另造一个新指标**，而是对异常、对象依赖、时序、齐套和执行单元证据进行编排与综合判断，最终得到\"候选源头 + 传导证据 + 证据缺口\"，而不是一步锁定根因、更不是直接定责任。",
}
PROFILES = (
    PLANNER_PROFILE,
    {
        "profile_id": "craft_engineer",
        "role": "工艺工程师转数据分析",
        "strength": "预处理与托盘工艺",
        "gap": "数据工具与图表",
    },
    {
        "profile_id": "line_leader",
        "role": "一线班组长/调度储备",
        "strength": "现场操作",
        "gap": "理论与数据基础",
    },
)


class StaticRetriever:
    def __init__(self, chunks: tuple[KnowledgeChunk, ...]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, str | None, tuple[str, ...]]] = []

    def retrieve(
        self,
        knowledge_point: str,
        difficulty: str | None,
        keywords: Sequence[str],
    ) -> tuple[KnowledgeChunk, ...]:
        self.calls.append((knowledge_point, difficulty, tuple(keywords)))
        return self.chunks


class SpyLLM:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        return LLMResult(
            data=deepcopy(self.data),
            model="qwen3-235b-a22b",
            latency_ms=87,
            token_usage=TokenUsage(100, 20, 120),
            attempts=1,
        )


def build_agent(
    *,
    llm_data: dict[str, Any],
    chunks: tuple[KnowledgeChunk, ...] = (KB003,),
) -> tuple[KnowledgeAgent, StaticRetriever, SpyLLM]:
    retriever = StaticRetriever(chunks)
    llm = SpyLLM(llm_data)
    agent = KnowledgeAgent(
        trace_id="trace-p3",
        retriever=retriever,
        llm_call=llm,
        clock=lambda: FIXED_NOW,
    )
    return agent, retriever, llm


def generate(agent: KnowledgeAgent) -> dict[str, Any]:
    return agent.generate(
        knowledge_point="完成率计算",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="混淆计划量与实际量",
    )


def test_prompt_matches_the_approved_design_verbatim() -> None:
    assert PROMPT_PATH.read_text(encoding="utf-8") == EXPECTED_PROMPT


def test_output_schema_accepts_only_exclusive_success_or_refusal() -> None:
    validator = Draft202012Validator(KNOWLEDGE_OUTPUT_SCHEMA)

    assert list(validator.iter_errors(SUCCESS)) == []
    assert list(validator.iter_errors(REFUSAL)) == []
    assert list(validator.iter_errors({**REFUSAL, "claims": []}))
    assert list(validator.iter_errors({"lecture_md": "x", "claims": [], "coverage": []}))


def test_output_schema_requires_fact_sentence_refs_and_rejects_legacy_quotes() -> None:
    validator = Draft202012Validator(KNOWLEDGE_OUTPUT_SCHEMA)
    speculation = {
        "lecture_md": "通常需要结合现场判断。",
        "claims": [{"text": "通常需要结合现场判断。", "kind": "speculation"}],
        "coverage": [],
    }

    assert list(validator.iter_errors(speculation)) == []
    missing_ref = deepcopy(SUCCESS)
    missing_ref["claims"][0].pop("sentence_ref")
    assert list(validator.iter_errors(missing_ref))
    legacy_quote = deepcopy(SUCCESS)
    legacy_quote["claims"][0]["quote"] = "完成率 = 实际量 ÷ 计划量。"
    assert list(validator.iter_errors(legacy_quote))
    empty_ref = deepcopy(SUCCESS)
    empty_ref["claims"][0]["sentence_ref"] = []
    assert list(validator.iter_errors(empty_ref)) == []


def test_valid_fact_builds_exact_kb_evidence_and_protocol_message(
    tmp_path: Path,
) -> None:
    agent, _, _ = build_agent(llm_data=SUCCESS)

    draft = generate(agent)

    assert draft["payload"]["content"]["event"] == "product_ready"
    assert draft["payload"]["content"]["quote_validation"] == {
        "checked": 1,
        "passed": 1,
        "failed": 0,
        "failures": [],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }
    assert draft["payload"]["content"]["responsibility_scope"] == [
        "完成率计算",
        "计划量与实际量口径",
    ]
    assert draft["claims"] == [
        {"text": "完成率 = 实际量 ÷ 计划量。", "kind": "fact"}
    ]
    assert draft["evidence"] == [
        {
            "kind": "kb_chunk",
            "ref": "KB-003",
            "quote": "完成率 = 实际量 ÷ 计划量。",
            "supports_claim": "完成率 = 实际量 ÷ 计划量。",
        }
    ]
    assert "完成率 = 实际量 ÷ 计划量。" in draft["payload"]["content"]["lecture_md"]
    assert all(set(claim) == {"text", "kind"} for claim in draft["claims"])
    assert "claim_id" not in json.dumps(draft, ensure_ascii=False)
    assert MessageBus(tmp_path / "traces").send(draft).accepted


@pytest.mark.parametrize(
    ("sentence_ref", "chunk_id"),
    (
        ([], "KB-003"),
        ([999], "KB-003"),
        (["1"], "KB-003"),
        ([1], "KB-999"),
    ),
)
def test_invalid_sentence_refs_do_not_launder_a_fact_as_speculation(
    sentence_ref: list[Any],
    chunk_id: str,
) -> None:
    data = deepcopy(SUCCESS)
    data["claims"][0].update(sentence_ref=sentence_ref, chunk_id=chunk_id)
    agent, _, _ = build_agent(llm_data=data)

    draft = generate(agent)

    lecture = draft["payload"]["content"]["lecture_md"]
    assert "完成率有固定口径。" not in lecture
    assert "完成率 = 实际量 ÷ 计划量。" in lecture
    assert draft["claims"] == [
        {"text": "完成率 = 实际量 ÷ 计划量。", "kind": "fact"}
    ]
    assert draft["evidence"] == [
        {
            "kind": "kb_chunk",
            "ref": "KB-003",
            "quote": "完成率 = 实际量 ÷ 计划量。",
            "supports_claim": "完成率 = 实际量 ÷ 计划量。",
        }
    ]
    assert draft["payload"]["content"]["quote_validation"] == {
        "checked": 1,
        "passed": 0,
        "failed": 1,
        "failures": [
            {
                "claim_text": "完成率有固定口径。",
                "sentence_ref": sentence_ref,
                "reason": "invalid_sentence_ref",
            }
        ],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }


def test_multiple_sentence_refs_create_ordered_evidence_without_joining() -> None:
    multi_chunk = make_chunk(
        body="第一句。第二句。",
        sentences=("第一句。", "第二句。"),
    )
    data = deepcopy(SUCCESS)
    data["claims"][0]["sentence_ref"] = [2, 1]
    agent, _, _ = build_agent(llm_data=data, chunks=(multi_chunk,))

    draft = generate(agent)

    assert draft["claims"] == [
        {"text": "第二句。", "kind": "fact"},
        {"text": "第一句。", "kind": "fact"},
    ]
    assert draft["evidence"] == [
        {
            "kind": "kb_chunk",
            "ref": "KB-003",
            "quote": "第二句。",
            "supports_claim": "第二句。",
        },
        {
            "kind": "kb_chunk",
            "ref": "KB-003",
            "quote": "第一句。",
            "supports_claim": "第一句。",
        },
    ]


def test_duplicate_public_atom_unions_evidence_in_source_order() -> None:
    first = make_chunk(
        "KB-DUP-1",
        body="同一条批准事实。",
    )
    second = make_chunk(
        "KB-DUP-2",
        body="同一条批准事实。",
    )
    data = {
        "lecture_md": "模型句甲。模型句乙。",
        "claims": [
            {
                "text": "模型句甲。",
                "kind": "fact",
                "chunk_id": "KB-DUP-1",
                "sentence_ref": [1],
            },
            {
                "text": "模型句乙。",
                "kind": "fact",
                "chunk_id": "KB-DUP-2",
                "sentence_ref": [1],
            },
        ],
        "coverage": ["模型自报覆盖"],
    }
    agent, _, _ = build_agent(llm_data=data, chunks=(first, second))

    draft = generate(agent)

    assert draft["claims"] == [{"text": "同一条批准事实。", "kind": "fact"}]
    assert draft["evidence"] == [
        {
            "kind": "kb_chunk",
            "ref": chunk_id,
            "quote": "同一条批准事实。",
            "supports_claim": "同一条批准事实。",
        }
        for chunk_id in ("KB-DUP-1", "KB-DUP-2")
    ]


def test_missing_direct_prerequisite_gets_one_grounded_foundation_atom() -> None:
    target = make_chunk(
        "KB-TARGET",
        "偏差率计算",
        body="偏差率用于描述实际量相对计划量的偏离。",
        prerequisites=("KB-PREREQ",),
    )
    prerequisite = make_chunk(
        "KB-PREREQ",
        "完成率计算",
        body="数据示例：计划100，实际80。完成率 = 实际量 ÷ 计划量。",
        sentences=("数据示例：计划100，实际80。", "完成率 = 实际量 ÷ 计划量。"),
    )
    data = {
        "lecture_md": "# 偏差率计算\n先识别偏离方向。",
        "claims": [
            {
                "text": "偏差率用于描述偏离。",
                "kind": "fact",
                "chunk_id": "KB-TARGET",
                "sentence_ref": [1],
            }
        ],
        "coverage": ["模型自报的不可信覆盖"],
    }
    agent, _, _ = build_agent(llm_data=data, chunks=(target, prerequisite))

    draft = agent.generate(
        knowledge_point="偏差率计算",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="尚未掌握完成率基础。",
    )

    assert draft["claims"] == [
        {
            "text": "偏差率用于描述实际量相对计划量的偏离。",
            "kind": "fact",
        },
        {"text": "完成率 = 实际量 ÷ 计划量。", "kind": "fact"},
    ]
    assert draft["payload"]["content"]["coverage"] == [
        "偏差率计算",
        "完成率计算",
    ]
    lecture = draft["payload"]["content"]["lecture_md"]
    assert "完成率 = 实际量 ÷ 计划量。" in lecture
    assert "数据示例：计划100，实际80。" not in lecture
    assert "模型自报的不可信覆盖" not in draft["payload"]["content"]["coverage"]


def test_prerequisite_identity_sentence_cannot_rename_the_current_lesson() -> None:
    target = make_chunk(
        "KB-TARGET",
        "跨工序归因方法",
        body="跨工序归因需要编排异常、时序与证据缺口。",
        prerequisites=("KB-PREREQ",),
    )
    prerequisite = make_chunk(
        "KB-PREREQ",
        "异常衰减规律",
        body=(
            '（本知识点名为"异常衰减规律"，实际指异常强度沿链变化。）'
            "异常判断需要比较相邻工序的强度变化。"
        ),
        sentences=(
            '（本知识点名为"异常衰减规律"，实际指异常强度沿链变化。）',
            "异常判断需要比较相邻工序的强度变化。",
        ),
    )
    data = {
        "lecture_md": "# 跨工序归因方法\n先编排目标工序的证据。",
        "claims": [
            {
                "text": "跨工序归因需要编排证据。",
                "kind": "fact",
                "chunk_id": "KB-TARGET",
                "sentence_ref": [1],
            }
        ],
        "coverage": ["跨工序归因方法"],
    }
    agent, _, _ = build_agent(llm_data=data, chunks=(target, prerequisite))

    draft = agent.generate(
        knowledge_point="跨工序归因方法",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="需要补充跨工序证据编排。",
    )

    lecture = draft["payload"]["content"]["lecture_md"]
    assert '本知识点名为"异常衰减规律"' not in lecture
    assert "异常判断需要比较相邻工序的强度变化。" in lecture


def test_structural_lead_ref_expands_original_list_sentences_as_evidence() -> None:
    sentences = (
        "引言。",
        "是否成立，要结合：",
        "- 第一项。",
        "第一项补充；",
        "继续说明。",
        "- 第二项。",
    )
    chunk = make_chunk(
        "KB-901",
        body="\n\n".join(sentences),
        sentences=sentences,
        evidence_context_groups=(
            EvidenceContextGroup(lead_ref=2, member_refs=(3, 4, 5, 6)),
        ),
    )
    claim_text = "判断需要综合第一项和第二项。"
    data = deepcopy(SUCCESS)
    data["claims"][0].update(
        text=claim_text,
        chunk_id="KB-901",
        sentence_ref=[2],
    )
    agent, _, _ = build_agent(llm_data=data, chunks=(chunk,))

    draft = generate(agent)

    atom_text = "是否成立，要结合："
    assert draft["claims"] == [{"text": atom_text, "kind": "fact"}]
    assert draft["evidence"] == [
        {
            "kind": "kb_chunk",
            "ref": "KB-901",
            "quote": sentence,
            "supports_claim": atom_text,
        }
        for sentence in sentences[1:]
    ]
    assert draft["payload"]["content"]["quote_validation"] == {
        "checked": 1,
        "passed": 1,
        "failed": 0,
        "failures": [],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }


def test_structural_expansion_deduplicates_and_orders_multiple_groups() -> None:
    sentences = tuple(f"句子{index}。" for index in range(1, 11))
    chunk = make_chunk(
        "KB-902",
        body="".join(sentences),
        sentences=sentences,
        evidence_context_groups=(
            EvidenceContextGroup(lead_ref=2, member_refs=(3, 4)),
            EvidenceContextGroup(lead_ref=7, member_refs=(8, 9)),
        ),
    )
    data = deepcopy(SUCCESS)
    data["claims"][0].update(
        chunk_id="KB-902",
        sentence_ref=[9, 7, 3, 2],
    )
    agent, _, _ = build_agent(llm_data=data, chunks=(chunk,))

    draft = generate(agent)

    assert [claim["text"] for claim in draft["claims"]] == [
        sentences[index - 1] for index in (9, 7, 3, 2)
    ]
    assert [
        (item["quote"], item["supports_claim"])
        for item in draft["evidence"]
    ] == [
        (sentences[8], sentences[8]),
        (sentences[6], sentences[6]),
        (sentences[7], sentences[6]),
        (sentences[8], sentences[6]),
        (sentences[2], sentences[2]),
        (sentences[1], sentences[1]),
        (sentences[2], sentences[1]),
        (sentences[3], sentences[1]),
    ]


def test_structural_evidence_groups_do_not_change_the_llm_request() -> None:
    sentences = ("引言。", "需要结合：", "- 第一项。", "- 第二项。")
    plain = make_chunk(
        "KB-903",
        body="\n\n".join(sentences),
        sentences=sentences,
    )
    grouped = make_chunk(
        "KB-903",
        body=plain.body,
        sentences=sentences,
        evidence_context_groups=(
            EvidenceContextGroup(lead_ref=2, member_refs=(3, 4)),
        ),
    )
    plain_agent, _, plain_llm = build_agent(llm_data=SUCCESS, chunks=(plain,))
    grouped_agent, _, grouped_llm = build_agent(
        llm_data=SUCCESS,
        chunks=(grouped,),
    )

    generate(plain_agent)
    generate(grouped_agent)

    assert grouped_llm.calls[0]["user"] == plain_llm.calls[0]["user"]


def test_real_kb006_applied_s6_expands_through_s13_without_changing_request() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    chunk = chunks["KB-006-A"]
    claim_text = "异常判断要结合偏差幅度与影响、持续性和独立佐证。"
    data = deepcopy(SUCCESS)
    data["claims"][0].update(
        text=claim_text,
        chunk_id="KB-006-A",
        sentence_ref=[6],
    )
    agent, _, llm = build_agent(llm_data=data, chunks=(chunk,))

    draft = agent.generate(
        knowledge_point="异常识别标准",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="需要综合多条线索判断异常。",
        difficulty="applied",
    )

    assert [item["quote"] for item in draft["evidence"]] == list(
        chunk.sentences[5:13]
    )
    atom_text = KnowledgeAgent._public_claim_text(chunk.sentences[5])
    assert all(
        item["supports_claim"] == atom_text for item in draft["evidence"]
    )
    request = json.loads(llm.calls[0]["user"])
    assert request["chunks"][0]["body"] == "\n".join(
        f"[S{index}] {sentence}"
        for index, sentence in enumerate(chunk.sentences, start=1)
    )


def test_structural_expansion_does_not_reintroduce_held_out_teaching_fact() -> None:
    visible_member = "- 可见列表项。"
    sentences = (
        "引言。",
        "需要结合：",
        TEACHING_FACT_1,
        visible_member,
    )
    chunk = make_chunk(
        "KB-904",
        body="\n\n".join(sentences),
        sentences=sentences,
        teaching_fact_card=make_teaching_fact_card(
            facts=(("TF-GENERIC-001", TEACHING_FACT_1),)
        ),
        evidence_context_groups=(
            EvidenceContextGroup(lead_ref=2, member_refs=(3, 4)),
        ),
    )
    claim_text = "需要结合列表中的可见信息。"
    data = deepcopy(SUCCESS)
    data["claims"][0].update(
        text=claim_text,
        chunk_id="KB-904",
        sentence_ref=[2],
    )
    agent, _, llm = build_agent(llm_data=data, chunks=(chunk,))

    draft = generate(agent)

    atom_text = "需要结合："
    claim_evidence = [
        item for item in draft["evidence"] if item["supports_claim"] == atom_text
    ]
    assert [item["quote"] for item in claim_evidence] == [
        "需要结合：",
        visible_member,
    ]
    assert TEACHING_FACT_1 not in llm.calls[0]["user"]


def test_native_speculation_stays_speculation_and_target_gets_grounded() -> None:
    data = {
        "lecture_md": "通常需要结合现场判断。",
        "claims": [{"text": "通常需要结合现场判断。", "kind": "speculation"}],
        "coverage": ["完成率计算"],
    }
    agent, _, _ = build_agent(llm_data=data)

    draft = generate(agent)

    assert draft["claims"] == [
        *data["claims"],
        {"text": "完成率 = 实际量 ÷ 计划量。", "kind": "fact"},
    ]
    assert draft["evidence"] == [
        {
            "kind": "kb_chunk",
            "ref": "KB-003",
            "quote": "完成率 = 实际量 ÷ 计划量。",
            "supports_claim": "完成率 = 实际量 ÷ 计划量。",
        }
    ]
    assert draft["payload"]["content"]["quote_validation"] == {
        "checked": 0,
        "passed": 0,
        "failed": 0,
        "failures": [],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }


def test_internal_markers_are_removed_and_counted_before_persistence() -> None:
    data = deepcopy(SUCCESS)
    data["lecture_md"] = (
        "# 标题[S1]\n属于M-02；M-03；保留AM-04、M-021与[Sx]。"
    )
    agent, _, _ = build_agent(llm_data=data)

    draft = generate(agent)

    content = draft["payload"]["content"]
    assert content["lecture_md"].startswith(
        "# 标题\n属于；；保留AM-04、M-021与[Sx]。"
    )
    assert "完成率 = 实际量 ÷ 计划量。" in content["lecture_md"]
    assert content["quote_validation"]["scaffold_leaks"] == 1
    assert content["quote_validation"]["m_id_leaks"] == 2


def test_metadata_and_stable_llm_request_are_filled_exactly() -> None:
    agent, retriever, llm = build_agent(llm_data=SUCCESS)

    first = generate(agent)
    second = generate(agent)

    assert retriever.calls == [
        ("完成率计算", None, ()),
        ("完成率计算", None, ()),
    ]
    assert llm.calls[0]["model"] == "qwen3-235b-a22b"
    assert llm.calls[0]["temperature"] == 0.7
    assert llm.calls[0]["system"] == EXPECTED_PROMPT
    assert llm.calls[0]["json_schema"] == KNOWLEDGE_OUTPUT_SCHEMA
    assert llm.calls[0]["user"] == llm.calls[1]["user"]
    expected_user = {
        "chunks": [
            {
                "applicable_processes": ["YCL"],
                "body": "[S1] 完成率 = 实际量 ÷ 计划量。",
                "chunk_id": "KB-003",
                "difficulty": "basic",
                "knowledge_point": "完成率计算",
                "learning_goal": "能够正确计算完成率",
                "prerequisites": [],
            }
        ],
        "knowledge_point": "完成率计算",
        "knowledge_point_match": True,
        "learning_report_summary": "混淆计划量与实际量",
        "semantic_claim_plan": [],
        "student_profile": PLANNER_PROFILE,
    }
    assert llm.calls[0]["user"] == json.dumps(
        expected_user,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert first["student_profile_ref"] == "planner_new"
    assert first["model"] == "qwen3-235b-a22b"
    assert first["latency_ms"] >= 87
    assert first["token_usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }
    assert first["timestamp"] == FIXED_NOW.isoformat()
    assert first["payload"]["content"]["retrieved_chunk_ids"] == ["KB-003"]
    assert first["payload"]["content"]["knowledge_point_match"] is True
    assert first["payload"]["content"]["knowledge_point_match_basis"] == {
        "matched_knowledge_points": ["完成率计算"],
        "unmatched_knowledge_points": [],
    }
    assert second["payload"]["content"]["coverage"] == ["完成率计算"]


def test_chunk_without_teaching_facts_matches_atomic_contract_fingerprints() -> None:
    agent, _, llm = build_agent(llm_data=SUCCESS)

    draft = generate(agent)
    normalized = deepcopy(draft)
    normalized.pop("latency_ms")
    canonical_message = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert sha256(llm.calls[0]["user"].encode("utf-8")).hexdigest() == (
        BASELINE_REQUEST_SHA256
    )
    assert sha256(canonical_message.encode("utf-8")).hexdigest() == (
        BASELINE_MESSAGE_WITHOUT_WALL_LATENCY_SHA256
    )


def test_teaching_facts_are_held_out_without_renumbering_visible_sentences() -> None:
    chunk = make_chunk(
        "KB-996",
        body="无数值概念句。\n\n" + TEACHING_FACT_1 + "\n\n保留原编号的方法句。\n\n" + TEACHING_FACT_2,
        sentences=(
            "无数值概念句。",
            TEACHING_FACT_1,
            "保留原编号的方法句。",
            TEACHING_FACT_2,
        ),
        teaching_fact_card=make_teaching_fact_card(),
    )
    data = deepcopy(SUCCESS)
    data["claims"][0].update(chunk_id="KB-996", sentence_ref=[3])
    agent, _, llm = build_agent(llm_data=data, chunks=(chunk,))

    generate(agent)
    user = json.loads(llm.calls[0]["user"])

    assert user["chunks"][0]["body"] == (
        "[S1] 无数值概念句。\n[S3] 保留原编号的方法句。"
    )
    serialized_user = llm.calls[0]["user"]
    assert TEACHING_FACT_1 not in serialized_user
    assert TEACHING_FACT_2 not in serialized_user
    assert "异常识别口径卡" not in serialized_user
    assert "项目业务资产" not in serialized_user


def test_llm_claim_cannot_use_a_held_out_sentence_reference() -> None:
    chunk = make_chunk(
        "KB-996",
        body="无数值概念句。\n\n" + TEACHING_FACT_1,
        sentences=("无数值概念句。", TEACHING_FACT_1),
        teaching_fact_card=make_teaching_fact_card(
            facts=(("TF-GENERIC-001", TEACHING_FACT_1),)
        ),
    )
    data = deepcopy(SUCCESS)
    data["claims"][0].update(
        text="LLM越权引用了隐藏句。",
        chunk_id="KB-996",
        sentence_ref=[2],
    )
    agent, _, _ = build_agent(llm_data=data, chunks=(chunk,))

    draft = generate(agent)

    assert draft["claims"][0] == {"text": "无数值概念句。", "kind": "fact"}
    assert all(
        claim["text"] != "LLM越权引用了隐藏句。"
        for claim in draft["claims"]
    )
    assert all(
        evidence["supports_claim"] != "LLM越权引用了隐藏句。"
        for evidence in draft["evidence"]
    )
    assert draft["payload"]["content"]["quote_validation"] == {
        "checked": 1,
        "passed": 0,
        "failed": 1,
        "failures": [
            {
                "claim_text": "LLM越权引用了隐藏句。",
                "sentence_ref": [2],
                "reason": "invalid_sentence_ref",
            }
        ],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }


def test_success_appends_one_deterministic_card_with_four_equal_values() -> None:
    chunk = make_chunk(
        "KB-996",
        body="无数值概念句。\n\n" + TEACHING_FACT_1 + "\n\n" + TEACHING_FACT_2,
        sentences=("无数值概念句。", TEACHING_FACT_1, TEACHING_FACT_2),
        teaching_fact_card=make_teaching_fact_card(),
    )
    data = deepcopy(SUCCESS)
    data["claims"][0].update(chunk_id="KB-996", sentence_ref=[1])
    agent, _, _ = build_agent(llm_data=data, chunks=(chunk,))

    draft = generate(agent)
    content = draft["payload"]["content"]

    assert content["lecture_md"] == (
        SUCCESS["lecture_md"]
        + "\n\n## 依据要点\n\n无数值概念句。"
        + "\n\n## 本项目异常识别口径卡\n\n"
        + TEACHING_FACT_1
        + "\n\n"
        + TEACHING_FACT_2
    )
    assert content["lecture_md"].count("## 本项目异常识别口径卡") == 1
    assert draft["claims"][1:] == [
        {"text": TEACHING_FACT_1, "kind": "fact"},
        {"text": TEACHING_FACT_2, "kind": "fact"},
    ]
    assert draft["evidence"][1:] == [
        {
            "kind": "kb_chunk",
            "ref": "KB-996",
            "quote": TEACHING_FACT_1,
            "supports_claim": TEACHING_FACT_1,
        },
        {
            "kind": "kb_chunk",
            "ref": "KB-996",
            "quote": TEACHING_FACT_2,
            "supports_claim": TEACHING_FACT_2,
        },
    ]
    for claim, evidence in zip(draft["claims"][1:], draft["evidence"][1:]):
        assert claim["text"] is evidence["quote"]
        assert claim["text"] is evidence["supports_claim"]
    assert content["teaching_fact_cards"] == [
        {
            "chunk_id": "KB-996",
            "card_id": "GENERIC-RANGE-CARD",
            "card_version": "1.0.0",
            "fact_ids": ["TF-GENERIC-001", "TF-GENERIC-002"],
        }
    ]
    assert content["quote_validation"] == {
        "checked": 1,
        "passed": 1,
        "failed": 0,
        "failures": [],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }


def test_refusal_does_not_inject_teaching_fact_card() -> None:
    chunk = make_chunk(
        "KB-996",
        body="无数值概念句。\n\n" + TEACHING_FACT_1,
        sentences=("无数值概念句。", TEACHING_FACT_1),
        teaching_fact_card=make_teaching_fact_card(
            facts=(("TF-GENERIC-001", TEACHING_FACT_1),)
        ),
    )
    agent, _, _ = build_agent(llm_data=REFUSAL, chunks=(chunk,))

    draft = generate(agent)
    content = draft["payload"]["content"]

    assert content["lecture_md"] is None
    assert "teaching_fact_cards" not in content
    assert draft["claims"] == []
    assert draft["evidence"] == []


def test_multiple_generic_cards_follow_retrieval_then_fact_order() -> None:
    first_fact = "第一项项目口径为10%。"
    second_fact = "第二项项目口径为20%。"
    first = make_chunk(
        "KB-991",
        body="第一项方法。\n\n" + first_fact,
        sentences=("第一项方法。", first_fact),
        teaching_fact_card=make_teaching_fact_card(
            card_id="GENERIC-CARD-1",
            title="第一张口径卡",
            facts=(("TF-991-001", first_fact),),
        ),
    )
    second = make_chunk(
        "KB-992",
        body="第二项方法。\n\n" + second_fact,
        sentences=("第二项方法。", second_fact),
        teaching_fact_card=make_teaching_fact_card(
            card_id="GENERIC-CARD-2",
            title="第二张口径卡",
            facts=(("TF-992-001", second_fact),),
        ),
    )
    data = deepcopy(SUCCESS)
    data["claims"][0].update(chunk_id="KB-991", sentence_ref=[1])
    agent, _, _ = build_agent(llm_data=data, chunks=(second, first))

    draft = generate(agent)

    lecture = draft["payload"]["content"]["lecture_md"]
    assert lecture.index("## 第二张口径卡") < lecture.index("## 第一张口径卡")
    assert [claim["text"] for claim in draft["claims"][-2:]] == [
        second_fact,
        first_fact,
    ]
    assert [
        item["chunk_id"]
        for item in draft["payload"]["content"]["teaching_fact_cards"]
    ] == ["KB-992", "KB-991"]


def test_real_kb006_basic_request_holds_out_all_frozen_and_example_values() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    kb006 = chunks["KB-006"]
    data = {
        "lecture_md": "# 异常识别标准\n先观察偏低信号是否持续。",
        "claims": [
            {
                "text": "识别异常需要结合持续性。",
                "kind": "fact",
                "chunk_id": "KB-006",
                "sentence_ref": [1],
            }
        ],
        "coverage": ["异常识别标准"],
    }
    agent, _, llm = build_agent(llm_data=data, chunks=(kb006,))

    draft = agent.generate(
        knowledge_point="异常识别标准",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="需要掌握异常识别口径",
        difficulty="basic",
    )

    assert kb006.teaching_fact_card is not None
    for held_value in ("88%", "103%", "62.36%", "90.61%"):
        assert held_value not in llm.calls[0]["user"]
    assert draft["payload"]["content"]["teaching_fact_cards"][0]["chunk_id"] == (
        "KB-006"
    )


@pytest.mark.parametrize(
    ("knowledge_point", "profile", "summary", "keywords"),
    (
        ("", PLANNER_PROFILE, "摘要", ()),
        ("完成率计算", {**PLANNER_PROFILE, "profile_id": "unknown"}, "摘要", ()),
        ("完成率计算", PLANNER_PROFILE, "", ()),
        ("完成率计算", PLANNER_PROFILE, "摘要", (1,)),
    ),
)
def test_invalid_inputs_fail_before_retrieval_and_llm(
    knowledge_point: str,
    profile: dict[str, Any],
    summary: str,
    keywords: tuple[Any, ...],
) -> None:
    agent, retriever, llm = build_agent(llm_data=SUCCESS)

    with pytest.raises(ValueError):
        agent.generate(
            knowledge_point=knowledge_point,
            student_profile=profile,
            learning_report_summary=summary,
            keywords=keywords,
        )

    assert retriever.calls == []
    assert llm.calls == []


def refusal_draft(
    trace_id: str = "trace-refusal",
    *,
    reason: str = "切片不足",
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "agent": "knowledge",
        "role": "produce",
        "payload": {
            "type": "lecture_note",
            "content": {
                "event": "knowledge_refused",
                "lecture_md": None,
                "knowledge_point": "族外知识点",
                "retrieved_chunk_ids": ["KB-900"],
                "refuse_reason": reason,
                "refusal_origin": "llm",
                "llm_latency_ms": 87,
            },
        },
        "claims": [],
        "evidence": [],
        "student_profile_ref": "planner_new",
        "timestamp": FIXED_NOW.isoformat(),
        "model": "qwen3-235b-a22b",
        "latency_ms": 87,
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }


def test_llm_refusal_has_no_claims_and_records_origin(tmp_path: Path) -> None:
    agent, _, _ = build_agent(llm_data=REFUSAL, chunks=(UNRELATED,))

    draft = agent.generate(
        knowledge_point="族外知识点",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="摘要",
    )

    content = draft["payload"]["content"]
    assert content["event"] == "knowledge_refused"
    assert content["refusal_origin"] == "llm"
    assert content["lecture_md"] is None
    assert content["refuse_reason"]
    assert content["knowledge_point_match"] is False
    assert content["knowledge_point_match_basis"] == {
        "matched_knowledge_points": [],
        "unmatched_knowledge_points": ["三道工序与传导关系"],
    }
    assert draft["claims"] == []
    assert draft["evidence"] == []
    assert MessageBus(tmp_path / "traces").send(draft).accepted


def test_r_f_rejects_llm_success_when_knowledge_point_match_is_false(
    tmp_path: Path,
) -> None:
    agent, _, _ = build_agent(llm_data=SUCCESS, chunks=(UNRELATED,))

    draft = agent.generate(
        knowledge_point="族外知识点",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="摘要",
    )

    result = MessageBus(tmp_path / "traces").send(draft)

    assert draft["payload"]["content"]["event"] == "product_ready"
    assert draft["payload"]["content"]["knowledge_point_match"] is False
    assert not result.accepted
    assert any("R-F" in error and "knowledge_point_match" in error for error in result.errors)
    persisted = json.loads(
        (tmp_path / "traces" / f"{draft['trace_id']}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert persisted["rejected_by_bus"] is True
    assert persisted["payload"]["content"]["knowledge_point_match_basis"] == {
        "matched_knowledge_points": [],
        "unmatched_knowledge_points": ["三道工序与传导关系"],
    }


def test_r_f_also_rejects_llm_success_when_retrieval_is_empty(
    tmp_path: Path,
) -> None:
    agent, _, _ = build_agent(llm_data=SUCCESS, chunks=())

    draft = agent.generate(
        knowledge_point="族外知识点",
        student_profile=PLANNER_PROFILE,
        learning_report_summary="摘要",
    )

    result = MessageBus(tmp_path / "traces").send(draft)

    content = draft["payload"]["content"]
    assert content["knowledge_point_match"] is False
    assert content["knowledge_point_match_basis"] == {
        "matched_knowledge_points": [],
        "unmatched_knowledge_points": [],
    }
    assert not result.accepted
    assert any("R-F" in error for error in result.errors)


def test_matching_chunks_still_allow_an_llm_refusal(tmp_path: Path) -> None:
    agent, _, _ = build_agent(llm_data=REFUSAL)

    draft = generate(agent)
    result = MessageBus(tmp_path / "traces").send(draft)

    content = draft["payload"]["content"]
    assert result.accepted, result.errors
    assert content["event"] == "knowledge_refused"
    assert content["refusal_origin"] == "llm"
    assert content["knowledge_point_match"] is True
    assert content["refuse_reason"] == "切片不足"


def test_r_e_rejects_claims_on_knowledge_refusal(tmp_path: Path) -> None:
    draft = refusal_draft()
    draft["claims"] = [{"text": "不应出现", "kind": "speculation"}]

    result = MessageBus(tmp_path / "traces").send(draft)

    assert not result.accepted
    assert any("R-E" in error and "$.claims" in error for error in result.errors)


def test_r_e_rejects_blank_reason_at_the_exact_content_path(tmp_path: Path) -> None:
    draft = refusal_draft(reason="   ")

    result = MessageBus(tmp_path / "traces").send(draft)

    assert not result.accepted
    assert any(
        "R-E" in error and "$.payload.content.refuse_reason" in error
        for error in result.errors
    )


def test_t03_accepts_product_ready_but_refusal_stays_in_s2(tmp_path: Path) -> None:
    trace_id = "trace-t03-p3"
    bus = MessageBus(tmp_path / "traces")
    engine = OrchestratorEngine(bus, trace_id, "planner_new")
    stubs = build_stubs(trace_id)
    assert engine.send(profile_loaded_draft(trace_id)).transitioned
    assert engine.send(stubs.diagnosis.profile_assessment()).transitioned
    assert engine.state is State.S2_KNOWLEDGE

    refused = engine.send(refusal_draft(trace_id))

    assert refused.bus_result.accepted
    assert not refused.transitioned
    assert refused.reason == "no_matching_transition"
    assert engine.state is State.S2_KNOWLEDGE
    product = engine.send(stubs.knowledge.lecture())
    assert product.transitioned
    assert product.transition is not None
    assert product.transition.transition_id == "T03"
    assert engine.state is State.S5_REVIEW


def test_runtime_factory_is_real_and_stub_fixture_remains_unchanged() -> None:
    assert hasattr(runtime, "build_knowledge_agent")

    agent = runtime.build_knowledge_agent("trace-runtime-p3")

    assert isinstance(agent, KnowledgeAgent)
    assert isinstance(build_stubs("trace-stub-p3").knowledge, KnowledgeStub)


@pytest.mark.parametrize("chunk_id", tuple(CANONICAL_QUOTES))
def test_canonical_sentence_anchor_uses_current_loader_sentence(chunk_id: str) -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    current_chunk = chunks[chunk_id]
    data = {
        "lecture_md": f"# {current_chunk.knowledge_point}\n句子锚点测试",
        "claims": [
            {
                "text": f"{current_chunk.knowledge_point}采用既定口径。",
                "kind": "fact",
                "chunk_id": chunk_id,
                "sentence_ref": [1],
            }
        ],
        "coverage": [current_chunk.knowledge_point],
    }
    agent, _, _ = build_agent(llm_data=data, chunks=(current_chunk,))

    draft = agent.generate(
        knowledge_point=current_chunk.knowledge_point,
        student_profile=PLANNER_PROFILE,
        learning_report_summary="当前句子锚点确定性测试",
    )

    atom_text = KnowledgeAgent._public_claim_text(current_chunk.sentences[0])
    assert draft["claims"][0] == {"text": atom_text, "kind": "fact"}
    expected_evidence = [
        {
            "kind": "kb_chunk",
            "ref": chunk_id,
            "quote": current_chunk.sentences[0],
            "supports_claim": atom_text,
        }
    ]
    expected_evidence.extend(
        {
            "kind": "kb_chunk",
            "ref": invariant.evidence_ref,
            "quote": invariant.evidence_quote,
            "supports_claim": invariant.canonical_claim,
        }
        for invariant in active_domain_config().semantic_invariants
        if invariant.evidence_ref == chunk_id
        and current_chunk.knowledge_point in invariant.knowledge_points
    )
    if current_chunk.teaching_fact_card is not None:
        expected_evidence.extend(
            {
                "kind": "kb_chunk",
                "ref": chunk_id,
                "quote": fact.text,
                "supports_claim": fact.text,
            }
            for fact in current_chunk.teaching_fact_card.facts
        )
    assert draft["evidence"] == expected_evidence
    assert draft["payload"]["content"]["quote_validation"] == {
        "checked": 1,
        "passed": 1,
        "failed": 0,
        "failures": [],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }


@pytest.mark.live
def test_live_knowledge_matrix_and_refusal() -> None:
    chunks = require_valid_chunks(CHUNK_DIR)
    retriever = BM25Retriever(chunks)
    bus = MessageBus(ROOT / "traces")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    summaries: list[dict[str, Any]] = []
    profile_lectures: dict[str, str] = {}
    quote_totals = {
        "checked": 0,
        "passed": 0,
        "failed": 0,
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }

    for chunk_index, chunk in enumerate(chunks, start=1):
        for profile_index, profile in enumerate(PROFILES, start=1):
            trace_id = f"p3-live-{run_id}-{chunk_index:02d}-{profile_index}"
            agent = KnowledgeAgent(trace_id=trace_id, retriever=retriever)
            draft = agent.generate(
                knowledge_point=chunk.knowledge_point,
                student_profile=profile,
                learning_report_summary=f"围绕{chunk.knowledge_point}进行岗位化微课学习",
            )
            bus_result = bus.send(draft)
            assert bus_result.accepted, bus_result.errors
            message = bus_result.message
            content = message["payload"]["content"]
            assert content["event"] == "product_ready"
            assert message["claims"]
            validation = content["quote_validation"]
            assert validation["checked"] > 0
            assert validation["failed"] == 0
            assert validation["failures"] == []
            assert validation["passed"] == validation["checked"]
            assert isinstance(validation["scaffold_leaks"], int)
            assert validation["scaffold_leaks"] >= 0
            assert validation["m_id_leaks"] == 0
            assert re.search(r"\[S\d+\]", content["lecture_md"]) is None
            assert re.search(
                r"(?<![A-Za-z0-9])M-\d{2}(?!\d)", content["lecture_md"]
            ) is None
            assert content["knowledge_point_match"] is True
            fact_claims = [
                claim for claim in message["claims"] if claim["kind"] == "fact"
            ]
            assert fact_claims
            chunk_by_id = {item.chunk_id: item for item in chunks}
            for claim in fact_claims:
                matches = [
                    evidence
                    for evidence in message["evidence"]
                    if evidence.get("kind") == "kb_chunk"
                    and evidence.get("supports_claim") == claim["text"]
                ]
                assert matches
                for evidence in matches:
                    assert evidence["ref"] in chunk_by_id
                    assert evidence["quote"] in chunk_by_id[evidence["ref"]].sentences
            for key in quote_totals:
                quote_totals[key] += validation[key]
            if chunk.knowledge_point == "完成率计算":
                profile_lectures[str(profile["profile_id"])] = content["lecture_md"]
            summaries.append(
                {
                    "trace_id": trace_id,
                    "knowledge_point": chunk.knowledge_point,
                    "profile_id": profile["profile_id"],
                    "claims": len(message["claims"]),
                    "facts": len(fact_claims),
                    "quote_validation": validation,
                    "model": message["model"],
                    "latency_ms": message["latency_ms"],
                    "token_usage": message["token_usage"],
                }
            )

    refusal_trace_id = f"p3-live-{run_id}-refusal"
    refusal_agent = KnowledgeAgent(
        trace_id=refusal_trace_id,
        retriever=StaticRetriever(
            (next(chunk for chunk in chunks if chunk.chunk_id == "KB-003"),)
        ),
    )
    refusal_result = bus.send(
        refusal_agent.generate(
            knowledge_point="焊接电流标准",
            student_profile=PLANNER_PROFILE,
            learning_report_summary="询问当前知识库覆盖范围之外的焊接参数",
        )
    )
    assert refusal_result.accepted, refusal_result.errors
    refusal_message = refusal_result.message
    refusal_content = refusal_message["payload"]["content"]
    assert refusal_content["event"] == "knowledge_refused"
    assert refusal_content["refusal_origin"] == "llm"
    assert refusal_content["refuse_reason"]
    assert refusal_message["claims"] == []
    assert refusal_message["evidence"] == []

    assert len(summaries) == len(chunks) * len(PROFILES)
    assert set(profile_lectures) == {
        "planner_new",
        "craft_engineer",
        "line_leader",
    }
    print(
        "P3_LIVE_EVIDENCE="
        + json.dumps(
            {
                "results": summaries,
                "quote_totals": quote_totals,
                "refusal_message": refusal_message,
                "profile_lectures": profile_lectures,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
