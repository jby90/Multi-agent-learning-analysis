from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from eval.case_matrix import EvaluationCase, PRETEST_PATTERNS
from eval.trace_dataset import (
    AttemptRecord,
    build_dataset,
    canonical_json,
    load_attempt_records,
    load_dataset,
    sha256_file,
    write_attempt_record,
)


def _case(case_id: str = "E2E-001") -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        profile_id="planner_new",
        knowledge_point="三道工序与传导关系",
        pretest_pattern="A5",
        answers=PRETEST_PATTERNS["A5"],
        task_template_id="T-03",
        learning_path="direct_correct",
        misconception_id=None,
        manual_review=True,
    )


def _message(trace_id: str, step: int) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        "msg_id": f"{trace_id}-{step:03d}",
        "step": step,
        "agent": "system",
        "role": "system",
        "payload": {
            "type": "control",
            "content": {"transition_id": "T20" if step == 2 else "T01"},
        },
        "evidence": [],
        "claims": [],
        "timestamp": "2026-07-17T00:00:00+00:00",
    }


def _write_trace(root: Path, trace_id: str) -> Path:
    path = root / "eval" / "results" / "traces" / f"{trace_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            json.dumps(_message(trace_id, step), ensure_ascii=False)
            for step in (1, 2)
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_failed_trace(root: Path, trace_id: str) -> Path:
    path = root / "eval" / "results" / "traces" / f"{trace_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_message(trace_id, 1), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _record(
    trace_id: str,
    *,
    attempt: int = 1,
    status: str = "succeeded",
    outcome: str | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        case_id="E2E-001",
        attempt=attempt,
        trace_id=trace_id,
        status=status,
        trace_path=f"eval/results/traces/{trace_id}.jsonl",
        cache_path=f"eval/results/cache/{trace_id}.jsonl",
        base_commit="f58bd8a",
        started_at="2026-07-17T00:00:00+00:00",
        finished_at="2026-07-17T00:01:00+00:00",
        terminal_state="S10_DONE" if status == "succeeded" else None,
        transition_sequence=("T01", "T20") if status == "succeeded" else ("T01",),
        message_count=2 if status == "succeeded" else 1,
        error_type=None if status == "succeeded" else "RuntimeError",
        error_message=None if status == "succeeded" else "failed honestly",
        outcome=outcome or ("completed" if status == "succeeded" else "system_error"),
    )


def test_canonical_json_is_stable_and_keeps_chinese() -> None:
    assert canonical_json({"b": 1, "a": "知识点"}) == '{"a":"知识点","b":1}'


def test_attempt_ledger_is_append_only_and_rejects_duplicate_attempt(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    first = _record("trace-a01")
    write_attempt_record(ledger, first)

    with pytest.raises(FileExistsError, match="attempt already recorded"):
        write_attempt_record(ledger, first)

    second = _record("trace-a02", attempt=2, status="failed")
    write_attempt_record(ledger, second)
    assert load_attempt_records(ledger) == (first, second)


def test_attempt_ledger_round_trips_explicit_outcome_and_loads_legacy_rows(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rejected = _record(
        "trace-a01",
        status="failed",
        outcome="safe_rejected",
    )
    write_attempt_record(ledger, rejected)

    assert load_attempt_records(ledger) == (rejected,)
    assert json.loads(ledger.read_text(encoding="utf-8"))["outcome"] == "safe_rejected"

    legacy = rejected.as_dict()
    legacy.pop("outcome")
    legacy_path = tmp_path / "legacy.jsonl"
    legacy_path.write_text(
        json.dumps(legacy, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    loaded = load_attempt_records(legacy_path)
    assert loaded[0].outcome == "system_error"


@pytest.mark.parametrize(
    ("status", "outcome"),
    (
        ("succeeded", "safe_rejected"),
        ("failed", "completed"),
        ("failed", "not_a_real_outcome"),
    ),
)
def test_attempt_record_rejects_status_outcome_mismatch(
    status: str,
    outcome: str,
) -> None:
    value = _record("trace-invalid", status=status).as_dict()
    value["outcome"] = outcome

    with pytest.raises(ValueError, match="outcome"):
        AttemptRecord.from_dict(value)


def test_build_dataset_embeds_exact_messages_and_matching_sha(tmp_path: Path) -> None:
    trace_id = "p7-e2e-e2e-001-a01"
    trace = _write_trace(tmp_path, trace_id)
    output = tmp_path / "eval" / "cases" / "e2e_50.jsonl"

    build_dataset((_case(),), (_record(trace_id),), tmp_path, output)

    rows = load_dataset(output)
    assert len(rows) == 1
    row = rows[0]
    assert row["case"]["case_id"] == "E2E-001"
    assert row["trace_sha256"] == sha256_file(trace)
    assert row["trace_sha256"] == hashlib.sha256(trace.read_bytes()).hexdigest()
    assert [message["step"] for message in row["messages"]] == [1, 2]
    assert row["messages"][1]["payload"]["content"]["transition_id"] == "T20"


def test_build_dataset_requires_one_success_for_each_case(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires one successful attempt"):
        build_dataset((_case(),), (_record("failed", status="failed"),), tmp_path, tmp_path / "out")


def test_build_dataset_can_explicitly_include_one_honest_failed_attempt(
    tmp_path: Path,
) -> None:
    trace_id = "p7-e2e-e2e-001-a01"
    trace = _write_failed_trace(tmp_path, trace_id)
    output = tmp_path / "eval" / "cases" / "e2e_50.jsonl"

    build_dataset(
        (_case(),),
        (_record(trace_id, status="failed"),),
        tmp_path,
        output,
        allow_failed=True,
    )

    row = load_dataset(output)[0]
    assert row["terminal_state"] is None
    assert row["message_count"] == 1
    assert row["trace_sha256"] == sha256_file(trace)


def test_build_dataset_rejects_trace_hash_or_identity_drift(tmp_path: Path) -> None:
    trace_id = "p7-e2e-e2e-001-a01"
    trace = _write_trace(tmp_path, trace_id)
    rows = trace.read_text(encoding="utf-8").splitlines()
    changed = json.loads(rows[0])
    changed["trace_id"] = "another-trace"
    rows[0] = json.dumps(changed, ensure_ascii=False)
    trace.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="trace_id mismatch"):
        build_dataset((_case(),), (_record(trace_id),), tmp_path, tmp_path / "out")


def test_load_dataset_rejects_noncanonical_or_invalid_rows(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"messages":[]}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_dataset(path)
