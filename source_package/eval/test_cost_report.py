from __future__ import annotations

from decimal import Decimal

import pytest

from eval.cost_report import cost_report


def _pricing() -> dict:
    return {
        "currency": "CNY",
        "unit": "per_million_tokens",
        "region": "中国内地",
        "retrieved_at": "2026-07-17",
        "source_url": "https://help.aliyun.com/zh/model-studio/model-pricing",
        "cache_policy": "standard_input_price_without_cache_discount",
        "prices": {
            "qwen3-32b": {
                "input_cny_per_million": "2",
                "output_cny_per_million": "8",
            },
            "qwen3-235b-a22b": {
                "input_cny_per_million": "2",
                "output_cny_per_million": "8",
            },
        },
        "traditional_training_estimate": {
            "development_hours": "8",
            "hourly_cny": "200",
            "learner_count": 50,
            "note": "人工粗估，可编辑；不是 trace 实测数据。",
        },
    }


def _text2sql_message() -> dict:
    return {
        "msg_id": "trace-001-004",
        "trace_id": "trace-001",
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {
                "routing_model": "qwen3-32b",
                "routing_prompt_tokens": 302,
                "routing_completion_tokens": 7,
                "routing_total_tokens": 309,
                "routing_cached_tokens": 12,
                "generation_model": "qwen3-235b-a22b",
                "generation_prompt_tokens": 2538,
                "generation_completion_tokens": 114,
                "generation_total_tokens": 2652,
                "generation_cached_tokens": 24,
            },
        },
        "model": "qwen3-235b-a22b",
        "token_usage": {
            "prompt_tokens": 2840,
            "completion_tokens": 121,
            "total_tokens": 2961,
        },
    }


def _dataset(*messages: dict) -> tuple[dict, ...]:
    return (
        {
            "case": {"case_id": "E2E-001", "profile_id": "planner_new"},
            "trace_id": "trace-001",
            "messages": list(messages),
        },
    )


def test_text2sql_split_usage_is_not_double_counted() -> None:
    report = cost_report(_dataset(_text2sql_message()), _pricing())

    assert report["models"]["qwen3-32b"]["prompt_tokens"] == 302
    assert report["models"]["qwen3-32b"]["completion_tokens"] == 7
    assert report["models"]["qwen3-235b-a22b"]["prompt_tokens"] == 2538
    assert report["models"]["qwen3-235b-a22b"]["completion_tokens"] == 114
    assert report["total_tokens"] == 302 + 7 + 2538 + 114
    assert report["cached_prompt_tokens_observed"] == 36


def test_normal_usage_is_attributed_by_message_model_and_cost_is_decimal_stable() -> None:
    normal = {
        "msg_id": "trace-001-002",
        "trace_id": "trace-001",
        "agent": "review",
        "role": "verdict",
        "payload": {"type": "review_verdict", "content": {}},
        "model": "qwen3-32b",
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }

    report = cost_report(_dataset(normal, _text2sql_message()), _pricing())

    assert report["models"]["qwen3-32b"]["prompt_tokens"] == 402
    assert report["models"]["qwen3-32b"]["completion_tokens"] == 27
    expected = (Decimal(402) * Decimal(2) + Decimal(27) * Decimal(8)) / Decimal(
        1_000_000
    )
    assert report["models"]["qwen3-32b"]["cost_cny"] == f"{expected:.6f}"
    assert report["cases"]["E2E-001"]["total_tokens"] == 3081
    assert report["profiles"]["planner_new"]["sessions"] == 1
    assert report["average_cost_per_session_cny"] == report["total_cost_cny"]
    assert report["traditional_training_comparison"]["measurement"] == "人工粗估"
    assert report["traditional_training_comparison"]["cost_per_learner_cny"] == "32.000000"


def test_text2sql_aggregate_must_equal_model_splits() -> None:
    message = _text2sql_message()
    message["token_usage"]["prompt_tokens"] = 9878
    message["token_usage"]["total_tokens"] = 9999

    with pytest.raises(ValueError, match="aggregate token usage does not match"):
        cost_report(_dataset(message), _pricing())


def test_nonzero_usage_for_unpriced_model_is_rejected() -> None:
    message = {
        "msg_id": "trace-001-001",
        "trace_id": "trace-001",
        "payload": {"type": "profile_assessment", "content": {}},
        "model": "unknown-model",
        "token_usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }

    with pytest.raises(ValueError, match="no price configured"):
        cost_report(_dataset(message), _pricing())
