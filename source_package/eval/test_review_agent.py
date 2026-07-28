from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.validate_message import validate_message
from agents.verification_agent import _render_claims
from orchestrator import runtime
from orchestrator.bus import MessageBus
from orchestrator.llm import LLMResult, TokenUsage


TIMESTAMP = "2026-07-15T12:00:00+08:00"
ROOT = Path(__file__).resolve().parents[1]
R02_PROMPT_PATH = ROOT / "agents" / "prompts" / "review_r02.md"
R03_PROMPT_PATH = ROOT / "agents" / "prompts" / "review_r03.md"
EXPECTED_R02_PROMPT = (
    '你是证据审核员。判断结论是否被引文支撑。只输出JSON{"supported":bool,'
    '"reason":"一句话"}。规则：引文必须直接陈述或必然蕴含结论；'
    "引文只是相关但不足以推出结论时判false；不使用引文之外的知识。"
    "[结论]...[引文]...\n"
)
EXPECTED_R03_PROMPT = (
    '你是培训难度语义审核员。只输出JSON{"blind_spots_scaffolded":bool,'
    '"required_skills":["技能"],"reason":"一句话"}。输入中的blind_spots已由代码按产物'
    "责任范围过滤，并排除了已学内容；只判断这些相关盲区是否在产物中有铺垫，并列出产物实际"
    "要求的前置技能。不得判断技能是否已在strengths或已学内容中，也不得判断显式难度档；"
    "这些确定性判断由代码完成。没有相关盲区时blind_spots_scaffolded必须为true；没有额外"
    "前置技能时required_skills必须为空数组。"
    "[结构化权威]...[学情报告]...[产物摘要]...\n"
)


def test_readability_axis_rejects_internal_protocol_leaks() -> None:
    product = {
        "msg_id": "lecture-readable-1",
        "payload": {
            "type": "lecture_note",
            "content": {"lecture_md": "请查看内部 trace_id 后再继续学习。"},
        },
    }

    hits, checks = _review_module()._readability_reviews(product)

    assert checks == 2
    assert [hit["rule_id"] for hit in hits] == ["R-06"]


def test_readability_axis_keeps_normal_structured_content() -> None:
    product = {
        "msg_id": "lecture-readable-2",
        "payload": {
            "type": "lecture_note",
            "content": {
                "lecture_md": "# 完成率\n\n先确认计划量，再核对实际量，最后计算完成率。"
            },
        },
    }

    hits, checks = _review_module()._readability_reviews(product)

    assert hits == ()
    assert checks == 2
LEARNING_REPORT = {
    "msg_id": "trace-soft-report-001",
    "trace_id": "trace-soft",
    "payload": {
        "type": "profile_assessment",
        "content": {
            "profile_id": "planner_new",
            "blind_spots": ["三道工序与传导关系", "计划量与实际量口径"],
            "hit_misconceptions": ["M-01"],
            "difficulty": "basic",
            "pretest_score": {"correct": 2, "total": 5, "rate": 0.4},
        },
    },
}
STUDENT_PROFILE = {
    "profile_id": "planner_new",
    "title": "新入职生产计划员",
    "background": "计算机/信息类背景校招生，会SQL和数据分析工具，不懂船舶工序与口径",
    "strengths": ["SQL基础", "数据分析工具"],
    "gaps_prior": ["三道工序与传导关系", "计划量与实际量口径", "传导时滞分析"],
    "difficulty_start": "basic",
    "lecture_style": "重讲工序与口径、少讲SQL",
}


