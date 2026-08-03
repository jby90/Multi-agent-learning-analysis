from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
import hashlib
import json

import pytest

from agents.sandbox import DatabaseSettings, ReadOnlyExecutor, validate_and_rewrite
from eval.text2sql_regression import (
    CASE_PATH,
    LatencySummary,
    ResultSet,
    assert_accuracy_gates,
    benchmark_payload,
    display_order_matches,
    evaluate_three_runs,
    load_cases,
    recompute_benchmark_summary,
    results_equivalent,
    routing_audit_errors,
    summarize_latencies,
)
from orchestrator.runtime import build_verification_agent


def test_corpus_has_approved_distribution() -> None:
    cases = load_cases(CASE_PATH)

    assert len(cases) == 40
    assert Counter(case.family for case in cases) == Counter(
        {**{f"Q{index}": 5 for index in range(1, 8)}, "OUT_OF_SCOPE": 5}
    )
    assert all(case.draft is True for case in cases)
    assert sum(case.is_metric_trap for case in cases) == 3
    assert all(case.question.strip() for case in cases)


def test_in_scope_cases_have_standards_and_refusals_do_not() -> None:
    cases = load_cases(CASE_PATH)

    for case in cases:
        if case.family == "OUT_OF_SCOPE":
            assert case.standard_sql is None
            assert case.expected_columns == ()
            assert case.expected_rows == ()
        else:
            assert case.standard_sql and case.standard_sql.startswith("SELECT")
            assert case.expected_columns
            assert case.expected_rows


def test_metric_traps_and_q3_ambiguity_match_the_approved_definition() -> None:
    cases = {case.case_id: case for case in load_cases(CASE_PATH)}
    traps = {case.case_id for case in cases.values() if case.is_metric_trap}

    assert traps == {"Q1-03", "Q2-03", "Q3-03"}
    assert "排产" in cases["Q1-03"].question
    assert cases["Q1-05"].question == "H2606二月预处理计划量是多少"
    assert "原计划" in cases["Q2-03"].question
    assert cases["Q3-03"].question == "H2601五月预处理完成情况相对计划偏差多少"
    assert "(SUM(actual_qty)-SUM(plan_qty))/SUM(plan_qty)" in str(
        cases["Q3-03"].standard_sql
    )
    assert cases["Q3-03"].expected_rows == ((Decimal("-0.3764"),),)


def test_all_aggregate_rate_standards_use_sum_ratios() -> None:
    cases = load_cases(CASE_PATH)

    for case in cases:
        sql = (case.standard_sql or "").upper()
        assert "AVG(COMPLETE_RATE)" not in sql, case.case_id
        assert "AVG(DEVIATION_RATE)" not in sql, case.case_id


def test_q4_and_q5_cases_declare_the_approved_display_order() -> None:
    cases = load_cases(CASE_PATH)

    assert all(case.sort_keys == ("month_label",) for case in cases if case.family == "Q4")
    assert all(case.sort_keys == ("complete_rate",) for case in cases if case.family == "Q5")


def test_results_equivalent_ignores_column_and_row_order_with_tolerance() -> None:
    expected = ResultSet(
        columns=("ship_no", "complete_rate"),
        rows=(("H2601", Decimal("0.6000000")), ("H2602", Decimal("0.9"))),
    )
    actual = ResultSet(
        columns=("complete_rate", "ship_no"),
        rows=((Decimal("0.9000004"), "H2602"), (Decimal("0.6"), "H2601")),
    )

    assert results_equivalent(expected, actual)


def test_results_equivalent_preserves_duplicate_rows() -> None:
    expected = ResultSet(columns=("value",), rows=((1,), (1,)))
    actual = ResultSet(columns=("value",), rows=((1,),))

    assert not results_equivalent(expected, actual)


def test_results_equivalent_compares_numeric_looking_strings_exactly() -> None:
    expected = ResultSet(columns=("batch_code",), rows=(("001",),))
    actual = ResultSet(columns=("batch_code",), rows=(("1",),))

    assert not results_equivalent(expected, actual)


