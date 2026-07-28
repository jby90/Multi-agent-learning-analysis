from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agents.validate_message import validate_file, validate_message


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "agents" / "samples"

VALID_SAMPLE_NAMES = (
    "valid_01_profile_assessment.json",
    "valid_02_lecture_note.json",
    "valid_03_practice_guide.json",
    "valid_04_quiz_set.json",
    "valid_05_sql_result.json",
    "valid_06_review_verdict.json",
    "valid_07_rebuttal_case.json",
    "valid_08_probe_questions.json",
    "valid_09_learning_path_update.json",
    "valid_10_control.json",
)


def _load(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_all_ten_payload_types_have_one_valid_sample_and_pass() -> None:
    messages = [_load(name) for name in VALID_SAMPLE_NAMES]

    assert {message["payload"]["type"] for message in messages} == {
        "profile_assessment",
        "lecture_note",
        "practice_guide",
        "quiz_set",
        "sql_result",
        "review_verdict",
        "rebuttal_case",
        "probe_questions",
        "learning_path_update",
        "control",
    }
    for name in VALID_SAMPLE_NAMES:
        assert validate_file(SAMPLES / name) == [], name


def test_verdict_sample_uses_r_01_r_02_r_03_numbering() -> None:
    message = _load("valid_06_review_verdict.json")

    assert {hit["rule_id"] for hit in message["verdict"]["rule_hits"]} == {
        "R-01",
        "R-02",
        "R-03",
    }


@pytest.mark.parametrize(
    ("name", "expected_path", "expected_rule"),
    (
        ("invalid_r_a_missing_claim_evidence.json", "$.claims[0]", "R-A"),
        (
            "invalid_r_b_missing_evidence_ref.json",
            "$.verdict.rule_hits[0].evidence_ref",
            "R-B",
        ),
        ("invalid_r_c_retry_exceeded.json", "$.retry.retry_count", "R-C"),
    ),
)
def test_business_rule_samples_are_rejected_with_precise_paths(
    name: str, expected_path: str, expected_rule: str
) -> None:
    errors = validate_file(SAMPLES / name)

    assert errors, name
    assert any(expected_path in error and expected_rule in error for error in errors), errors


def test_r_b_requires_verdict_object_for_verdict_roles() -> None:
    message = _load("valid_06_review_verdict.json")
    del message["verdict"]

    errors = validate_message(message)

    assert any("$.verdict" in error and "R-B" in error for error in errors)


def test_r_a_requires_sql_query_for_data_conclusions() -> None:
    message = _load("valid_05_sql_result.json")
    message["evidence"][0]["kind"] = "kb_chunk"

    errors = validate_message(message)

    assert any("$.claims[0]" in error and "sql_query" in error for error in errors)


def test_r_b_requires_verdict_object_for_re_verdict_role() -> None:
    message = _load("valid_06_review_verdict.json")
    message["role"] = "re_verdict"
    del message["verdict"]

    errors = validate_message(message)

    assert any("$.verdict" in error and "role=re_verdict" in error for error in errors)


def test_r_c_uses_schema_defaults_when_retry_limits_are_omitted() -> None:
    message = _load("valid_10_control.json")
    message["retry"] = {"retry_count": 3}

    errors = validate_message(message)

    assert any("R-C" in error and "max_retries=2" in error for error in errors)


def test_r_c_allows_retry_count_equal_to_max_retries() -> None:
    message = _load("valid_10_control.json")
    message["retry"] = {"retry_count": 2, "max_retries": 2}

    errors = validate_message(message)

    assert not any("R-C" in error for error in errors)


def test_json_schema_error_identifies_the_specific_field() -> None:
    message = copy.deepcopy(_load("valid_01_profile_assessment.json"))
    message["timestamp"] = "not-a-date-time"

    errors = validate_message(message)

    assert any("$.timestamp" in error and "date-time" in error for error in errors)


def test_sample_directory_contains_exactly_thirteen_json_files() -> None:
    assert len(list(SAMPLES.glob("*.json"))) == 13


def test_non_utf8_json_returns_a_validation_error(tmp_path: Path) -> None:
    message_path = tmp_path / "non-utf8.json"
    message_path.write_bytes(b"{\"message\": \"\xff\"}")

    errors = validate_file(message_path)

    assert errors
    assert errors[0].startswith("JSON $")


def test_token_usage_rejects_negative_or_inconsistent_counts() -> None:
    message = _load("valid_05_sql_result.json")
    message["token_usage"] = {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 18,
    }

    errors = validate_message(message)

    assert any("$.token_usage.total_tokens" in error and "R-D" in error for error in errors)


@pytest.mark.parametrize("name", VALID_SAMPLE_NAMES[:8])
def test_llm_samples_demonstrate_model_latency_and_token_usage(name: str) -> None:
    message = _load(name)

    assert message["model"]
    assert message["latency_ms"] >= message["payload"]["content"]["llm_latency_ms"]
    assert message["token_usage"]["total_tokens"] == (
        message["token_usage"]["prompt_tokens"]
        + message["token_usage"]["completion_tokens"]
    )
    assert validate_message(message) == []