class SequentialLLM:
    def __init__(self, outputs: list[LLMResult]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("unexpected LLM call")
        return self.outputs.pop(0)


def _llm_result(
    data: dict[str, Any],
    *,
    latency_ms: int = 11,
    prompt_tokens: int = 10,
    completion_tokens: int = 2,
) -> LLMResult:
    return LLMResult(
        data=data,
        model="qwen3-32b",
        latency_ms=latency_ms,
        token_usage=TokenUsage(
            prompt_tokens,
            completion_tokens,
            prompt_tokens + completion_tokens,
        ),
        attempts=1,
    )


def _review_module() -> Any:
    try:
        return importlib.import_module("agents.review_agent")
    except ModuleNotFoundError:
        pytest.fail("agents.review_agent is not implemented")


def _query_quote(
    sql: str,
    rows: list[dict[str, Any]],
    *,
    status: str = "completed",
) -> str:
    return json.dumps(
        {
            "generated_sql": sql,
            "executed_sql": f"{sql} LIMIT 200",
            "status": status,
            "row_count": len(rows),
            "rows": rows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sql_product(
    *,
    question: str,
    family: str,
    sql: str,
    rows: list[dict[str, Any]],
    claim_rows: list[dict[str, Any]] | None = None,
    evidence_ref: str = "sql-run-1",
    query_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    claim_texts = _render_claims(
        family, question, rows if claim_rows is None else claim_rows
    )
    quote = _query_quote(sql, rows)
    product = {
        "msg_id": "trace-review-001",
        "trace_id": "trace-review",
        "step": 1,
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {
                "event": "query_completed",
                "question": question,
                "family": family,
                "query_id": evidence_ref,
                "generated_sql": sql,
                "executed_sql": f"{sql} LIMIT 200",
                "columns": list(rows[0]) if rows else [],
                "rows": rows,
                "row_count": len(rows),
            },
        },
        "evidence": [
            {
                "kind": "sql_query",
                "ref": evidence_ref,
                "quote": quote,
                "supports_claim": claim,
            }
            for claim in claim_texts
        ],
        "claims": [
            {"text": claim, "kind": "data_conclusion"} for claim in claim_texts
        ],
        "timestamp": TIMESTAMP,
    }
    if query_authority is not None:
        product["payload"]["content"]["query_authority"] = deepcopy(
            query_authority
        )
    assert validate_message(product) == []
    return product


def _document_product(
    body: str,
    *,
    payload_type: str = "lecture_note",
    body_field: str = "lecture_md",
    claim_text: str | None = None,
) -> dict[str, Any]:
    claims = [] if claim_text is None else [{"text": claim_text, "kind": "fact"}]
    evidence = (
        []
        if claim_text is None
        else [
            {
                "kind": "kb_chunk",
                "ref": "KB-003",
                "quote": "完成率 = 实际量 ÷ 计划量。",
                "supports_claim": claim_text,
            }
        ]
    )
    product = {
        "msg_id": "trace-review-002",
        "trace_id": "trace-review",
        "step": 2,
        "agent": "knowledge" if payload_type == "lecture_note" else "task",
        "role": "produce",
        "payload": {
            "type": payload_type,
            "content": {
                "event": "product_ready",
                body_field: body,
                "difficulty": "basic",
            },
        },
        "evidence": evidence,
        "claims": claims,
        "timestamp": TIMESTAMP,
    }
    assert validate_message(product) == []
    return product


def _hard_rule_ids(product: dict[str, Any]) -> list[str]:
    return [
        hit["rule_id"] for hit in _review_module().evaluate_hard_rules(product)
    ]


def test_r04_rejects_failed_lecture_quote_validation() -> None:
    product = _document_product("# 岗位微课\n\n讲义正文。")
    product["payload"]["content"]["quote_validation"] = {
        "checked": 1,
        "passed": 0,
        "failed": 1,
        "failures": [
            {
                "claim_text": "无效引用不应进入成品。",
                "sentence_ref": [4, 18],
                "reason": "invalid_sentence_ref",
            }
        ],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }

    assert _hard_rule_ids(product) == ["R-04"]


def test_r04_preserves_a_lecture_with_valid_quote_validation() -> None:
    product = _document_product("# 岗位微课\n\n讲义正文。")
    product["payload"]["content"]["quote_validation"] = {
        "checked": 1,
        "passed": 1,
        "failed": 0,
        "failures": [],
        "scaffold_leaks": 0,
        "m_id_leaks": 0,
    }

    assert "R-04" not in _hard_rule_ids(product)


def _task_r04_product(
    contextualized_stem: str,
    *,
    standard_stem: str,
    payload_type: str,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "event": "product_ready",
        "template_id": "T-03",
        "knowledge_point": "三道工序与传导关系",
        "difficulty": "basic",
        "question": contextualized_stem,
        "family": "Q6",
        "standard_stem": standard_stem,
        "contextualized_stem": contextualized_stem,
        "guide_intro": "结合岗位职责完成查询。",
        "contextualize_fallback": False,
        "contextualize_fallback_reason": None,
        "llm_latency_ms": 2500,
    }
    if payload_type == "practice_guide":
        content["guide_md"] = contextualized_stem
    elif payload_type == "quiz_set":
        content["questions"] = [{"id": "T-03", "prompt": contextualized_stem}]
    else:
        raise AssertionError(payload_type)
    product = {
        "msg_id": "trace-review-task-001",
        "trace_id": "trace-review-task",
        "step": 1,
        "agent": "task",
        "role": "produce",
        "payload": {"type": payload_type, "content": content},
        "evidence": [],
        "claims": [],
        "timestamp": TIMESTAMP,
    }
    assert validate_message(product) == []
    return product


def _plan_used_as_actual_product() -> dict[str, Any]:
    question = "H2601五月预处理实际完成了多少"
    sql = (
        "SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress "
        "WHERE ship_no='H2601' AND process_code='YCL' "
        "AND period_date>='2025-05-01' AND period_date<'2025-06-01'"
    )
    return _sql_product(
        question=question,
        family="Q1",
        sql=sql,
        rows=[{"plan_qty": "1855.06"}],
        claim_rows=[{"actual_qty": "1855.06"}],
    )


def _tampered_numeric_product() -> dict[str, Any]:
    question = "H2601五月预处理完成率"
    sql = (
        "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
        "FROM fact_production_progress WHERE ship_no='H2601' "
        "AND process_code='YCL' AND period_date>='2025-05-01' "
        "AND period_date<'2025-06-01'"
    )
    return _sql_product(
        question=question,
        family="Q3",
        sql=sql,
        rows=[{"complete_rate": "0.6236"}],
        claim_rows=[{"complete_rate": "0.7000"}],
    )


def _workshop_question_with_ship_level_sql() -> dict[str, Any]:
    question = "五月预处理各责任单元完成率"
    sql = (
        "SELECT ship_no, ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
        "FROM fact_production_progress WHERE process_code='YCL' "
        "AND period_date>='2025-05-01' AND period_date<'2025-06-01' "
        "GROUP BY ship_no ORDER BY complete_rate ASC"
    )
    return _sql_product(
        question=question,
        family="Q5",
        sql=sql,
        rows=[{"ship_no": "H2601", "complete_rate": "0.6236"}],
    )


@pytest.mark.parametrize(
    "product_factory",
    [
        _plan_used_as_actual_product,
        _tampered_numeric_product,
        _workshop_question_with_ship_level_sql,
    ],
    ids=["plan-as-actual", "tampered-number", "workshop-as-ship"],
)
def test_r01_blocks_three_approved_attack_classes(product_factory: Any) -> None:
    assert _hard_rule_ids(product_factory()) == ["R-01"]


@pytest.mark.parametrize(
    ("question", "alias", "rows"),
    [
        (
            "H2601五月预处理完成率",
            "complete_rate",
            [{"complete_rate": "0.6236"}],
        ),
        (
            "H2601五月预处理偏差率",
            "deviation_rate",
            [{"deviation_rate": "-0.3764"}],
        ),
    ],
    ids=["complete-rate-addition", "deviation-rate-addition"],
)
def test_r01_rejects_wrong_arithmetic_even_when_formula_mentions_required_columns(
    question: str, alias: str, rows: list[dict[str, str]]
) -> None:
    product = _sql_product(
        question=question,
        family="Q3",
        sql=(
            "SELECT ROUND((SUM(actual_qty)+SUM(plan_qty))/SUM(plan_qty),4) "
            f"AS {alias} FROM fact_production_progress WHERE ship_no='H2601' "
            "AND process_code='YCL' AND period_date>='2025-05-01' "
            "AND period_date<'2025-06-01'"
        ),
        rows=rows,
    )

    assert _hard_rule_ids(product) == ["R-01"]


@pytest.mark.parametrize(
    ("question", "alias", "expression", "rows"),
    [
        (
            "H2601五月预处理实际完成了多少",
            "actual_qty",
            "SUM(actual_qty+plan_qty)",
            [{"actual_qty": "3011.93"}],
        ),
        (
            "H2601五月预处理计划量",
            "plan_qty",
            "SUM(plan_qty+actual_qty)",
            [{"plan_qty": "3011.93"}],
        ),
    ],
    ids=["mixed-actual", "mixed-plan"],
)
def test_r01_rejects_quantity_aliases_built_from_mixed_columns(
    question: str,
    alias: str,
    expression: str,
    rows: list[dict[str, str]],
) -> None:
    product = _sql_product(
        question=question,
        family="Q1",
        sql=(
            f"SELECT {expression} AS {alias} FROM fact_production_progress "
            "WHERE ship_no='H2601' AND process_code='YCL' "
            "AND period_date>='2025-05-01' AND period_date<'2025-06-01'"
        ),
        rows=rows,
    )

    assert _hard_rule_ids(product) == ["R-01"]


@pytest.mark.parametrize(
    "product",
    [
        _sql_product(
            question="H2601五月预处理实际完成了多少",
            family="Q1",
            sql=(
                "SELECT SUM(actual_qty) AS actual_qty FROM fact_production_progress "
                "WHERE ship_no='H2601' AND process_code='YCL' "
                "AND period_date>='2025-05-01' AND period_date<'2025-06-01'"
            ),
            rows=[{"actual_qty": "1156.87"}],
        ),
        _sql_product(
            question="H2601五月预处理计划量与实际量",
            family="Q2",
            sql=(
                "SELECT SUM(plan_qty) AS plan_qty, "
                "SUM(actual_qty) AS actual_qty FROM fact_production_progress "
                "WHERE ship_no='H2601' AND process_code='YCL' "
                "AND period_date>='2025-05-01' AND period_date<'2025-06-01'"
            ),
            rows=[{"plan_qty": "1855.06", "actual_qty": "1156.87"}],
        ),
        _sql_product(
            question="H2601五月预处理完成率",
            family="Q3",
            sql=(
                "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
                "FROM fact_production_progress WHERE ship_no='H2601' "
                "AND process_code='YCL' AND period_date>='2025-05-01' "
                "AND period_date<'2025-06-01'"
            ),
            rows=[{"complete_rate": "0.6236"}],
        ),
        _sql_product(
            question="H2601五月预处理偏差率",
            family="Q3",
            sql=(
                "SELECT ROUND((SUM(actual_qty)-SUM(plan_qty))/SUM(plan_qty),4) "
                "AS deviation_rate FROM fact_production_progress "
                "WHERE ship_no='H2601' AND process_code='YCL' "
                "AND period_date>='2025-05-01' AND period_date<'2025-06-01'"
            ),
            rows=[{"deviation_rate": "-0.3764"}],
        ),
        _sql_product(
            question="五月预处理各责任单元完成率",
            family="Q7",
            sql=(
                "SELECT workshop_code, "
                "ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
                "FROM fact_production_progress WHERE process_code='YCL' "
                "AND period_date>='2025-05-01' AND period_date<'2025-06-01' "
                "GROUP BY workshop_code ORDER BY complete_rate ASC"
            ),
            rows=[
                {"workshop_code": "WSA", "complete_rate": "0.9044"},
                {"workshop_code": "WSB", "complete_rate": "0.9045"},
            ],
        ),
    ],
    ids=["actual", "plan-and-actual", "ratio", "deviation", "workshop"],
)
def test_r01_accepts_three_correct_products(product: dict[str, Any]) -> None:
    assert "R-01" not in _hard_rule_ids(product)


def test_r01_allows_q6_cross_process_comparison_without_single_process_filter() -> None:
    product = _sql_product(
        question="从H2601七月安装托盘完成率偏低出发，构造工序×月份对照表定位源头",
        family="Q6",
        sql=(
            "SELECT process_code, DATE_FORMAT(period_date,'%Y-%m') AS month_label, "
            "ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
            "FROM fact_production_progress WHERE ship_no='H2601' "
            "AND period_date>='2025-04-01' AND period_date<'2025-08-01' "
            "GROUP BY process_code, month_label ORDER BY process_code, month_label"
        ),
        rows=[
            {
                "process_code": "YCL",
                "month_label": "2025-05",
                "complete_rate": "0.6236",
            },
            {
                "process_code": "ZZTP",
                "month_label": "2025-06",
                "complete_rate": "0.7545",
            },
            {
                "process_code": "AZTP",
                "month_label": "2025-07",
                "complete_rate": "0.8501",
            },
        ],
    )

    assert "R-01" not in _hard_rule_ids(product)


def test_r01_uses_structured_task_metrics_instead_of_incidental_question_words() -> None:
    authority = {
        "source": "task_template",
        "template_id": "T-01",
        "family": "Q2",
        "standard_stem": "查询H26012025-05YCL的计划量与实际量",
        "output_columns": ["plan_qty", "actual_qty"],
        "metric_columns": ["plan_qty", "actual_qty"],
        "dimension_columns": [],
        "filter_columns": ["period_date", "process_code", "ship_no"],
        "group_by_columns": [],
        "time_column": "period_date",
        "time_values": ["2025-05"],
    }
    product = _sql_product(
        question=(
            "查询H2601在2025-05的YCL计划量与实际量，"
            "为后续完成率计算提供数据支持"
        ),
        family="Q2",
        sql=(
            "SELECT SUM(plan_qty) AS plan_qty, SUM(actual_qty) AS actual_qty "
            "FROM fact_production_progress WHERE ship_no='H2601' "
            "AND process_code='YCL' AND period_date>='2025-05-01' "
            "AND period_date<'2025-06-01'"
        ),
        rows=[{"plan_qty": "1855.06", "actual_qty": "1156.87"}],
        query_authority=authority,
    )

    assert "R-01" not in _hard_rule_ids(product)


def test_r01_ratio_tolerance_is_exactly_one_e_minus_four_after_percent_normalization() -> None:
    product = _sql_product(
        question="H2601五月预处理完成率",
        family="Q3",
        sql=(
            "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
            "FROM fact_production_progress WHERE ship_no='H2601' "
            "AND process_code='YCL' AND period_date>='2025-05-01' "
            "AND period_date<'2025-06-01'"
        ),
        rows=[{"complete_rate": "0.6236"}],
        claim_rows=[{"complete_rate": "0.6237"}],
    )

    assert "R-01" not in _hard_rule_ids(product)


def test_r01_binds_each_claim_value_to_its_labeled_result_row() -> None:
    rows = [
        {"ship_no": "H2601", "complete_rate": "0.6236"},
        {"ship_no": "H2604", "complete_rate": "0.8924"},
    ]
    product = _sql_product(
        question="五月预处理各船完成率排名",
        family="Q5",
        sql=(
            "SELECT ship_no, ROUND(SUM(actual_qty)/SUM(plan_qty),4) "
            "AS complete_rate FROM fact_production_progress "
            "WHERE process_code='YCL' AND period_date>='2025-05-01' "
            "AND period_date<'2025-06-01' GROUP BY ship_no "
            "ORDER BY complete_rate ASC"
        ),
        rows=rows,
        claim_rows=[
            {"ship_no": "H2601", "complete_rate": "0.8924"},
            {"ship_no": "H2604", "complete_rate": "0.6236"},
        ],
    )

    assert _hard_rule_ids(product) == ["R-01"]


def test_r01_uses_protocol_trimmed_supports_claim_when_checking_numbers() -> None:
    product = _tampered_numeric_product()
    claim_text = product["claims"][0]["text"]
    product["evidence"][0]["supports_claim"] = f"  {claim_text}  "

    assert validate_message(product) == []
    assert _hard_rule_ids(product) == ["R-01"]


def test_r04_rejects_unclaimed_deterministic_sentence_and_short_circuits_llm() -> None:
    calls: list[dict[str, Any]] = []
    product = _document_product("完成率为62.36%。")

    verdict = _review_module().ReviewAgent(
        "trace-review", llm_call=lambda **kwargs: calls.append(kwargs)
    ).review(product)

    assert verdict["verdict"]["decision"] == "reject"
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == [
        "R-04"
    ]
    assert calls == []


def _teaching_fact_card_product(*, invented_sentence: str | None = None) -> dict[str, Any]:
    facts = (
        "本项目岗位培训与评测采用的月完成率正常波动区间为88%—103%。",
        (
            "月完成率低于88%时，先作为偏低信号观察，不能仅凭单月数值直接定性；"
            "还须结合持续性或伴随的风险记录。"
        ),
    )
    body = "## 本项目异常识别口径卡\n\n" + "\n\n".join(facts)
    if invented_sentence is not None:
        body += "\n\n" + invented_sentence
    product = _document_product(body)
    product["claims"] = [{"text": fact, "kind": "fact"} for fact in facts]
    product["evidence"] = [
        {
            "kind": "kb_chunk",
            "ref": "KB-006",
            "quote": fact,
            "supports_claim": fact,
        }
        for fact in facts
    ]
    return product


def test_r04_accepts_exact_deterministic_teaching_fact_card_claims() -> None:
    product = _teaching_fact_card_product()

    assert "R-04" not in _hard_rule_ids(product)


def test_r04_still_rejects_an_invented_number_beside_the_teaching_fact_card() -> None:
    product = _teaching_fact_card_product(
        invented_sentence="月完成率低于90%即可直接定性为异常。"
    )

    assert _hard_rule_ids(product) == ["R-04"]


@pytest.mark.parametrize("payload_type", ["practice_guide", "quiz_set"])
def test_r04_task_allows_numbers_whitelisted_by_standard_stem(
    payload_type: str,
) -> None:
    product = _task_r04_product(
        (
            "作为新入职的生产计划员，你需要根据H2601项目2025-05批次的"
            "生产流程，按工序顺序查询三道关键工序的完成率。"
        ),
        standard_stem="按工序顺序查询H26012025-05三道工序完成率",
        payload_type=payload_type,
    )

    assert "R-04" not in _hard_rule_ids(product)


@pytest.mark.parametrize("payload_type", ["practice_guide", "quiz_set"])
@pytest.mark.parametrize(
    "contextualized_stem",
    [
        "作为生产计划员，查询H2601项目2025-08三道工序的完成率。",
        "作为生产计划员，查询H2601项目2025-05三道工序的完成率99.9%。",
    ],
    ids=["changed-month", "invented-percentage"],
)
def test_r04_task_rejects_numbers_absent_from_standard_stem(
    payload_type: str,
    contextualized_stem: str,
) -> None:
    product = _task_r04_product(
        contextualized_stem,
        standard_stem="按工序顺序查询H26012025-05三道工序完成率",
        payload_type=payload_type,
    )

    assert _hard_rule_ids(product) == ["R-04"]


def test_r04_nfkc_whitespace_and_markdown_matching_avoids_false_positive() -> None:
    product = _document_product(
        "完成率为６２．３６％。",
        claim_text="**完成率** 为 62.36%。",
    )

    assert "R-04" not in _hard_rule_ids(product)


def test_r04_accepts_natural_paraphrase_at_character_similarity_threshold() -> None:
    product = _document_product(
        "该月完成率为62.36%，处于异常区间。",
        claim_text="该月完成率为62.36%，属于异常区间。",
    )

    assert "R-04" not in _hard_rule_ids(product)


def test_r04_normalizes_math_symbols_before_similarity_matching() -> None:
    product = _document_product(
        "完成率的基本公式为：**实际量 ÷ 计划量**。",
        claim_text="完成率的计算公式为实际量除以计划量。",
    )

    assert "R-04" not in _hard_rule_ids(product)


def test_r04_accepts_matching_result_with_additional_calculation_detail() -> None:
    product = _document_product(
        "H2601船2025年5月YCL工序的完成率为62.36%。",
        claim_text=(
            "H2601船2025年5月YCL工序按1156.87除以1855.06计算，"
            "完成率为62.36%。"
        ),
    )

    assert "R-04" not in _hard_rule_ids(product)


def test_r04_similarity_does_not_hide_numeric_tampering() -> None:
    product = _document_product(
        "该月完成率为62.36%，处于异常区间。",
        claim_text="该月完成率为82.36%，处于异常区间。",
    )

    assert _hard_rule_ids(product) == ["R-04"]


@pytest.mark.parametrize("marker", ["1. ", "12. "])
def test_r04_ignores_markdown_ordered_list_marker_as_a_number(marker: str) -> None:
    product = _document_product(
        f"{marker}**定位现象**：异常源头是预处理工序。"
    )

    assert "R-04" not in _hard_rule_ids(product)


def test_r04_still_scans_real_number_after_markdown_ordered_list_marker() -> None:
    product = _document_product("1. 完成率为62.36%。")

    assert _hard_rule_ids(product) == ["R-04"]


def test_r04_does_not_ignore_number_outside_line_start_list_marker() -> None:
    product = _document_product("步骤1. 异常源头是预处理工序。")

    assert _hard_rule_ids(product) == ["R-04"]


def test_r04_scans_practice_body_but_excludes_code_headings_and_questions() -> None:
    product = _document_product(
        "# 完成率为62.36%\n"
        "```sql\nSELECT 1 AS 完成率\n```\n"
        "完成率为多少？\n"
        "实际量合计1156.87。",
        payload_type="practice_guide",
        body_field="guide_md",
    )

    hits = _review_module().evaluate_hard_rules(product)

    assert [hit["rule_id"] for hit in hits] == ["R-04"]
    assert "实际量合计1156.87" in hits[0]["reason"]


def test_r04_excludes_example_lead_without_a_complete_judgment() -> None:
    product = _document_product("以H2601船2025年5月YCL数据为例：")

    assert "R-04" not in _hard_rule_ids(product)


def test_r04_does_not_exclude_example_sentence_with_a_complete_judgment() -> None:
    product = _document_product("例如，H2601船完成率为62.36%。")

    assert _hard_rule_ids(product) == ["R-04"]


def test_plan_actual_tier_bodies_have_no_r04_numeric_conclusions() -> None:
    module = _review_module()
    chunks = {
        chunk.chunk_id: chunk
        for chunk in require_valid_chunks(
            ROOT / "agents" / "knowledge_base" / "chunks"
        )
    }

    for chunk_id in ("KB-002-A", "KB-002-B"):
        sentences = module._sentences(chunks[chunk_id].body)
        assert sentences
        assert all(not any(character.isdigit() for character in sentence) for sentence in sentences)
        product = _document_product(chunks[chunk_id].body)
        product["claims"] = [
            {"text": sentence, "kind": "fact"} for sentence in sentences
        ]

        assert _hard_rule_ids(product) == []


def test_process_propagation_tier_bodies_have_no_r04_numeric_conclusions() -> None:
    module = _review_module()
    chunks = {
        chunk.chunk_id: chunk
        for chunk in require_valid_chunks(
            ROOT / "agents" / "knowledge_base" / "chunks"
        )
    }

    for chunk_id in ("KB-001-A", "KB-001-B"):
        sentences = module._sentences(chunks[chunk_id].body)
        assert sentences
        assert all(
            not any(character.isdigit() for character in sentence)
            for sentence in sentences
        )
        product = _document_product(chunks[chunk_id].body)
        product["claims"] = [
            {"text": sentence, "kind": "fact"} for sentence in sentences
        ]

        assert _hard_rule_ids(product) == []


def test_deviation_risk_tier_bodies_have_no_r04_numeric_conclusions() -> None:
    module = _review_module()
    chunks = {
        chunk.chunk_id: chunk
        for chunk in require_valid_chunks(
            ROOT / "agents" / "knowledge_base" / "chunks"
        )
    }

    for chunk_id in ("KB-004-A", "KB-004-B"):
        sentences = module._sentences(chunks[chunk_id].body)
        assert sentences
        assert all(
            not any(character.isdigit() for character in sentence)
            for sentence in sentences
        )
        product = _document_product(chunks[chunk_id].body)
        product["claims"] = [
            {"text": sentence, "kind": "fact"} for sentence in sentences
        ]

        assert _hard_rule_ids(product) == []


def test_monthly_aggregation_tier_bodies_have_no_r04_numeric_conclusions() -> None:
    module = _review_module()
    chunks = {
        chunk.chunk_id: chunk
        for chunk in require_valid_chunks(
            ROOT / "agents" / "knowledge_base" / "chunks"
        )
    }

    for chunk_id in ("KB-005-A", "KB-005-B"):
        sentences = module._sentences(chunks[chunk_id].body)
        assert sentences
        assert all(
            not any(character.isdigit() for character in sentence)
            for sentence in sentences
        )
        product = _document_product(chunks[chunk_id].body)
        product["claims"] = [
            {"text": sentence, "kind": "fact"} for sentence in sentences
        ]

        assert _hard_rule_ids(product) == []


def test_anomaly_identification_tier_bodies_have_no_r04_numeric_conclusions() -> None:
    module = _review_module()
    chunks = {
        chunk.chunk_id: chunk
        for chunk in require_valid_chunks(
            ROOT / "agents" / "knowledge_base" / "chunks"
        )
    }

    for chunk_id in ("KB-006-A", "KB-006-B"):
        sentences = module._sentences(chunks[chunk_id].body)
        assert sentences
        assert all(
            not any(character.isdigit() for character in sentence)
            for sentence in sentences
        )
        product = _document_product(chunks[chunk_id].body)
        product["claims"] = [
            {"text": sentence, "kind": "fact"} for sentence in sentences
        ]

        assert _hard_rule_ids(product) == []


def test_all_transmission_group_tier_bodies_pass_r04_without_numeric_conclusions() -> None:
    module = _review_module()
    chunks = {
        chunk.chunk_id: chunk
        for chunk in require_valid_chunks(
            ROOT / "agents" / "knowledge_base" / "chunks"
        )
    }

    for chunk_id in (
        "KB-007",
        "KB-007-A",
        "KB-007-B",
        "KB-008",
        "KB-008-A",
        "KB-008-B",
        "KB-009",
        "KB-009-A",
        "KB-009-B",
        "KB-010",
        "KB-010-A",
        "KB-010-B",
    ):
        sentences = module._sentences(chunks[chunk_id].body)
        assert sentences
        product = _document_product(chunks[chunk_id].body)
        product["claims"] = [
            {"text": sentence, "kind": "fact"} for sentence in sentences
        ]

        assert _hard_rule_ids(product) == []


def _review_document(product: dict[str, Any], llm: SequentialLLM) -> dict[str, Any]:
    return _review_module().ReviewAgent(
        "trace-review",
        llm_call=llm,
        knowledge_chunks=_synthetic_knowledge_chunks(product),
    ).review(
        product,
        learning_report=LEARNING_REPORT,
        student_profile=STUDENT_PROFILE,
    )


def test_r04_low_similarity_numeric_compatible_rewrite_is_deferred_to_r02() -> None:
    sentence = "完成率是衡量生产任务完成情况的重要指标。"
    claim = "完成率用于反映计划执行状态。"
    product = _document_product(sentence, claim_text=claim)
    llm = SequentialLLM(
        [
            _llm_result({"supported": True, "reason": "是已申报结论的自然改写"}),
            _llm_result({"supported": True, "reason": "引文支撑结论"}),
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                }
            ),
        ]
    )

    verdict = _review_document(product, llm)

    assert verdict["verdict"]["decision"] == "approve"
    assert json.loads(llm.calls[0]["user"]) == {
        "claim": sentence,
        "quotes": [claim],
    }
    assert verdict["payload"]["content"]["r02_checks"] == 2


def test_r04_preflight_reuses_the_semantic_result_during_persisted_review() -> None:
    sentence = "完成率是衡量生产任务完成情况的重要指标。"
    claim = "完成率用于反映计划执行状态。"
    product = _document_product(sentence, claim_text=claim)
    llm = SequentialLLM(
        [
            _llm_result({"supported": True, "reason": "是已申报结论的自然改写"}),
            _llm_result({"supported": True, "reason": "引文支撑结论"}),
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                }
            ),
        ]
    )
    agent = _review_module().ReviewAgent(
        "trace-review",
        llm_call=llm,
        knowledge_chunks=_synthetic_knowledge_chunks(product),
    )

    assert agent.preflight_r04(product) is None
    verdict = agent.review(
        product,
        learning_report=LEARNING_REPORT,
        student_profile=STUDENT_PROFILE,
    )

    assert verdict["verdict"]["decision"] == "approve"
    assert len(llm.calls) == 3
    assert verdict["payload"]["content"]["r02_checks"] == 2