def test_results_equivalent_does_not_coerce_identifier_string_to_integer() -> None:
    expected = ResultSet(columns=("batch_code",), rows=(("001",),))
    actual = ResultSet(columns=("batch_code",), rows=((1,),))

    assert not results_equivalent(expected, actual)


def test_results_equivalent_does_not_coerce_identifier_string_to_decimal() -> None:
    expected = ResultSet(columns=("batch_code",), rows=(("001",),))
    actual = ResultSet(columns=("batch_code",), rows=((Decimal("1"),),))

    assert not results_equivalent(expected, actual)


def test_results_equivalent_allows_numeric_string_for_metric_column() -> None:
    expected = ResultSet(columns=("complete_rate",), rows=((Decimal("1.000000"),),))
    actual = ResultSet(columns=("complete_rate",), rows=(("1.000001",),))

    assert results_equivalent(expected, actual)


def test_case_loader_marks_contract_metric_values_as_numeric() -> None:
    cases = {case.case_id: case for case in load_cases(CASE_PATH)}

    assert cases["Q1-01"].expected_rows == ((Decimal("1855.06"),),)
    assert cases["Q3-03"].expected_rows == ((Decimal("-0.3764"),),)


def test_results_equivalent_enforces_tolerance_null_date_and_string() -> None:
    baseline = ResultSet(
        columns=("complete_rate", "missing", "day", "label"),
        rows=((Decimal("1.000000"), None, date(2025, 5, 1), "YCL"),),
    )
    within = ResultSet(
        columns=baseline.columns,
        rows=((Decimal("1.000001"), None, "2025-05-01", "YCL"),),
    )
    outside = ResultSet(
        columns=baseline.columns,
        rows=((Decimal("1.000002"), None, "2025-05-01", "YCL"),),
    )
    wrong_string = ResultSet(
        columns=baseline.columns,
        rows=((Decimal("1"), None, "2025-05-01", "ZZTP"),),
    )

    assert results_equivalent(baseline, within)
    assert not results_equivalent(baseline, outside)
    assert not results_equivalent(baseline, wrong_string)


def test_display_order_checks_q4_q5_sort_keys_separately() -> None:
    ascending = (
        {"month_label": "2025-02", "complete_rate": Decimal("0.9")},
        {"month_label": "2025-03", "complete_rate": Decimal("0.8")},
    )
    descending = tuple(reversed(ascending))

    assert display_order_matches(ascending, ("month_label",))
    assert not display_order_matches(descending, ("month_label",))
    assert display_order_matches(descending, ("-month_label",))


def test_latency_summary_uses_approved_median_and_nearest_rank_p95() -> None:
    assert summarize_latencies(tuple(range(1, 121))) == LatencySummary(
        sample_count=120,
        median_ms=60.5,
        p95_ms=114,
        max_ms=120,
    )


def test_latency_summary_rejects_empty_samples() -> None:
    with pytest.raises(ValueError, match="latency sample"):
        summarize_latencies(())


@pytest.mark.live
def test_all_standard_sql_matches_stored_live_results() -> None:
    executor = ReadOnlyExecutor(DatabaseSettings.from_environment())

    for case in load_cases(CASE_PATH):
        if case.standard_sql is None:
            continue
        decision = validate_and_rewrite(case.standard_sql)
        assert decision.allowed, (case.case_id, decision.rule_id, decision.reason)
        assert decision.executed_sql is not None
        result = executor.execute(decision.executed_sql)
        actual = ResultSet.from_mapping_rows(result.columns, result.rows)
        assert results_equivalent(case.expected_result, actual), case.case_id
        if case.sort_keys:
            assert display_order_matches(result.rows, case.sort_keys), case.case_id


