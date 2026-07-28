"""Layered product review with deterministic hard-rule enforcement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from functools import lru_cache
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Callable
import unicodedata

from jsonschema import Draft202012Validator
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.evidence_review_agent import EvidenceReviewAgent
from agents.deterministic_review_agents import (
    DataSafetyReviewAgent,
    ReadabilityReviewAgent,
)
from agents.pedagogy_review_agent import PedagogyReviewAgent
from agents.sandbox import validate_and_rewrite
from agents.query_authority import QueryAuthority, query_authority_from_mapping
from agents.review_arbiter import DeterministicReviewArbiter
from agents.verification_agent import _render_claims, _result_is_empty
from coordination.artifacts import ArtifactEnvelope
from coordination.contracts import LearningContract
from coordination.parallel import BranchSpec, ParallelStage, ParallelStageExecutor
from orchestrator.llm import LLMResult, TokenUsage, call_llm


NUMERIC_TOLERANCE = Decimal("0.0001")
R04_SIMILARITY_THRESHOLD = 0.8
REVIEW_MODEL = "qwen3-32b"
PROMPT_DIR = Path(__file__).with_name("prompts")
KNOWLEDGE_CHUNK_DIR = Path(__file__).with_name("knowledge_base") / "chunks"
R02_PROMPT = (PROMPT_DIR / "review_r02.md").read_text(encoding="utf-8")
R03_PROMPT = (PROMPT_DIR / "review_r03.md").read_text(encoding="utf-8")
FOLLOW_UP_EVIDENCE_PROMPT = (
    PROMPT_DIR / "review_follow_up_evidence.md"
).read_text(encoding="utf-8")
R02_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["supported", "reason"],
    "properties": {
        "supported": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}
R03_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["blind_spots_scaffolded", "required_skills", "reason"],
    "properties": {
        "blind_spots_scaffolded": {"type": "boolean"},
        "required_skills": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "reason": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}
_R02_VALIDATOR = Draft202012Validator(R02_OUTPUT_SCHEMA)
_R03_VALIDATOR = Draft202012Validator(R03_OUTPUT_SCHEMA)
DIFFICULTY_ORDER = ("basic", "applied", "advanced")
PLAN_TERMS = ("计划量", "计划数", "plan", "应完成", "应该做")
ACTUAL_TERMS = (
    "实际量",
    "实际数",
    "actual",
    "完成了多少",
    "实际完成",
    "做了多少",
)
COMPLETE_RATE_TERMS = ("完成率", "complete_rate")
DEVIATION_RATE_TERMS = ("偏差率", "deviation")
ACTUAL_TRAP_TERMS = ("完成多少", "完成情况")
PROCESS_TERMS = ("预处理", "YCL", "制作托盘", "ZZTP", "安装托盘", "AZTP")
WORKSHOP_TERMS = ("责任单元", "车间", "单元", "workshop")
CERTAINTY_PREDICATES = (
    "是",
    "为",
    "等于",
    "达到",
    "高于",
    "低于",
    "超过",
    "共",
    "合计",
    "必须",
    "始终",
)
TASK_R04_PAYLOAD_TYPES = frozenset({"quiz_set", "practice_guide"})
METRIC_TERMS = tuple(
    dict.fromkeys(
        PLAN_TERMS
        + ACTUAL_TERMS
        + COMPLETE_RATE_TERMS
        + DEVIATION_RATE_TERMS
        + ACTUAL_TRAP_TERMS
    )
)
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_ORDERED_LIST_MARKER_RE = re.compile(r"^\s*\d+\.\s+")
_SHIP_RE = re.compile(r"[A-Z]\d{4}", re.IGNORECASE)
_MONTH_RE = re.compile(
    r"(?:20\d{2}[-年/]\d{1,2}|\d{1,2}月|[一二三四五六七八九十]{1,3}月)"
)
_NUMBER_RE = r"[-+]?\d+(?:\.\d+)?"
_REVERSED_COMPLETE_RATE_RE = re.compile(
    r"(?:计划量|计划数|plan_qty|plan)\s*(?:汇总)?\s*"
    r"(?:除以|除|÷|/)\s*"
    r"(?:实际量|实际数|实际完成量|actual_qty|actual)",
    re.IGNORECASE,
)
_FORMULA_NEGATION_TERMS = (
    "错误",
    "不能",
    "不可",
    "禁止",
    "不应",
    "避免",
    "而不是",
    "≠",
)
_INTERNAL_PRESENTATION_RE = re.compile(
    r"(?:trace_id|msg_id|evidence_bundle_ref|learning_contract|payload\.content|"
    r"S\d+_[A-Z_]+)",
    re.IGNORECASE,
)
_METRIC_VALUE_PATTERNS = {
    "plan_qty": re.compile(
        rf"(?:计划量|计划数|plan_qty|plan)\s*(?:为|是|等于|达到|共|合计|=|：|:)?\s*({_NUMBER_RE})(%)?",
        re.IGNORECASE,
    ),
    "actual_qty": re.compile(
        rf"(?:实际量|实际数|actual_qty|actual|实际完成)\s*(?:为|是|等于|达到|共|合计|=|：|:)?\s*({_NUMBER_RE})(%)?",
        re.IGNORECASE,
    ),
    "complete_rate": re.compile(
        rf"(?:完成率|complete_rate)\s*(?:为|是|等于|达到|共|合计|=|：|:)?\s*({_NUMBER_RE})(%)?",
        re.IGNORECASE,
    ),
    "deviation_rate": re.compile(
        rf"(?:偏差率|deviation_rate|deviation)\s*(?:为|是|等于|达到|共|合计|=|：|:)?\s*({_NUMBER_RE})(%)?",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True, slots=True)
class QueryEvidence:
    ref: str
    generated_sql: str
    status: str
    row_count: int | None
    rows: tuple[dict[str, Any], ...]
    supports_claim: str | None


@dataclass(frozen=True, slots=True)
class R04SemanticCheck:
    sentence: str
    claims: tuple[str, ...]


@lru_cache(maxsize=1)
def _approved_knowledge_chunks() -> tuple[KnowledgeChunk, ...]:
    return require_valid_chunks(KNOWLEDGE_CHUNK_DIR)


def _payload(product: Mapping[str, Any]) -> Mapping[str, Any]:
    value = product.get("payload")
    return value if isinstance(value, Mapping) else {}


def _content(product: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _payload(product).get("content")
    return value if isinstance(value, Mapping) else {}


def _items(product: Mapping[str, Any], field: str) -> tuple[Mapping[str, Any], ...]:
    value = product.get(field)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _evidence_ref(product: Mapping[str, Any], preferred_kind: str | None = None) -> str:
    for item in _items(product, "evidence"):
        if preferred_kind is not None and item.get("kind") != preferred_kind:
            continue
        ref = item.get("ref")
        if isinstance(ref, str) and ref.strip():
            return ref
    msg_id = product.get("msg_id")
    return msg_id if isinstance(msg_id, str) and msg_id.strip() else "reviewed-product"


def _rule_hit(rule_id: str, reason: str, evidence_ref: str) -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "reason": reason,
        "evidence_ref": evidence_ref,
    }


def _catalog_by_id(
    chunks: Sequence[KnowledgeChunk],
) -> dict[str, KnowledgeChunk]:
    return {chunk.chunk_id: chunk for chunk in chunks}


def _approved_quotes(chunk: KnowledgeChunk) -> frozenset[str]:
    quotes = list(chunk.sentences)
    if chunk.teaching_fact_card is not None:
        quotes.extend(fact.text for fact in chunk.teaching_fact_card.facts)
    return frozenset(quotes)


def _audit_kb_claim(
    claim_text: str,
    evidence: Sequence[Mapping[str, Any]],
    chunks_by_id: Mapping[str, KnowledgeChunk],
) -> tuple[str, tuple[Mapping[str, Any], ...], tuple[str, ...], str]:
    """Classify a fact as certified, invalid provenance, or legacy paraphrase."""

    citations = tuple(
        item
        for item in evidence
        if item.get("kind") == "kb_chunk"
        and isinstance(item.get("supports_claim"), str)
        and str(item["supports_claim"]).strip() == claim_text.strip()
    )
    if not citations:
        return "invalid", (), (), "没有绑定知识库引文"

    authentic: list[tuple[str, str]] = []
    for citation in citations:
        ref = citation.get("ref")
        quote = citation.get("quote")
        if not isinstance(ref, str) or not ref.strip():
            return "invalid", citations, (), "知识库引文缺少有效ref"
        if not isinstance(quote, str) or not quote.strip():
            return "invalid", citations, (), f"知识库引文{ref}缺少有效quote"
        chunk = chunks_by_id.get(ref)
        if chunk is None:
            return "invalid", citations, (), f"知识库ref {ref}不在批准目录"
        if quote not in _approved_quotes(chunk):
            return "invalid", citations, (), f"引文不属于批准切片{ref}"
        authentic.append((ref, quote))

    claim_key = _match_key(claim_text)
    certified_refs = tuple(
        dict.fromkeys(
            ref
            for ref, quote in authentic
            if claim_key and claim_key in _match_key(quote)
        )
    )
    if certified_refs:
        return "certified", citations, certified_refs, "原子结论由原句机械包含"
    return "legacy", citations, (), "合法引文需要语义蕴含判断"


def _catalog_responsibility_scope(
    knowledge_point: str,
    difficulty: str | None,
    chunks: Sequence[KnowledgeChunk],
) -> tuple[str, ...]:
    if not knowledge_point:
        return ()
    candidates = tuple(
        chunk
        for chunk in chunks
        if chunk.knowledge_point == knowledge_point
        and (difficulty is None or chunk.difficulty == difficulty)
    )
    if not candidates:
        return ()
    chunks_by_id = _catalog_by_id(chunks)
    scope = [knowledge_point]
    for chunk in sorted(candidates, key=lambda item: item.chunk_id):
        for prerequisite_id in chunk.prerequisites:
            prerequisite = chunks_by_id.get(prerequisite_id)
            if (
                prerequisite is not None
                and prerequisite.knowledge_point not in scope
            ):
                scope.append(prerequisite.knowledge_point)
    return tuple(scope)


def _grounded_coverage(
    product: Mapping[str, Any],
    chunks: Sequence[KnowledgeChunk],
) -> tuple[str, ...]:
    if _payload(product).get("type") != "lecture_note":
        return ()
    content = _content(product)
    lecture = content.get("lecture_md")
    if not isinstance(lecture, str):
        return ()
    declared = _non_empty_strings(content.get("coverage", ()))
    chunks_by_id = _catalog_by_id(chunks)
    evidence = _items(product, "evidence")
    certified_sources: dict[str, frozenset[str]] = {}
    for claim in _items(product, "claims"):
        claim_text = claim.get("text")
        if claim.get("kind") != "fact" or not isinstance(claim_text, str):
            continue
        if claim_text not in lecture:
            continue
        status, _, certified_refs, _ = _audit_kb_claim(
            claim_text,
            evidence,
            chunks_by_id,
        )
        if status == "certified":
            certified_sources[claim_text] = frozenset(certified_refs)

    grounded: list[str] = []
    for point in declared:
        point_chunk_ids = {
            chunk.chunk_id for chunk in chunks if chunk.knowledge_point == point
        }
        if point_chunk_ids and any(
            refs & point_chunk_ids for refs in certified_sources.values()
        ):
            grounded.append(point)
    return tuple(grounded)


def _query_evidence(product: Mapping[str, Any]) -> tuple[QueryEvidence, ...]:
    records: list[QueryEvidence] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in _items(product, "evidence"):
        if item.get("kind") != "sql_query":
            continue
        ref = item.get("ref")
        quote = item.get("quote")
        supports = item.get("supports_claim")
        if not isinstance(ref, str) or not isinstance(quote, str):
            continue
        normalized_supports = supports.strip() if isinstance(supports, str) else None
        identity = (ref, quote, normalized_supports)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            data = json.loads(quote)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, Mapping):
            continue
        rows_value = data.get("rows")
        rows = (
            tuple(dict(row) for row in rows_value if isinstance(row, Mapping))
            if isinstance(rows_value, list)
            else ()
        )
        row_count = data.get("row_count")
        records.append(
            QueryEvidence(
                ref=ref,
                generated_sql=str(data.get("generated_sql", "")),
                status=str(data.get("status", "")),
                row_count=(
                    row_count
                    if isinstance(row_count, int) and not isinstance(row_count, bool)
                    else None
                ),
                rows=rows,
                supports_claim=normalized_supports,
            )
        )
    return tuple(records)


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def _question_metrics(question: str) -> frozenset[str]:
    if _contains_any(question, DEVIATION_RATE_TERMS):
        return frozenset({"deviation_rate"})
    if _contains_any(question, COMPLETE_RATE_TERMS):
        return frozenset({"complete_rate"})
    metrics: set[str] = set()
    if _contains_any(question, PLAN_TERMS):
        metrics.add("plan_qty")
    if _contains_any(question, ACTUAL_TERMS) or _contains_any(
        question, ACTUAL_TRAP_TERMS
    ):
        metrics.add("actual_qty")
    return frozenset(metrics)


def _projection_sources(statement: exp.Select) -> dict[str, tuple[exp.Expression, frozenset[str]]]:
    projections: dict[str, tuple[exp.Expression, frozenset[str]]] = {}
    for projection in statement.expressions:
        alias = projection.alias_or_name.casefold() if projection.alias_or_name else ""
        if not alias:
            continue
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        sources = frozenset(column.name.casefold() for column in expression.find_all(exp.Column))
        projections[alias] = (expression, sources)
    return projections


def _unwrap_formula(expression: exp.Expression) -> exp.Expression:
    while isinstance(expression, (exp.Paren, exp.Round)):
        expression = expression.this
    return expression


def _is_zero_literal(expression: exp.Expression) -> bool:
    return isinstance(expression, exp.Literal) and not expression.is_string and str(
        expression.this
    ) in {"0", "0.0"}


def _is_metric_operand(expression: exp.Expression, column: str) -> bool:
    expression = _unwrap_formula(expression)
    if isinstance(expression, exp.Nullif):
        fallback = expression.expression
        if not isinstance(fallback, exp.Expression) or not _is_zero_literal(fallback):
            return False
        expression = _unwrap_formula(expression.this)
    if isinstance(expression, exp.Sum):
        expression = _unwrap_formula(expression.this)
    return isinstance(expression, exp.Column) and expression.name.casefold() == column


def _is_ratio_formula(expression: exp.Expression, metric: str) -> bool:
    expression = _unwrap_formula(expression)
    if not isinstance(expression, exp.Div):
        return False
    denominator = expression.expression
    if not isinstance(denominator, exp.Expression) or not _is_metric_operand(
        denominator, "plan_qty"
    ):
        return False
    numerator = _unwrap_formula(expression.this)
    if metric == "complete_rate":
        return _is_metric_operand(numerator, "actual_qty")
    if metric != "deviation_rate" or not isinstance(numerator, exp.Sub):
        return False
    right = numerator.expression
    return isinstance(right, exp.Expression) and _is_metric_operand(
        numerator.this, "actual_qty"
    ) and _is_metric_operand(right, "plan_qty")


def _metric_sql_issue(
    question: str,
    statement: exp.Select,
    *,
    query_authority: QueryAuthority | None = None,
) -> str | None:
    expected = (
        frozenset(query_authority.metric_columns)
        if query_authority is not None
        else _question_metrics(question)
    )
    if not expected:
        return None
    projections = _projection_sources(statement)
    for metric in sorted(expected):
        if metric not in projections:
            return f"问题口径要求{metric}，SQL输出未提供该口径"
        expression, _sources = projections[metric]
        if metric in {"plan_qty", "actual_qty"} and not _is_metric_operand(
            expression, metric
        ):
            return f"SQL将其他字段伪装为{metric}口径"
        if metric == "complete_rate" and not _is_ratio_formula(
            expression, "complete_rate"
        ):
            return "问题口径要求complete_rate=actual_qty/plan_qty，SQL公式不一致"
        if metric == "deviation_rate" and not _is_ratio_formula(
            expression, "deviation_rate"
        ):
            return "问题口径要求deviation=(actual_qty-plan_qty)/plan_qty，SQL公式不一致"
    return None


def _columns_in(node: exp.Expression | None) -> frozenset[str]:
    if node is None:
        return frozenset()
    return frozenset(column.name.casefold() for column in node.find_all(exp.Column))


def _dimension_issue(
    question: str,
    statement: exp.Select,
    *,
    family: str,
    query_authority: QueryAuthority | None = None,
) -> str | None:
    where_columns = _columns_in(statement.args.get("where"))
    group_columns = _columns_in(statement.args.get("group"))

    if query_authority is not None:
        missing_filters = set(query_authority.filter_columns) - set(where_columns)
        if missing_filters:
            return "SQL缺少任务目标过滤列：" + ",".join(sorted(missing_filters))
        missing_groups = set(query_authority.group_by_columns) - set(group_columns)
        if missing_groups:
            return "SQL缺少任务目标分组列：" + ",".join(sorted(missing_groups))
        if (
            query_authority.time_column == "period_date"
            and "batch_code" in where_columns
        ):
            return "YYYY-MM任务时间槽必须使用period_date，不能使用batch_code"
        return None

    if _SHIP_RE.search(question) and "ship_no" not in where_columns:
        return "问题指定船号，但SQL未在WHERE中锁定ship_no"
    if (
        family != "Q6"
        and _contains_any(question, PROCESS_TERMS)
        and "process_code" not in where_columns
    ):
        generic_process_comparison = any(
            term in question for term in ("各工序", "三道工序", "工序级", "按工序")
        )
        if not generic_process_comparison:
            return "问题指定工序，但SQL未在WHERE中锁定process_code"
    if _MONTH_RE.search(question) and "period_date" not in where_columns:
        return "问题指定月份，但SQL未在WHERE中锁定period_date"

    grouped_layers = (
        (("责任单元", "车间", "单元", "workshop"), "workshop_code"),
        (("各船", "船级", "按船", "船号排名"), "ship_no"),
        (("各工序", "三道工序", "工序级", "按工序"), "process_code"),
        (("各月", "按月", "走势", "序列"), "month_label"),
    )
    for terms, column in grouped_layers:
        if _contains_any(question, terms) and column not in group_columns:
            return f"问题要求{column}层级聚合，SQL的GROUP BY层级不一致"
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _claimed_metric_values(text: str) -> tuple[tuple[str, Decimal], ...]:
    normalized = unicodedata.normalize("NFKC", text)
    values: list[tuple[str, Decimal]] = []
    for metric, pattern in _METRIC_VALUE_PATTERNS.items():
        for match in pattern.finditer(normalized):
            value = _decimal(match.group(1))
            if value is None:
                continue
            if match.group(2):
                value /= Decimal(100)
            values.append((metric, value))
    return tuple(values)


def _numeric_issue(claim_text: str, rows: Sequence[Mapping[str, Any]]) -> str | None:
    for metric, claimed in _claimed_metric_values(claim_text):
        evidence_values = tuple(
            parsed
            for row in rows
            if metric in row
            for parsed in (_decimal(row[metric]),)
            if parsed is not None
        )
        if not evidence_values:
            return f"结论声明{metric}={claimed}，结构化SQL结果没有该口径"
        if not any(abs(claimed - actual) <= NUMERIC_TOLERANCE for actual in evidence_values):
            return f"结论数值{metric}={claimed}与结构化SQL结果不一致"
    return None


def _claim_shape(text: str) -> str:
    shaped = unicodedata.normalize("NFKC", text)
    for metric, pattern in _METRIC_VALUE_PATTERNS.items():
        shaped = pattern.sub(f"{metric}=<value>", shaped)
    return _normalize_claim_text(shaped)


def _rows_for_claim(
    family: str,
    question: str,
    claim_text: str,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    target_shape = _claim_shape(claim_text)
    matches: list[Mapping[str, Any]] = []
    for row in rows:
        try:
            rendered = _render_claims(family, question, [dict(row)])
        except (KeyError, TypeError, ValueError):
            continue
        if len(rendered) == 1 and _claim_shape(rendered[0]) == target_shape:
            matches.append(row)
    return tuple(matches)


def _r01(product: Mapping[str, Any]) -> dict[str, str] | None:
    if _payload(product).get("type") != "sql_result":
        return None
    claims = tuple(
        claim
        for claim in _items(product, "claims")
        if claim.get("kind") == "data_conclusion"
        and isinstance(claim.get("text"), str)
    )
    if not claims:
        return None
    records = _query_evidence(product)
    if not records:
        return None
    content = _content(product)
    question = str(content.get("question", ""))
    family = str(content.get("family", ""))
    authority_value = content.get("query_authority")
    query_authority: QueryAuthority | None = None
    authority_issue: str | None = None
    if isinstance(authority_value, Mapping):
        try:
            query_authority = query_authority_from_mapping(authority_value)
        except ValueError as exc:
            authority_issue = str(exc)
        else:
            family = query_authority.family
    sql = str(content.get("generated_sql", "")) or records[0].generated_sql
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except (ParseError, ValueError):
        return None
    if not isinstance(statement, exp.Select):
        return None
    issue = authority_issue or _metric_sql_issue(
        question,
        statement,
        query_authority=query_authority,
    ) or _dimension_issue(
        question,
        statement,
        family=family,
        query_authority=query_authority,
    )
    evidence_ref = records[0].ref
    if issue is None:
        for claim in claims:
            claim_text = str(claim["text"])
            supporting = tuple(
                record
                for record in records
                if record.supports_claim == claim_text.strip()
            )
            if not supporting:
                continue
            for record in supporting:
                matching_rows = _rows_for_claim(
                    family, question, claim_text, record.rows
                )
                issue = (
                    _numeric_issue(claim_text, matching_rows)
                    if matching_rows
                    else "结论维度标签与结构化SQL结果行不一致"
                )
                if issue is not None:
                    evidence_ref = record.ref
                    break
            if issue is not None:
                break
    if issue is None:
        return None
    return _rule_hit(
        "R-01",
        f"问题口径 vs SQL实际口径：{issue}，请重新生成。",
        evidence_ref,
    )


def _normalize_claim_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"[`*_~>#|\[\]()]", "", normalized)
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] not in {"P", "S"}
    )


def _numeric_values(text: str) -> frozenset[Decimal]:
    return frozenset(
        Decimal(token)
        for token in re.findall(_NUMBER_RE, unicodedata.normalize("NFKC", text))
    )


def _numbers_compatible(candidate: str, claim: str) -> bool:
    return _numeric_values(candidate).issubset(_numeric_values(claim))


def _r04_text_declared(candidate_text: str, claim_text: str) -> bool:
    candidate = _normalize_claim_text(candidate_text)
    claim = _normalize_claim_text(claim_text)
    if not candidate or not claim:
        return False
    if candidate in claim or claim in candidate:
        return True
    return SequenceMatcher(
        None,
        candidate,
        claim,
        autojunk=False,
    ).ratio() >= R04_SIMILARITY_THRESHOLD


def _is_incomplete_example_lead(sentence: str) -> bool:
    markers = ("为例", "例如", "比如")
    if not any(marker in sentence for marker in markers):
        return False
    judgment_text = sentence
    for marker in markers:
        judgment_text = judgment_text.replace(marker, "")
    has_predicate = _contains_any(judgment_text, CERTAINTY_PREDICATES)
    has_subject_or_value = re.search(r"\d", judgment_text) is not None or _contains_any(
        judgment_text,
        METRIC_TERMS,
    )
    return not (has_predicate and has_subject_or_value)


def _sentences(body: str) -> tuple[str, ...]:
    without_code = _FENCED_CODE_RE.sub("", body)
    lines = [
        _ORDERED_LIST_MARKER_RE.sub("", line)
        for line in without_code.splitlines()
        if not line.lstrip().startswith("#")
    ]
    text = "\n".join(lines)
    return tuple(
        match.group(0).strip()
        for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text)
        if match.group(0).strip()
    )


def _r02_formula_hit(product: Mapping[str, Any]) -> dict[str, str] | None:
    """Reject a reversed completion-rate formula before probabilistic review."""
    content = _content(product)
    bodies = tuple(
        value
        for field in ("lecture_md", "practice_guide", "guide_md")
        for value in (content.get(field),)
        if isinstance(value, str)
    )
    for body in bodies:
        for sentence in _sentences(body):
            normalized = unicodedata.normalize("NFKC", sentence)
            if not _contains_any(normalized, COMPLETE_RATE_TERMS):
                continue
            if _contains_any(normalized, _FORMULA_NEGATION_TERMS):
                continue
            if _REVERSED_COMPLETE_RATE_RE.search(normalized) is not None:
                return _rule_hit(
                    "R-02",
                    "完成率公式方向错误：必须使用实际量÷计划量，不能反向计算。",
                    _evidence_ref(product),
                )
    return None


def _task_r04_bodies(content: Mapping[str, Any]) -> tuple[str, ...]:
    bodies = [
        value
        for field in ("contextualized_stem", "practice_guide", "guide_md")
        for value in (content.get(field),)
        if isinstance(value, str)
    ]
    questions = content.get("questions")
    if isinstance(questions, list):
        bodies.extend(
            str(question["prompt"])
            for question in questions
            if isinstance(question, Mapping)
            and isinstance(question.get("prompt"), str)
        )
    if not bodies and isinstance(content.get("question"), str):
        bodies.append(str(content["question"]))
    return tuple(dict.fromkeys(bodies))


def _task_r04_numeric_tokens(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", text)
    protected_spans: list[tuple[int, int]] = []
    tokens: set[str] = set()
    for prefix, pattern in (("ship", _SHIP_RE), ("month", _MONTH_RE)):
        for match in pattern.finditer(normalized):
            protected_spans.append(match.span())
            tokens.add(f"{prefix}:{match.group(0).casefold()}")
    for match in re.finditer(_NUMBER_RE, normalized):
        if any(
            match.start() < end and start < match.end()
            for start, end in protected_spans
        ):
            continue
        tokens.add(f"number:{Decimal(match.group(0))}")
    return frozenset(tokens)


def _r04_task_scan(product: Mapping[str, Any]) -> dict[str, str] | None:
    content = _content(product)
    standard_stem = content.get("standard_stem")
    allowed = _task_r04_numeric_tokens(
        standard_stem if isinstance(standard_stem, str) else ""
    )
    for body in _task_r04_bodies(content):
        for sentence in _sentences(body):
            if sentence.endswith(("?", "？")):
                continue
            if _task_r04_numeric_tokens(sentence) - allowed:
                return _rule_hit(
                    "R-04",
                    (
                        f"任务题干中'{sentence.rstrip('。')}'含standard_stem"
                        "不存在的数字参数。"
                    ),
                    _evidence_ref(product),
                )
    return None


def _r04_scan(
    product: Mapping[str, Any],
) -> tuple[dict[str, str] | None, tuple[R04SemanticCheck, ...]]:
    if (
        _payload(product).get("type") in TASK_R04_PAYLOAD_TYPES
        and product.get("claims") == []
    ):
        return _r04_task_scan(product), ()
    content = _content(product)
    bodies = tuple(
        value
        for field in ("lecture_md", "practice_guide", "guide_md")
        for value in (content.get(field),)
        if isinstance(value, str)
    )
    if not bodies:
        return None, ()
    claim_texts = tuple(
        str(claim["text"])
        for claim in _items(product, "claims")
        if isinstance(claim.get("text"), str)
    )
    pending: list[R04SemanticCheck] = []
    for body in bodies:
        for sentence in _sentences(body):
            normalized_sentence = unicodedata.normalize("NFKC", sentence)
            if normalized_sentence.endswith(("?", "？")):
                continue
            if _is_incomplete_example_lead(normalized_sentence):
                continue
            if not _contains_any(normalized_sentence, CERTAINTY_PREDICATES):
                continue
            if re.search(r"\d", normalized_sentence) is None and not _contains_any(
                normalized_sentence, METRIC_TERMS
            ):
                continue
            compatible_claims = tuple(
                claim
                for claim in claim_texts
                if _numbers_compatible(normalized_sentence, claim)
            )
            if not compatible_claims:
                return (
                    _rule_hit(
                        "R-04",
                        f"正文中'{sentence.rstrip('。')}'含claims中不存在的数字。",
                        _evidence_ref(product),
                    ),
                    (),
                )
            declared = any(
                _r04_text_declared(normalized_sentence, claim)
                for claim in compatible_claims
            )
            if not declared:
                pending.append(
                    R04SemanticCheck(
                        sentence=normalized_sentence,
                        claims=compatible_claims,
                    )
                )
    return None, tuple(pending)


def _r04(product: Mapping[str, Any]) -> dict[str, str] | None:
    if _payload(product).get("type") == "lecture_note":
        quote_validation = _content(product).get("quote_validation")
        failed = (
            quote_validation.get("failed")
            if isinstance(quote_validation, Mapping)
            else None
        )
        if (
            isinstance(failed, int)
            and not isinstance(failed, bool)
            and failed > 0
        ):
            return _rule_hit(
                "R-04",
                "讲义包含未通过 KnowledgeAgent 校验的知识库句子编号引用。",
                _evidence_ref(product),
            )
    hit, _ = _r04_scan(product)
    return hit


def _r04_product_key(product: Mapping[str, Any]) -> str:
    content = _content(product)
    return json.dumps(
        {
            "bodies": [
                content[field]
                for field in ("lecture_md", "practice_guide", "guide_md")
                if isinstance(content.get(field), str)
            ],
            "claims": [
                str(claim["text"])
                for claim in _items(product, "claims")
                if isinstance(claim.get("text"), str)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _r05(product: Mapping[str, Any]) -> dict[str, str] | None:
    if _payload(product).get("type") != "sql_result":
        return None
    content = _content(product)
    if content.get("event") != "query_completed":
        return None
    reasons: list[str] = []
    evidence_ref = _evidence_ref(product, "sql_query")
    for evidence in _items(product, "evidence"):
        ref = evidence.get("ref")
        if (
            evidence.get("kind") == "review_rule"
            and isinstance(ref, str)
            and re.fullmatch(r"S-\d+", ref)
        ):
            reasons.append(f"引用了沙箱拒绝记录{ref}")
            evidence_ref = ref
            break
    records = _query_evidence(product)
    if not records:
        reasons.append("query_completed没有可核验的sql_query证据")
    for record in records:
        if record.status != "completed":
            reasons.append(f"SQL证据状态为{record.status or 'missing'}")
            evidence_ref = record.ref
            break
        if record.row_count == 0 or not record.rows:
            reasons.append("SQL证据返回空集")
            evidence_ref = record.ref
            break
    rows_value = content.get("rows")
    rows = (
        [dict(row) for row in rows_value if isinstance(row, Mapping)]
        if isinstance(rows_value, list)
        else []
    )
    family = str(content.get("family", ""))
    if not rows or _result_is_empty(family, rows):
        reasons.append("query_completed正文结果为空")
    generated_sql = content.get("generated_sql")
    if isinstance(generated_sql, str):
        decision = validate_and_rewrite(generated_sql)
        if not decision.allowed:
            reasons.append(f"SQL未通过沙箱{decision.rule_id}")
    else:
        reasons.append("query_completed缺少generated_sql")
    if not reasons:
        return None
    return _rule_hit(
        "R-05",
        f"引用的SQL不能作为数据结论证据：{'；'.join(dict.fromkeys(reasons))}。",
        evidence_ref,
    )


def evaluate_hard_rules(product: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Return all deterministic rule hits in fixed R-01/R-04/R-05 order."""

    if not isinstance(product, Mapping):
        raise ValueError("product must be a mapping")
    return tuple(
        hit
        for evaluator in (_r01, _r04, _r05)
        for hit in (evaluator(product),)
        if hit is not None
    )