def test_r04_low_similarity_rewrite_rejected_by_r02_becomes_r04_hit() -> None:
    sentence = "完成率是衡量生产任务完成情况的重要指标。"
    claim = "计划量与实际量必须分别理解。"
    product = _document_product(sentence, claim_text=claim)
    llm = SequentialLLM(
        [_llm_result({"supported": False, "reason": "不是已申报结论的改写"})]
    )

    verdict = _review_document(product, llm)

    assert verdict["verdict"]["decision"] == "reject"
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == ["R-04"]
    assert len(llm.calls) == 1


def test_r04_candidate_numbers_must_be_subset_of_a_claim_before_r02() -> None:
    sentence = "H2601船2025年5月YCL工序完成率为62.36%。"
    claim = (
        "H2601船2025年5月YCL工序按1156.87除以1855.06计算，"
        "完成率等于62.36%。"
    )
    product = _document_product(sentence, claim_text=claim)
    llm = SequentialLLM(
        [
            _llm_result({"supported": True, "reason": "是同一结果的简写"}),
            _llm_result({"supported": True, "reason": "引文支撑结论"}),
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                }
            ),
        ]
    )

    verdict = _review_document(product, llm)

    assert verdict["verdict"]["decision"] == "approve"
    assert json.loads(llm.calls[0]["user"])["quotes"] == [claim]