def test_three_run_report_calculates_all_approved_metrics() -> None:
    cases = (load_cases(CASE_PATH)[0], load_cases(CASE_PATH)[-1])

    def answer(case: object, run_index: int) -> dict:
        if case.family == "OUT_OF_SCOPE":
            return {
                "payload": {"content": {"event": "refuse_out_of_scope", "family": "OUT_OF_SCOPE"}},
                "latency_ms": 10 + run_index,
            }
        rows = [
            dict(zip(case.expected_columns, row, strict=True))
            for row in case.expected_rows
        ]
        return {
            "payload": {
                "content": {
                    "event": "query_completed",
                    "family": case.family,
                    "columns": list(reversed(case.expected_columns)),
                    "rows": [
                        {column: row[column] for column in reversed(case.expected_columns)}
                        for row in reversed(rows)
                    ],
                }
            },
            "latency_ms": 10 + run_index,
        }

    report = evaluate_three_runs(cases, answer)

    assert report.per_run_accuracy == (1.0, 1.0, 1.0)
    assert report.three_run_consistent_correctness == 1.0
    assert report.family_consistency == 1.0
    assert report.per_family_accuracy == {"OUT_OF_SCOPE": 1.0, "Q1": 1.0}
    assert report.max_latency_ms == 12


def test_three_run_report_detects_one_wrong_run_and_family_instability() -> None:
    case = load_cases(CASE_PATH)[0]

    def answer(case: object, run_index: int) -> dict:
        family = "Q2" if run_index == 2 else case.family
        return {
            "payload": {
                "content": {
                    "event": "query_completed",
                    "family": family,
                    "columns": list(case.expected_columns),
                    "rows": [
                        dict(zip(case.expected_columns, row, strict=True))
                        for row in case.expected_rows
                    ],
                }
            },
            "latency_ms": 10,
        }

    report = evaluate_three_runs((case,), answer)

    assert report.per_run_accuracy == (1.0, 1.0, 0.0)
    assert report.three_run_consistent_correctness == 0.0
    assert report.family_consistency == 0.0

    with pytest.raises(AssertionError, match="accuracy gate failed"):
        assert_accuracy_gates(report)


def test_benchmark_payload_is_raw_and_recomputable() -> None:
    cases = (load_cases(CASE_PATH)[0], load_cases(CASE_PATH)[-1])

    def answer(case: object, run_index: int) -> dict:
        content = {
            "event": "refuse_out_of_scope",
            "family": "OUT_OF_SCOPE",
            "llm_latency_ms": 7 + run_index,
        }
        if case.family != "OUT_OF_SCOPE":
            content = {
                "event": "query_completed",
                "family": case.family,
                "columns": list(case.expected_columns),
                "rows": [
                    dict(zip(case.expected_columns, row, strict=True))
                    for row in case.expected_rows
                ],
                "query_elapsed_ms": 2,
                "llm_latency_ms": 7 + run_index,
            }
        return {
            "payload": {"content": content},
            "model": "qwen3-235b-a22b",
            "latency_ms": 10 + run_index,
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
            },
        }

    report = evaluate_three_runs(cases, answer)
    payload = benchmark_payload(
        report,
        lever="unit_fixture",
        baseline_commit="text2sql-baseline",
        implementation_commit="telemetry-test",
        measured_at="2026-07-15T10:00:00+00:00",
    )

    assert payload["schema_version"] == 1
    assert payload["summary"]["latency"] == {
        "sample_count": 6,
        "median_ms": 11.0,
        "p95_ms": 12,
        "max_ms": 12,
    }
    assert payload["summary"]["per_run_accuracy"] == [1.0, 1.0, 1.0]
    assert len(payload["outcomes"]) == 6
    first = payload["outcomes"][0]
    assert first["routing_predicted_family"] is None
    assert first["routing_final_family"] == "Q1"
    assert first["routing_fallback"] is False
    assert first["routing_family_mismatch"] is False
    assert first["routing_prompt_profile"] == "FULL_15"
    assert first["routing_few_shot_ids"] == [
        f"FS-{index:02d}" for index in range(1, 16)
    ]
    assert first["generation_model"] == "qwen3-235b-a22b"
    assert first["generation_prompt_tokens"] == 100
    assert first["query_elapsed_ms"] == 2
    round_tripped = json.loads(json.dumps(payload, ensure_ascii=False))
    assert recompute_benchmark_summary(round_tripped["outcomes"]) == (
        round_tripped["summary"]
    )