def _data_safety_reviews(
    product: Mapping[str, Any],
) -> tuple[tuple[dict[str, str], ...], int]:
    """Re-attest data semantics and SQL safety inside the parallel gate."""
    hits = tuple(
        hit
        for evaluator in (_r01, _r05)
        for hit in (evaluator(product),)
        if hit is not None
    )
    return hits, 2


def _readability_reviews(
    product: Mapping[str, Any],
) -> tuple[tuple[dict[str, str], ...], int]:
    """Reject only severe presentation defects; ordinary style stays non-blocking."""
    content = _content(product)
    bodies = tuple(
        value
        for field in (
            "lecture_md",
            "practice_guide",
            "guide_md",
            "contextualized_stem",
            "question",
        )
        for value in (content.get(field),)
        if isinstance(value, str) and value.strip()
    )
    if not bodies:
        return (), 1
    for body in bodies:
        leaked = _INTERNAL_PRESENTATION_RE.search(body)
        if leaked is not None:
            return (
                (
                    _rule_hit(
                        "R-06",
                        f"面向学员的内容泄露内部协议标识：{leaked.group(0)}。",
                        _evidence_ref(product),
                    ),
                ),
                len(bodies) + 1,
            )
        paragraphs = tuple(
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if paragraphs and max(len(paragraph) for paragraph in paragraphs) > 480:
            return (
                (
                    _rule_hit(
                        "R-06",
                        "面向学员的内容存在超过480字的连续段落，需要分段后重新生成。",
                        _evidence_ref(product),
                    ),
                ),
                len(bodies) + 1,
            )
    return (), len(bodies) + 1


def _call_result(
    llm_call: Callable[..., Any],
    *,
    system: str,
    user_data: Mapping[str, Any],
    schema: Mapping[str, Any],
    validator: Draft202012Validator,
) -> LLMResult:
    result = llm_call(
        model=REVIEW_MODEL,
        system=system,
        user=json.dumps(
            user_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        json_schema=schema,
        temperature=0.1,
    )
    if not isinstance(result, LLMResult):
        raise ValueError("review llm_call must return LLMResult")
    validator.validate(result.data)
    return result


def _r04_semantic_review(
    product: Mapping[str, Any],
    llm_call: Callable[..., Any],
) -> tuple[dict[str, str] | None, tuple[LLMResult, ...], int]:
    hard_hit, checks = _r04_scan(product)
    if hard_hit is not None:
        return hard_hit, (), 0
    results: list[LLMResult] = []
    for check in checks:
        result = _call_result(
            llm_call,
            system=R02_PROMPT,
            user_data={"claim": check.sentence, "quotes": list(check.claims)},
            schema=R02_OUTPUT_SCHEMA,
            validator=_R02_VALIDATOR,
        )
        results.append(result)
        if result.data["supported"]:
            continue
        return (
            _rule_hit(
                "R-04",
                f"正文中'{check.sentence.rstrip('。')}'不是claims中已申报结论的自然改写："
                f"{result.data['reason']}。",
                _evidence_ref(product),
            ),
            tuple(results),
            len(results),
        )
    return None, tuple(results), len(results)


def _r02_reviews(
    product: Mapping[str, Any],
    llm_call: Callable[..., Any],
    *,
    knowledge_chunks: Sequence[KnowledgeChunk] | None = None,
    rebuttal: Mapping[str, Any] | None = None,
) -> tuple[tuple[dict[str, str], ...], tuple[LLMResult, ...], int]:
    del rebuttal  # Re-review never repeats the whole rebuttal once per claim.
    hits: list[dict[str, str]] = []
    results: list[LLMResult] = []
    checks = 0
    formula_hit = _r02_formula_hit(product)
    if formula_hit is not None:
        hits.append(formula_hit)
        checks += 1
    evidence = _items(product, "evidence")
    chunks_by_id = _catalog_by_id(
        tuple(knowledge_chunks)
        if knowledge_chunks is not None
        else _approved_knowledge_chunks()
    )
    for claim in _items(product, "claims"):
        if claim.get("kind") != "fact" or not isinstance(claim.get("text"), str):
            continue
        checks += 1
        claim_text = str(claim["text"])
        status, citations, _, audit_reason = _audit_kb_claim(
            claim_text,
            evidence,
            chunks_by_id,
        )
        if status == "certified":
            continue
        evidence_ref = (
            str(citations[0]["ref"])
            if citations and isinstance(citations[0].get("ref"), str)
            else _evidence_ref(product)
        )
        if status == "invalid":
            hits.append(
                _rule_hit(
                    "R-02",
                    f"结论'{claim_text}'的知识库来源无效：{audit_reason}。",
                    evidence_ref,
                )
            )
            continue

        quotes = [str(item["quote"]) for item in citations]
        user_data: dict[str, Any] = {"claim": claim_text, "quotes": quotes}
        result = _call_result(
            llm_call,
            system=R02_PROMPT,
            user_data=user_data,
            schema=R02_OUTPUT_SCHEMA,
            validator=_R02_VALIDATOR,
        )
        results.append(result)
        if result.data["supported"]:
            continue
        hits.append(
            _rule_hit(
                "R-02",
                f"结论'{claim_text}'未被联合引文支撑：{result.data['reason']}；"
                f"引文={json.dumps(quotes, ensure_ascii=False)}。",
                evidence_ref,
            )
        )
    return tuple(hits), tuple(results), checks


def _follow_up_evidence_review(
    product: Mapping[str, Any],
    llm_call: Callable[..., Any],
    *,
    rebuttal: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str] | None, tuple[LLMResult, ...], int]:
    content = _content(product)
    if (
        product.get("role") != "probe"
        or _payload(product).get("type") != "quiz_set"
        or content.get("event") != "follow_up_question_ready"
    ):
        return None, (), 0

    question = content.get("question")
    standard_stem = content.get("standard_stem")
    evidence = _items(product, "evidence")
    evidence_refs = tuple(
        str(item["ref"])
        for item in evidence
        if isinstance(item.get("ref"), str) and str(item["ref"]).strip()
    )
    declared_refs = _non_empty_strings(content.get("evidence_refs"))
    evidence_quotes = tuple(
        str(item["quote"])
        for item in evidence
        if isinstance(item.get("quote"), str) and str(item["quote"]).strip()
    )
    if (
        not isinstance(question, str)
        or not question.strip()
        or not isinstance(standard_stem, str)
        or not standard_stem.strip()
        or not evidence_refs
        or not evidence_quotes
        or declared_refs != evidence_refs
    ):
        return (
            _rule_hit(
                "R-02",
                "动态追问未与完整证据集合建立精确关联。",
                _evidence_ref(product),
            ),
            (),
            0,
        )

    user_data: dict[str, Any] = {
        "question": question,
        "standard_stem": standard_stem,
        "evidence_quotes": list(evidence_quotes),
    }
    if rebuttal is not None:
        user_data["rebuttal"] = dict(rebuttal)
    result = _call_result(
        llm_call,
        system=FOLLOW_UP_EVIDENCE_PROMPT,
        user_data=user_data,
        schema=R02_OUTPUT_SCHEMA,
        validator=_R02_VALIDATOR,
    )
    if result.data["supported"]:
        return None, (result,), 1
    return (
        _rule_hit(
            "R-02",
            f"动态追问未被绑定证据支撑：{result.data['reason']}。",
            evidence_refs[0],
        ),
        (result,),
        1,
    )


def _non_empty_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _related_to_any(value: str, candidates: Sequence[str]) -> bool:
    key = _match_key(value)
    return bool(key) and any(
        key == candidate_key
        or key in candidate_key
        or candidate_key in key
        for candidate in candidates
        if (candidate_key := _match_key(candidate))
    )


def _filter_related(
    values: Sequence[str], responsibility_scope: Sequence[str]
) -> tuple[str, ...]:
    if not responsibility_scope:
        return tuple(values)
    return tuple(
        value for value in values if _related_to_any(value, responsibility_scope)
    )


def _skill_key(value: str) -> str:
    key = _match_key(value)
    while key:
        for suffix in ("使用", "基础", "能力", "技能"):
            if key.endswith(suffix):
                key = key[: -len(suffix)]
                break
        else:
            break
    return key


def _skill_covered_by(required: str, available: Sequence[str]) -> bool:
    required_key = _skill_key(required)
    return bool(required_key) and any(
        required_key == available_key
        for item in available
        if (available_key := _skill_key(item))
    )


def _r03_review(
    product: Mapping[str, Any],
    llm_call: Callable[..., Any],
    learning_report: Mapping[str, Any] | None,
    student_profile: Mapping[str, Any] | None,
    *,
    learned_knowledge_points: Sequence[str] = (),
    knowledge_chunks: Sequence[KnowledgeChunk] | None = None,
    rebuttal: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str] | None, LLMResult | None, str, int | None]:
    payload_type = _payload(product).get("type")
    if payload_type not in {"lecture_note", "quiz_set", "practice_guide"}:
        return None, None, "none", None
    if not isinstance(learning_report, Mapping):
        raise ValueError("learning_report is required for lecture and task review")
    if not isinstance(student_profile, Mapping):
        raise ValueError("student_profile is required for lecture and task review")
    report_msg_id = learning_report.get("msg_id")
    if not isinstance(report_msg_id, str) or not report_msg_id.strip():
        raise ValueError("learning_report.msg_id must be a non-empty string")
    catalog = (
        tuple(knowledge_chunks)
        if knowledge_chunks is not None
        else _approved_knowledge_chunks()
    )
    learned_points = _non_empty_strings(learned_knowledge_points)
    product_content = _content(product)
    knowledge_point = product_content.get("knowledge_point")
    normalized_point = (
        knowledge_point.strip()
        if isinstance(knowledge_point, str) and knowledge_point.strip()
        else ""
    )
    declared_scope = _non_empty_strings(
        product_content.get("responsibility_scope", ())
    )
    product_difficulty = product_content.get("difficulty")
    catalog_scope = _catalog_responsibility_scope(
        normalized_point,
        str(product_difficulty) if product_difficulty in DIFFICULTY_ORDER else None,
        catalog,
    )
    responsibility_scope = catalog_scope or declared_scope
    if not responsibility_scope and normalized_point:
        responsibility_scope = (normalized_point,)
    scope_is_closed = bool(catalog_scope and declared_scope == catalog_scope)
    grounded_points = _grounded_coverage(product, catalog)
    available_points = tuple(dict.fromkeys((*learned_points, *grounded_points)))

    report_copy = deepcopy(dict(learning_report))
    report_content = _content(report_copy)
    report_blind_spots = _non_empty_strings(report_content.get("blind_spots", ()))
    relevant_blind_spots = _filter_related(
        report_blind_spots, responsibility_scope
    )
    outstanding_blind_spots = tuple(
        item
        for item in relevant_blind_spots
        if not _related_to_any(item, available_points)
    )
    if isinstance(report_content, dict):
        report_content["blind_spots"] = list(outstanding_blind_spots)

    profile_copy = deepcopy(dict(student_profile))
    strengths = _non_empty_strings(profile_copy.get("strengths", ()))
    profile_gaps = _non_empty_strings(profile_copy.get("gaps_prior", ()))
    relevant_profile_gaps = _filter_related(profile_gaps, responsibility_scope)
    profile_copy["gaps_prior"] = [
        item
        for item in relevant_profile_gaps
        if not _related_to_any(item, available_points)
    ]

    report_difficulty = learning_report.get("difficulty")
    if report_difficulty not in DIFFICULTY_ORDER:
        original_report_content = _content(learning_report)
        report_difficulty = original_report_content.get("difficulty")
    explicit_gap = 0
    action = "keep"
    difficulty_comparable = (
        report_difficulty in DIFFICULTY_ORDER
        and product_difficulty in DIFFICULTY_ORDER
    )
    if difficulty_comparable:
        report_index = DIFFICULTY_ORDER.index(str(report_difficulty))
        product_index = DIFFICULTY_ORDER.index(str(product_difficulty))
        explicit_gap = abs(product_index - report_index)
        if explicit_gap > 0:
            action = "step_down" if product_index > report_index else "step_up"

    available_scope = (*strengths, *available_points)
    scope_covered = bool(responsibility_scope) and all(
        _skill_covered_by(point, available_scope)
        for point in responsibility_scope
    )
    if (
        scope_is_closed
        and difficulty_comparable
        and explicit_gap == 0
        and not outstanding_blind_spots
        and scope_covered
    ):
        return None, None, "keep", 0

    user_data: dict[str, Any] = {
        "learning_report": report_copy,
        "student_profile": profile_copy,
        "product": {
            "agent": product.get("agent"),
            "payload_type": payload_type,
            "content": dict(product_content),
            "claims": [dict(claim) for claim in _items(product, "claims")],
        },
        "structured_authority": {
            "knowledge_point": normalized_point,
            "responsibility_scope": list(responsibility_scope),
            "learned_knowledge_points": list(learned_points),
            "relevant_blind_spots": list(outstanding_blind_spots),
            "strengths": list(strengths),
        },
    }
    if rebuttal is not None:
        user_data["rebuttal"] = dict(rebuttal)
    result = _call_result(
        llm_call,
        system=R03_PROMPT,
        user_data=user_data,
        schema=R03_OUTPUT_SCHEMA,
        validator=_R03_VALIDATOR,
    )
    semantic_scaffolded = (
        not outstanding_blind_spots
        or bool(result.data["blind_spots_scaffolded"])
    )
    required_skills = _non_empty_strings(result.data["required_skills"])
    allowed_prerequisites = tuple(
        point
        for point in responsibility_scope
        if not normalized_point or not _related_to_any(point, (normalized_point,))
    )
    bounded_required_skills = tuple(
        skill
        for skill in required_skills
        if _related_to_any(skill, allowed_prerequisites)
    )
    available_skills = strengths + available_points
    missing_skills = tuple(
        skill
        for skill in bounded_required_skills
        if not _skill_covered_by(skill, available_skills)
    )
    matched = explicit_gap == 0 and semantic_scaffolded and not missing_skills
    if matched:
        return None, result, "keep", 0

    difficulty_gap = explicit_gap or 1
    if explicit_gap == 0:
        action = "step_down"
    reasons = [str(result.data["reason"])]
    if not semantic_scaffolded:
        reasons.append(
            "责任范围内盲区未铺垫：" + "、".join(outstanding_blind_spots)
        )
    if missing_skills:
        reasons.append(
            "前置技能未见于strengths或已学内容：" + "、".join(missing_skills)
        )
    if explicit_gap:
        reasons.append(f"显式难度档距离={explicit_gap}")
    reason = "；".join(reasons)
    return (
        _rule_hit(
            "R-03",
            "结构化责任范围/强项 vs 产物语义与显式难度："
            f"{reason}（difficulty_gap={difficulty_gap}）。",
            report_msg_id,
        ),
        result,
        action,
        difficulty_gap,
    )