def test_r04_unknown_candidate_number_is_a_zero_token_hard_hit() -> None:
    product = _document_product(
        "该月完成率为82.36%，处于异常区间。",
        claim_text="该月完成率为62.36%，处于异常区间。",
    )
    calls: list[dict[str, Any]] = []

    verdict = _review_module().ReviewAgent(
        "trace-review", llm_call=lambda **kwargs: calls.append(kwargs)
    ).review(
        product,
        learning_report=LEARNING_REPORT,
        student_profile=STUDENT_PROFILE,
    )

    assert verdict["verdict"]["decision"] == "reject"
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == ["R-04"]
    assert calls == []


def _r05_product(
    *,
    content_rows: list[dict[str, Any]],
    status: str,
    evidence_kind: str = "sql_query",
    evidence_ref: str = "sql-run-r05",
) -> dict[str, Any]:
    sql = "SELECT SUM(actual_qty) AS actual_qty FROM fact_production_progress"
    evidence = {
        "kind": evidence_kind,
        "ref": evidence_ref,
        "quote": (
            "the AST root must be Select"
            if evidence_kind == "review_rule"
            else _query_quote(sql, content_rows, status=status)
        ),
    }
    product = {
        "msg_id": "trace-review-003",
        "trace_id": "trace-review",
        "step": 3,
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {
                "event": "query_completed",
                "question": "实际量",
                "family": "Q1",
                "generated_sql": sql,
                "executed_sql": f"{sql} LIMIT 200",
                "columns": list(content_rows[0]) if content_rows else ["actual_qty"],
                "rows": content_rows,
                "row_count": len(content_rows),
            },
        },
        "evidence": [evidence],
        "claims": [],
        "timestamp": TIMESTAMP,
    }
    assert validate_message(product) == []
    return product