def test_correct_measurement_rows_keep_only_generated_sql_hash() -> None:
    case = load_cases(CASE_PATH)[0]
    assert case.standard_sql is not None

    def answer(case: object, run_index: int) -> dict:
        return {
            "payload": {
                "content": {
                    "event": "query_completed",
                    "family": case.family,
                    "raw_llm_family": case.family,
                    "generated_sql": case.standard_sql,
                    "columns": list(case.expected_columns),
                    "rows": [
                        dict(zip(case.expected_columns, row, strict=True))
                        for row in case.expected_rows
                    ],
                }
            },
            "model": "qwen3-235b-a22b",
            "latency_ms": 10,
        }

    payload = benchmark_payload(
        evaluate_three_runs((case,), answer),
        lever="audit_fixture",
        baseline_commit="baseline",
        implementation_commit="implementation",
        measured_at="2026-07-15T10:00:00+00:00",
    )
    expected_hash = hashlib.sha256(
        case.standard_sql.encode("utf-8")
    ).hexdigest()

    assert len(payload["outcomes"]) == 3
    for row in payload["outcomes"]:
        assert row["correct"] is True
        assert row["generated_sql"] is None
        assert row["generated_sql_sha256"] == expected_hash
        assert row["sandbox_rule_id"] is None
        assert row["raw_llm_family"] == case.family


def test_failed_sandbox_measurement_keeps_full_sql_rule_and_raw_family() -> None:
    case = next(
        item for item in load_cases(CASE_PATH) if item.case_id == "Q2-02"
    )
    rejected_sql = (
        "SELECT SUM(plan_qty) AS plan_qty, SUM(actual_qty) AS actual_qty "
        "FROM forbidden_table"
    )

    def answer(case: object, run_index: int) -> dict:
        return {
            "payload": {
                "content": {
                    "event": "sandbox_rejected",
                    "family": "Q2",
                    "raw_llm_family": "Q2",
                    "generated_sql": rejected_sql,
                }
            },
            "evidence": [
                {
                    "kind": "review_rule",
                    "ref": "S-04",
                    "quote": "table is not allowed",
                }
            ],
            "model": "qwen3-235b-a22b",
            "latency_ms": 10,
        }

    payload = benchmark_payload(
        evaluate_three_runs((case,), answer),
        lever="audit_fixture",
        baseline_commit="baseline",
        implementation_commit="implementation",
        measured_at="2026-07-15T10:00:00+00:00",
    )
    expected_hash = hashlib.sha256(rejected_sql.encode("utf-8")).hexdigest()

    assert len(payload["outcomes"]) == 3
    for row in payload["outcomes"]:
        assert row["correct"] is False
        assert row["event"] == "sandbox_rejected"
        assert row["generated_sql"] == rejected_sql
        assert row["generated_sql_sha256"] == expected_hash
        assert row["sandbox_rule_id"] == "S-04"
        assert row["raw_llm_family"] == "Q2"


@pytest.mark.parametrize(
    ("generated_sql", "evidence", "message"),
    (
        (
            None,
            [{"kind": "review_rule", "ref": "S-04", "quote": "blocked"}],
            "generated_sql",
        ),
        (
            "SELECT * FROM forbidden_table",
            [],
            "sandbox_rule_id",
        ),
    ),
)
def test_sandbox_measurement_rejects_incomplete_failure_audit(
    generated_sql: str | None,
    evidence: list[dict[str, str]],
    message: str,
) -> None:
    case = next(
        item for item in load_cases(CASE_PATH) if item.case_id == "Q2-02"
    )

    def answer(case: object, run_index: int) -> dict:
        content = {
            "event": "sandbox_rejected",
            "family": "Q2",
            "raw_llm_family": "Q2",
        }
        if generated_sql is not None:
            content["generated_sql"] = generated_sql
        return {
            "payload": {"content": content},
            "evidence": evidence,
            "model": "qwen3-235b-a22b",
            "latency_ms": 10,
        }

    with pytest.raises(ValueError, match=message):
        evaluate_three_runs((case,), answer)


