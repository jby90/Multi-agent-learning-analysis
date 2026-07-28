from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.attempt_cost_audit import audit_attempt_cost
from eval.trace_dataset import AttemptRecord, canonical_json


def _pricing() -> dict:
    return {
        "prices": {
            "qwen3-32b": {
                "input_cny_per_million": "2",
                "output_cny_per_million": "8",
            },
            "qwen3-235b-a22b": {
                "input_cny_per_million": "2",
                "output_cny_per_million": "8",
            },
        }
    }


def _record(case_id: str, status: str, cache_path: str) -> AttemptRecord:
    return AttemptRecord(
        case_id=case_id,
        attempt=1,
        trace_id=f"trace-{case_id}",
        status=status,
        trace_path=f"traces/{case_id}.jsonl",
        cache_path=cache_path,
        base_commit="a" * 40,
        started_at="2026-07-17T00:00:00+00:00",
        finished_at="2026-07-17T00:01:00+00:00",
        terminal_state="S10_DONE" if status == "succeeded" else None,
        transition_sequence=("T20",) if status == "succeeded" else (),
        message_count=1,
        error_type=None if status == "succeeded" else "RuntimeError",
        error_message=None if status == "succeeded" else "failed",
        outcome="completed" if status == "succeeded" else "system_error",
    )


def _cache(path: Path, rows: list[tuple[str, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = []
    for step, (model, prompt, completion) in enumerate(rows, start=1):
        values.append(
            {
                "schema_version": 1,
                "trace_id": path.stem,
                "step": step,
                "request": {"model": model, "sha256": str(step) * 64},
                "result": {
                    "model": model,
                    "token_usage": {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": prompt + completion,
                        "cached_tokens": 0,
                    },
                    "attempts": 1,
                },
            }
        )
    path.write_text(
        "".join(canonical_json(value) + "\n" for value in values),
        encoding="utf-8",
        newline="",
    )


def test_attempt_cost_counts_every_cached_model_result_once(tmp_path: Path) -> None:
    _cache(tmp_path / "cache" / "success.jsonl", [("qwen3-32b", 100, 10)])
    _cache(tmp_path / "cache" / "failed.jsonl", [("qwen3-235b-a22b", 50, 20)])
    records = (
        _record("E2E-001", "succeeded", "cache/success.jsonl"),
        _record("E2E-002", "failed", "cache/failed.jsonl"),
    )

    report = audit_attempt_cost(records, repository_root=tmp_path, pricing=_pricing())

    assert report["all_attempts"]["attempts"] == 2
    assert report["all_attempts"]["api_results"] == 2
    assert report["all_attempts"]["total_tokens"] == 180
    assert report["all_attempts"]["cost_cny"] == "0.000540"
    assert report["succeeded_attempts"]["total_tokens"] == 110
    assert report["failed_attempts"]["total_tokens"] == 70
    assert report["models"]["qwen3-32b"]["total_tokens"] == 110


def test_attempt_cost_rejects_inconsistent_cache_usage(tmp_path: Path) -> None:
    path = tmp_path / "cache.jsonl"
    _cache(path, [("qwen3-32b", 100, 10)])
    value = json.loads(path.read_text("utf-8"))
    value["result"]["token_usage"]["total_tokens"] = 999
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt plus completion"):
        audit_attempt_cost(
            (_record("E2E-001", "failed", "cache.jsonl"),),
            repository_root=tmp_path,
            pricing=_pricing(),
        )