@pytest.mark.parametrize(
    "product",
    [
        _r05_product(
            content_rows=[{"actual_qty": "1156.87"}],
            status="completed",
            evidence_kind="review_rule",
            evidence_ref="S-01",
        ),
        _r05_product(
            content_rows=[{"actual_qty": "1156.87"}], status="rejected"
        ),
        _r05_product(content_rows=[], status="completed"),
    ],
    ids=["disguised-sandbox-rejection", "non-completed-status", "empty-result"],
)
def test_r05_blocks_direct_api_disguises(product: dict[str, Any]) -> None:
    assert _hard_rule_ids(product) == ["R-05"]


def test_hard_rule_reject_is_protocol_valid_and_has_no_model_metadata(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    product = _plan_used_as_actual_product()
    draft = _review_module().ReviewAgent(
        "trace-review", llm_call=lambda **kwargs: calls.append(kwargs)
    ).review(product)

    result = MessageBus(tmp_path / "traces").send(draft)

    assert result.accepted, result.errors
    assert result.message["payload"]["content"] == {
        "event": "review_complete",
        "reviewed_payload_type": "sql_result",
        "reviewed_msg_id": "trace-review-001",
    }
    assert result.message["verdict"]["rule_hits"][0]["evidence_ref"] == "sql-run-1"
    assert calls == []
    for field in ("model", "token_usage", "latency_ms"):
        assert field not in result.message


def _soft_product(
    claim_specs: list[tuple[str, list[tuple[str, str]]]],
    *,
    payload_type: str = "lecture_note",
    difficulty: str = "basic",
) -> dict[str, Any]:
    claims = [{"text": text, "kind": "fact"} for text, _ in claim_specs]
    evidence = [
        {
            "kind": "kb_chunk",
            "ref": ref,
            "quote": quote,
            "supports_claim": claim_text,
        }
        for claim_text, quotes in claim_specs
        for ref, quote in quotes
    ]
    content: dict[str, Any] = {
        "event": "product_ready",
        "difficulty": difficulty,
    }
    body = "\n".join(text for text, _ in claim_specs) or "请完成本任务。"
    if payload_type == "lecture_note":
        content["lecture_md"] = body
    elif payload_type == "practice_guide":
        content["guide_md"] = body
    elif payload_type == "quiz_set":
        content["questions"] = [{"id": "q1", "prompt": body}]
    else:
        raise AssertionError(payload_type)
    product = {
        "msg_id": "trace-soft-001",
        "trace_id": "trace-soft",
        "step": 1,
        "agent": "knowledge" if payload_type == "lecture_note" else "task",
        "role": "produce",
        "payload": {"type": payload_type, "content": content},
        "evidence": evidence,
        "claims": claims,
        "timestamp": TIMESTAMP,
    }
    assert validate_message(product) == []
    return product


def _review_soft(product: dict[str, Any], llm: SequentialLLM) -> dict[str, Any]:
    return _review_module().ReviewAgent(
        "trace-soft",
        llm_call=llm,
        knowledge_chunks=_synthetic_knowledge_chunks(product),
    ).review(
        product,
        learning_report=LEARNING_REPORT,
        student_profile=STUDENT_PROFILE,
    )


def _semantic_r03_result(
    *,
    blind_spots_scaffolded: bool,
    required_skills: list[str] | None = None,
    reason: str = "语义审核完成",
) -> LLMResult:
    return _llm_result(
        {
            "blind_spots_scaffolded": blind_spots_scaffolded,
            "required_skills": required_skills or [],
            "reason": reason,
        }
    )


def _atomic_lecture_product(*, include_prerequisite: bool) -> dict[str, Any]:
    chunks = {
        chunk.chunk_id: chunk
        for chunk in require_valid_chunks(
            ROOT / "agents" / "knowledge_base" / "chunks"
        )
    }
    selected = [chunks["KB-003"]]
    if include_prerequisite:
        selected.append(chunks["KB-002"])
    claims: list[dict[str, str]] = []
    evidence: list[dict[str, str]] = []
    for chunk in selected:
        quote = chunk.sentences[0]
        atom = quote.replace("**", "").replace("__", "").replace("`", "")
        claims.append({"text": atom, "kind": "fact"})
        evidence.append(
            {
                "kind": "kb_chunk",
                "ref": chunk.chunk_id,
                "quote": quote,
                "supports_claim": atom,
            }
        )
    product = {
        "msg_id": "trace-soft-atomic-001",
        "trace_id": "trace-soft",
        "step": 1,
        "agent": "knowledge",
        "role": "produce",
        "payload": {
            "type": "lecture_note",
            "content": {
                "event": "product_ready",
                "lecture_md": "\n\n".join(claim["text"] for claim in claims),
                "knowledge_point": "完成率计算",
                "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
                "coverage": [
                    "完成率计算",
                    *(["计划量与实际量口径"] if include_prerequisite else []),
                ],
                "difficulty": "basic",
            },
        },
        "evidence": evidence,
        "claims": claims,
        "timestamp": TIMESTAMP,
    }
    assert validate_message(product) == []
    return product


def _synthetic_knowledge_chunks(
    product: dict[str, Any],
) -> tuple[KnowledgeChunk, ...]:
    quotes_by_ref: dict[str, list[str]] = {}
    for item in product.get("evidence", []):
        if item.get("kind") != "kb_chunk":
            continue
        ref = item.get("ref")
        quote = item.get("quote")
        if not isinstance(ref, str) or not isinstance(quote, str):
            continue
        quotes = quotes_by_ref.setdefault(ref, [])
        if quote not in quotes:
            quotes.append(quote)
    return tuple(
        KnowledgeChunk(
            path=Path(f"{ref}.md"),
            chunk_id=ref,
            knowledge_point="测试知识点",
            difficulty="basic",
            prerequisites=(),
            learning_goal="仅用于审核单元测试",
            common_mistakes=(),
            applicable_processes=(),
            body="\n".join(quotes),
            sentences=tuple(quotes),
            metadata={},
        )
        for ref, quotes in quotes_by_ref.items()
    )


def test_review_prompts_match_design11_and_decision20_verbatim() -> None:
    assert R02_PROMPT_PATH.read_text(encoding="utf-8") == EXPECTED_R02_PROMPT
    assert R03_PROMPT_PATH.read_text(encoding="utf-8") == EXPECTED_R03_PROMPT


def test_r02_certified_atomic_claim_short_circuits_the_llm() -> None:
    product = _atomic_lecture_product(include_prerequisite=False)
    llm = SequentialLLM([])

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=LEARNING_REPORT,
        student_profile=STUDENT_PROFILE,
        learned_knowledge_points=("完成率计算", "计划量与实际量口径"),
    )

    assert verdict["verdict"]["decision"] == "approve"
    assert verdict["verdict"]["rule_hits"] == []
    assert llm.calls == []


def test_r02_invalid_kb_provenance_is_a_deterministic_hit() -> None:
    product = _atomic_lecture_product(include_prerequisite=False)
    product["evidence"][0]["quote"] = "被篡改的知识库原句。"
    llm = SequentialLLM([])

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=LEARNING_REPORT,
        student_profile=STUDENT_PROFILE,
        learned_knowledge_points=("完成率计算", "计划量与实际量口径"),
    )

    assert verdict["verdict"]["decision"] == "reject"
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == [
        "R-02"
    ]
    assert llm.calls == []


def test_r02_rejects_reversed_completion_rate_formula_deterministically() -> None:
    product = _document_product(
        "# 计算方法\n\n通过计划量除以实际完成量计算完成率。"
    )
    llm = SequentialLLM([])

    hits, results, checks = _review_module()._r02_reviews(product, llm)

    assert checks == 1
    assert results == ()
    assert [hit["rule_id"] for hit in hits] == ["R-02"]
    assert "实际量÷计划量" in hits[0]["reason"]
    assert llm.calls == []


def test_r02_does_not_reject_a_warning_against_the_reversed_formula() -> None:
    product = _document_product(
        "# 计算方法\n\n不能用计划量除以实际完成量计算完成率。"
    )
    llm = SequentialLLM([])

    hits, results, checks = _review_module()._r02_reviews(product, llm)

    assert hits == ()
    assert results == ()
    assert checks == 0
    assert llm.calls == []