def _usage_sum(results: Sequence[LLMResult]) -> TokenUsage:
    usage = TokenUsage(0, 0, 0)
    for result in results:
        usage = usage + result.token_usage
    return usage


def _verdict_draft(
    *,
    trace_id: str,
    role: str,
    product: Mapping[str, Any],
    hits: Sequence[Mapping[str, str]],
    decision: str,
    difficulty_action: str,
    clock: Callable[[], datetime],
    llm_results: Sequence[LLMResult] = (),
    r02_checks: int = 0,
    r03_checked: bool = False,
    specialist_reviews: Sequence[Mapping[str, Any]] = (),
    started: float | None = None,
) -> dict[str, Any]:
    reviewed_msg_id = str(product["msg_id"])
    payload_type = str(_payload(product)["type"])
    llm_latency_ms = sum(result.latency_ms for result in llm_results)
    content: dict[str, Any] = {
        "event": "review_complete",
        "reviewed_payload_type": payload_type,
        "reviewed_msg_id": reviewed_msg_id,
    }
    if specialist_reviews:
        content["specialist_reviews"] = [dict(item) for item in specialist_reviews]
        content["arbitration"] = {
            "agent": "review",
            "mode": "deterministic_rule_table",
            "decision": decision,
        }
    draft: dict[str, Any] = {
        "trace_id": trace_id,
        "agent": "review",
        "role": role,
        "payload": {"type": "review_verdict", "content": content},
        "evidence": [
            {
                "kind": "review_rule",
                "ref": hit["rule_id"],
                "quote": hit["reason"],
            }
            for hit in hits
        ],
        "claims": [],
        "verdict": {
            "decision": decision,
            "rule_hits": [dict(hit) for hit in hits],
            "difficulty_action": difficulty_action,
        },
        "timestamp": clock().isoformat(),
    }
    if llm_results:
        content.update(
            {
                "llm_latency_ms": llm_latency_ms,
                "r02_checks": r02_checks,
                "r03_checked": r03_checked,
            }
        )
        elapsed_ms = (
            round(max(0.0, perf_counter() - started) * 1000)
            if started is not None
            else 0
        )
        draft.update(
            {
                "model": REVIEW_MODEL,
                "latency_ms": max(elapsed_ms, llm_latency_ms),
                "token_usage": _usage_sum(llm_results).as_dict(),
            }
        )
    return draft


