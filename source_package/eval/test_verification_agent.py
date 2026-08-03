from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Any

import pytest

from agents.prompts.build_verification_prompt import ROUTED_FEW_SHOT_IDS
from agents.sandbox import QueryExecutionError, QueryResult, QueryTimeoutError
from agents.text2sql_generation import (
    GENERATOR_MODEL,
    ROUTER_MODEL,
    Text2SQLGenerationResult,
)
from agents.verification_agent import VerificationAgent
from agents.validate_message import validate_message
from orchestrator.bus import MessageBus
from orchestrator.llm import LLMResult, TokenUsage


MODEL = "qwen3-235b-a22b"
FIXED_NOW = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)


@dataclass
class StubCoordinator:
    result: Text2SQLGenerationResult
    questions: list[str] = field(default_factory=list)
    authorities: list[dict[str, Any] | None] = field(default_factory=list)

    def generate(
        self,
        question: str,
        *,
        query_authority: dict[str, Any] | None = None,
    ) -> Text2SQLGenerationResult:
        self.questions.append(question)
        self.authorities.append(query_authority)
        return self.result


@dataclass
class SequentialLLM:
    outcomes: list[LLMResult]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        return self.outcomes.pop(0)


@dataclass
class SpyExecutor:
    outcome: QueryResult | BaseException
    calls: list[str] = field(default_factory=list)

    def execute(self, sql: str) -> QueryResult:
        self.calls.append(sql)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def query_result(
    *rows: dict[str, Any], **single_row: Any
) -> QueryResult:
    if single_row:
        assert not rows
        rows = (single_row,)
    columns = tuple(rows[0]) if rows else ()
    return QueryResult(columns=columns, rows=tuple(rows), elapsed_ms=7)


def generation_result(data: dict[str, Any]) -> Text2SQLGenerationResult:
    family = str(data["family"])
    profile = "OUT_OF_SCOPE_MINI_7" if family == "OUT_OF_SCOPE" else family
    routing_usage = TokenUsage(10, 2, 12)
    generation_usage = TokenUsage(90, 18, 108)
    return Text2SQLGenerationResult(
        data=data,
        model=MODEL,
        latency_ms=120,
        token_usage=routing_usage + generation_usage,
        routing_model=ROUTER_MODEL,
        routing_predicted_family=family,
        routing_final_family=family,
        routing_fallback=False,
        routing_family_mismatch=False,
        routing_fallback_reason="none",
        routing_prompt_profile=profile,
        routing_few_shot_ids=ROUTED_FEW_SHOT_IDS[profile],
        routing_latency_ms=20,
        generation_latency_ms=100,
        routing_token_usage=routing_usage,
        generation_token_usage=generation_usage,
    )


def assert_default_routing(content: dict[str, Any], family: str) -> None:
    profile = "OUT_OF_SCOPE_MINI_7" if family == "OUT_OF_SCOPE" else family
    assert content["routing_predicted_family"] == family
    assert content["routing_final_family"] == family
    assert content["routing_fallback"] is False
    assert content["routing_family_mismatch"] is False
    assert content["routing_fallback_reason"] == "none"
    assert content["routing_prompt_profile"] == profile
    assert content["routing_few_shot_ids"] == list(ROUTED_FEW_SHOT_IDS[profile])
    assert content["routing_llm_latency_ms"] == 20
    assert content["generation_llm_latency_ms"] == 100


def build_agent(
    data: dict[str, Any], outcome: QueryResult | BaseException
) -> tuple[VerificationAgent, StubCoordinator, SpyExecutor]:
    coordinator = StubCoordinator(generation_result(data))
    executor = SpyExecutor(outcome)
    agent = VerificationAgent(
        trace_id="trace-verification",
        executor=executor,
        clock=lambda: FIXED_NOW,
        query_id_factory=lambda: "sql-run-test",
        generation_coordinator=coordinator,
    )
    return agent, coordinator, executor