def test_follow_up_question_requires_semantic_support_from_bound_evidence() -> None:
    product = {
        "msg_id": "trace-soft-follow-up-001",
        "trace_id": "trace-soft",
        "step": 1,
        "agent": "task",
        "role": "probe",
        "payload": {
            "type": "quiz_set",
            "content": {
                "event": "follow_up_question_ready",
                "question": "天气变化为什么会造成当天进度异常？",
                "standard_stem": "分别读取当日实际数和完成率，核对是否混成同一口径",
                "evidence_refs": ["M-FS01:1"],
            },
        },
        "evidence": [
            {
                "kind": "quiz_answer_key",
                "ref": "M-FS01:1",
                "quote": json.dumps(
                    {
                        "expected_points": [
                            "当日实际数与完成率是两个独立存储字段"
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        "claims": [],
        "timestamp": TIMESTAMP,
    }
    llm = SequentialLLM(
        [
            _llm_result(
                {
                    "supported": False,
                    "reason": "引文没有提到天气或因果关系。",
                }
            )
        ]
    )

    hit, _, checks = _review_module()._follow_up_evidence_review(product, llm)

    assert checks == 1
    assert hit is not None
    assert hit["rule_id"] == "R-02"
    request = json.loads(llm.calls[0]["user"])
    assert request["question"] == "天气变化为什么会造成当天进度异常？"
    assert request["evidence_quotes"]


def test_r03_grounded_scope_proof_short_circuits_the_llm() -> None:
    product = _atomic_lecture_product(include_prerequisite=True)
    report = deepcopy(LEARNING_REPORT)
    report["payload"]["content"]["blind_spots"] = [
        "完成率计算",
        "计划量与实际量口径",
    ]
    llm = SequentialLLM([])

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=report,
        student_profile=STUDENT_PROFILE,
    )

    assert verdict["verdict"]["decision"] == "approve"
    assert verdict["verdict"]["rule_hits"] == []
    assert llm.calls == []


def test_r03_declared_coverage_without_grounding_stays_on_strict_llm_path() -> None:
    product = _atomic_lecture_product(include_prerequisite=False)
    product["payload"]["content"]["coverage"] = [
        "完成率计算",
        "计划量与实际量口径",
    ]
    report = deepcopy(LEARNING_REPORT)
    report["payload"]["content"]["blind_spots"] = ["计划量与实际量口径"]
    llm = SequentialLLM(
        [
            _semantic_r03_result(
                blind_spots_scaffolded=False,
                reason="前置口径没有机械证据",
            )
        ]
    )

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=report,
        student_profile=STUDENT_PROFILE,
    )

    assert verdict["verdict"]["decision"] == "approve_with_fix"
    assert [call["system"] for call in llm.calls] == [EXPECTED_R03_PROMPT]


def test_r02_calls_once_per_fact_with_all_quotes_in_evidence_order() -> None:
    claim = "上游异常会在次月传导到下游。"
    product = _soft_product(
        [
            (
                claim,
                [
                    ("KB-007-A", "上游异常影响下游不是即时的。"),
                    ("KB-007-B", "在制品缓冲使传导存在约一期（一个月）的时滞。"),
                ],
            )
        ]
    )
    llm = SequentialLLM(
        [
            _llm_result(
                {"supported": False, "reason": "相关但不能联合推出该结论"}
            ),
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                }
            ),
        ]
    )

    verdict = _review_soft(product, llm)
    r02_user = json.loads(llm.calls[0]["user"])

    assert r02_user == {
        "claim": claim,
        "quotes": [
            "上游异常影响下游不是即时的。",
            "在制品缓冲使传导存在约一期（一个月）的时滞。",
        ],
    }
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == [
        "R-02"
    ]
    assert verdict["verdict"]["rule_hits"][0]["evidence_ref"] == "KB-007-A"
    assert verdict["verdict"]["decision"] == "reject"


def test_r02_uses_protocol_trimmed_supports_claim_to_collect_quotes() -> None:
    claim = "完成率等于实际量除以计划量。"
    quote = "完成率 = 实际量 ÷ 计划量。"
    product = _soft_product([(claim, [("KB-003", quote)])])
    product["evidence"][0]["supports_claim"] = f"  {claim}  "
    llm = SequentialLLM(
        [
            _llm_result({"supported": True, "reason": "直接支撑"}),
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                }
            ),
        ]
    )

    assert validate_message(product) == []
    verdict = _review_soft(product, llm)

    assert json.loads(llm.calls[0]["user"])["quotes"] == [quote]
    assert verdict["verdict"]["decision"] == "approve"


def test_r02_re_review_does_not_inject_global_rebuttal_per_claim() -> None:
    product = _soft_product(
        [
            ("第一条旧式改写。", [("KB-LEGACY-1", "第一条相关原句。")]),
            ("第二条旧式改写。", [("KB-LEGACY-2", "第二条相关原句。")]),
        ]
    )
    llm = SequentialLLM(
        [
            _llm_result({"supported": True, "reason": "第一条成立"}),
            _llm_result({"supported": True, "reason": "第二条成立"}),
        ]
    )

    hits, _, checks = _review_module()._r02_reviews(
        product,
        llm,
        knowledge_chunks=_synthetic_knowledge_chunks(product),
        rebuttal={"payload": {"content": {"rebuttal": "只针对第一条的辩护"}}},
    )

    assert hits == ()
    assert checks == 2
    assert all("rebuttal" not in json.loads(call["user"]) for call in llm.calls)


@pytest.mark.parametrize(
    (
        "product_difficulty",
        "r03_data",
        "expected_decision",
        "expected_action",
        "expected_hits",
    ),
    [
        (
            "applied",
            {
                "blind_spots_scaffolded": True,
                "required_skills": [],
                "reason": "语义要求已满足",
            },
            "approve_with_fix",
            "step_down",
            ["R-03"],
        ),
        (
            "advanced",
            {
                "blind_spots_scaffolded": True,
                "required_skills": [],
                "reason": "语义要求已满足",
            },
            "reject",
            "step_down",
            ["R-03"],
        ),
        (
            "basic",
            {
                "blind_spots_scaffolded": True,
                "required_skills": [],
                "reason": "语义要求已满足",
            },
            "approve",
            "keep",
            [],
        ),
    ],
    ids=["gap-one-fix", "gap-two-reject", "matched-approve"],
)
def test_r03_decision_uses_explicit_difficulty_gap(
    product_difficulty: str,
    r03_data: dict[str, Any],
    expected_decision: str,
    expected_action: str,
    expected_hits: list[str],
) -> None:
    product = _soft_product(
        [], payload_type="quiz_set", difficulty=product_difficulty
    )
    llm = SequentialLLM([_llm_result(r03_data)])

    verdict = _review_soft(product, llm)
    r03_user = json.loads(llm.calls[0]["user"])

    assert verdict["verdict"]["decision"] == expected_decision
    assert verdict["verdict"]["difficulty_action"] == expected_action
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == expected_hits
    assert r03_user["learning_report"] == LEARNING_REPORT
    assert r03_user["student_profile"] == STUDENT_PROFILE
    assert r03_user["product"]["payload_type"] == "quiz_set"
    assert r03_user["product"]["content"] == product["payload"]["content"]


def test_r03_computes_explicit_difficulty_gap_without_model_judgment() -> None:
    product = _soft_product([], payload_type="quiz_set", difficulty="advanced")
    llm = SequentialLLM(
        [
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                }
            )
        ]
    )

    verdict = _review_soft(product, llm)

    assert verdict["verdict"]["decision"] == "reject"
    assert "difficulty_gap=2" in verdict["verdict"]["rule_hits"][0]["reason"]


@pytest.mark.parametrize(
    "r03_data",
    [
        {
            "blind_spots_scaffolded": "yes",
            "required_skills": [],
            "reason": "布尔字段类型错误",
        },
        {
            "blind_spots_scaffolded": True,
            "required_skills": ["SQL", "SQL"],
            "reason": "技能列表重复",
        },
        {
            "blind_spots_scaffolded": True,
            "required_skills": [],
            "reason": "夹带旧总判断字段",
            "matched": True,
        },
    ],
    ids=["invalid-bool", "duplicate-skills", "legacy-total-judgment"],
)
def test_r03_schema_rejects_invalid_or_legacy_total_judgments(
    r03_data: dict[str, Any],
) -> None:
    errors = list(_review_module()._R03_VALIDATOR.iter_errors(r03_data))

    assert errors


def test_r03_preserves_semantic_gap_when_explicit_difficulty_levels_match() -> None:
    product = _soft_product([], payload_type="quiz_set", difficulty="basic")
    llm = SequentialLLM(
        [
            _llm_result(
                {
                    "blind_spots_scaffolded": False,
                    "required_skills": [],
                    "reason": "同档但缺少盲区铺垫",
                }
            )
        ]
    )

    verdict = _review_soft(product, llm)

    assert verdict["verdict"]["decision"] == "approve_with_fix"
    assert verdict["verdict"]["difficulty_action"] == "step_down"
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == ["R-03"]
    assert "difficulty_gap=1" in verdict["verdict"]["rule_hits"][0]["reason"]