def _original_hits(message: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    verdict = message.get("verdict")
    value = verdict.get("rule_hits") if isinstance(verdict, Mapping) else None
    if not isinstance(value, list):
        return ()
    hits: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        rule_id = item.get("rule_id")
        reason = item.get("reason")
        evidence_ref = item.get("evidence_ref")
        if all(
            isinstance(field, str) and field.strip()
            for field in (rule_id, reason, evidence_ref)
        ):
            hits.append(
                {
                    "rule_id": str(rule_id),
                    "reason": str(reason),
                    "evidence_ref": str(evidence_ref),
                }
            )
    return tuple(hits)


def _rebuttal_content(rebuttal: Mapping[str, Any]) -> Mapping[str, Any]:
    return _content(rebuttal)


class ReviewAgent:
    """Review one bus-enveloped product; hard hits never call the model."""

    def __init__(
        self,
        trace_id: str,
        llm_call: Callable[..., Any] = call_llm,
        clock: Callable[[], datetime] | None = None,
        knowledge_chunks: Sequence[KnowledgeChunk] | None = None,
        parallel_executor: ParallelStageExecutor | None = None,
    ) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        self._trace_id = trace_id
        self._llm_call = llm_call
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Production wiring opts into bounded concurrency explicitly. Direct
        # construction remains deterministic for narrow unit-level callers.
        self._parallel_executor = parallel_executor or ParallelStageExecutor(
            max_concurrency=1
        )
        self._knowledge_chunks = (
            tuple(knowledge_chunks)
            if knowledge_chunks is not None
            else _approved_knowledge_chunks()
        )
        self._evidence_review_agent = EvidenceReviewAgent(
            trace_id,
            evaluator=lambda product: _r02_reviews(
                product,
                self._llm_call,
                knowledge_chunks=self._knowledge_chunks,
            ),
        )
        self._pedagogy_review_agent = PedagogyReviewAgent(
            trace_id,
            evaluator=lambda product, report, profile, learned: _r03_review(
                product,
                self._llm_call,
                report,
                profile,
                learned_knowledge_points=learned,
                knowledge_chunks=self._knowledge_chunks,
            ),
        )
        self._data_safety_review_agent = DataSafetyReviewAgent(
            trace_id,
            evaluator=_data_safety_reviews,
        )
        self._readability_review_agent = ReadabilityReviewAgent(
            trace_id,
            evaluator=_readability_reviews,
        )
        self._arbiter = DeterministicReviewArbiter()
        self._r04_preflight_results: dict[
            str,
            tuple[tuple[LLMResult, ...], int],
        ] = {}

    def grounded_coverage(
        self,
        product: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Return only coverage proven by authentic atomic facts in a lecture."""

        if not isinstance(product, Mapping):
            raise ValueError("product must be a mapping")
        return _grounded_coverage(product, self._knowledge_chunks)

    def preflight_r04(self, product: Mapping[str, Any]) -> dict[str, str] | None:
        """Run all R-04 layers before persistence and retain approved LLM checks."""

        if not isinstance(product, Mapping):
            raise ValueError("product must be a mapping")
        hit, results, checks = _r04_semantic_review(product, self._llm_call)
        if hit is None and results:
            self._r04_preflight_results[_r04_product_key(product)] = (
                results,
                checks,
            )
        return hit

    def review(
        self,
        product: Mapping[str, Any],
        *,
        learning_report: Mapping[str, Any] | None = None,
        student_profile: Mapping[str, Any] | None = None,
        learned_knowledge_points: Sequence[str] = (),
        activity_observer: Callable[[str, Mapping[str, Any]], None] | None = None,
        learning_contract: LearningContract | None = None,
    ) -> dict[str, Any]:
        if not isinstance(product, Mapping):
            raise ValueError("product must be a mapping")
        reviewed_msg_id = product.get("msg_id")
        payload_type = _payload(product).get("type")
        if not isinstance(reviewed_msg_id, str) or not reviewed_msg_id.strip():
            raise ValueError("product.msg_id must be a non-empty string")
        if not isinstance(payload_type, str) or not payload_type.strip():
            raise ValueError("product payload.type must be a non-empty string")
        if learning_contract is not None:
            learning_contract.validate_context(
                profile=student_profile,
                diagnosis=learning_report,
            )
        hard_hits = evaluate_hard_rules(product)
        if hard_hits:
            return _verdict_draft(
                trace_id=self._trace_id,
                role="verdict",
                product=product,
                hits=hard_hits,
                decision="reject",
                difficulty_action="none",
                clock=self._clock,
            )

        started = perf_counter()
        preflight = self._r04_preflight_results.pop(_r04_product_key(product), None)
        if preflight is None:
            r04_hit, r04_results, r04_checks = _r04_semantic_review(
                product,
                self._llm_call,
            )
        else:
            r04_results, r04_checks = preflight
            r04_hit = None
        if r04_hit is not None:
            return _verdict_draft(
                trace_id=self._trace_id,
                role="verdict",
                product=product,
                hits=(r04_hit,),
                decision="reject",
                difficulty_action="none",
                clock=self._clock,
                llm_results=r04_results,
                r02_checks=r04_checks,
                r03_checked=False,
                started=started,
            )
        follow_up_hit, follow_up_results, follow_up_checks = (
            _follow_up_evidence_review(product, self._llm_call)
        )
        if follow_up_hit is not None:
            return _verdict_draft(
                trace_id=self._trace_id,
                role="verdict",
                product=product,
                hits=(follow_up_hit,),
                decision="reject",
                difficulty_action="none",
                clock=self._clock,
                llm_results=follow_up_results,
                r02_checks=r04_checks + follow_up_checks,
                r03_checked=False,
                started=started,
            )
        contract_id = (
            learning_contract.contract_id
            if learning_contract is not None
            else f"legacy-{self._trace_id}"
        )
        artifact = ArtifactEnvelope.from_message(
            product,
            contract_id=contract_id,
        )
        stage = ParallelStage[Any](
            stage_id="quality-review-axes",
            branches=(
                BranchSpec(
                    branch_id=self._evidence_review_agent.agent_id,
                    task=lambda: self._evidence_review_agent.review(artifact),
                ),
                BranchSpec(
                    branch_id=self._pedagogy_review_agent.agent_id,
                    task=lambda: self._pedagogy_review_agent.review(
                        artifact,
                        learning_report=learning_report,
                        student_profile=student_profile,
                        learned_knowledge_points=learned_knowledge_points,
                    ),
                ),
                BranchSpec(
                    branch_id=self._data_safety_review_agent.agent_id,
                    task=lambda: self._data_safety_review_agent.review(artifact),
                ),
                BranchSpec(
                    branch_id=self._readability_review_agent.agent_id,
                    task=lambda: self._readability_review_agent.review(artifact),
                ),
            ),
        )
        is_parallel = self._parallel_executor.max_concurrency > 1
        if is_parallel and activity_observer is not None:
            for agent_id, rule_id, label in (
                ("evidence_review", "R-02", "正在独立核验事实与证据"),
                ("pedagogy_review", "R-03", "正在独立审核难度与岗位适配"),
                ("data_safety_review", "R-05", "正在独立校验数据口径与安全边界"),
                ("readability_review", "R-06", "正在独立检查表达与可读性"),
            ):
                activity_observer(
                    "specialist_review_started",
                    {
                        "agent": agent_id,
                        "rule_id": rule_id,
                        "label": label,
                        "stage_id": stage.stage_id,
                        "contract_id": contract_id,
                        "artifact_id": artifact.artifact_id,
                    },
                )
            activity_observer(
                "parallel_review_started",
                {
                    "stage_id": stage.stage_id,
                    "axes": ["R-02", "R-03", "R-05", "R-06"],
                    "agents": [
                        "evidence_review",
                        "pedagogy_review",
                        "data_safety_review",
                        "readability_review",
                    ],
                    "fan_out": 4,
                    "aggregation": "pending",
                    "contract_id": contract_id,
                    "artifact_id": artifact.artifact_id,
                },
            )

        def observe_stage(event: str, details: Mapping[str, Any]) -> None:
            if not is_parallel or activity_observer is None:
                return
            if event not in {"branch_completed", "branch_failed"}:
                return
            branch_id = details.get("branch_id")
            specialist_meta = {
                "evidence_review": ("R-02", "事实与证据核验完成"),
                "pedagogy_review": ("R-03", "难度与岗位适配审核完成"),
                "data_safety_review": ("R-05", "数据口径与安全边界校验完成"),
                "readability_review": ("R-06", "表达与可读性检查完成"),
            }
            if branch_id not in specialist_meta:
                return
            rule_id, label = specialist_meta[str(branch_id)]
            activity_observer(
                "specialist_review_completed",
                {
                    **dict(details),
                    "agent": branch_id,
                    "rule_id": rule_id,
                    "status": "done" if event == "branch_completed" else "blocked",
                    "label": label,
                    "contract_id": contract_id,
                    "artifact_id": artifact.artifact_id,
                },
            )

        stage_result = self._parallel_executor.execute(
            stage,
            correlation_id=reviewed_msg_id,
            observer=observe_stage,
        ).require_success()
        evidence_result = stage_result.require("evidence_review")
        pedagogy_result = stage_result.require("pedagogy_review")
        data_safety_result = stage_result.require("data_safety_review")
        readability_result = stage_result.require("readability_review")
        arbitration = self._arbiter.decide(
            evidence_result,
            pedagogy_result,
            data_safety_result,
            readability_result,
        )
        r02_results = evidence_result.llm_results
        r02_checks = evidence_result.checks
        r03_result = pedagogy_result.llm_result
        difficulty_action = arbitration.difficulty_action
        if is_parallel and activity_observer is not None:
            activity_observer(
                "parallel_review_completed",
                {
                    "stage_id": stage.stage_id,
                    "axes": ["R-02", "R-03", "R-05", "R-06"],
                    "agents": [
                        "evidence_review",
                        "pedagogy_review",
                        "data_safety_review",
                        "readability_review",
                    ],
                    "r02_checks": r02_checks,
                    "r03_checked": r03_result is not None,
                    "fan_out": 4,
                    "aggregation": "deterministic",
                    "parallel_elapsed_ms": stage_result.elapsed_ms,
                    "correlation_id": stage_result.correlation_id,
                    "branches": stage_result.summary()["branches"],
                    "contract_id": contract_id,
                    "artifact_id": artifact.artifact_id,
                },
            )
        hits = tuple(hit.as_dict() for hit in arbitration.hits)
        llm_results = (
            r04_results
            + follow_up_results
            + r02_results
            + (() if r03_result is None else (r03_result,))
        )
        decision = arbitration.decision
        return _verdict_draft(
            trace_id=self._trace_id,
            role="verdict",
            product=product,
            hits=hits,
            decision=decision,
            difficulty_action=difficulty_action,
            clock=self._clock,
            llm_results=llm_results,
            r02_checks=r04_checks + follow_up_checks + r02_checks,
            r03_checked=r03_result is not None,
            specialist_reviews=(
                {
                    "agent": evidence_result.agent_id,
                    "rule_id": "R-02",
                    "artifact_id": evidence_result.artifact_id,
                    "contract_id": evidence_result.contract_id,
                    "status": "completed",
                    "checks": evidence_result.checks,
                    "hit_count": len(evidence_result.hits),
                },
                {
                    "agent": pedagogy_result.agent_id,
                    "rule_id": "R-03",
                    "artifact_id": pedagogy_result.artifact_id,
                    "contract_id": pedagogy_result.contract_id,
                    "status": "completed",
                    "checked": pedagogy_result.llm_result is not None,
                    "hit_count": 0 if pedagogy_result.hit is None else 1,
                },
                {
                    "agent": data_safety_result.agent_id,
                    "rule_id": "R-05",
                    "artifact_id": data_safety_result.artifact_id,
                    "contract_id": data_safety_result.contract_id,
                    "status": "completed",
                    "checks": data_safety_result.checks,
                    "hit_count": len(data_safety_result.hits),
                },
                {
                    "agent": readability_result.agent_id,
                    "rule_id": "R-06",
                    "artifact_id": readability_result.artifact_id,
                    "contract_id": readability_result.contract_id,
                    "status": "completed",
                    "checks": readability_result.checks,
                    "hit_count": len(readability_result.hits),
                },
            ),
            started=started,
        )

    def re_review(
        self,
        product: Mapping[str, Any],
        original_verdict: Mapping[str, Any],
        rebuttal: Mapping[str, Any],
        *,
        learning_report: Mapping[str, Any] | None = None,
        student_profile: Mapping[str, Any] | None = None,
        learned_knowledge_points: Sequence[str] = (),
        learning_contract: LearningContract | None = None,
    ) -> dict[str, Any]:
        if not all(
            isinstance(item, Mapping)
            for item in (product, original_verdict, rebuttal)
        ):
            raise ValueError("product, original_verdict, and rebuttal must be mappings")
        if learning_contract is not None:
            learning_contract.validate_context(
                profile=student_profile,
                diagnosis=learning_report,
            )
        product_msg_id = product.get("msg_id")
        if not isinstance(product_msg_id, str) or not product_msg_id.strip():
            raise ValueError("product.msg_id must be a non-empty string")
        product_type = _payload(product).get("type")
        original_msg_id = original_verdict.get("msg_id")
        original_content = _content(original_verdict)
        if (
            product.get("trace_id") != self._trace_id
            or product.get("role") not in {"produce", "probe"}
            or not isinstance(product_type, str)
            or original_verdict.get("trace_id") != self._trace_id
            or original_verdict.get("agent") != "review"
            or original_verdict.get("role") != "verdict"
            or _payload(original_verdict).get("type") != "review_verdict"
            or not isinstance(original_msg_id, str)
            or not original_msg_id.strip()
            or original_content.get("reviewed_msg_id") != product_msg_id
            or original_content.get("reviewed_payload_type") != product_type
        ):
            raise ValueError("original verdict association is invalid")
        original_data = original_verdict.get("verdict")
        if not isinstance(original_data, Mapping) or original_data.get("decision") != "reject":
            raise ValueError("original_verdict must be a reject verdict")
        hits = _original_hits(original_verdict)
        if not hits:
            raise ValueError("original_verdict must contain rule_hits")
        rebuttal_content = _rebuttal_content(rebuttal)
        retry = rebuttal.get("retry")
        if (
            rebuttal.get("trace_id") != self._trace_id
            or rebuttal.get("agent") != product.get("agent")
            or rebuttal.get("role") != "rebuttal"
            or _payload(rebuttal).get("type") != "rebuttal_case"
            or rebuttal_content.get("product_msg_id") != product_msg_id
            or rebuttal_content.get("verdict_msg_id") != original_msg_id
            or not isinstance(retry, Mapping)
            or retry.get("in_reply_to") != product_msg_id
        ):
            raise ValueError("rebuttal association is invalid")
        concede = rebuttal_content.get("concede")
        if not isinstance(concede, bool):
            raise ValueError("rebuttal concede must be boolean")
        rule_ids = frozenset(hit["rule_id"] for hit in hits)
        defensible = rule_ids.issubset({"R-02", "R-03"})
        if not concede and defensible:
            defense_text = rebuttal_content.get("rebuttal")
            evidence_refs = rebuttal_content.get("evidence_refs")
            product_refs = {
                str(item["ref"])
                for item in _items(product, "evidence")
                if isinstance(item.get("ref"), str)
            }
            if (
                not isinstance(defense_text, str)
                or not defense_text.strip()
                or not isinstance(evidence_refs, list)
                or not evidence_refs
                or any(
                    not isinstance(ref, str) or ref not in product_refs
                    for ref in evidence_refs
                )
            ):
                raise ValueError(
                    "non-conceding rebuttal must cite bounded product evidence"
                )
        if concede or not defensible:
            return _verdict_draft(
                trace_id=self._trace_id,
                role="re_verdict",
                product=product,
                hits=hits,
                decision="reject",
                difficulty_action=str(original_data.get("difficulty_action", "none")),
                clock=self._clock,
            )

        current_hard_hits = evaluate_hard_rules(product)
        if current_hard_hits:
            return _verdict_draft(
                trace_id=self._trace_id,
                role="re_verdict",
                product=product,
                hits=current_hard_hits,
                decision="reject",
                difficulty_action="none",
                clock=self._clock,
            )

        started = perf_counter()
        preflight = self._r04_preflight_results.pop(_r04_product_key(product), None)
        if preflight is None:
            r04_hit, r04_results, r04_checks = _r04_semantic_review(
                product,
                self._llm_call,
            )
        else:
            r04_results, r04_checks = preflight
            r04_hit = None
        if r04_hit is not None:
            return _verdict_draft(
                trace_id=self._trace_id,
                role="re_verdict",
                product=product,
                hits=(r04_hit,),
                decision="reject",
                difficulty_action="none",
                clock=self._clock,
                llm_results=r04_results,
                r02_checks=r04_checks,
                r03_checked=False,
                started=started,
            )
        r02_hits: tuple[dict[str, str], ...] = ()
        r02_results: tuple[LLMResult, ...] = ()
        r02_checks = 0
        if "R-02" in rule_ids:
            follow_up_hit, follow_up_results, follow_up_checks = (
                _follow_up_evidence_review(
                    product,
                    self._llm_call,
                    rebuttal=rebuttal,
                )
            )
            claim_hits, claim_results, claim_checks = _r02_reviews(
                product,
                self._llm_call,
                knowledge_chunks=self._knowledge_chunks,
                rebuttal=rebuttal,
            )
            r02_hits = (
                (() if follow_up_hit is None else (follow_up_hit,))
                + claim_hits
            )
            r02_results = follow_up_results + claim_results
            r02_checks = follow_up_checks + claim_checks
            if r02_checks == 0:
                r02_hits = tuple(hit for hit in hits if hit["rule_id"] == "R-02")

        r03_hit: dict[str, str] | None = None
        r03_result: LLMResult | None = None
        difficulty_action = "none"
        if "R-03" in rule_ids:
            r03_hit, r03_result, difficulty_action, _ = _r03_review(
                product,
                self._llm_call,
                learning_report,
                student_profile,
                learned_knowledge_points=learned_knowledge_points,
                knowledge_chunks=self._knowledge_chunks,
                rebuttal=rebuttal,
            )
            if r03_result is None and difficulty_action == "none":
                r03_hit = next(hit for hit in hits if hit["rule_id"] == "R-03")

        new_hits = r02_hits + (() if r03_hit is None else (r03_hit,))
        llm_results = (
            r04_results
            + r02_results
            + (() if r03_result is None else (r03_result,))
        )
        return _verdict_draft(
            trace_id=self._trace_id,
            role="re_verdict",
            product=product,
            hits=new_hits,
            decision="approve" if not new_hits else "reject",
            difficulty_action=difficulty_action,
            clock=self._clock,
            llm_results=llm_results,
            r02_checks=r04_checks + r02_checks,
            r03_checked=r03_result is not None,
            started=started,
        )