def test_run_live_benchmark_builds_exactly_120_recomputable_outcomes() -> None:
    from eval.run_text2sql_benchmark import run_live_benchmark

    def answer(case: object, run_index: int) -> dict:
        if case.family == "OUT_OF_SCOPE":
            content = {
                "event": "refuse_out_of_scope",
                "family": "OUT_OF_SCOPE",
                "llm_latency_ms": 1,
            }
        else:
            content = {
                "event": "query_completed",
                "family": case.family,
                "columns": list(case.expected_columns),
                "rows": [
                    dict(zip(case.expected_columns, row, strict=True))
                    for row in case.expected_rows
                ],
                "query_elapsed_ms": 1,
                "llm_latency_ms": 1,
            }
        return {
            "payload": {"content": content},
            "model": "qwen3-235b-a22b",
            "latency_ms": 2,
            "token_usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        }

    report, payload = run_live_benchmark(
        lever="unit_runner",
        baseline_commit="text2sql-baseline",
        answer=answer,
    )

    assert len(report.outcomes) == len(payload["outcomes"]) == 120
    assert payload["summary"]["per_run_accuracy"] == [1.0, 1.0, 1.0]
    assert payload["summary"] == benchmark_payload(
        report,
        lever="ignored",
        baseline_commit="ignored",
        implementation_commit="ignored",
        measured_at="ignored",
    )["summary"]


def test_benchmark_cli_writes_evidence_before_accuracy_gate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval import run_text2sql_benchmark as benchmark

    case = load_cases(CASE_PATH)[0]

    def wrong_answer(case: object, run_index: int) -> dict:
        family = "Q2" if run_index == 2 else case.family
        return {
            "payload": {
                "content": {
                    "event": "query_completed",
                    "family": family,
                    "columns": list(case.expected_columns),
                    "rows": [
                        dict(zip(case.expected_columns, row, strict=True))
                        for row in case.expected_rows
                    ],
                }
            },
            "latency_ms": 1,
        }

    failing_report = evaluate_three_runs((case,), wrong_answer)
    monkeypatch.setattr(
        benchmark,
        "run_live_benchmark",
        lambda **kwargs: (failing_report, {"marker": "written"}),
    )
    output = tmp_path / "failed-gate.json"

    with pytest.raises(AssertionError, match="accuracy gate failed"):
        benchmark.main(
            [
                "--lever",
                "unit_cli",
                "--baseline-commit",
                "text2sql-baseline",
                "--output",
                str(output),
            ]
        )

    assert json.loads(output.read_text(encoding="utf-8")) == {"marker": "written"}


def test_benchmark_cli_writes_evidence_before_routing_audit_gate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval import run_text2sql_benchmark as benchmark

    case = load_cases(CASE_PATH)[-1]

    def correct_but_unrouted(case: object, run_index: int) -> dict:
        return {
            "payload": {
                "content": {
                    "event": "refuse_out_of_scope",
                    "family": "OUT_OF_SCOPE",
                }
            },
            "model": "qwen3-235b-a22b",
            "latency_ms": 1,
        }

    report = evaluate_three_runs((case,), correct_but_unrouted)
    monkeypatch.setattr(
        benchmark,
        "run_live_benchmark",
        lambda **kwargs: (report, {"marker": "routing-written"}),
    )
    output = tmp_path / "failed-routing.json"

    with pytest.raises(AssertionError, match="routing audit failed"):
        benchmark.main(
            [
                "--lever",
                "family_routing",
                "--baseline-commit",
                "text2sql-baseline",
                "--output",
                str(output),
            ]
        )

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "marker": "routing-written"
    }


