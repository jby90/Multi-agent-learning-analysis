"""Reusable result-equivalence and corpus helpers for Text2SQL evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import ceil
from pathlib import Path
import re
from statistics import median
from typing import Any, Callable, Mapping, Sequence

from agents.prompts.build_verification_prompt import (
    FULL_15_IDS,
    ROUTED_FEW_SHOT_IDS,
    ROUTER_OUTPUT_SCHEMA,
)
from agents.text2sql_generation import GENERATOR_MODEL, ROUTER_MODEL


CASE_PATH = Path(__file__).with_name("cases") / "text2sql_regression.jsonl"
NUMERIC_TOLERANCE = Decimal("0.000001")
_NUMERIC_RE = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")
NUMERIC_RESULT_COLUMNS = frozenset(
    {
        "plan_qty",
        "actual_qty",
        "complete_rate",
        "deviation_rate",
        "high_risk_rows",
    }
)
FULL_15_FEW_SHOT_IDS = tuple(f"FS-{index:02d}" for index in range(1, 16))


@dataclass(frozen=True, slots=True)
class ResultSet:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    @classmethod
    def from_mapping_rows(
        cls, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]
    ) -> "ResultSet":
        resolved_columns = tuple(columns)
        return cls(
            columns=resolved_columns,
            rows=tuple(
                tuple(row.get(column) for column in resolved_columns) for row in rows
            ),
        )


@dataclass(frozen=True, slots=True)
class RegressionCase:
    case_id: str
    draft: bool
    family: str
    question: str
    standard_sql: str | None
    expected_columns: tuple[str, ...]
    expected_rows: tuple[tuple[Any, ...], ...]
    sort_keys: tuple[str, ...]
    is_metric_trap: bool

    @property
    def expected_result(self) -> ResultSet:
        return ResultSet(self.expected_columns, self.expected_rows)


@dataclass(frozen=True, slots=True)
class RegressionOutcome:
    case_id: str
    run_index: int
    expected_family: str
    reported_family: str
    event: str
    correct: bool
    result: ResultSet | None
    latency_ms: int
    generated_sql: str | None = None
    generated_sql_sha256: str | None = None
    sandbox_rule_id: str | None = None
    raw_llm_family: str = ""
    routing_predicted_family: str | None = None
    routing_final_family: str = ""
    routing_fallback: bool = False
    routing_family_mismatch: bool = False
    routing_fallback_reason: str = "none"
    routing_prompt_profile: str = "FULL_15"
    routing_few_shot_ids: tuple[str, ...] = ()
    routing_latency_ms: int = 0
    generation_latency_ms: int = 0
    query_elapsed_ms: int = 0
    routing_prompt_tokens: int = 0
    routing_cached_tokens: int = 0
    generation_prompt_tokens: int = 0
    generation_cached_tokens: int = 0
    model: str = ""
    routing_model: str = ""
    generation_model: str = ""
    routing_completion_tokens: int = 0
    routing_total_tokens: int = 0
    generation_completion_tokens: int = 0
    generation_total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LatencySummary:
    sample_count: int
    median_ms: float
    p95_ms: int
    max_ms: int


@dataclass(frozen=True, slots=True)
class RegressionReport:
    per_run_accuracy: tuple[float, float, float]
    three_run_consistent_correctness: float
    family_consistency: float
    per_family_accuracy: dict[str, float]
    max_latency_ms: int
    latency_summary: LatencySummary
    outcomes: tuple[RegressionOutcome, ...]


def summarize_latencies(values: Sequence[int]) -> LatencySummary:
    if not values:
        raise ValueError("at least one latency sample is required")
    ordered = sorted(int(value) for value in values)
    return LatencySummary(
        sample_count=len(ordered),
        median_ms=float(median(ordered)),
        p95_ms=ordered[ceil(0.95 * len(ordered)) - 1],
        max_ms=ordered[-1],
    )


def assert_accuracy_gates(report: RegressionReport) -> None:
    approved = (
        report.per_run_accuracy == (1.0, 1.0, 1.0)
        and report.three_run_consistent_correctness == 1.0
        and report.family_consistency == 1.0
        and all(value == 1.0 for value in report.per_family_accuracy.values())
    )
    if not approved:
        raise AssertionError(
            "accuracy gate failed: "
            f"runs={report.per_run_accuracy}, "
            f"consistent={report.three_run_consistent_correctness}, "
            f"family={report.family_consistency}, "
            f"per_family={report.per_family_accuracy}"
        )


def _expected_value(column: str, value: Any) -> Any:
    if (
        column in NUMERIC_RESULT_COLUMNS
        and isinstance(value, str)
        and _NUMERIC_RE.fullmatch(value)
    ):
        return Decimal(value)
    return value


def load_cases(path: Path = CASE_PATH) -> tuple[RegressionCase, ...]:
    cases: list[RegressionCase] = []
    with path.open(encoding="utf-8") as case_file:
        for line_number, line in enumerate(case_file, start=1):
            if not line.strip():
                continue
            data = json.loads(line)
            columns = tuple(data.get("expected_columns", []))
            row_mappings = data.get("expected_rows", [])
            rows = tuple(
                tuple(
                    _expected_value(column, row.get(column))
                    for column in columns
                )
                for row in row_mappings
            )
            cases.append(
                RegressionCase(
                    case_id=str(data["id"]),
                    draft=bool(data["draft"]),
                    family=str(data["family"]),
                    question=str(data["question"]),
                    standard_sql=data.get("standard_sql"),
                    expected_columns=columns,
                    expected_rows=rows,
                    sort_keys=tuple(data.get("sort_keys", [])),
                    is_metric_trap=bool(data.get("is_metric_trap", False)),
                )
            )
    return tuple(cases)


def _canonical_scalar(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str) and _NUMERIC_RE.fullmatch(value):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def _values_equal(
    expected: Any,
    actual: Any,
    tolerance: Decimal,
    *,
    numeric: bool,
) -> bool:
    expected = _canonical_scalar(expected)
    actual = _canonical_scalar(actual)
    if numeric:
        expected_number = _as_decimal(expected)
        actual_number = _as_decimal(actual)
        if expected_number is not None and actual_number is not None:
            return abs(expected_number - actual_number) <= tolerance
    return expected == actual


def _rows_equal(
    expected: tuple[Any, ...],
    actual: tuple[Any, ...],
    columns: tuple[str, ...],
    tolerance: Decimal,
) -> bool:
    return len(expected) == len(actual) == len(columns) and all(
        _values_equal(
            expected_value,
            actual_value,
            tolerance,
            numeric=column in NUMERIC_RESULT_COLUMNS,
        )
        for column, expected_value, actual_value in zip(
            columns, expected, actual, strict=True
        )
    )


def results_equivalent(
    expected: ResultSet,
    actual: ResultSet,
    tolerance: Decimal = NUMERIC_TOLERANCE,
) -> bool:
    """Compare column names and an unordered row multiset with numeric tolerance."""

    if len(expected.columns) != len(actual.columns):
        return False
    if set(expected.columns) != set(actual.columns):
        return False
    actual_indexes = [actual.columns.index(column) for column in expected.columns]
    reordered_actual = [
        tuple(row[index] for index in actual_indexes) for row in actual.rows
    ]
    if len(expected.rows) != len(reordered_actual):
        return False
    unmatched = list(reordered_actual)
    for expected_row in expected.rows:
        match_index = next(
            (
                index
                for index, actual_row in enumerate(unmatched)
                if _rows_equal(expected_row, actual_row, expected.columns, tolerance)
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched.pop(match_index)
    return not unmatched


def _ordering_value(value: Any) -> tuple[int, Any]:
    value = _canonical_scalar(value)
    if value is None:
        return (0, "")
    number = _as_decimal(value)
    if number is not None:
        return (1, number)
    return (2, str(value))


def display_order_matches(
    rows: Sequence[Mapping[str, Any]], sort_keys: Sequence[str]
) -> bool:
    """Check declared display sort keys without changing result-set equivalence."""

    if not sort_keys or len(rows) < 2:
        return True
    for left, right in zip(rows, rows[1:]):
        for key_spec in sort_keys:
            descending = key_spec.startswith("-")
            key = key_spec[1:] if descending else key_spec
            if key not in left or key not in right:
                return False
            left_value = _ordering_value(left[key])
            right_value = _ordering_value(right[key])
            if left_value == right_value:
                continue
            if descending and left_value < right_value:
                return False
            if not descending and left_value > right_value:
                return False
            break
    return True


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(str(item) for item in value)


def _sandbox_rule_id(draft: Mapping[str, Any], event: str) -> str | None:
    if event != "sandbox_rejected":
        return None
    evidence = draft.get("evidence", [])
    if not isinstance(evidence, (list, tuple)):
        return None
    for item in evidence:
        if not isinstance(item, Mapping) or item.get("kind") != "review_rule":
            continue
        ref = item.get("ref")
        if isinstance(ref, str) and ref:
            return ref
    return None


def _outcome_from_draft(
    case: RegressionCase, run_index: int, draft: Mapping[str, Any]
) -> RegressionOutcome:
    payload = draft.get("payload", {})
    content = payload.get("content", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(content, Mapping):
        content = {}
    event = str(content.get("event", ""))
    family = str(content.get("family", ""))
    latency_ms = _integer(draft.get("latency_ms", 0))
    if case.family == "OUT_OF_SCOPE":
        correct = event == "refuse_out_of_scope" and family == "OUT_OF_SCOPE"
        result: ResultSet | None = None
    else:
        columns_value = content.get("columns", [])
        rows_value = content.get("rows", [])
        columns = tuple(str(column) for column in columns_value) if isinstance(columns_value, list) else ()
        row_mappings = (
            tuple(row for row in rows_value if isinstance(row, Mapping))
            if isinstance(rows_value, list)
            else ()
        )
        result = ResultSet.from_mapping_rows(columns, row_mappings)
        ordered = display_order_matches(row_mappings, case.sort_keys)
        correct = (
            event == "query_completed"
            and family == case.family
            and results_equivalent(case.expected_result, result)
            and ordered
        )
    token_usage = draft.get("token_usage", {})
    if not isinstance(token_usage, Mapping):
        token_usage = {}
    predicted_value = content.get("routing_predicted_family")
    predicted_family = (
        str(predicted_value) if isinstance(predicted_value, str) else None
    )
    model = str(draft.get("model", ""))
    generated_value = content.get("generated_sql")
    generated_text = (
        generated_value if isinstance(generated_value, str) else None
    )
    generated_hash = (
        hashlib.sha256(generated_text.encode("utf-8")).hexdigest()
        if generated_text is not None
        else None
    )
    normal_event = event in {"query_completed", "refuse_out_of_scope"}
    retained_generated_sql = (
        generated_text if not correct or not normal_event else None
    )
    raw_family_value = content.get(
        "raw_llm_family",
        content.get("routing_final_family", family),
    )
    sandbox_rule_id = _sandbox_rule_id(draft, event)
    if event == "sandbox_rejected":
        if generated_text is None or not generated_text.strip():
            raise ValueError(
                "sandbox_rejected measurement requires generated_sql"
            )
        if sandbox_rule_id is None:
            raise ValueError(
                "sandbox_rejected measurement requires sandbox_rule_id"
            )
    return RegressionOutcome(
        case_id=case.case_id,
        run_index=run_index,
        expected_family=case.family,
        reported_family=family,
        event=event,
        correct=correct,
        result=result,
        latency_ms=latency_ms,
        generated_sql=retained_generated_sql,
        generated_sql_sha256=generated_hash,
        sandbox_rule_id=sandbox_rule_id,
        raw_llm_family=str(raw_family_value),
        routing_predicted_family=predicted_family,
        routing_final_family=str(content.get("routing_final_family", family)),
        routing_fallback=content.get("routing_fallback") is True,
        routing_family_mismatch=content.get("routing_family_mismatch") is True,
        routing_fallback_reason=str(
            content.get("routing_fallback_reason", "none")
        ),
        routing_prompt_profile=str(
            content.get("routing_prompt_profile", "FULL_15")
        ),
        routing_few_shot_ids=_string_tuple(
            content.get("routing_few_shot_ids"), FULL_15_FEW_SHOT_IDS
        ),
        routing_latency_ms=_integer(content.get("routing_llm_latency_ms", 0)),
        generation_latency_ms=_integer(
            content.get(
                "generation_llm_latency_ms",
                content.get("llm_latency_ms", 0),
            )
        ),
        query_elapsed_ms=_integer(content.get("query_elapsed_ms", 0)),
        routing_prompt_tokens=_integer(content.get("routing_prompt_tokens", 0)),
        routing_cached_tokens=_integer(content.get("routing_cached_tokens", 0)),
        generation_prompt_tokens=_integer(
            content.get(
                "generation_prompt_tokens",
                token_usage.get("prompt_tokens", 0),
            )
        ),
        generation_cached_tokens=_integer(
            content.get("generation_cached_tokens", 0)
        ),
        model=model,
        routing_model=str(content.get("routing_model", "")),
        generation_model=str(content.get("generation_model", model)),
        routing_completion_tokens=_integer(
            content.get("routing_completion_tokens", 0)
        ),
        routing_total_tokens=_integer(content.get("routing_total_tokens", 0)),
        generation_completion_tokens=_integer(
            content.get(
                "generation_completion_tokens",
                token_usage.get("completion_tokens", 0),
            )
        ),
        generation_total_tokens=_integer(
            content.get(
                "generation_total_tokens",
                token_usage.get("total_tokens", 0),
            )
        ),
    )


def evaluate_three_runs(
    cases: Sequence[RegressionCase],
    answer: Callable[[RegressionCase, int], Mapping[str, Any]],
) -> RegressionReport:
    """Run every case exactly three times and compute the approved metrics."""

    if not cases:
        raise ValueError("at least one regression case is required")
    outcomes = tuple(
        _outcome_from_draft(case, run_index, answer(case, run_index))
        for run_index in range(3)
        for case in cases
    )
    per_run_accuracy = tuple(
        sum(outcome.correct for outcome in outcomes if outcome.run_index == run_index)
        / len(cases)
        for run_index in range(3)
    )
    by_case = {
        case.case_id: tuple(
            outcome for outcome in outcomes if outcome.case_id == case.case_id
        )
        for case in cases
    }
    consistent_correct = 0
    family_consistent = 0
    for case in cases:
        case_outcomes = by_case[case.case_id]
        if len({outcome.reported_family for outcome in case_outcomes}) == 1:
            family_consistent += 1
        if not all(outcome.correct for outcome in case_outcomes):
            continue
        if case.family == "OUT_OF_SCOPE":
            consistent_correct += 1
            continue
        results = [outcome.result for outcome in case_outcomes]
        if all(result is not None for result in results) and all(
            results_equivalent(results[0], result)  # type: ignore[arg-type]
            for result in results[1:]
        ):
            consistent_correct += 1
    families = sorted({case.family for case in cases})
    per_family_accuracy = {
        family: (
            sum(
                outcome.correct
                for outcome in outcomes
                if outcome.expected_family == family
            )
            / sum(1 for outcome in outcomes if outcome.expected_family == family)
        )
        for family in families
    }
    return RegressionReport(
        per_run_accuracy=per_run_accuracy,  # type: ignore[arg-type]
        three_run_consistent_correctness=consistent_correct / len(cases),
        family_consistency=family_consistent / len(cases),
        per_family_accuracy=per_family_accuracy,
        max_latency_ms=max(outcome.latency_ms for outcome in outcomes),
        latency_summary=summarize_latencies(
            tuple(outcome.latency_ms for outcome in outcomes)
        ),
        outcomes=outcomes,
    )


def _outcome_value(outcome: RegressionOutcome | Mapping[str, Any], name: str) -> Any:
    if isinstance(outcome, RegressionOutcome):
        return getattr(outcome, name)
    return outcome.get(name)


def routing_audit_errors(
    outcomes: Sequence[RegressionOutcome | Mapping[str, Any]],
) -> tuple[str, ...]:
    allowed_predictions = frozenset(
        ROUTER_OUTPUT_SCHEMA["properties"]["family"]["enum"]
    )
    errors: list[str] = []
    for outcome in outcomes:
        case_id = str(_outcome_value(outcome, "case_id"))
        run_index = _integer(_outcome_value(outcome, "run_index"))
        prefix = f"case={case_id} run={run_index + 1}"
        predicted = _outcome_value(outcome, "routing_predicted_family")
        final_family = str(_outcome_value(outcome, "routing_final_family"))
        reported_family = str(_outcome_value(outcome, "reported_family"))
        fallback = _outcome_value(outcome, "routing_fallback") is True
        mismatch = _outcome_value(outcome, "routing_family_mismatch") is True
        reason = str(_outcome_value(outcome, "routing_fallback_reason"))
        profile = str(_outcome_value(outcome, "routing_prompt_profile"))
        few_shot_ids = _string_tuple(
            _outcome_value(outcome, "routing_few_shot_ids"), ()
        )

        final_matches_content = final_family == reported_family
        if final_family not in allowed_predictions or not final_matches_content:
            errors.append(
                f"{prefix}: final family must be valid and equal content family"
            )

        if fallback:
            if predicted is not None:
                errors.append(f"{prefix}: fallback prediction must be null")
            if mismatch:
                errors.append(f"{prefix}: fallback mismatch must be false")
            if reason != "router_error":
                errors.append(f"{prefix}: fallback reason must be router_error")
            if profile != "FULL_15":
                errors.append(f"{prefix}: fallback profile must be FULL_15")
            if few_shot_ids != FULL_15_IDS:
                errors.append(f"{prefix}: fallback few-shot IDs must be FULL_15")
        else:
            if predicted not in allowed_predictions:
                errors.append(f"{prefix}: non-fallback prediction is missing or invalid")
            else:
                expected_profile = (
                    "OUT_OF_SCOPE_MINI_7"
                    if predicted == "OUT_OF_SCOPE"
                    else str(predicted)
                )
                if profile != expected_profile:
                    errors.append(
                        f"{prefix}: non-fallback profile must be {expected_profile}"
                    )
                expected_ids = ROUTED_FEW_SHOT_IDS[expected_profile]
                if few_shot_ids != expected_ids:
                    errors.append(
                        f"{prefix}: non-fallback few-shot IDs differ from matrix"
                    )
                if final_matches_content and mismatch != (predicted != final_family):
                    errors.append(
                        f"{prefix}: mismatch does not equal predicted/final difference"
                    )
            if reason != "none":
                errors.append(f"{prefix}: non-fallback reason must be none")

        if str(_outcome_value(outcome, "routing_model")) != ROUTER_MODEL:
            errors.append(f"{prefix}: routing model must be {ROUTER_MODEL}")
        if str(_outcome_value(outcome, "generation_model")) != GENERATOR_MODEL:
            errors.append(f"{prefix}: generation model must be {GENERATOR_MODEL}")
        if str(_outcome_value(outcome, "model")) != GENERATOR_MODEL:
            errors.append(f"{prefix}: top-level model must be {GENERATOR_MODEL}")
    return tuple(errors)


def _result_from_outcome(
    outcome: RegressionOutcome | Mapping[str, Any],
) -> ResultSet | None:
    value = _outcome_value(outcome, "result")
    if value is None or isinstance(value, ResultSet):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("outcome result must be an object or null")
    columns_value = value.get("columns", [])
    rows_value = value.get("rows", [])
    if not isinstance(columns_value, list) or not isinstance(rows_value, list):
        raise ValueError("outcome result columns and rows must be arrays")
    columns = tuple(str(column) for column in columns_value)
    rows = tuple(
        tuple(row) for row in rows_value if isinstance(row, (list, tuple))
    )
    if len(rows) != len(rows_value):
        raise ValueError("outcome result rows must contain arrays")
    return ResultSet(columns, rows)


def _ratio_summary(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        raise ValueError("at least one ratio sample is required")
    ordered = sorted(float(value) for value in values)
    return {
        "sample_count": len(ordered),
        "median_ratio": float(median(ordered)),
        "p95_ratio": ordered[ceil(0.95 * len(ordered)) - 1],
        "max_ratio": ordered[-1],
    }


def recompute_benchmark_summary(
    outcomes: Sequence[RegressionOutcome | Mapping[str, Any]],
) -> dict[str, Any]:
    if not outcomes:
        raise ValueError("at least one benchmark outcome is required")
    run_indexes = sorted(
        {_integer(_outcome_value(outcome, "run_index")) for outcome in outcomes}
    )
    per_run_accuracy = [
        sum(
            _outcome_value(outcome, "correct") is True
            for outcome in outcomes
            if _integer(_outcome_value(outcome, "run_index")) == run_index
        )
        / sum(
            1
            for outcome in outcomes
            if _integer(_outcome_value(outcome, "run_index")) == run_index
        )
        for run_index in run_indexes
    ]
    by_case: dict[str, list[RegressionOutcome | Mapping[str, Any]]] = {}
    for outcome in outcomes:
        by_case.setdefault(str(_outcome_value(outcome, "case_id")), []).append(
            outcome
        )
    consistent_correct = 0
    family_consistent = 0
    for case_outcomes in by_case.values():
        final_families = {
            str(_outcome_value(outcome, "routing_final_family"))
            for outcome in case_outcomes
        }
        if len(final_families) == 1:
            family_consistent += 1
        if not all(
            _outcome_value(outcome, "correct") is True
            for outcome in case_outcomes
        ):
            continue
        expected_family = str(
            _outcome_value(case_outcomes[0], "expected_family")
        )
        if expected_family == "OUT_OF_SCOPE":
            consistent_correct += 1
            continue
        results = [_result_from_outcome(outcome) for outcome in case_outcomes]
        if all(result is not None for result in results) and all(
            results_equivalent(results[0], result)  # type: ignore[arg-type]
            for result in results[1:]
        ):
            consistent_correct += 1
    families = sorted(
        {str(_outcome_value(outcome, "expected_family")) for outcome in outcomes}
    )
    per_family_accuracy = {
        family: sum(
            _outcome_value(outcome, "correct") is True
            for outcome in outcomes
            if str(_outcome_value(outcome, "expected_family")) == family
        )
        / sum(
            1
            for outcome in outcomes
            if str(_outcome_value(outcome, "expected_family")) == family
        )
        for family in families
    }
    latency_values = [
        _integer(_outcome_value(outcome, "latency_ms")) for outcome in outcomes
    ]
    query_values = [
        _integer(_outcome_value(outcome, "query_elapsed_ms"))
        for outcome in outcomes
    ]
    inference_shares = []
    for outcome, latency_ms in zip(outcomes, latency_values, strict=True):
        model_latency = _integer(
            _outcome_value(outcome, "routing_latency_ms")
        ) + _integer(_outcome_value(outcome, "generation_latency_ms"))
        inference_shares.append(
            model_latency / latency_ms if latency_ms > 0 else 0.0
        )
    fallback_count = sum(
        _outcome_value(outcome, "routing_fallback") is True
        for outcome in outcomes
    )
    mismatch_count = sum(
        _outcome_value(outcome, "routing_family_mismatch") is True
        for outcome in outcomes
    )
    sample_count = len(outcomes)
    return {
        "latency": asdict(summarize_latencies(latency_values)),
        "per_run_accuracy": per_run_accuracy,
        "three_run_consistent_correctness": consistent_correct / len(by_case),
        "family_consistency": family_consistent / len(by_case),
        "per_family_accuracy": per_family_accuracy,
        "routing_fallback_count": fallback_count,
        "routing_fallback_rate": fallback_count / sample_count,
        "routing_family_mismatch_count": mismatch_count,
        "routing_family_mismatch_rate": mismatch_count / sample_count,
        "model_inference_share": _ratio_summary(inference_shares),
        "query_latency": asdict(summarize_latencies(query_values)),
    }


def _json_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _serialize_result(result: ResultSet | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "columns": list(result.columns),
        "rows": [
            [_json_scalar(value) for value in row]
            for row in result.rows
        ],
    }


def _serialize_outcome(outcome: RegressionOutcome) -> dict[str, Any]:
    data = asdict(outcome)
    data["result"] = _serialize_result(outcome.result)
    data["routing_few_shot_ids"] = list(outcome.routing_few_shot_ids)
    return data


def benchmark_payload(
    report: RegressionReport,
    *,
    lever: str,
    baseline_commit: str,
    implementation_commit: str,
    measured_at: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "lever": lever,
        "baseline_commit": baseline_commit,
        "implementation_commit": implementation_commit,
        "measured_at": measured_at,
        "summary": recompute_benchmark_summary(report.outcomes),
        "outcomes": [_serialize_outcome(outcome) for outcome in report.outcomes],
        "extra": dict(extra or {}),
    }
