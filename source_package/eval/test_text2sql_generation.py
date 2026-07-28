from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from agents.prompts.build_verification_prompt import (
    DICTIONARY_PATH,
    IMPORT_SQL_PATH,
    build_prompt_catalog,
    extract_few_shot_examples,
)
from agents.text2sql_generation import (
    GENERATOR_MODEL,
    ROUTER_MODEL,
    Text2SQLGenerationCoordinator,
)
from orchestrator.llm import LLMCallError, LLMResult, TokenUsage


def llm_result(
    data: dict[str, Any],
    *,
    model: str,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> LLMResult:
    return LLMResult(
        data=data,
        model=model,
        latency_ms=latency_ms,
        token_usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cached_tokens=cached_tokens,
        ),
        attempts=1,
    )


class SequentialLLM:
    def __init__(self, outcomes: list[LLMResult | BaseException]) -> None:
        self._outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def prompt_catalog():
    return build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )


def test_coordinator_uses_the_catalog_owned_router_and_generator_schemas() -> None:
    router_schema = {"type": "object", "properties": {"family": {"const": "PILOT"}}}
    generation_schema = {
        "type": "object",
        "properties": {"family": {"const": "PILOT"}},
    }
    catalog = replace(
        prompt_catalog(),
        router_output_schema=router_schema,
        text2sql_output_schema=generation_schema,
    )
    llm = SequentialLLM(
        [
            llm_result(
                {"family": "Q1"},
                model=ROUTER_MODEL,
                latency_ms=1,
                prompt_tokens=1,
                completion_tokens=1,
            ),
            llm_result(
                {"sql": None, "family": "OUT_OF_SCOPE", "explanation": "x"},
                model=GENERATOR_MODEL,
                latency_ms=1,
                prompt_tokens=1,
                completion_tokens=1,
            ),
        ]
    )

    Text2SQLGenerationCoordinator(llm_call=llm, prompt_catalog=catalog).generate("x")

    assert llm.calls[0]["json_schema"] is router_schema
    assert llm.calls[1]["json_schema"] is generation_schema


def test_q5_route_uses_exact_q5_q7_examples_and_aggregates_telemetry() -> None:
    llm = SequentialLLM(
        [
            llm_result(
                {"family": "Q5"},
                model=ROUTER_MODEL,
                latency_ms=30,
                prompt_tokens=20,
                completion_tokens=2,
                cached_tokens=1,
            ),
            llm_result(
                {
                    "sql": "SELECT ship_no, 1 AS complete_rate FROM dim_ship",
                    "family": "Q5",
                    "explanation": "x",
                },
                model=GENERATOR_MODEL,
                latency_ms=80,
                prompt_tokens=100,
                completion_tokens=10,
                cached_tokens=7,
            ),
        ]
    )
    coordinator = Text2SQLGenerationCoordinator(
        llm_call=llm,
        prompt_catalog=prompt_catalog(),
    )

    result = coordinator.generate("五月各船预处理完成率排名")

    assert result.routing_predicted_family == result.routing_final_family == "Q5"
    assert result.routing_prompt_profile == "Q5"
    assert result.routing_few_shot_ids == (
        "FS-09",
        "FS-10",
        "FS-13",
        "FS-14",
    )
    assert result.routing_fallback is False
    assert result.routing_family_mismatch is False
    assert result.model == GENERATOR_MODEL
    assert result.latency_ms == 110
    assert result.token_usage == TokenUsage(120, 12, 132, cached_tokens=8)
    assert [call["max_attempts"] for call in llm.calls] == [1, 3]
    assert [call["temperature"] for call in llm.calls] == [0.0, 0.1]
    rendered = extract_few_shot_examples(llm.calls[1]["system"])
    assert tuple(item.example_id for item in rendered) == result.routing_few_shot_ids
    content = result.routing_content()
    assert content["raw_llm_family"] == "Q5"
    assert content["routing_model"] == ROUTER_MODEL
    assert content["generation_model"] == GENERATOR_MODEL
    assert content["routing_cached_tokens"] == 1
    assert content["generation_cached_tokens"] == 7


