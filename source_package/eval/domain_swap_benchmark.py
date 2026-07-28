"""Component-level domain-swap benchmark with auditable, redacted attempts."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from time import perf_counter
from typing import Any

from agents.domain_config import DomainConfig, load_domain_config
from agents.sandbox import validate_and_rewrite
from agents.text2sql_generation import (
    Text2SQLGenerationCoordinator,
    default_prompt_catalog,
)
from eval.first_segment_oracle import (
    Cell,
    CsvOracle,
    DomainSwapCase,
    load_first_segment_cases,
)
from orchestrator.llm import LLMResult, call_llm


ROOT = Path(__file__).resolve().parents[1]
_SENSITIVE_NAME_RE = re.compile(r"(?:KEY|PASSWORD|TOKEN|SECRET)", re.IGNORECASE)
_INLINE_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|password|token|secret)\s*[:=]\s*)[^\s,;}]+"
)


def _redact(value: str) -> str:
    redacted = value
    secrets = {
        item
        for name, item in os.environ.items()
        if _SENSITIVE_NAME_RE.search(name) and len(item) >= 4
    }
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return _INLINE_SECRET_RE.sub(r"\1[REDACTED]", redacted)


def _canonical_case(case: DomainSwapCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "section": case.section,
        "split": case.split,
        "question": case.question,
        "expected_intent": case.expected_intent,
        "answer_key": {
            "table": case.answer_key.table,
            "selected_columns": list(case.answer_key.selected_columns),
            "filters": dict(case.answer_key.filters),
            "order_by": list(case.answer_key.order_by),
            "expected_sql": case.answer_key.expected_sql,
            "expected_columns": list(case.answer_key.expected_columns),
            "expected_rows": [list(row) for row in case.answer_key.expected_rows],
        },
        "sources": [
            {"file": item.file, "sha256": item.sha256, "role": item.role}
            for item in case.sources
        ],
    }


def _case_hash(case: DomainSwapCase) -> str:
    encoded = json.dumps(
        _canonical_case(case), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


@dataclass(frozen=True, slots=True)
class BenchmarkAttempt:
    case_id: str
    section: str
    split: str
    commit_sha: str
    domain_id: str
    domain_sha256: str
    case_sha256: str
    source_hashes: tuple[tuple[str, str], ...]
    expected_intent: str
    predicted_intent: str | None
    generated_family: str | None
    intent_correct: bool
    generated_sql: str | None
    executed_sql: str | None
    sandbox_allowed: bool
    sandbox_rule_id: str | None
    expected_columns: tuple[str, ...]
    actual_columns: tuple[str, ...]
    expected_rows: tuple[tuple[Cell, ...], ...]
    actual_rows: tuple[tuple[Cell, ...], ...]
    sql_correct: bool
    correct: bool
    router_model: str | None
    generator_model: str | None
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "section": self.section,
            "split": self.split,
            "commit_sha": self.commit_sha,
            "domain_id": self.domain_id,
            "domain_sha256": self.domain_sha256,
            "case_sha256": self.case_sha256,
            "source_hashes": [
                {"file": name, "sha256": sha256}
                for name, sha256 in self.source_hashes
            ],
            "expected_intent": self.expected_intent,
            "predicted_intent": self.predicted_intent,
            "generated_family": self.generated_family,
            "intent_correct": self.intent_correct,
            "generated_sql": self.generated_sql,
            "executed_sql": self.executed_sql,
            "sandbox_allowed": self.sandbox_allowed,
            "sandbox_rule_id": self.sandbox_rule_id,
            "expected_columns": list(self.expected_columns),
            "actual_columns": list(self.actual_columns),
            "expected_rows": [list(row) for row in self.expected_rows],
            "actual_rows": [list(row) for row in self.actual_rows],
            "sql_correct": self.sql_correct,
            "correct": self.correct,
            "router_model": self.router_model,
            "generator_model": self.generator_model,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class MetricSlice:
    total: int
    intent_correct: int
    sql_correct: int
    component_correct: int
    intent_accuracy: float
    sql_accuracy: float
    component_accuracy: float

    @classmethod
    def from_attempts(cls, attempts: Sequence[BenchmarkAttempt]) -> "MetricSlice":
        total = len(attempts)
        intent_correct = sum(item.intent_correct for item in attempts)
        sql_correct = sum(item.sql_correct for item in attempts)
        component_correct = sum(item.correct for item in attempts)
        denominator = total or 1
        return cls(
            total=total,
            intent_correct=intent_correct,
            sql_correct=sql_correct,
            component_correct=component_correct,
            intent_accuracy=intent_correct / denominator if total else 0.0,
            sql_accuracy=sql_correct / denominator if total else 0.0,
            component_accuracy=component_correct / denominator if total else 0.0,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "intent_correct": self.intent_correct,
            "sql_correct": self.sql_correct,
            "component_correct": self.component_correct,
            "intent_accuracy": self.intent_accuracy,
            "sql_accuracy": self.sql_accuracy,
            "component_accuracy": self.component_accuracy,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    attempts: tuple[BenchmarkAttempt, ...]

    @classmethod
    def from_attempts(
        cls, attempts: Sequence[BenchmarkAttempt]
    ) -> "BenchmarkReport":
        return cls(tuple(attempts))

    @property
    def metrics(self) -> MetricSlice:
        return MetricSlice.from_attempts(self.attempts)

    @property
    def split_metrics(self) -> dict[str, MetricSlice]:
        return {
            split: MetricSlice.from_attempts(
                tuple(item for item in self.attempts if item.split == split)
            )
            for split in ("calibration", "holdout")
        }

    @property
    def total(self) -> int:
        return self.metrics.total

    @property
    def intent_correct(self) -> int:
        return self.metrics.intent_correct

    @property
    def sql_correct(self) -> int:
        return self.metrics.sql_correct

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.as_dict(),
            "splits": {
                name: metrics.as_dict() for name, metrics in self.split_metrics.items()
            },
            "domain_ids": sorted({item.domain_id for item in self.attempts}),
            "domain_sha256": sorted(
                {item.domain_sha256 for item in self.attempts}
            ),
            "commit_sha": sorted({item.commit_sha for item in self.attempts}),
            "attempt_count": len(self.attempts),
        }


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    v0: BenchmarkReport
    v1: BenchmarkReport
    intent_recovery_case_ids: tuple[str, ...]
    intent_regression_case_ids: tuple[str, ...]
    sql_recovery_case_ids: tuple[str, ...]
    sql_regression_case_ids: tuple[str, ...]
    recovery_case_ids: tuple[str, ...]
    regression_case_ids: tuple[str, ...]
    holdout_recovery_case_ids: tuple[str, ...]
    holdout_regression_case_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "v0": self.v0.as_dict(),
            "v1": self.v1.as_dict(),
            "intent_recovery_count": len(self.intent_recovery_case_ids),
            "intent_regression_count": len(self.intent_regression_case_ids),
            "intent_recovery_case_ids": list(self.intent_recovery_case_ids),
            "intent_regression_case_ids": list(self.intent_regression_case_ids),
            "sql_recovery_count": len(self.sql_recovery_case_ids),
            "sql_regression_count": len(self.sql_regression_case_ids),
            "sql_recovery_case_ids": list(self.sql_recovery_case_ids),
            "sql_regression_case_ids": list(self.sql_regression_case_ids),
            "recovery_count": len(self.recovery_case_ids),
            "regression_count": len(self.regression_case_ids),
            "recovery_case_ids": list(self.recovery_case_ids),
            "regression_case_ids": list(self.regression_case_ids),
            "holdout_recovery_count": len(self.holdout_recovery_case_ids),
            "holdout_regression_count": len(self.holdout_regression_case_ids),
            "holdout_recovery_case_ids": list(self.holdout_recovery_case_ids),
            "holdout_regression_case_ids": list(self.holdout_regression_case_ids),
        }


def _empty_attempt(
    case: DomainSwapCase,
    domain_config: DomainConfig,
    commit_sha: str,
    *,
    latency_ms: int,
    error: str,
) -> BenchmarkAttempt:
    return BenchmarkAttempt(
        case_id=case.case_id,
        section=case.section,
        split=case.split,
        commit_sha=commit_sha,
        domain_id=domain_config.domain_id,
        domain_sha256=domain_config.package_sha256,
        case_sha256=_case_hash(case),
        source_hashes=tuple((item.file, item.sha256) for item in case.sources),
        expected_intent=case.expected_intent,
        predicted_intent=None,
        generated_family=None,
        intent_correct=False,
        generated_sql=None,
        executed_sql=None,
        sandbox_allowed=False,
        sandbox_rule_id=None,
        expected_columns=case.answer_key.expected_columns,
        actual_columns=(),
        expected_rows=case.answer_key.expected_rows,
        actual_rows=(),
        sql_correct=False,
        correct=False,
        router_model=None,
        generator_model=None,
        latency_ms=latency_ms,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        error=_redact(error),
    )


def run_benchmark(
    cases: Sequence[DomainSwapCase],
    domain_config: DomainConfig,
    raw_root: str | Path,
    llm_call: Callable[..., LLMResult] = call_llm,
    *,
    commit_sha: str | None = None,
) -> BenchmarkReport:
    """Run routing + SQL generation + sandbox + CSV truth comparison."""

    commit = commit_sha or _current_commit()
    coordinator = Text2SQLGenerationCoordinator(
        llm_call=llm_call,
        prompt_catalog=default_prompt_catalog(domain_config),
    )
    oracle = CsvOracle(raw_root)
    attempts: list[BenchmarkAttempt] = []
    try:
        for case in cases:
            started = perf_counter()
            try:
                generation = coordinator.generate(case.question)
            except Exception as exc:
                attempts.append(
                    _empty_attempt(
                        case,
                        domain_config,
                        commit,
                        latency_ms=round((perf_counter() - started) * 1000),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            predicted_intent = generation.routing_predicted_family
            generated_family = str(generation.data.get("family", "")) or None
            raw_sql = generation.data.get("sql")
            generated_sql = raw_sql.strip() if isinstance(raw_sql, str) and raw_sql.strip() else None
            intent_correct = predicted_intent == case.expected_intent
            executed_sql: str | None = None
            sandbox_allowed = False
            sandbox_rule_id: str | None = None
            actual_columns: tuple[str, ...] = ()
            actual_rows: tuple[tuple[Cell, ...], ...] = ()
            error: str | None = None
            if generated_sql is None:
                error = "generator returned null SQL"
            else:
                decision = validate_and_rewrite(
                    generated_sql, domain_config=domain_config
                )
                sandbox_allowed = decision.allowed
                sandbox_rule_id = decision.rule_id
                executed_sql = decision.executed_sql
                if not decision.allowed:
                    error = f"sandbox {decision.rule_id}: {decision.reason}"
                else:
                    try:
                        actual = oracle.execute(generated_sql, domain_config)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                    else:
                        executed_sql = actual.executed_sql
                        actual_columns = actual.columns
                        actual_rows = actual.rows
            sql_correct = (
                generated_family == case.expected_intent
                and sandbox_allowed
                and actual_columns == case.answer_key.expected_columns
                and actual_rows == case.answer_key.expected_rows
            )
            attempts.append(
                BenchmarkAttempt(
                    case_id=case.case_id,
                    section=case.section,
                    split=case.split,
                    commit_sha=commit,
                    domain_id=domain_config.domain_id,
                    domain_sha256=domain_config.package_sha256,
                    case_sha256=_case_hash(case),
                    source_hashes=tuple(
                        (item.file, item.sha256) for item in case.sources
                    ),
                    expected_intent=case.expected_intent,
                    predicted_intent=predicted_intent,
                    generated_family=generated_family,
                    intent_correct=intent_correct,
                    generated_sql=generated_sql,
                    executed_sql=executed_sql,
                    sandbox_allowed=sandbox_allowed,
                    sandbox_rule_id=sandbox_rule_id,
                    expected_columns=case.answer_key.expected_columns,
                    actual_columns=actual_columns,
                    expected_rows=case.answer_key.expected_rows,
                    actual_rows=actual_rows,
                    sql_correct=sql_correct,
                    correct=intent_correct and sql_correct,
                    router_model=generation.routing_model,
                    generator_model=generation.model,
                    latency_ms=generation.latency_ms,
                    prompt_tokens=generation.token_usage.prompt_tokens,
                    completion_tokens=generation.token_usage.completion_tokens,
                    total_tokens=generation.token_usage.total_tokens,
                    error=_redact(error) if error else None,
                )
            )
    finally:
        oracle.close()
    return BenchmarkReport.from_attempts(attempts)


def _attempt_from_dict(raw: Mapping[str, Any]) -> BenchmarkAttempt:
    def rows(field: str) -> tuple[tuple[Cell, ...], ...]:
        value = raw[field]
        if not isinstance(value, list):
            raise ValueError(f"attempt {field} must be a list")
        return tuple(tuple(cell for cell in row) for row in value)

    source_hashes = raw["source_hashes"]
    if not isinstance(source_hashes, list):
        raise ValueError("attempt source_hashes must be a list")
    return BenchmarkAttempt(
        case_id=str(raw["case_id"]),
        section=str(raw["section"]),
        split=str(raw["split"]),
        commit_sha=str(raw["commit_sha"]),
        domain_id=str(raw["domain_id"]),
        domain_sha256=str(raw["domain_sha256"]),
        case_sha256=str(raw["case_sha256"]),
        source_hashes=tuple(
            (str(item["file"]), str(item["sha256"])) for item in source_hashes
        ),
        expected_intent=str(raw["expected_intent"]),
        predicted_intent=(
            str(raw["predicted_intent"])
            if raw["predicted_intent"] is not None
            else None
        ),
        generated_family=(
            str(raw["generated_family"])
            if raw["generated_family"] is not None
            else None
        ),
        intent_correct=bool(raw["intent_correct"]),
        generated_sql=str(raw["generated_sql"]) if raw["generated_sql"] is not None else None,
        executed_sql=str(raw["executed_sql"]) if raw["executed_sql"] is not None else None,
        sandbox_allowed=bool(raw["sandbox_allowed"]),
        sandbox_rule_id=(
            str(raw["sandbox_rule_id"])
            if raw["sandbox_rule_id"] is not None
            else None
        ),
        expected_columns=tuple(str(item) for item in raw["expected_columns"]),
        actual_columns=tuple(str(item) for item in raw["actual_columns"]),
        expected_rows=rows("expected_rows"),
        actual_rows=rows("actual_rows"),
        sql_correct=bool(raw["sql_correct"]),
        correct=bool(raw["correct"]),
        router_model=str(raw["router_model"]) if raw["router_model"] is not None else None,
        generator_model=str(raw["generator_model"]) if raw["generator_model"] is not None else None,
        latency_ms=int(raw["latency_ms"]),
        prompt_tokens=int(raw["prompt_tokens"]),
        completion_tokens=int(raw["completion_tokens"]),
        total_tokens=int(raw["total_tokens"]),
        error=str(raw["error"]) if raw["error"] is not None else None,
    )


def write_attempts(path: str | Path, attempts: Sequence[BenchmarkAttempt]) -> None:
    output = Path(path)
    if output.exists():
        raise FileExistsError(f"benchmark attempts already exist: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        json.dumps(item.as_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in attempts
    )
    output.write_text(_redact(rendered), encoding="utf-8", newline="\n")


def load_attempts(path: str | Path) -> tuple[BenchmarkAttempt, ...]:
    attempts: list[BenchmarkAttempt] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read benchmark attempts: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid attempt JSON line {line_number}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError(f"attempt line {line_number} must be an object")
        attempts.append(_attempt_from_dict(raw))
    case_ids = [item.case_id for item in attempts]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("benchmark attempt case IDs must be unique")
    return tuple(attempts)


def compare_reports(v0: BenchmarkReport, v1: BenchmarkReport) -> ComparisonReport:
    v0_by_id = {item.case_id: item for item in v0.attempts}
    v1_by_id = {item.case_id: item for item in v1.attempts}
    if set(v0_by_id) != set(v1_by_id):
        raise ValueError("v0 and v1 benchmark case IDs must match")
    ordered_ids = tuple(item.case_id for item in v0.attempts)
    for case_id in ordered_ids:
        if v0_by_id[case_id].split != v1_by_id[case_id].split:
            raise ValueError(f"benchmark split changed for {case_id}")
    recovery = tuple(
        case_id
        for case_id in ordered_ids
        if not v0_by_id[case_id].correct and v1_by_id[case_id].correct
    )
    regression = tuple(
        case_id
        for case_id in ordered_ids
        if v0_by_id[case_id].correct and not v1_by_id[case_id].correct
    )
    intent_recovery = tuple(
        case_id
        for case_id in ordered_ids
        if not v0_by_id[case_id].intent_correct
        and v1_by_id[case_id].intent_correct
    )
    intent_regression = tuple(
        case_id
        for case_id in ordered_ids
        if v0_by_id[case_id].intent_correct
        and not v1_by_id[case_id].intent_correct
    )
    sql_recovery = tuple(
        case_id
        for case_id in ordered_ids
        if not v0_by_id[case_id].sql_correct and v1_by_id[case_id].sql_correct
    )
    sql_regression = tuple(
        case_id
        for case_id in ordered_ids
        if v0_by_id[case_id].sql_correct and not v1_by_id[case_id].sql_correct
    )
    return ComparisonReport(
        v0=v0,
        v1=v1,
        intent_recovery_case_ids=intent_recovery,
        intent_regression_case_ids=intent_regression,
        sql_recovery_case_ids=sql_recovery,
        sql_regression_case_ids=sql_regression,
        recovery_case_ids=recovery,
        regression_case_ids=regression,
        holdout_recovery_case_ids=tuple(
            case_id for case_id in recovery if v0_by_id[case_id].split == "holdout"
        ),
        holdout_regression_case_ids=tuple(
            case_id for case_id in regression if v0_by_id[case_id].split == "holdout"
        ),
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"benchmark report already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--cases", type=Path, required=True)
    run_parser.add_argument("--raw-root", type=Path, required=True)
    run_parser.add_argument("--attempts", type=Path, required=True)
    run_parser.add_argument("--report", type=Path, required=True)
    run_parser.add_argument("--commit")
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--v0", type=Path, required=True)
    compare_parser.add_argument("--v1", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "compare":
        comparison = compare_reports(
            BenchmarkReport.from_attempts(load_attempts(args.v0)),
            BenchmarkReport.from_attempts(load_attempts(args.v1)),
        )
        _write_json(args.output, comparison.as_dict())
        print(json.dumps(comparison.as_dict(), ensure_ascii=False))
        return 0

    domain = load_domain_config(args.config)
    cases = load_first_segment_cases(args.cases, raw_root=args.raw_root)
    report = run_benchmark(
        cases,
        domain,
        args.raw_root,
        commit_sha=args.commit,
    )
    write_attempts(args.attempts, report.attempts)
    _write_json(args.report, report.as_dict())
    print(json.dumps(report.as_dict(), ensure_ascii=False))
    return 0 if report.total == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