@pytest.mark.parametrize(
    ("family", "sql", "result", "expected_claims"),
    (
        (
            "Q1",
            "SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress",
            query_result(plan_qty=Decimal("1855.06")),
            ("查询“H2601五月预处理计划量”的结果：计划量为1855.06。",),
        ),
        (
            "Q2",
            "SELECT SUM(plan_qty) AS plan_qty, SUM(actual_qty) AS actual_qty FROM fact_production_progress",
            query_result(
                plan_qty=Decimal("1855.06"), actual_qty=Decimal("1156.87")
            ),
            (
                "查询“H2601五月预处理计划和实际”的结果：计划量为1855.06，实际量为1156.87。",
            ),
        ),
        (
            "Q3",
            "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate FROM fact_production_progress",
            query_result(complete_rate=Decimal("0.6236")),
            ("查询“H2601五月预处理完成率”的结果：完成率为62.36%。",),
        ),
        (
            "Q4",
            "SELECT DATE_FORMAT(period_date,'%Y-%m') AS month_label, ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate FROM fact_production_progress GROUP BY month_label ORDER BY month_label",
            query_result(
                month_label="2025-05", complete_rate=Decimal("0.6236")
            ),
            (
                "查询“H2601预处理月度趋势”的2025-05结果：完成率为62.36%。",
            ),
        ),
        (
            "Q5",
            "SELECT ship_no, ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate FROM fact_production_progress GROUP BY ship_no ORDER BY complete_rate",
            query_result(ship_no="H2601", complete_rate=Decimal("0.6236")),
            ("查询“五月各船预处理排名”的H2601结果：完成率为62.36%。",),
        ),
        (
            "Q6",
            "SELECT process_code, DATE_FORMAT(period_date,'%Y-%m') AS month_label, ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate FROM fact_production_progress GROUP BY process_code, month_label ORDER BY process_code, month_label",
            query_result(
                process_code="ZZTP",
                month_label="2025-06",
                complete_rate=Decimal("0.7545"),
            ),
            (
                "查询“H2601异常传导”的2025-06制作托盘结果：完成率为75.45%。",
            ),
        ),
        (
            "Q7",
            "SELECT workshop_code, COUNT(*) AS high_risk_rows FROM fact_production_progress GROUP BY workshop_code ORDER BY high_risk_rows DESC",
            query_result(workshop_code="WSA", high_risk_rows=31),
            ("查询“五月高风险责任单元”的WSA结果：高风险记录数为31。",),
        ),
    ),
)
def test_family_renderer_uses_only_normalized_result_values(
    tmp_path: Any,
    family: str,
    sql: str,
    result: QueryResult,
    expected_claims: tuple[str, ...],
) -> None:
    data = {
        "sql": sql,
        "family": family,
        "explanation": "恶意复述数字999999",
    }
    question = {
        "Q1": "H2601五月预处理计划量",
        "Q2": "H2601五月预处理计划和实际",
        "Q3": "H2601五月预处理完成率",
        "Q4": "H2601预处理月度趋势",
        "Q5": "五月各船预处理排名",
        "Q6": "H2601异常传导",
        "Q7": "五月高风险责任单元",
    }[family]
    agent, coordinator, _ = build_agent(data, result)

    draft = agent.answer(question)
    accepted = MessageBus(tmp_path).send(draft)

    assert accepted.accepted, accepted.errors
    assert validate_message(accepted.message) == []
    assert draft["payload"]["content"]["event"] == "query_completed"
    assert tuple(claim["text"] for claim in draft["claims"]) == expected_claims
    assert "999999" not in json.dumps(draft, ensure_ascii=False)
    assert draft["payload"]["content"]["generated_sql"] == sql
    assert draft["payload"]["content"]["executed_sql"].endswith("LIMIT 200")
    assert_default_routing(draft["payload"]["content"], family)
    assert draft["model"] == MODEL
    assert draft["latency_ms"] >= draft["payload"]["content"]["llm_latency_ms"]
    assert draft["token_usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }
    for claim in draft["claims"]:
        assert any(
            evidence["kind"] == "sql_query"
            and evidence["supports_claim"] == claim["text"]
            for evidence in draft["evidence"]
        )
    assert coordinator.questions == [question]


def test_result_rows_are_json_lossless_and_evidence_has_a_bounded_summary() -> None:
    rows = [
        {"month_label": date(2025, 5, 1), "actual_qty": Decimal("1.20")}
        for _ in range(25)
    ]
    data = {
        "sql": "SELECT period_date AS month_label, actual_qty FROM fact_production_progress",
        "family": "Q4",
        "explanation": "x",
    }
    agent, _, _ = build_agent(data, query_result(*rows))

    draft = agent.answer("日期与实际量")
    quote = json.loads(draft["evidence"][0]["quote"])

    assert draft["payload"]["content"]["rows"][0] == {
        "month_label": "2025-05-01",
        "actual_qty": "1.20",
    }
    assert quote["row_count"] == 25
    assert len(quote["rows"]) == 20


def test_out_of_scope_does_not_execute() -> None:
    agent, _, executor = build_agent(
        {"sql": None, "family": "OUT_OF_SCOPE", "explanation": "天气不在范围"},
        query_result(),
    )

    draft = agent.answer("今天天气如何")

    assert draft["payload"]["content"]["event"] == "refuse_out_of_scope"
    assert draft.get("claims", []) == []
    assert draft["evidence"] == []
    assert executor.calls == []
    assert_default_routing(draft["payload"]["content"], "OUT_OF_SCOPE")


def test_sandbox_rejection_has_review_rule_evidence() -> None:
    sql = "DROP TABLE dim_ship"
    agent, _, executor = build_agent(
        {"sql": sql, "family": "Q1", "explanation": "x"}, query_result()
    )

    draft = agent.answer("删除船表")

    assert draft["payload"]["content"]["event"] == "sandbox_rejected"
    assert draft["payload"]["content"]["generated_sql"] == sql
    assert draft["evidence"] == [
        {
            "kind": "review_rule",
            "ref": "S-01",
            "quote": "the AST root must be Select",
        }
    ]
    assert draft.get("claims", []) == []
    assert executor.calls == []
    assert_default_routing(draft["payload"]["content"], "Q1")


def test_empty_result_produces_clarification_without_data_conclusion() -> None:
    agent, _, _ = build_agent(
        {
            "sql": "SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress WHERE ship_no='NONE'",
            "family": "Q1",
            "explanation": "x",
        },
        query_result(),
    )

    draft = agent.answer("不存在的船")

    assert draft["payload"]["content"]["event"] == "query_empty"
    assert draft["payload"]["content"]["student_message"] == "查询无数据，请检查查询条件。"
    assert draft.get("claims", []) == []
    assert_default_routing(draft["payload"]["content"], "Q1")


@pytest.mark.parametrize(
    ("family", "sql", "null_row"),
    (
        (
            "Q1",
            "SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress",
            {"plan_qty": None},
        ),
        (
            "Q2",
            "SELECT SUM(plan_qty) AS plan_qty, SUM(actual_qty) AS actual_qty "
            "FROM fact_production_progress",
            {"plan_qty": None, "actual_qty": None},
        ),
        (
            "Q3",
            "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
            "FROM fact_production_progress",
            {"complete_rate": None},
        ),
    ),
)
def test_null_only_aggregate_row_is_treated_as_empty_result(
    family: str, sql: str, null_row: dict[str, Any]
) -> None:
    agent, _, _ = build_agent(
        {"sql": sql, "family": family, "explanation": "x"},
        query_result(null_row),
    )

    draft = agent.answer("不存在的数据范围")
    content = draft["payload"]["content"]

    assert content["event"] == "query_empty"
    assert content["rows"] == []
    assert content["row_count"] == 0
    assert draft["claims"] == []
    assert_default_routing(content, family)


def test_timeout_produces_auditable_failure_without_data_conclusion() -> None:
    agent, _, _ = build_agent(
        {
            "sql": "SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress",
            "family": "Q1",
            "explanation": "x",
        },
        QueryTimeoutError("timeout"),
    )

    draft = agent.answer("船号")

    assert draft["payload"]["content"]["event"] == "query_timeout"
    assert draft["payload"]["content"]["student_message"] == "查询超时，请缩小查询范围。"
    assert draft.get("claims", []) == []
    assert draft["evidence"][0]["kind"] == "sql_query"
    assert_default_routing(draft["payload"]["content"], "Q1")


def test_query_execution_failure_is_auditable_without_exposing_backend_error() -> None:
    agent, _, _ = build_agent(
        {
            "sql": "SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress",
            "family": "Q1",
            "explanation": "x",
        },
        QueryExecutionError(
            "mysql.connector.errors.InterfaceError: connection reset by peer"
        ),
    )

    draft = agent.answer("船号")

    content = draft["payload"]["content"]
    assert content["event"] == "query_failed"
    assert content["student_message"] == "查询服务暂时不可用，请稍后重试。"
    assert "connection reset" not in json.dumps(draft, ensure_ascii=False)
    assert draft.get("claims", []) == []
    assert draft["evidence"][0]["kind"] == "sql_query"
    assert json.loads(draft["evidence"][0]["quote"])["status"] == "failed"
    assert_default_routing(content, "Q1")


def test_family_contract_mismatch_is_rejected_before_execution() -> None:
    agent, _, executor = build_agent(
        {
            "sql": "SELECT SUM(actual_qty) AS actual_qty FROM fact_production_progress",
            "family": "Q2",
            "explanation": "x",
        },
        query_result(actual_qty=Decimal("1")),
    )

    draft = agent.answer("计划与实际")

    assert draft["payload"]["content"]["event"] == "sandbox_rejected"
    assert draft["evidence"][0]["ref"] == "S-04"
    assert executor.calls == []
    assert_default_routing(draft["payload"]["content"], "Q2")


def test_official_q6_process_order_drift_is_s04_before_execution() -> None:
    sql = (
        "SELECT process_order, "
        "DATE_FORMAT(period_date,'%Y-%m') AS month_label, "
        "ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
        "FROM fact_production_progress "
        "GROUP BY process_order, month_label "
        "ORDER BY process_order, month_label"
    )
    agent, coordinator, executor = build_agent(
        {"sql": sql, "family": "Q6", "explanation": "按工序顺序分析"},
        query_result(
            process_order=1,
            month_label="2025-06",
            complete_rate=Decimal("0.7545"),
        ),
    )
    authority = {
        "source": "task_template",
        "template_id": "T-08",
        "family": "Q6",
        "standard_stem": "分析H2601各工序的月度完成率传导",
        "output_columns": ["process_code", "month_label", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["process_code", "month_label"],
        "filter_columns": ["ship_no", "period_date"],
        "group_by_columns": ["process_code", "month_label"],
        "time_column": "period_date",
        "time_values": ["2025-05", "2025-06"],
    }

    draft = agent.answer(
        "分析H2601各工序的月度完成率传导",
        query_authority=authority,
    )

    assert draft["payload"]["content"]["event"] == "sandbox_rejected"
    assert draft["claims"] == []
    assert [item["ref"] for item in draft["evidence"]] == ["S-04"]
    assert executor.calls == []
    assert coordinator.authorities == [authority]


def test_template_month_authority_rejects_batch_code_before_execution() -> None:
    sql = (
        "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
        "FROM fact_production_progress WHERE ship_no='H2601' "
        "AND process_code='YCL' AND batch_code='2025-05'"
    )
    agent, coordinator, executor = build_agent(
        {"sql": sql, "family": "Q3", "explanation": "按批次查询"},
        query_result(complete_rate=Decimal("0.6236")),
    )
    authority = {
        "source": "task_template",
        "template_id": "T-02",
        "family": "Q3",
        "standard_stem": "查询H26012025-05YCL的完成率",
        "output_columns": ["complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": [],
        "filter_columns": ["period_date", "process_code", "ship_no"],
        "group_by_columns": [],
        "time_column": "period_date",
        "time_values": ["2025-05"],
    }

    draft = agent.answer(
        "查询H2601在2025-05批次的YCL完成率",
        query_authority=authority,
    )

    assert draft["payload"]["content"]["event"] == "template_authority_rejected"
    assert draft["payload"]["content"]["family"] == "Q3"
    assert draft["evidence"][0]["ref"] == "QUERY_AUTHORITY"
    assert executor.calls == []
    assert coordinator.questions == [authority["standard_stem"]]
    assert coordinator.authorities == [authority]


def test_router_out_of_scope_misclassification_uses_235b_q1_contract() -> None:
    llm = SequentialLLM(
        outcomes=[
            LLMResult(
                data={"family": "OUT_OF_SCOPE"},
                model=ROUTER_MODEL,
                latency_ms=30,
                token_usage=TokenUsage(10, 1, 11),
                attempts=1,
            ),
            LLMResult(
                data={
                    "sql": (
                        "SELECT SUM(plan_qty) AS plan_qty "
                        "FROM fact_production_progress"
                    ),
                    "family": "Q1",
                    "explanation": "x",
                },
                model=GENERATOR_MODEL,
                latency_ms=90,
                token_usage=TokenUsage(90, 9, 99),
                attempts=1,
            ),
        ]
    )
    executor = SpyExecutor(query_result(plan_qty=Decimal("1855.06")))
    agent = VerificationAgent(
        trace_id="trace-verification",
        llm_call=llm,
        executor=executor,
        clock=lambda: FIXED_NOW,
        query_id_factory=lambda: "sql-run-test",
    )

    draft = agent.answer("H2601五月预处理计划量")
    content = draft["payload"]["content"]

    assert content["event"] == "query_completed"
    assert content["family"] == "Q1"
    assert content["columns"] == ["plan_qty"]
    assert content["rows"] == [{"plan_qty": "1855.06"}]
    assert content["routing_predicted_family"] == "OUT_OF_SCOPE"
    assert content["routing_final_family"] == "Q1"
    assert content["routing_fallback"] is False
    assert content["routing_family_mismatch"] is False
    assert content["routing_model_disagreement"] is True
    assert content["routing_few_shot_ids"] == [
        "FS-02",
        "FS-03",
        "FS-06",
        "FS-07",
        "FS-09",
        "FS-11",
        "FS-14",
    ]
    assert executor.calls[0].endswith("LIMIT 200")
    assert len(llm.calls) == 2
    assert [call["max_attempts"] for call in llm.calls] == [1, 3]
    assert draft["model"] == GENERATOR_MODEL
    assert draft["token_usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
    }


def test_question_must_be_non_empty() -> None:
    agent, _, _ = build_agent(
        {"sql": None, "family": "OUT_OF_SCOPE", "explanation": "x"},
        query_result(),
    )

    with pytest.raises(ValueError, match="question"):
        agent.answer("  ")
