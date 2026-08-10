"""Live Text2SQL verification agent with deterministic result messages."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from time import perf_counter
from typing import Any
from uuid import uuid4

import sqlglot
from sqlglot import exp

from agents.sandbox import (
    QueryExecutionError,
    QueryResult,
    QueryTimeoutError,
    ReadOnlyExecutor,
    validate_and_rewrite,
)
from agents.text2sql_generation import (
    Text2SQLGenerationCoordinator,
    Text2SQLGenerationResult,
)
from agents.query_authority import QueryAuthority, query_authority_from_mapping
from orchestrator.llm import LLMResult, call_llm


RESULT_SUMMARY_ROWS = 20

FAMILY_COLUMN_SETS: dict[str, frozenset[frozenset[str]]] = {
    "Q1": frozenset(
        {
            frozenset({"plan_qty"}),
            frozenset({"actual_qty"}),
            frozenset({"complete_rate"}),
            frozenset({"deviation_rate"}),
        }
    ),
    "Q2": frozenset({frozenset({"plan_qty", "actual_qty"})}),
    "Q3": frozenset(
        {frozenset({"complete_rate"}), frozenset({"deviation_rate"})}
    ),
    "Q4": frozenset(
        {
            frozenset({"month_label", "complete_rate"}),
            frozenset({"month_label", "actual_qty"}),
        }
    ),
    "Q5": frozenset({frozenset({"ship_no", "complete_rate"})}),
    "Q6": frozenset(
        {
            frozenset({"process_code", "month_label", "complete_rate"}),
            frozenset({"process_code", "complete_rate"}),
        }
    ),
    "Q7": frozenset(
        {
            frozenset({"workshop_code", "complete_rate"}),
            frozenset({"workshop_code", "high_risk_rows"}),
        }
    ),
}

FAMILY_DIMENSION_COLUMNS: dict[str, frozenset[str]] = {
    "Q1": frozenset(),
    "Q2": frozenset(),
    "Q3": frozenset(),
    "Q4": frozenset({"month_label"}),
    "Q5": frozenset({"ship_no"}),
    "Q6": frozenset({"process_code", "month_label"}),
    "Q7": frozenset({"workshop_code"}),
}

METRIC_LABELS = {
    "plan_qty": "计划量",
    "actual_qty": "实际量",
    "complete_rate": "完成率",
    "deviation_rate": "偏差率",
    "high_risk_rows": "高风险记录数",
}
PROCESS_LABELS = {"YCL": "预处理", "ZZTP": "制作托盘", "AZTP": "安装托盘"}


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _normalize_rows(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {key: _normalize_value(value) for key, value in row.items()}
        for row in rows
    ]


def _result_is_empty(family: str, rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    dimensions = FAMILY_DIMENSION_COLUMNS.get(family, frozenset())
    metric_values = [
        value
        for row in rows
        for name, value in row.items()
        if name not in dimensions
    ]
    return not metric_values or all(value is None for value in metric_values)


def _metric_text(metric: str, value: Any) -> str:
    label = METRIC_LABELS[metric]
    if metric in {"complete_rate", "deviation_rate"}:
        percentage = (Decimal(str(value)) * Decimal(100)).quantize(Decimal("0.01"))
        return f"{label}为{format(percentage, 'f')}%"
    return f"{label}为{value}"


def _metric_column(row: Mapping[str, Any], excluded: set[str]) -> str:
    metrics = [name for name in row if name not in excluded]
    if len(metrics) != 1 or metrics[0] not in METRIC_LABELS:
        raise ValueError("result row does not match the family metric contract")
    return metrics[0]


def _render_claims(
    family: str, question: str, rows: list[dict[str, Any]]
) -> list[str]:
    claims: list[str] = []
    for row in rows:
        if family in {"Q1", "Q3"}:
            metric = _metric_column(row, set())
            claims.append(f"查询“{question}”的结果：{_metric_text(metric, row[metric])}。")
        elif family == "Q2":
            claims.append(
                f"查询“{question}”的结果：{_metric_text('plan_qty', row['plan_qty'])}，"
                f"{_metric_text('actual_qty', row['actual_qty'])}。"
            )
        elif family == "Q4":
            metric = _metric_column(row, {"month_label"})
            claims.append(
                f"查询“{question}”的{row['month_label']}结果："
                f"{_metric_text(metric, row[metric])}。"
            )
        elif family == "Q5":
            metric = _metric_column(row, {"ship_no"})
            claims.append(
                f"查询“{question}”的{row['ship_no']}结果："
                f"{_metric_text(metric, row[metric])}。"
            )
        elif family == "Q6":
            excluded = {"process_code", "month_label"}
            metric = _metric_column(row, excluded)
            process = PROCESS_LABELS.get(str(row["process_code"]), str(row["process_code"]))
            month = f"{row['month_label']}" if "month_label" in row else ""
            claims.append(
                f"查询“{question}”的{month}{process}结果："
                f"{_metric_text(metric, row[metric])}。"
            )
        elif family == "Q7":
            metric = _metric_column(row, {"workshop_code"})
            claims.append(
                f"查询“{question}”的{row['workshop_code']}结果："
                f"{_metric_text(metric, row[metric])}。"
            )
        else:
            raise ValueError(f"unsupported family: {family}")
    return claims


def _output_aliases(sql: str) -> frozenset[str]:
    statement = sqlglot.parse_one(sql, read="mysql")
    if not isinstance(statement, exp.Select):
        return frozenset()
    return frozenset(name.casefold() for name in statement.named_selects)


def _authority_columns(node: exp.Expression | None) -> frozenset[str]:
    if node is None:
        return frozenset()
    return frozenset(
        column.name.casefold() for column in node.find_all(exp.Column)
    )


def query_authority_issue(
    sql: str, authority: QueryAuthority
) -> str | None:
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except (ValueError, sqlglot.errors.ParseError):
        return "generated SQL cannot be parsed against template authority"
    if not isinstance(statement, exp.Select):
        return "generated SQL is not a SELECT"
    actual_outputs = frozenset(name.casefold() for name in statement.named_selects)
    expected_outputs = frozenset(authority.output_columns)
    if actual_outputs != expected_outputs:
        return (
            "output columns do not match template authority: "
            f"expected={sorted(expected_outputs)}, actual={sorted(actual_outputs)}"
        )
    where_columns = _authority_columns(statement.args.get("where"))
    missing_filters = set(authority.filter_columns) - set(where_columns)
    if missing_filters:
        return "missing template filter columns: " + ",".join(
            sorted(missing_filters)
        )
    if authority.time_column == "period_date" and "batch_code" in where_columns:
        return "YYYY-MM template time must use period_date, not batch_code"
    group_columns = _authority_columns(statement.args.get("group"))
    missing_groups = set(authority.group_by_columns) - set(group_columns)
    if missing_groups:
        return "missing template group columns: " + ",".join(
            sorted(missing_groups)
        )
    return None


class VerificationAgent:
    """Generate, sandbox, execute, and deterministically narrate one question."""

    def __init__(
        self,
        trace_id: str,
        llm_call: Callable[..., LLMResult] = call_llm,
        executor: ReadOnlyExecutor | Any = None,
        clock: Callable[[], datetime] | None = None,
        query_id_factory: Callable[[], str] | None = None,
        generation_coordinator: Text2SQLGenerationCoordinator | Any = None,
    ) -> None:
        if executor is None:
            raise ValueError("executor is required")
        self._trace_id = trace_id
        self._executor = executor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._query_id_factory = query_id_factory or (
            lambda: f"sql-run-{uuid4().hex}"
        )
        self._generation_coordinator = (
            Text2SQLGenerationCoordinator(llm_call=llm_call)
            if generation_coordinator is None
            else generation_coordinator
        )

    def answer(
        self,
        question: str,
        *,
        query_authority: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        question = question.strip()
        authority = (
            query_authority_from_mapping(query_authority)
            if query_authority is not None
            else None
        )
        authority_content = authority.as_dict() if authority is not None else None
        started = perf_counter()
        if authority is None:
            generation = self._generation_coordinator.generate(question)
        else:
            generation = self._generation_coordinator.generate(
                authority.standard_stem,
                query_authority=authority_content,
            )
        family = authority.family if authority is not None else generation.routing_final_family
        generated_sql = generation.data["sql"]
        if family == "OUT_OF_SCOPE":
            return self._failure_draft(
                event="refuse_out_of_scope",
                question=question,
                family=family,
                llm_result=generation,
                started=started,
                student_message="该问题超出数据验证智能体的职责范围。",
                query_authority=authority_content,
            )
        if not isinstance(generated_sql, str):
            return self._sandbox_failure(
                question,
                family,
                str(generated_sql),
                "S-07",
                "sql must be a string",
                generation,
                started,
                query_authority=authority_content,
            )
        decision = validate_and_rewrite(generated_sql)
        if not decision.allowed:
            return self._sandbox_failure(
                question,
                family,
                generated_sql,
                str(decision.rule_id),
                str(decision.reason),
                generation,
                started,
                query_authority=authority_content,
            )
        allowed_contracts = FAMILY_COLUMN_SETS.get(family, frozenset())
        if _output_aliases(generated_sql) not in allowed_contracts:
            return self._sandbox_failure(
                question,
                family,
                generated_sql,
                "S-04",
                f"output columns do not match the {family} contract",
                generation,
                started,
                query_authority=authority_content,
            )
        if authority is not None:
            authority_issue = query_authority_issue(generated_sql, authority)
            if authority_issue is not None:
                return self._failure_draft(
                    event="template_authority_rejected",
                    question=question,
                    family=family,
                    llm_result=generation,
                    started=started,
                    student_message="生成的查询偏离了任务模板目标。",
                    generated_sql=generated_sql,
                    evidence=[
                        {
                            "kind": "review_rule",
                            "ref": "QUERY_AUTHORITY",
                            "quote": authority_issue,
                        }
                    ],
                    query_authority=authority_content,
                )
        executed_sql = str(decision.executed_sql)
        query_id = self._query_id_factory()
        try:
            result = self._executor.execute(executed_sql)
        except QueryTimeoutError:
            quote = self._query_quote(generated_sql, executed_sql, None, "timeout")
            return self._failure_draft(
                event="query_timeout",
                question=question,
                family=family,
                llm_result=generation,
                started=started,
                student_message="查询超时，请缩小查询范围。",
                generated_sql=generated_sql,
                executed_sql=executed_sql,
                evidence=[{"kind": "sql_query", "ref": query_id, "quote": quote}],
                query_authority=authority_content,
            )
        except QueryExecutionError:
            quote = self._query_quote(generated_sql, executed_sql, None, "failed")
            return self._failure_draft(
                event="query_failed",
                question=question,
                family=family,
                llm_result=generation,
                started=started,
                student_message="查询服务暂时不可用，请稍后重试。",
                generated_sql=generated_sql,
                executed_sql=executed_sql,
                evidence=[{"kind": "sql_query", "ref": query_id, "quote": quote}],
                query_authority=authority_content,
            )
        normalized_rows = _normalize_rows(result.rows)
        if _result_is_empty(family, normalized_rows):
            quote = self._query_quote(generated_sql, executed_sql, [], "empty")
            return self._failure_draft(
                event="query_empty",
                question=question,
                family=family,
                llm_result=generation,
                started=started,
                student_message="查询无数据，请检查查询条件。",
                generated_sql=generated_sql,
                executed_sql=executed_sql,
                rows=[],
                row_count=0,
                evidence=[{"kind": "sql_query", "ref": query_id, "quote": quote}],
                query_authority=authority_content,
            )
        claim_texts = _render_claims(family, question, normalized_rows)
        quote = self._query_quote(
            generated_sql, executed_sql, normalized_rows, "completed"
        )
        evidence = [
            {
                "kind": "sql_query",
                "ref": query_id,
                "quote": quote,
                "supports_claim": claim,
            }
            for claim in claim_texts
        ]
        content = {
            "event": "query_completed",
            "question": question,
            "family": family,
            "query_id": query_id,
            "generated_sql": generated_sql,
            "executed_sql": executed_sql,
            "columns": list(result.columns),
            "rows": normalized_rows,
            "row_count": len(normalized_rows),
            "query_elapsed_ms": result.elapsed_ms,
            "llm_latency_ms": generation.latency_ms,
            **generation.routing_content(),
        }
        if authority_content is not None:
            content["query_authority"] = authority_content
        return self._draft(
            content=content,
            evidence=evidence,
            claims=[{"text": claim, "kind": "data_conclusion"} for claim in claim_texts],
            llm_result=generation,
            started=started,
        )

    @staticmethod
    def _query_quote(
        generated_sql: str,
        executed_sql: str,
        rows: list[dict[str, Any]] | None,
        status: str,
    ) -> str:
        return json.dumps(
            {
                "generated_sql": generated_sql,
                "executed_sql": executed_sql,
                "status": status,
                "row_count": None if rows is None else len(rows),
                "rows": [] if rows is None else rows[:RESULT_SUMMARY_ROWS],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _sandbox_failure(
        self,
        question: str,
        family: str,
        generated_sql: str,
        rule_id: str,
        reason: str,
        llm_result: Text2SQLGenerationResult,
        started: float,
        *,
        query_authority: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._failure_draft(
            event="sandbox_rejected",
            question=question,
            family=family,
            llm_result=llm_result,
            started=started,
            student_message="生成的查询未通过安全校验。",
            generated_sql=generated_sql,
            evidence=[{"kind": "review_rule", "ref": rule_id, "quote": reason}],
            query_authority=query_authority,
        )

    def _failure_draft(
        self,
        *,
        event: str,
        question: str,
        family: str,
        llm_result: Text2SQLGenerationResult,
        started: float,
        student_message: str,
        generated_sql: str | None = None,
        executed_sql: str | None = None,
        rows: list[dict[str, Any]] | None = None,
        row_count: int | None = None,
        evidence: list[dict[str, Any]] | None = None,
        query_authority: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "event": event,
            "question": question,
            "family": family,
            "student_message": student_message,
            "llm_latency_ms": llm_result.latency_ms,
            **llm_result.routing_content(),
        }
        if query_authority is not None:
            content["query_authority"] = query_authority
        if generated_sql is not None:
            content["generated_sql"] = generated_sql
        if executed_sql is not None:
            content["executed_sql"] = executed_sql
        if rows is not None:
            content["rows"] = rows
        if row_count is not None:
            content["row_count"] = row_count
        return self._draft(
            content=content,
            evidence=evidence or [],
            claims=[],
            llm_result=llm_result,
            started=started,
        )

    def _draft(
        self,
        *,
        content: dict[str, Any],
        evidence: list[dict[str, Any]],
        claims: list[dict[str, Any]],
        llm_result: Text2SQLGenerationResult,
        started: float,
    ) -> dict[str, Any]:
        total_latency = max(
            round((perf_counter() - started) * 1000), llm_result.latency_ms
        )
        return {
            "trace_id": self._trace_id,
            "agent": "verification",
            "role": "produce",
            "payload": {"type": "sql_result", "content": content},
            "evidence": evidence,
            "claims": claims,
            "timestamp": self._clock().isoformat(),
            "model": llm_result.model,
            "latency_ms": total_latency,
            "token_usage": llm_result.token_usage.as_dict(),
        }
