"""Audit one-shot P7 execution outcomes without filtering failed traces."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from eval.trace_dataset import (
    AttemptRecord,
    canonical_json,
    load_attempt_records,
    load_dataset,
    sha256_file,
)
from orchestrator.outcomes import Outcome


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.000000"
    return str(
        (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.000001"))
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def execution_audit(
    dataset: Sequence[Mapping[str, Any]],
    records: Sequence[AttemptRecord],
) -> dict[str, Any]:
    case_ids = [str(_mapping(row.get("case")).get("case_id", "")) for row in dataset]
    record_case_ids = [record.case_id for record in records]
    if any(not case_id for case_id in case_ids) or len(case_ids) != len(set(case_ids)):
        raise ValueError("dataset must contain unique non-empty case IDs")
    if len(record_case_ids) != len(set(record_case_ids)):
        raise ValueError("one-shot execution audit requires one attempt per case")
    if set(case_ids) != set(record_case_ids):
        raise ValueError("dataset and ledger case IDs do not match")

    verification: list[tuple[str, Mapping[str, Any]]] = []
    for row in dataset:
        case_id = str(_mapping(row.get("case")).get("case_id"))
        messages = row.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            payload = _mapping(message.get("payload"))
            content = _mapping(payload.get("content"))
            if (
                message.get("agent") == "verification"
                and message.get("role") == "produce"
                and payload.get("type") == "sql_result"
            ):
                verification.append((case_id, content))

    routing = [
        (case_id, content)
        for case_id, content in verification
        if isinstance(content.get("routing_family_mismatch"), bool)
    ]
    mismatch_cases = [
        case_id
        for case_id, content in routing
        if content.get("routing_family_mismatch") is True
    ]
    sandbox_cases = [
        case_id
        for case_id, content in verification
        if content.get("event") == "sandbox_rejected"
    ]
    failure_types = Counter(
        record.error_message or record.error_type or "unknown_failure"
        for record in records
        if record.status == "failed"
    )
    events = Counter(str(content.get("event")) for _, content in verification)
    succeeded = sum(record.status == "succeeded" for record in records)
    failed = len(records) - succeeded
    outcome_counts = Counter(record.outcome for record in records)
    outcomes = {
        outcome.value: outcome_counts[outcome.value]
        for outcome in Outcome
    }
    outcome_case_ids = {
        outcome.value: sorted(
            record.case_id
            for record in records
            if record.outcome == outcome.value
        )
        for outcome in Outcome
    }
    return {
        "dataset_rows": len(dataset),
        "attempts": len(records),
        "succeeded_attempts": succeeded,
        "failed_attempts": failed,
        "success_rate": _rate(succeeded, len(records)),
        "terminal_s10": sum(row.get("terminal_state") == "S10_DONE" for row in dataset),
        "failure_types": dict(sorted(failure_types.items())),
        "outcomes": outcomes,
        "outcome_case_ids": outcome_case_ids,
        "system_error_attempts": outcomes[Outcome.SYSTEM_ERROR.value],
        "verification_products": len(verification),
        "verification_events": dict(sorted(events.items())),
        "sandbox_rejected": len(sandbox_cases),
        "sandbox_case_ids": sandbox_cases,
        "routing_observations": len(routing),
        "routing_family_mismatch": len(mismatch_cases),
        "routing_family_mismatch_rate": _rate(len(mismatch_cases), len(routing)),
        "routing_mismatch_case_ids": mismatch_cases,
        "base_commits": sorted({record.base_commit for record in records}),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = execution_audit(
        load_dataset(args.dataset),
        load_attempt_records(args.ledger),
    )
    report.update(
        {
            "dataset_path": args.dataset.as_posix(),
            "dataset_sha256": sha256_file(args.dataset),
            "ledger_path": args.ledger.as_posix(),
            "ledger_sha256": sha256_file(args.ledger),
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(canonical_json(report) + "\n", encoding="utf-8", newline="")
    temporary.replace(args.output)
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