def test_out_of_scope_prediction_mismatch_keeps_first_235b_q1_result() -> None:
    generated = {
        "sql": "SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress",
        "family": "Q1",
        "explanation": "x",
    }
    llm = SequentialLLM(
        [
            llm_result(
                {"family": "OUT_OF_SCOPE"},
                model=ROUTER_MODEL,
                latency_ms=15,
                prompt_tokens=10,
                completion_tokens=1,
            ),
            llm_result(
                generated,
                model=GENERATOR_MODEL,
                latency_ms=60,
                prompt_tokens=90,
                completion_tokens=8,
            ),
        ]
    )
    coordinator = Text2SQLGenerationCoordinator(
        llm_call=llm,
        prompt_catalog=prompt_catalog(),
    )

    result = coordinator.generate("H2601五月预处理计划量")

    assert result.data is generated
    assert result.routing_predicted_family == "OUT_OF_SCOPE"
    assert result.routing_final_family == "Q1"
    assert result.routing_prompt_profile == "OUT_OF_SCOPE_MINI_7"
    assert result.routing_few_shot_ids == (
        "FS-02",
        "FS-03",
        "FS-06",
        "FS-07",
        "FS-09",
        "FS-11",
        "FS-14",
    )
    assert result.routing_fallback is False
    assert result.routing_family_mismatch is True
    assert result.routing_fallback_reason == "none"
    assert len(llm.calls) == 2


def test_router_error_uses_full15_once_without_counting_mismatch() -> None:
    clock: Iterator[float] = iter((1.0, 1.025))
    llm = SequentialLLM(
        [
            LLMCallError("router failed"),
            llm_result(
                {"sql": None, "family": "OUT_OF_SCOPE", "explanation": "x"},
                model=GENERATOR_MODEL,
                latency_ms=90,
                prompt_tokens=140,
                completion_tokens=5,
            ),
        ]
    )
    coordinator = Text2SQLGenerationCoordinator(
        llm_call=llm,
        prompt_catalog=prompt_catalog(),
        clock=lambda: next(clock),
    )

    result = coordinator.generate("如何改进食堂菜谱")

    assert result.routing_predicted_family is None
    assert result.routing_final_family == "OUT_OF_SCOPE"
    assert result.routing_prompt_profile == "FULL_15"
    assert result.routing_few_shot_ids == tuple(
        f"FS-{index:02d}" for index in range(1, 16)
    )
    assert result.routing_fallback is True
    assert result.routing_family_mismatch is False
    assert result.routing_fallback_reason == "router_error"
    assert result.routing_latency_ms == 25
    assert result.generation_latency_ms == 90
    assert result.latency_ms == 115
    assert result.token_usage == TokenUsage(140, 5, 145)
    assert len(llm.calls) == 2
    assert llm.calls[0]["max_attempts"] == 1
    assert llm.calls[1]["max_attempts"] == 3
    rendered = extract_few_shot_examples(llm.calls[1]["system"])
    assert tuple(item.example_id for item in rendered) == result.routing_few_shot_ids


def test_template_family_overrides_generator_self_label_and_directs_prompt() -> None:
    generated = {
        "sql": (
            "SELECT process_code, "
            "ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
            "FROM fact_production_progress WHERE ship_no='H2601' "
            "AND period_date>='2025-05-01' AND period_date<'2025-06-01' "
            "GROUP BY process_code"
        ),
        "family": "Q3",
        "explanation": "按工序计算完成率",
    }
    llm = SequentialLLM(
        [
            llm_result(
                {"family": "Q6"},
                model=ROUTER_MODEL,
                latency_ms=20,
                prompt_tokens=10,
                completion_tokens=1,
            ),
            llm_result(
                generated,
                model=GENERATOR_MODEL,
                latency_ms=60,
                prompt_tokens=90,
                completion_tokens=8,
            ),
        ]
    )
    coordinator = Text2SQLGenerationCoordinator(
        llm_call=llm,
        prompt_catalog=prompt_catalog(),
    )
    authority = {
        "source": "task_template",
        "template_id": "T-02-A",
        "family": "Q6",
        "standard_stem": "查询H26012025-05三道工序的完成率",
        "output_columns": ["process_code", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["process_code"],
        "filter_columns": ["period_date", "ship_no"],
        "group_by_columns": ["process_code"],
        "time_column": "period_date",
        "time_values": ["2025-05"],
    }

    result = coordinator.generate(
        "作为计划员，查询H26012025-05三道工序的完成率",
        query_authority=authority,
    )

    assert result.data is generated
    assert result.routing_final_family == "Q6"
    assert result.routing_prompt_profile == "Q6"
    assert result.routing_family_mismatch is True
    assert result.routing_content()["raw_llm_family"] == "Q3"
    assert llm.calls[1]["user"] == authority["standard_stem"]
    assert tuple(
        item.example_id
        for item in extract_few_shot_examples(llm.calls[1]["system"])
    ) == result.routing_few_shot_ids