def valid_routing_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "case_id": "Q5-01",
        "run_index": 0,
        "reported_family": "Q5",
        "routing_predicted_family": "Q5",
        "routing_final_family": "Q5",
        "routing_fallback": False,
        "routing_family_mismatch": False,
        "routing_fallback_reason": "none",
        "routing_prompt_profile": "Q5",
        "routing_few_shot_ids": ["FS-09", "FS-10", "FS-13", "FS-14"],
        "model": "qwen3-235b-a22b",
        "routing_model": "qwen3-32b",
        "generation_model": "qwen3-235b-a22b",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"routing_predicted_family": None}, "prediction"),
        ({"routing_few_shot_ids": ["FS-09"]}, "few-shot"),
        ({"routing_prompt_profile": "Q7"}, "profile"),
        ({"routing_family_mismatch": True}, "mismatch"),
        ({"routing_fallback_reason": "router_error"}, "reason"),
        ({"routing_final_family": "Q7"}, "final family"),
        ({"routing_model": "wrong"}, "routing model"),
        ({"generation_model": "wrong"}, "generation model"),
        ({"model": "wrong"}, "top-level model"),
    ),
)
def test_routing_audit_rejects_each_non_fallback_contract_violation(
    overrides: dict[str, object], message: str
) -> None:
    errors = routing_audit_errors((valid_routing_row(**overrides),))

    assert len(errors) == 1
    assert message in errors[0]


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"routing_predicted_family": "Q1"}, "prediction"),
        ({"routing_family_mismatch": True}, "mismatch"),
        ({"routing_fallback_reason": "none"}, "reason"),
        ({"routing_prompt_profile": "Q1"}, "profile"),
        ({"routing_few_shot_ids": ["FS-01"]}, "few-shot"),
    ),
)
def test_routing_audit_rejects_each_fallback_contract_violation(
    overrides: dict[str, object], message: str
) -> None:
    fallback_values: dict[str, object] = {
        "reported_family": "Q1",
        "routing_predicted_family": None,
        "routing_final_family": "Q1",
        "routing_fallback": True,
        "routing_family_mismatch": False,
        "routing_fallback_reason": "router_error",
        "routing_prompt_profile": "FULL_15",
        "routing_few_shot_ids": [
            f"FS-{index:02d}" for index in range(1, 16)
        ],
    }
    fallback_values.update(overrides)
    fallback = valid_routing_row(**fallback_values)

    errors = routing_audit_errors((fallback,))

    assert len(errors) == 1
    assert message in errors[0]


def test_routing_audit_accepts_valid_mismatch_and_router_fallback() -> None:
    mismatch = valid_routing_row(
        reported_family="Q7",
        routing_final_family="Q7",
        routing_family_mismatch=True,
    )
    fallback = valid_routing_row(
        reported_family="Q1",
        routing_predicted_family=None,
        routing_final_family="Q1",
        routing_fallback=True,
        routing_fallback_reason="router_error",
        routing_prompt_profile="FULL_15",
        routing_few_shot_ids=[f"FS-{index:02d}" for index in range(1, 16)],
    )

    assert routing_audit_errors((mismatch, fallback)) == ()


def _live_answer(case: object, run_index: int) -> dict:
    trace_id = f"eval-{case.case_id.lower()}-run-{run_index + 1}"
    draft = build_verification_agent(trace_id).answer(case.question)
    content = draft["payload"]["content"]
    print(
        f"LIVE run={run_index + 1} case={case.case_id} "
        f"expected={case.family} actual={content.get('family')} "
        f"event={content.get('event')} latency_ms={draft.get('latency_ms')}"
    )
    return draft


@pytest.mark.live
def test_live_text2sql_regression_three_runs() -> None:
    cases = load_cases(CASE_PATH)

    report = evaluate_three_runs(cases, _live_answer)

    print(f"per_run_accuracy={report.per_run_accuracy}")
    print(
        "three_run_consistent_correctness="
        f"{report.three_run_consistent_correctness:.4f}"
    )
    print(f"family_consistency={report.family_consistency:.4f}")
    print(f"per_family_accuracy={report.per_family_accuracy}")
    print(f"max_latency_ms={report.max_latency_ms}")
    assert report.per_run_accuracy == (1.0, 1.0, 1.0)
    assert report.three_run_consistent_correctness == 1.0
    assert report.family_consistency == 1.0