def test_r03_filters_global_gaps_to_product_responsibility_and_trusts_strengths() -> None:
    product = _soft_product([], payload_type="quiz_set", difficulty="basic")
    product["payload"]["content"].update(
        {
            "knowledge_point": "完成率计算",
            "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
        }
    )
    learning_report = deepcopy(LEARNING_REPORT)
    learning_report["payload"]["content"]["blind_spots"] = [
        "三道工序与传导关系",
        "计划量与实际量口径",
        "传导时滞分析",
    ]
    student_profile = deepcopy(STUDENT_PROFILE)
    student_profile["gaps_prior"] = [
        "三道工序与传导关系",
        "计划量与实际量口径",
        "传导时滞分析",
    ]
    llm = SequentialLLM(
        [
            _semantic_r03_result(
                blind_spots_scaffolded=True,
                required_skills=["数据分析工具使用"],
                reason="产物已有相关盲区铺垫",
            )
        ]
    )

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=learning_report,
        student_profile=student_profile,
    )
    r03_user = json.loads(llm.calls[0]["user"])

    assert verdict["verdict"]["decision"] == "approve"
    assert verdict["verdict"]["rule_hits"] == []
    assert r03_user["learning_report"]["payload"]["content"]["blind_spots"] == [
        "计划量与实际量口径"
    ]
    assert r03_user["student_profile"]["gaps_prior"] == [
        "计划量与实际量口径"
    ]
    assert r03_user["structured_authority"] == {
        "knowledge_point": "完成率计算",
        "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
        "learned_knowledge_points": [],
        "relevant_blind_spots": ["计划量与实际量口径"],
        "strengths": ["SQL基础", "数据分析工具"],
    }


def test_r03_does_not_require_a_followup_task_to_reteach_learned_scope() -> None:
    product = _soft_product([], payload_type="quiz_set", difficulty="applied")
    product["payload"]["content"].update(
        {
            "knowledge_point": "完成率计算",
            "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
        }
    )
    learning_report = deepcopy(LEARNING_REPORT)
    learning_report["payload"]["content"].update(
        {
            "blind_spots": ["完成率计算", "计划量与实际量口径"],
            "difficulty": "applied",
        }
    )
    llm = SequentialLLM(
        [
            _semantic_r03_result(
                blind_spots_scaffolded=False,
                reason="任务本身未重复讲解",
            )
        ]
    )

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=learning_report,
        student_profile=STUDENT_PROFILE,
        learned_knowledge_points=("完成率计算", "计划量与实际量口径"),
    )

    assert verdict["verdict"]["decision"] == "approve"
    assert llm.calls == []


def test_r03_still_rejects_an_unscaffolded_relevant_blind_spot() -> None:
    product = _soft_product([], payload_type="lecture_note", difficulty="basic")
    product["payload"]["content"].update(
        {
            "knowledge_point": "完成率计算",
            "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
        }
    )
    learning_report = deepcopy(LEARNING_REPORT)
    learning_report["payload"]["content"]["blind_spots"] = [
        "计划量与实际量口径"
    ]
    llm = SequentialLLM(
        [
            _semantic_r03_result(
                blind_spots_scaffolded=False,
                reason="相关口径没有铺垫",
            )
        ]
    )

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=learning_report,
        student_profile=STUDENT_PROFILE,
    )

    assert verdict["verdict"]["decision"] == "approve_with_fix"
    assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == [
        "R-03"
    ]


def test_r03_ignores_model_required_skill_outside_closed_scope() -> None:
    product = _soft_product([], payload_type="quiz_set", difficulty="basic")
    product["payload"]["content"].update(
        {
            "knowledge_point": "完成率计算",
            "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
        }
    )
    learning_report = deepcopy(LEARNING_REPORT)
    learning_report["payload"]["content"]["blind_spots"] = []
    llm = SequentialLLM(
        [
            _semantic_r03_result(
                blind_spots_scaffolded=True,
                required_skills=["统计建模"],
                reason="任务需要额外前置技能",
            )
        ]
    )

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=learning_report,
        student_profile=STUDENT_PROFILE,
    )

    assert verdict["verdict"]["decision"] == "approve"
    assert verdict["verdict"]["rule_hits"] == []
    assert len(llm.calls) == 1


def test_r03_keeps_missing_direct_prerequisite_as_a_real_gap() -> None:
    product = _soft_product([], payload_type="quiz_set", difficulty="basic")
    product["payload"]["content"].update(
        {
            "knowledge_point": "完成率计算",
            "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
        }
    )
    learning_report = deepcopy(LEARNING_REPORT)
    learning_report["payload"]["content"]["blind_spots"] = []
    llm = SequentialLLM(
        [
            _semantic_r03_result(
                blind_spots_scaffolded=True,
                required_skills=["计划量与实际量口径"],
                reason="任务确实要求直接前置口径",
            )
        ]
    )

    verdict = _review_module().ReviewAgent(
        "trace-soft", llm_call=llm
    ).review(
        product,
        learning_report=learning_report,
        student_profile=STUDENT_PROFILE,
    )

    assert verdict["verdict"]["decision"] == "approve_with_fix"
    assert "计划量与实际量口径" in verdict["verdict"]["rule_hits"][0]["reason"]


def test_r03_does_not_run_for_sql_result() -> None:
    product = _sql_product(
        question="H2601五月预处理实际完成了多少",
        family="Q1",
        sql=(
            "SELECT SUM(actual_qty) AS actual_qty FROM fact_production_progress "
            "WHERE ship_no='H2601' AND process_code='YCL' "
            "AND period_date>='2025-05-01' AND period_date<'2025-06-01'"
        ),
        rows=[{"actual_qty": "1156.87"}],
    )
    llm = SequentialLLM([])

    verdict = _review_soft(product, llm)

    assert verdict["verdict"] == {
        "decision": "approve",
        "rule_hits": [],
        "difficulty_action": "none",
    }
    assert llm.calls == []
    for field in ("model", "token_usage", "latency_ms"):
        assert field not in verdict


def test_soft_rule_telemetry_sums_every_32b_call_and_keeps_all_hit_refs(
    tmp_path: Path,
) -> None:
    product = _soft_product(
        [
            (
                "完成率等于实际量除以计划量。",
                [("KB-003", "完成率 = 实际量 ÷ 计划量。")],
            ),
            (
                "五月异常必然由单日波动导致。",
                [("KB-006", "正常区间约为88%—103%。")],
            ),
        ],
        payload_type="practice_guide",
        difficulty="advanced",
    )
    llm = SequentialLLM(
        [
            _llm_result(
                {"supported": True, "reason": "直接支撑"},
                latency_ms=11,
                prompt_tokens=10,
                completion_tokens=2,
            ),
            _llm_result(
                {"supported": False, "reason": "仅相关但不能推出"},
                latency_ms=12,
                prompt_tokens=20,
                completion_tokens=3,
            ),
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                },
                latency_ms=13,
                prompt_tokens=30,
                completion_tokens=4,
            ),
        ]
    )

    draft = _review_soft(product, llm)
    result = MessageBus(tmp_path / "traces").send(draft)

    assert result.accepted, result.errors
    assert draft["verdict"]["decision"] == "reject"
    assert [hit["rule_id"] for hit in draft["verdict"]["rule_hits"]] == [
        "R-02",
        "R-03",
    ]
    assert [hit["evidence_ref"] for hit in draft["verdict"]["rule_hits"]] == [
        "KB-006",
        "trace-soft-report-001",
    ]
    assert draft["model"] == "qwen3-32b"
    assert draft["payload"]["content"]["llm_latency_ms"] == 36
    assert draft["payload"]["content"]["r02_checks"] == 2
    assert draft["payload"]["content"]["r03_checked"] is True
    assert draft["token_usage"] == {
        "prompt_tokens": 60,
        "completion_tokens": 9,
        "total_tokens": 69,
    }
    assert draft["latency_ms"] >= 36


def test_soft_calls_use_32b_temperature_and_closed_json_schemas() -> None:
    product = _soft_product(
        [
            (
                "完成率等于实际量除以计划量。",
                [("KB-003", "完成率 = 实际量 ÷ 计划量。")],
            )
        ]
    )
    llm = SequentialLLM(
        [
            _llm_result({"supported": True, "reason": "直接支撑"}),
            _llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "语义要求已满足",
                }
            ),
        ]
    )

    _review_soft(product, llm)

    assert [call["system"] for call in llm.calls] == [
        EXPECTED_R02_PROMPT,
        EXPECTED_R03_PROMPT,
    ]
    for call in llm.calls:
        assert call["model"] == "qwen3-32b"
        assert call["temperature"] == 0.1
        assert call["json_schema"]["additionalProperties"] is False


