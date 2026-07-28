from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
from typing import Any

import pytest

from agents.domain_config import load_domain_config
from eval.domain_swap_benchmark import (
    BenchmarkReport,
    compare_reports,
    load_attempts,
    run_benchmark,
    write_attempts,
)
from eval.first_segment_oracle import load_first_segment_cases
from orchestrator.llm import LLMCallError, LLMResult, TokenUsage


ROOT = Path(__file__).resolve().parents[1]
CASES = load_first_segment_cases(ROOT / "eval" / "cases" / "first_segment_50.jsonl")


def _llm_result(data: dict[str, Any], model: str) -> LLMResult:
    return LLMResult(
        data=data,
        model=model,
        latency_ms=3,
        token_usage=TokenUsage(10, 2, 12),
        attempts=1,
    )


class SequentialLLM:
    def __init__(self, outcomes: list[LLMResult | BaseException]) -> None:
        self.outcomes = outcomes

    def __call__(self, **_: Any) -> LLMResult:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _correct_llm(case_index: int) -> SequentialLLM:
    case = CASES[case_index]
    return SequentialLLM(
        [
            _llm_result({"family": case.expected_intent}, "router-test"),
            _llm_result(
                {
                    "sql": case.answer_key.expected_sql,
                    "family": case.expected_intent,
                    "explanation": "test",
                },
                "generator-test",
            ),
        ]
    )


def test_benchmark_scores_intent_sql_result_and_split(
    first_segment_csv_root: Path,
) -> None:
    case = CASES[2]
    report = run_benchmark(
        (case,),
        load_domain_config("first_segment"),
        first_segment_csv_root,
        _correct_llm(2),
        commit_sha="4818c28-test",
    )

    assert report.total == report.intent_correct == report.sql_correct == 1
    assert report.split_metrics["holdout"].component_accuracy == 1.0
    attempt = report.attempts[0]
    assert attempt.domain_sha256 == load_domain_config("first_segment").package_sha256
    assert attempt.executed_sql is not None
    assert attempt.expected_rows == attempt.actual_rows
    with pytest.raises(FrozenInstanceError):
        attempt.correct = False  # type: ignore[misc]


def test_comparison_reports_recovery_regression_and_holdout_separately(
    first_segment_csv_root: Path,
) -> None:
    first = run_benchmark(
        (CASES[0],),
        load_domain_config("first_segment"),
        first_segment_csv_root,
        _correct_llm(0),
        commit_sha="test",
    ).attempts[0]
    second = run_benchmark(
        (CASES[2],),
        load_domain_config("first_segment"),
        first_segment_csv_root,
        _correct_llm(2),
        commit_sha="test",
    ).attempts[0]
    v0 = BenchmarkReport.from_attempts(
        (
            replace(first, correct=False, sql_correct=False),
            second,
        )
    )
    v1 = BenchmarkReport.from_attempts(
        (
            first,
            replace(second, correct=False, sql_correct=False),
        )
    )

    comparison = compare_reports(v0, v1)

    assert comparison.recovery_case_ids == (first.case_id,)
    assert comparison.regression_case_ids == (second.case_id,)
    assert comparison.sql_recovery_case_ids == (first.case_id,)
    assert comparison.sql_regression_case_ids == (second.case_id,)
    assert comparison.intent_recovery_case_ids == ()
    assert comparison.intent_regression_case_ids == ()
    assert comparison.holdout_recovery_case_ids == ()
    assert comparison.holdout_regression_case_ids == (second.case_id,)


def test_attempt_writer_is_recomputable_and_redacts_exception_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_segment_csv_root: Path,
) -> None:
    secret = "test-secret-must-not-appear"
    monkeypatch.setenv("DASHSCOPE_API_KEY", secret)
    failing = SequentialLLM(
        [LLMCallError(f"provider rejected {secret}"), LLMCallError(secret)]
    )
    report = run_benchmark(
        (CASES[0],),
        load_domain_config("first_segment"),
        first_segment_csv_root,
        failing,
        commit_sha="test",
    )
    output = tmp_path / "attempts.jsonl"

    write_attempts(output, report.attempts)
    with pytest.raises(FileExistsError, match="already exist"):
        write_attempts(output, report.attempts)
    rendered = output.read_text(encoding="utf-8")

    assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert "system_prompt" not in rendered
    recomputed = BenchmarkReport.from_attempts(load_attempts(output))
    assert recomputed.as_dict() == report.as_dict()
    row = json.loads(rendered)
    assert row["source_hashes"]
