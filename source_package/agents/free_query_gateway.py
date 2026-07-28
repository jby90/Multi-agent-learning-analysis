"""Explicit boundary for untrusted external free-query text."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Any, Literal
import unicodedata
from uuid import uuid4

import sqlglot
from sqlglot import exp

from agents.domain_config import DomainConfig, active_domain_config
from agents.sandbox import validate_and_rewrite
from agents.text2sql_generation import (
    Text2SQLGenerationCoordinator,
    Text2SQLGenerationResult,
)
from agents.verification_agent import VerificationAgent
from orchestrator.llm import TokenUsage


BoundaryDecision = Literal["allow", "refuse", "clarify"]

SUPPORTED_PROCESSES = ("YCL", "ZZTP", "AZTP")
PROCESS_PATTERNS: dict[str, re.Pattern[str]] = {
    "YCL": re.compile(r"YCL|预处理", re.IGNORECASE),
    "ZZTP": re.compile(r"ZZTP|制作托盘|托盘制作", re.IGNORECASE),
    "AZTP": re.compile(r"AZTP|安装托盘|托盘安装", re.IGNORECASE),
}
PROCESS_LABELS = {
    "YCL": "YCL",
    "ZZTP": "ZZTP",
    "AZTP": "AZTP",
}

_ATTACK_RE = re.compile(
    r"(?:忽略|无视|覆盖).{0,16}(?:指令|规则|提示词)"
    r"|(?:绕过|跳过|关闭).{0,16}(?:审核|限制|规则|安全|沙箱)"
    r"|(?:假装|扮演).{0,16}(?:无限制|没有限制|管理员|系统)"
    r"|(?:系统提示词|隐藏规则|数据库(?:账号|密码)|账号密码|API\s*KEY|密钥)"
    r"|family.{0,16}(?:伪装|强制|返回)",
    re.IGNORECASE,
)
_SQL_ATTACK_RE = re.compile(
    r"(?:;|--|/\*|\*/|['\"]\s*;?)\s*"
    r"(?:DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE)\b"
    r"|[\"']?sql[\"']?\s*:\s*[\"']?(?:DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE)",
    re.IGNORECASE,
)
_GATEWAY_SCOPE_FIELDS = frozenset(
    {"unsupported_literals", "bounded_phrases", "identity_message"}
)
_BOUNDED_PHRASE_FIELDS = frozenset({"prefix", "suffix", "max_gap"})


def _gateway_scope(
    domain_config: DomainConfig,
) -> tuple[re.Pattern[str], str]:
    scope = domain_config.gateway_scope
    if set(scope) != _GATEWAY_SCOPE_FIELDS:
        raise ValueError(
            f"gateway scope fields must be exactly {sorted(_GATEWAY_SCOPE_FIELDS)}"
        )

    identity_message = scope["identity_message"]
    if not isinstance(identity_message, str) or not identity_message.strip():
        raise ValueError("gateway identity_message must be a non-empty string")

    literals = scope["unsupported_literals"]
    bounded_phrases = scope["bounded_phrases"]
    if not isinstance(literals, tuple):
        raise ValueError("gateway unsupported_literals must be an array")
    if not isinstance(bounded_phrases, tuple):
        raise ValueError("gateway bounded_phrases must be an array")

    expressions: list[str] = []
    seen_literals: set[str] = set()
    for literal in literals:
        if not isinstance(literal, str) or not literal.strip() or len(literal) > 64:
            raise ValueError(
                "gateway unsupported literals must be non-empty strings of at most 64 characters"
            )
        folded = literal.casefold()
        if folded in seen_literals:
            raise ValueError("gateway unsupported literals must be unique")
        seen_literals.add(folded)
        expressions.append(re.escape(literal))

    for phrase in bounded_phrases:
        if not isinstance(phrase, Mapping) or set(phrase) != _BOUNDED_PHRASE_FIELDS:
            raise ValueError(
                "gateway bounded phrase fields must be exactly "
                f"{sorted(_BOUNDED_PHRASE_FIELDS)}"
            )
        prefix = phrase["prefix"]
        suffix = phrase["suffix"]
        max_gap = phrase["max_gap"]
        if (
            not isinstance(prefix, str)
            or not prefix
            or len(prefix) > 32
            or not isinstance(suffix, str)
            or not suffix
            or len(suffix) > 32
        ):
            raise ValueError(
                "gateway bounded phrase prefix and suffix must be non-empty strings "
                "of at most 32 characters"
            )
        if (
            not isinstance(max_gap, int)
            or isinstance(max_gap, bool)
            or not 0 <= max_gap <= 8
        ):
            raise ValueError("gateway bounded phrase max_gap must be an integer from 0 to 8")
        expressions.append(
            rf"{re.escape(prefix)}.{{0,{max_gap}}}{re.escape(suffix)}"
        )

    unsupported_pattern = "|".join(expressions) if expressions else r"(?!x)x"
    return re.compile(unsupported_pattern, re.IGNORECASE), identity_message


_UNSUPPORTED_CAPABILITY_RE, _IDENTITY_MESSAGE = _gateway_scope(
    active_domain_config()
)
_COMPLEX_CAUSAL_RE = re.compile(r"为什么|是不是因为|原因|归因|导致|关系(?:是|为|如何|什么)")
_PLURAL_PROCESS_RE = re.compile(r"各工序|所有工序|全部工序|三道工序")
_SHIP_RE = re.compile(r"(?<![A-Za-z0-9])H\d{4}(?![A-Za-z0-9])", re.IGNORECASE)
_ISO_MONTH_RE = re.compile(r"(?P<year>20\d{2})[-/.](?P<month>0?[1-9]|1[0-2])")
_CN_MONTH_RE = re.compile(
    r"(?:(?P<year>20\d{2})年)?"
    r"(?P<month>1[0-2]|0?[1-9]|十二|十一|十|九|八|七|六|五|四|三|二|一)月"
)
_CHINESE_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}

@dataclass(frozen=True, slots=True)
class FreeQuerySpec:
    """Validated intent contract emitted only by the external boundary."""

    decision: BoundaryDecision
    original_question: str
    canonical_question: str | None
    family: str
    output_columns: tuple[str, ...]
    ship_no: str | None = None
    process_codes: tuple[str, ...] = ()
    start_date: str | None = None
    end_date: str | None = None
    student_message: str | None = None


@dataclass(frozen=True, slots=True)
class _Intent:
    metric: str | None
    dual_values: bool
    explicit_dual_values: bool
    deviation: bool
    completion: bool
    high_risk: bool


def _normalized(question: str) -> str:
    value = unicodedata.normalize("NFKC", question).strip()
    return re.sub(r"\s+", " ", value)


def _process_codes(question: str) -> tuple[str, ...]:
    positions: list[tuple[int, str]] = []
    for code, pattern in PROCESS_PATTERNS.items():
        first = pattern.search(question)
        if first is not None:
            positions.append((first.start(), code))
    if _PLURAL_PROCESS_RE.search(question):
        return SUPPORTED_PROCESSES
    return tuple(code for _, code in sorted(positions))


def _only_process(question: str) -> str | None:
    for code, pattern in PROCESS_PATTERNS.items():
        if re.search(
            rf"(?:只看|仅看|只查|仅查).{{0,8}}(?:{pattern.pattern})",
            question,
            flags=re.IGNORECASE,
        ):
            return code
    return None


def _month_number(raw: str) -> int:
    return _CHINESE_MONTHS.get(raw, int(raw) if raw.isdigit() else 0)


def _months(question: str) -> tuple[tuple[int, int], ...]:
    found: list[tuple[int, int, int]] = []
    occupied: list[tuple[int, int]] = []
    for match in _ISO_MONTH_RE.finditer(question):
        found.append((match.start(), int(match.group("year")), int(match.group("month"))))
        occupied.append(match.span())
    for match in _CN_MONTH_RE.finditer(question):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        found.append(
            (
                match.start(),
                int(match.group("year") or "2025"),
                _month_number(match.group("month")),
            )
        )
    ordered: list[tuple[int, int]] = []
    for _, year, month in sorted(found):
        value = (year, month)
        if value not in ordered:
            ordered.append(value)
    return tuple(ordered)


def _month_start(value: tuple[int, int]) -> str:
    return f"{value[0]:04d}-{value[1]:02d}-01"


def _next_month(value: tuple[int, int]) -> str:
    year, month = value
    if month == 12:
        return date(year + 1, 1, 1).isoformat()
    return date(year, month + 1, 1).isoformat()


def _month_label(value: tuple[int, int]) -> str:
    return f"{value[0]:04d}-{value[1]:02d}"


def _intent(question: str) -> _Intent:
    deviation = bool(
        re.search(
            r"偏差率|偏差多少|相对计划.{0,8}(?:差|偏差)"
            r"|实际.{0,8}(?:和|与|比|相对).{0,8}计划.{0,8}差多少"
            r"|计划.{0,8}(?:和|与|比|相对).{0,8}实际.{0,8}差多少",
            question,
        )
    )
    plan = bool(re.search(r"计划量|计划完成|原计划|原本|计划", question))
    actual = bool(re.search(r"实际量|实际完成|实际做|做了多少|实际.{0,6}多少", question))
    explicit_dual = plan and actual and bool(
        re.search(r"分别|各自|各是多少|原本.{0,16}实际|计划.{0,16}实际.{0,8}(?:各|分别)", question)
    )
    completion = bool(re.search(r"完成率|达成率|完成情况|进度|做得怎么样|完成得怎么样", question))
    high_risk = bool(re.search(r"高风险记录|严重预警|风险记录", question))
    if deviation:
        metric = "deviation_rate"
    elif high_risk:
        metric = "high_risk_rows"
    elif completion:
        metric = "complete_rate"
    elif plan and actual:
        metric = "plan_actual"
    elif plan:
        metric = "plan_qty"
    elif actual:
        metric = "actual_qty"
    else:
        metric = None
    return _Intent(
        metric=metric,
        dual_values=plan and actual,
        explicit_dual_values=explicit_dual,
        deviation=deviation,
        completion=completion,
        high_risk=high_risk,
    )


def _refuse(original: str, message: str = _IDENTITY_MESSAGE) -> FreeQuerySpec:
    return FreeQuerySpec(
        decision="refuse",
        original_question=original,
        canonical_question=None,
        family="OUT_OF_SCOPE",
        output_columns=(),
        student_message=message,
    )


def _clarify(original: str, message: str) -> FreeQuerySpec:
    return FreeQuerySpec(
        decision="clarify",
        original_question=original,
        canonical_question=None,
        family="OUT_OF_SCOPE",
        output_columns=(),
        student_message=message,
    )


class FreeQueryBoundary:
    """Map external text to one closed query contract or fail closed."""

    def __init__(self, domain_config: DomainConfig | None = None) -> None:
        self._unsupported_capability_re, self._identity_message = _gateway_scope(
            domain_config or active_domain_config()
        )

    @property
    def identity_message(self) -> str:
        return self._identity_message

    def classify(self, question: str) -> FreeQuerySpec:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        original = question.strip()
        normalized = _normalized(question)
        upper = normalized.upper()

        if _ATTACK_RE.search(normalized) or _SQL_ATTACK_RE.search(normalized):
            return _refuse(original, self._identity_message)
        if self._unsupported_capability_re.search(normalized):
            return _refuse(original, self._identity_message)

        processes = _process_codes(upper)
        only_process = _only_process(upper)
        if only_process is not None and any(
            process != only_process for process in processes
        ):
            return _clarify(
                original,
                "题目中的工序条件存在矛盾，请明确只查询哪个工序。",
            )
        if "工序" in normalized and not processes and not _PLURAL_PROCESS_RE.search(normalized):
            return _refuse(
                original,
                "该工序不在数据范围；支持查询YCL、ZZTP和AZTP工序。",
            )

        intent = _intent(normalized)
        if intent.metric is None:
            return _refuse(original, self._identity_message)
        if intent.deviation and intent.explicit_dual_values:
            return _clarify(
                original,
                "问题同时要求计划量、实际量和差值，请明确先查询两个原始量还是偏差率。",
            )
        if _COMPLEX_CAUSAL_RE.search(normalized) and len(processes) < 2:
            return _clarify(
                original,
                "该问题包含查询与归因等复杂诉求，请拆解为数据查询和原因分析两步。",
            )

        ship_match = _SHIP_RE.search(upper)
        ship_no = ship_match.group(0).upper() if ship_match else None
        months = _months(normalized)
        responsibility = bool(re.search(r"责任单元|车间|工区", normalized))
        across_ships = bool(re.search(r"各船|所有船|全部船|按船|船舶.{0,6}(?:排名|对比)", normalized))
        trend = len(months) >= 2 or bool(re.search(r"每月|趋势|走势|逐月", normalized))

        if responsibility:
            family = "Q7"
            metric = "high_risk_rows" if intent.high_risk else "complete_rate"
            outputs = ("workshop_code", metric)
        elif across_ships:
            family = "Q5"
            metric = "deviation_rate" if intent.deviation else "complete_rate"
            outputs = ("ship_no", metric)
        elif len(processes) >= 2:
            family = "Q6"
            metric = "deviation_rate" if intent.deviation else "complete_rate"
            outputs = ("process_code", metric)
        elif trend:
            family = "Q4"
            metric = "actual_qty" if intent.metric == "actual_qty" else "complete_rate"
            outputs = ("month_label", metric)
        elif intent.deviation or intent.completion:
            family = "Q3"
            metric = "deviation_rate" if intent.deviation else "complete_rate"
            outputs = (metric,)
        elif intent.metric == "plan_actual":
            family = "Q2"
            metric = "plan_actual"
            outputs = ("plan_qty", "actual_qty")
        elif intent.metric in {"plan_qty", "actual_qty"}:
            family = "Q1"
            metric = intent.metric
            outputs = (metric,)
        else:
            return _refuse(original, self._identity_message)

        missing: list[str] = []
        if family not in {"Q5", "Q7"} and ship_no is None:
            missing.append("船号")
        if not months:
            missing.append("月份")
        if family != "Q6" and len(processes) != 1:
            missing.append("工序")
        if family == "Q6" and len(processes) < 2:
            missing.append("至少两个工序")
        if family == "Q4" and len(months) < 2:
            missing.append("月份范围")
        if missing:
            return _clarify(
                original,
                "请补充并明确" + "、".join(dict.fromkeys(missing)) + "后再查询。",
            )

        start_date = _month_start(months[0])
        end_date = _next_month(months[-1])
        process_text = "、".join(PROCESS_LABELS[item] for item in processes)
        start_label = _month_label(months[0])
        end_label = _month_label(months[-1])

        if family == "Q1":
            metric_text = "计划量" if metric == "plan_qty" else "实际量"
            canonical = f"查询{ship_no}在{start_label}的{process_text}{metric_text}"
        elif family == "Q2":
            canonical = f"查询{ship_no}在{start_label}的{process_text}计划量与实际量"
        elif family == "Q3":
            metric_text = "偏差率" if metric == "deviation_rate" else "完成率"
            canonical = f"查询{ship_no}在{start_label}的{process_text}{metric_text}"
        elif family == "Q4":
            canonical = (
                f"查询{ship_no}的{process_text}在{start_label}至{end_label}的月完成率走势"
            )
        elif family == "Q5":
            canonical = f"查询{start_label}各船{process_text}完成率排名"
        elif family == "Q6":
            canonical = (
                f"查询{ship_no}在{start_label}的{process_text}完成率，"
                "仅统计这三道工序并按工序返回"
            )
        else:
            metric_text = "高风险记录数" if metric == "high_risk_rows" else "完成率"
            canonical = f"查询{start_label}{process_text}各责任单元{metric_text}"

        return FreeQuerySpec(
            decision="allow",
            original_question=original,
            canonical_question=canonical,
            family=family,
            output_columns=outputs,
            ship_no=ship_no,
            process_codes=processes,
            start_date=start_date,
            end_date=end_date,
        )


@dataclass(frozen=True, slots=True)
class _FilterFact:
    column: str
    operation: str
    value: str


class FreeQueryContractError(ValueError):
    """Raised before execution when generated SQL leaves the bound free-query spec."""

    def __init__(
        self,
        reason: str,
        *,
        generation: Text2SQLGenerationResult | None = None,
    ) -> None:
        super().__init__(reason)
        self.generation = generation
        data = generation.data if generation is not None else {}
        generated_sql = data.get("sql")
        self.generated_sql = generated_sql if isinstance(generated_sql, str) else None


def _literal_values(node: exp.Expression | None) -> tuple[str, ...]:
    if node is None:
        return ()
    values: list[str] = []
    if isinstance(node, exp.Literal):
        values.append(str(node.this))
    values.extend(str(item.this) for item in node.find_all(exp.Literal))
    return tuple(dict.fromkeys(values))


def _direct_column(node: exp.Expression | None) -> str | None:
    return node.name.casefold() if isinstance(node, exp.Column) else None


def _sql_filter_facts(statement: exp.Select) -> tuple[_FilterFact, ...]:
    where = statement.args.get("where")
    if where is None:
        return ()
    facts: list[_FilterFact] = []
    comparison_types: tuple[tuple[type[exp.Expression], str], ...] = (
        (exp.EQ, "eq"),
        (exp.GTE, "gte"),
        (exp.GT, "gt"),
        (exp.LTE, "lte"),
        (exp.LT, "lt"),
    )
    for node_type, operation in comparison_types:
        for node in where.find_all(node_type):
            left = node.args.get("this")
            right = node.args.get("expression")
            column = _direct_column(left)
            values = _literal_values(right)
            if column is None:
                column = _direct_column(right)
                values = _literal_values(left)
            for value in values:
                facts.append(_FilterFact(column or "", operation, value))
    for node in where.find_all(exp.In):
        column = _direct_column(node.args.get("this"))
        if column is None:
            continue
        for value_node in node.expressions:
            for value in _literal_values(value_node):
                facts.append(_FilterFact(column, "in", value))
    for node in where.find_all(exp.Between):
        column = _direct_column(node.args.get("this"))
        if column is None:
            continue
        for value in _literal_values(node.args.get("low")):
            facts.append(_FilterFact(column, "between_low", value))
        for value in _literal_values(node.args.get("high")):
            facts.append(_FilterFact(column, "between_high", value))
    return tuple(facts)


def _filter_values(
    facts: tuple[_FilterFact, ...],
    column: str,
    operations: frozenset[str],
) -> frozenset[str]:
    return frozenset(
        item.value
        for item in facts
        if item.column == column and item.operation in operations
    )


def _projection_expressions(statement: exp.Select) -> dict[str, exp.Expression]:
    projections: dict[str, exp.Expression] = {}
    for item in statement.expressions:
        name = item.alias_or_name
        if not name:
            continue
        projections[name.casefold()] = item.this if isinstance(item, exp.Alias) else item
    return projections


def _column_names(node: exp.Expression | None) -> frozenset[str]:
    if node is None:
        return frozenset()
    return frozenset(column.name.casefold() for column in node.find_all(exp.Column))


def _has_complete_rate_formula(expression: exp.Expression) -> bool:
    for division in expression.find_all(exp.Div):
        numerator_columns = _column_names(division.args.get("this"))
        denominator_columns = _column_names(division.args.get("expression"))
        if (
            "actual_qty" in numerator_columns
            and "plan_qty" not in numerator_columns
            and "plan_qty" in denominator_columns
        ):
            return True
    return False


def _has_deviation_rate_formula(expression: exp.Expression) -> bool:
    for division in expression.find_all(exp.Div):
        numerator = division.args.get("this")
        denominator_columns = _column_names(division.args.get("expression"))
        if "plan_qty" not in denominator_columns or numerator is None:
            continue
        for subtraction in numerator.find_all(exp.Sub):
            if (
                "actual_qty" in _column_names(subtraction.args.get("this"))
                and "plan_qty" in _column_names(subtraction.args.get("expression"))
            ):
                return True
    for subtraction in expression.find_all(exp.Sub):
        left = subtraction.args.get("this")
        right = subtraction.args.get("expression")
        if left is None or right is None or "1" not in _literal_values(right):
            continue
        if _has_complete_rate_formula(left):
            return True
    return False


def _formula_issue(
    projections: Mapping[str, exp.Expression],
    output_columns: tuple[str, ...],
) -> str | None:
    for output in output_columns:
        expression = projections.get(output.casefold())
        if expression is None:
            continue
        columns = _column_names(expression)
        if output in {"plan_qty", "actual_qty"} and output not in columns:
            return f"{output} formula does not reference {output}"
        if output == "complete_rate" and not _has_complete_rate_formula(expression):
            return "complete_rate formula must divide actual_qty by plan_qty"
        if output == "deviation_rate" and not _has_deviation_rate_formula(expression):
            return (
                "deviation_rate formula must subtract plan_qty from actual_qty "
                "and divide by plan_qty"
            )
        if output == "high_risk_rows" and expression.find(exp.Count) is None:
            return "high_risk_rows formula must be a count"
    return None


def _grouping_issue(
    statement: exp.Select,
    projections: Mapping[str, exp.Expression],
    family: str,
) -> str | None:
    required_aliases = {
        "Q4": ("month_label",),
        "Q5": ("ship_no",),
        "Q6": ("process_code",),
        "Q7": ("workshop_code",),
    }.get(family, ())
    if not required_aliases:
        return None
    group = statement.args.get("group")
    if not isinstance(group, exp.Group):
        return "missing required GROUP BY"
    group_columns = _column_names(group)
    rendered_groups = {
        item.sql(dialect="mysql").casefold() for item in group.expressions
    }
    for alias in required_aliases:
        if alias in group_columns:
            continue
        projection = projections.get(alias)
        if projection is not None and projection.sql(dialect="mysql").casefold() in rendered_groups:
            continue
        return f"missing GROUP BY contract for {alias}"
    return None


def _contract_issue(
    spec: FreeQuerySpec,
    generation: Text2SQLGenerationResult,
) -> str | None:
    raw_family = str(generation.data.get("family", ""))
    if raw_family != spec.family or generation.routing_final_family != spec.family:
        return (
            "family mismatch: "
            f"expected={spec.family}, raw={raw_family}, "
            f"final={generation.routing_final_family}"
        )
    sql = generation.data.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return "generated SQL must be a non-empty string"
    try:
        statements = sqlglot.parse(sql, read="mysql")
    except (ValueError, sqlglot.errors.ParseError):
        return "generated SQL cannot be parsed"
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return "generated SQL must contain exactly one SELECT"
    statement = statements[0]
    projections = _projection_expressions(statement)
    actual_outputs = frozenset(projections)
    expected_outputs = frozenset(item.casefold() for item in spec.output_columns)
    if actual_outputs != expected_outputs:
        return (
            "output columns mismatch: "
            f"expected={sorted(expected_outputs)}, actual={sorted(actual_outputs)}"
        )

    facts = _sql_filter_facts(statement)
    if spec.ship_no is not None:
        actual_ships = frozenset(
            value.upper()
            for value in _filter_values(facts, "ship_no", frozenset({"eq", "in"}))
        )
        if actual_ships != frozenset({spec.ship_no}):
            return (
                "ship_no filter mismatch: "
                f"expected={[spec.ship_no]}, actual={sorted(actual_ships)}"
            )
    actual_processes = frozenset(
        value.upper()
        for value in _filter_values(facts, "process_code", frozenset({"eq", "in"}))
    )
    expected_processes = frozenset(spec.process_codes)
    if actual_processes != expected_processes:
        return (
            "process_code filter mismatch: "
            f"expected={sorted(expected_processes)}, actual={sorted(actual_processes)}"
        )
    if spec.start_date is not None:
        start_values = _filter_values(
            facts,
            "period_date",
            frozenset({"gte", "between_low"}),
        )
        if spec.start_date not in start_values:
            return (
                "period_date start mismatch: "
                f"expected={spec.start_date}, actual={sorted(start_values)}"
            )
    if spec.end_date is not None:
        exclusive_end_values = _filter_values(
            facts,
            "period_date",
            frozenset({"lt"}),
        )
        inclusive_end_values = _filter_values(
            facts,
            "period_date",
            frozenset({"lte", "between_high"}),
        )
        inclusive_end = (
            date.fromisoformat(spec.end_date) - timedelta(days=1)
        ).isoformat()
        if (
            spec.end_date not in exclusive_end_values
            and inclusive_end not in inclusive_end_values
        ):
            return (
                "period_date end mismatch: "
                f"expected_exclusive={spec.end_date}, "
                f"actual_exclusive={sorted(exclusive_end_values)}, "
                f"actual_inclusive={sorted(inclusive_end_values)}"
            )
    issue = _grouping_issue(statement, projections, spec.family)
    if issue is not None:
        return issue
    return _formula_issue(projections, spec.output_columns)


class GuardedFreeQueryCoordinator:
    """Bind one validated free-query spec to the unchanged generation coordinator."""

    def __init__(self, inner: Any, spec: FreeQuerySpec) -> None:
        if spec.decision != "allow" or spec.canonical_question is None:
            raise ValueError("guarded generation requires one allowed free-query spec")
        self._inner = inner
        self._spec = spec

    def generate(
        self,
        question: str,
        *,
        query_authority: Mapping[str, Any] | None = None,
    ) -> Text2SQLGenerationResult:
        if query_authority is not None:
            raise FreeQueryContractError(
                "external free-query generation cannot accept template authority"
            )
        generation = self._inner.generate(str(self._spec.canonical_question))
        issue = _contract_issue(self._spec, generation)
        if issue is not None:
            raise FreeQueryContractError(issue, generation=generation)
        return generation


class FreeQueryGateway:
    """Explicit entry point for untrusted external free-query text only."""

    def __init__(
        self,
        *,
        trace_id: str,
        executor: Any,
        generation_coordinator: Any | None = None,
        boundary: FreeQueryBoundary | None = None,
        clock: Callable[[], datetime] | None = None,
        query_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._trace_id = trace_id
        self._executor = executor
        self._generation_coordinator = (
            Text2SQLGenerationCoordinator()
            if generation_coordinator is None
            else generation_coordinator
        )
        self._boundary = boundary or FreeQueryBoundary()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._query_id_factory = query_id_factory or (
            lambda: f"sql-run-{uuid4().hex}"
        )

    def answer(self, question: str) -> dict[str, Any]:
        spec = self._boundary.classify(question)
        if spec.decision == "refuse":
            return self._boundary_draft(spec)
        if spec.decision == "clarify":
            if str(spec.student_message).startswith("请补充并明确"):
                preflight_sql = self._preflight_sql(spec.original_question)
                if preflight_sql is not None:
                    return self._preflight_or_clarify(spec, preflight_sql)
            return self._boundary_draft(spec)

        guarded = GuardedFreeQueryCoordinator(self._generation_coordinator, spec)
        agent = VerificationAgent(
            trace_id=self._trace_id,
            executor=self._executor,
            clock=self._clock,
            query_id_factory=self._query_id_factory,
            generation_coordinator=guarded,
        )
        try:
            return agent.answer(spec.original_question)
        except FreeQueryContractError as exc:
            return self._contract_rejection(spec, exc)

    def _draft(
        self,
        *,
        content: dict[str, Any],
        evidence: list[dict[str, Any]] | None = None,
        generation: Text2SQLGenerationResult | None = None,
    ) -> dict[str, Any]:
        token_usage = generation.token_usage if generation is not None else TokenUsage(0, 0, 0)
        model = generation.model if generation is not None else "deterministic-free-query-boundary"
        latency_ms = generation.latency_ms if generation is not None else 0
        return {
            "trace_id": self._trace_id,
            "agent": "verification",
            "role": "produce",
            "payload": {"type": "sql_result", "content": content},
            "evidence": evidence or [],
            "claims": [],
            "timestamp": self._clock().isoformat(),
            "model": model,
            "latency_ms": latency_ms,
            "token_usage": token_usage.as_dict(),
        }

    def _boundary_draft(
        self,
        spec: FreeQuerySpec,
        *,
        extra_content: Mapping[str, Any] | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "event": "refuse_out_of_scope",
            "question": spec.original_question,
            "family": "OUT_OF_SCOPE",
            "boundary_decision": spec.decision,
            "student_message": spec.student_message or self._boundary.identity_message,
            "llm_latency_ms": 0,
        }
        if extra_content is not None:
            content.update(extra_content)
        return self._draft(content=content, evidence=evidence)

    def _contract_rejection(
        self,
        spec: FreeQuerySpec,
        error: FreeQueryContractError,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "event": "sandbox_rejected",
            "question": spec.original_question,
            "family": spec.family,
            "boundary_decision": "reject",
            "student_message": "生成的查询未通过自由问句语义契约校验。",
            "llm_latency_ms": (
                error.generation.latency_ms if error.generation is not None else 0
            ),
        }
        if error.generated_sql is not None:
            content["generated_sql"] = error.generated_sql
        if error.generation is not None:
            content.update(error.generation.routing_content())
        return self._draft(
            content=content,
            evidence=[
                {
                    "kind": "review_rule",
                    "ref": "FREE_QUERY_CONTRACT",
                    "quote": str(error),
                }
            ],
            generation=error.generation,
        )

    @staticmethod
    def _preflight_sql(question: str) -> str | None:
        normalized = _normalized(question)
        ship_match = _SHIP_RE.search(normalized.upper())
        ship_no = ship_match.group(0).upper() if ship_match is not None else None
        months = _months(normalized)
        if ship_no is None and not months:
            return None
        if ship_no is not None and not months:
            return f"SELECT ship_no FROM dim_ship WHERE ship_no='{ship_no}' LIMIT 1"
        start_date = _month_start(months[0])
        end_date = _next_month(months[-1])
        filters = [
            f"period_date>='{start_date}'",
            f"period_date<'{end_date}'",
        ]
        if ship_no is not None:
            filters.insert(0, f"ship_no='{ship_no}'")
        return (
            "SELECT ship_no FROM fact_production_progress WHERE "
            + " AND ".join(filters)
            + " LIMIT 1"
        )

    def _preflight_or_clarify(
        self,
        spec: FreeQuerySpec,
        generated_sql: str,
    ) -> dict[str, Any]:
        decision = validate_and_rewrite(generated_sql)
        if not decision.allowed or decision.executed_sql is None:
            raise RuntimeError(
                "deterministic free-query preflight did not pass the read-only sandbox"
            )
        executed_sql = decision.executed_sql
        result = self._executor.execute(executed_sql)
        query_id = self._query_id_factory()
        quote = json.dumps(
            {
                "generated_sql": generated_sql,
                "executed_sql": executed_sql,
                "status": "exists" if result.rows else "empty",
                "row_count": len(result.rows),
                "rows": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        evidence = [{"kind": "sql_query", "ref": query_id, "quote": quote}]
        if result.rows:
            return self._boundary_draft(
                spec,
                extra_content={
                    "preflight_query_id": query_id,
                    "preflight_executed_sql": executed_sql,
                },
                evidence=evidence,
            )
        return self._draft(
            content={
                "event": "query_empty",
                "question": spec.original_question,
                "family": "OUT_OF_SCOPE",
                "boundary_decision": "clarify",
                "student_message": (
                    "查询无数据，请检查船号或月份；也可以补充船号、月份和工序后再查询。"
                ),
                "query_id": query_id,
                "preflight_executed_sql": executed_sql,
                "rows": [],
                "row_count": 0,
                "query_elapsed_ms": result.elapsed_ms,
                "llm_latency_ms": 0,
            },
            evidence=evidence,
        )