def _live_fact_product(
    index: int, claim: str, quote: str, *, ref: str
) -> dict[str, Any]:
    product = {
        "msg_id": f"trace-live-r02-{index:03d}",
        "trace_id": f"trace-live-r02-{index}",
        "step": 1,
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {"event": "fact_review", "case_index": index},
        },
        "evidence": [
            {
                "kind": "kb_chunk",
                "ref": ref,
                "quote": quote,
                "supports_claim": claim,
            }
        ],
        "claims": [{"text": claim, "kind": "fact"}],
        "timestamp": TIMESTAMP,
    }
    assert validate_message(product) == []
    return product


def test_live_r01_three_attacks_blocked_and_three_correct_accepted() -> None:
    attacks = [
        _plan_used_as_actual_product(),
        _tampered_numeric_product(),
        _workshop_question_with_ship_level_sql(),
    ]
    correct = [
        _sql_product(
            question="H2601五月预处理实际完成了多少",
            family="Q1",
            sql=(
                "SELECT SUM(actual_qty) AS actual_qty FROM fact_production_progress "
                "WHERE ship_no='H2601' AND process_code='YCL' "
                "AND period_date>='2025-05-01' AND period_date<'2025-06-01'"
            ),
            rows=[{"actual_qty": "1156.87"}],
        ),
        _sql_product(
            question="H2601五月预处理完成率",
            family="Q3",
            sql=(
                "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
                "FROM fact_production_progress WHERE ship_no='H2601' "
                "AND process_code='YCL' AND period_date>='2025-05-01' "
                "AND period_date<'2025-06-01'"
            ),
            rows=[{"complete_rate": "0.6236"}],
        ),
        _sql_product(
            question="五月预处理各责任单元完成率",
            family="Q7",
            sql=(
                "SELECT workshop_code, "
                "ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
                "FROM fact_production_progress WHERE process_code='YCL' "
                "AND period_date>='2025-05-01' AND period_date<'2025-06-01' "
                "GROUP BY workshop_code ORDER BY complete_rate ASC"
            ),
            rows=[{"workshop_code": "WSA", "complete_rate": "0.9044"}],
        ),
    ]

    attack_hits = [_hard_rule_ids(product) for product in attacks]
    correct_hits = [_hard_rule_ids(product) for product in correct]

    assert attack_hits == [["R-01"], ["R-01"], ["R-01"]]
    assert correct_hits == [[], [], []]
    print(
        "LIVE_R01="
        + json.dumps(
            {"attacks_blocked": len(attack_hits), "correct_accepted": len(correct_hits)},
            ensure_ascii=False,
        )
    )


def test_live_r02_three_non_entailing_rejected_and_three_entailing_accepted() -> None:
    # Keep one invalid ref to exercise the deterministic no-LLM R-02 path;
    # semantic cases use byte-exact slices from the approved catalog.
    cases = [
        (
            False,
            "KB-LIVE-001",
            "处于正常完成率区间的生产任务都不会发生延期。",
            "本数据环境下各船各工序的月完成率正常范围约为88%—103%。",
        ),
        (
            False,
            "KB-007",
            "制作托盘出现异常就能证明安装托盘完成率必然低于50%。",
            "上游异常影响下游可能存在**时滞**：在制品缓冲使传导不一定当月显现。",
        ),
        (
            False,
            "KB-009",
            "WSA是五月预处理异常的唯一责任单元。",
            "每条生产记录归属一个**责任单元（workshop_code，车间/工区）**。",
        ),
        (
            True,
            "KB-003",
            "完成率等于实际量除以计划量。",
            "**完成率 = 实际量 ÷ 计划量**。",
        ),
        (
            True,
            "KB-001",
            "上游异常对下游的影响存在约一个月时滞。",
            "例如预处理某月完成率大幅下滑，制作托盘环节通常要到下一个月才显现缺料影响——因为车间通常持有约一个月的在制缓冲。",
        ),
        (
            True,
            "KB-010",
            "跨工序归因方法应包括候选源头、疑似传导路径及证据缺口。",
            "7. **输出结论**：候选源头、疑似传导路径及证据缺口。",
        ),
    ]
    outcomes: list[dict[str, Any]] = []
    for index, (supported, ref, claim, quote) in enumerate(cases, start=1):
        agent = runtime.build_review_agent(f"trace-live-r02-{index}")
        verdict = agent.review(_live_fact_product(index, claim, quote, ref=ref))
        rule_ids = [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]]
        if supported:
            assert verdict["verdict"]["decision"] == "approve", (claim, verdict)
            assert rule_ids == []
        else:
            assert verdict["verdict"]["decision"] == "reject", (claim, verdict)
            assert rule_ids == ["R-02"]
        outcome = {
            "case": index,
            "expected_supported": supported,
            "decision": verdict["verdict"]["decision"],
            "rule_ids": rule_ids,
        }
        llm_called = "llm_latency_ms" in verdict["payload"]["content"]
        if llm_called:
            for field in ("model", "latency_ms", "token_usage"):
                assert field in verdict
                outcome[field] = verdict[field]
        else:
            for field in ("model", "latency_ms", "token_usage"):
                assert field not in verdict
        outcomes.append(outcome)
    print("LIVE_R02=" + json.dumps(outcomes, ensure_ascii=False))


def _live_r03_product(
    index: int, payload_type: str, difficulty: str, instruction: str
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "event": "product_ready",
        "difficulty": difficulty,
        "difficulty_features": instruction,
    }
    if payload_type == "lecture_note":
        content["lecture_md"] = instruction
        agent = "knowledge"
    elif payload_type == "practice_guide":
        content["guide_md"] = instruction
        agent = "task"
    else:
        content["questions"] = [{"id": f"live-{index}", "prompt": instruction}]
        agent = "task"
    product = {
        "msg_id": f"trace-live-r03-{index:03d}",
        "trace_id": f"trace-live-r03-{index}",
        "step": 1,
        "agent": agent,
        "role": "produce",
        "payload": {"type": payload_type, "content": content},
        "evidence": [],
        "claims": [],
        "timestamp": TIMESTAMP,
    }
    assert validate_message(product) == []
    return product


def test_live_r03_three_explicit_cross_level_mismatches_rejected() -> None:
    cases = [
        (
            "line_leader",
            "basic",
            0.2,
            "quiz_set",
            "advanced",
            "不提供讲解，直接编写包含三个表JOIN、窗口函数和子查询的SQL。",
        ),
        (
            "planner_new",
            "basic",
            0.4,
            "practice_guide",
            "advanced",
            "跳过三道工序和计划/实际口径解释，直接完成跨工序多表归因SQL。",
        ),
        (
            "craft_engineer",
            "advanced",
            1.0,
            "lecture_note",
            "basic",
            "本节只要求背诵预处理、制作托盘、安装托盘三个工序名称。",
        ),
    ]
    outcomes: list[dict[str, Any]] = []
    profiles = {
        "planner_new": STUDENT_PROFILE,
        "line_leader": {
            "profile_id": "line_leader",
            "title": "一线班组长（晋升培训）",
            "background": "高职毕业一线班组长，现场熟，理论与数据双弱",
            "strengths": ["现场操作经验"],
            "gaps_prior": ["计划量与实际量口径", "完成率计算", "异常识别标准", "责任单元定位"],
            "difficulty_start": "basic",
            "lecture_style": "步骤化短句、每步带检查点、基础案例优先",
        },
        "craft_engineer": {
            "profile_id": "craft_engineer",
            "title": "转岗数字化的工艺工程师",
            "background": "船舶工艺背景转数字化岗，精通预处理/托盘工艺，不会数据工具",
            "strengths": ["船舶工艺知识", "现场生产经验"],
            "gaps_prior": ["完成率计算", "月度聚合方法", "跨工序归因方法"],
            "difficulty_start": "applied",
            "lecture_style": "重讲数据工具与图表解读、少讲工艺常识",
        },
    }
    for index, (
        profile_id,
        report_difficulty,
        rate,
        payload_type,
        product_difficulty,
        instruction,
    ) in enumerate(cases, start=1):
        report = {
            "msg_id": f"trace-live-r03-report-{index:03d}",
            "profile_id": profile_id,
            "difficulty": report_difficulty,
            "blind_spots": profiles[profile_id]["gaps_prior"],
            "pretest_score": {"correct": round(rate * 5), "total": 5, "rate": rate},
        }
        product = _live_r03_product(
            index, payload_type, product_difficulty, instruction
        )
        verdict = runtime.build_review_agent(f"trace-live-r03-{index}").review(
            product,
            learning_report=report,
            student_profile=profiles[profile_id],
        )
        assert verdict["verdict"]["decision"] == "reject", verdict
        assert [hit["rule_id"] for hit in verdict["verdict"]["rule_hits"]] == [
            "R-03"
        ]
        outcomes.append(
            {
                "case": index,
                "profile_id": profile_id,
                "decision": verdict["verdict"]["decision"],
                "reason": verdict["verdict"]["rule_hits"][0]["reason"],
                "latency_ms": verdict["latency_ms"],
                "token_usage": verdict["token_usage"],
            }
        )
    print("LIVE_R03=" + json.dumps(outcomes, ensure_ascii=False))
