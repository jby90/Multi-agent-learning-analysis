"""Validate REF agent messages against JSON Schema and bus business rules."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_PATH = Path(__file__).with_name("message_schema.json")
_RFC3339_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
        return json.load(schema_file)


_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _is_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if _RFC3339_DATETIME_RE.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


_VALIDATOR = Draft202012Validator(_load_schema(), format_checker=_FORMAT_CHECKER)
_REQUIRED_PROPERTY_RE = re.compile(r"^'(.+)' is a required property$")


def _json_path(parts: Sequence[object]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _schema_error_path(error: Any) -> str:
    parts = list(error.absolute_path)
    if error.validator == "required":
        match = _REQUIRED_PROPERTY_RE.match(error.message)
        if match:
            parts.append(match.group(1))
    return _json_path(parts)


def _schema_errors(message: Any) -> list[str]:
    errors = sorted(
        _VALIDATOR.iter_errors(message),
        key=lambda error: (_json_path(list(error.absolute_path)), error.message),
    )
    return [f"SCHEMA {_schema_error_path(error)}: {error.message}" for error in errors]


def _business_rule_errors(message: Any) -> list[str]:
    if not isinstance(message, Mapping):
        return []

    errors: list[str] = []
    evidence = message.get("evidence", [])
    usable_evidence = evidence if isinstance(evidence, list) else []

    claims = message.get("claims", [])
    if isinstance(claims, list):
        required_evidence_kind = {
            "fact": "kb_chunk",
            "data_conclusion": "sql_query",
        }
        for index, claim in enumerate(claims):
            if not isinstance(claim, Mapping):
                continue
            claim_kind = claim.get("kind")
            evidence_kind = required_evidence_kind.get(claim_kind)
            claim_text = claim.get("text")
            if evidence_kind is None or not isinstance(claim_text, str):
                continue
            normalized_claim = claim_text.strip()
            supported = any(
                isinstance(item, Mapping)
                and item.get("kind") == evidence_kind
                and isinstance(item.get("supports_claim"), str)
                and item["supports_claim"].strip() == normalized_claim
                for item in usable_evidence
            )
            if not supported:
                errors.append(
                    f'R-A $.claims[{index}]: {claim_kind} claim "{claim_text}" '
                    f"缺少匹配的 {evidence_kind} 证据；要求 evidence[].supports_claim "
                    "与该 claim.text 一致"
                )

    role = message.get("role")
    if role in {"verdict", "re_verdict"}:
        verdict = message.get("verdict")
        if not isinstance(verdict, Mapping):
            errors.append(f"R-B $.verdict: role={role} 必须包含对象型 verdict 字段")
        else:
            rule_hits = verdict.get("rule_hits", [])
            if isinstance(rule_hits, list):
                for index, rule_hit in enumerate(rule_hits):
                    if not isinstance(rule_hit, Mapping):
                        continue
                    evidence_ref = rule_hit.get("evidence_ref")
                    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
                        errors.append(
                            "R-B $.verdict.rule_hits"
                            f"[{index}].evidence_ref: 每条 rule_hit 必须提供非空 evidence_ref"
                        )

    retry = message.get("retry")
    if isinstance(retry, Mapping):
        retry_count = retry.get("retry_count", 0)
        max_retries = retry.get("max_retries", 2)
        both_integers = all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (retry_count, max_retries)
        )
        if both_integers and retry_count > max_retries:
            errors.append(
                f"R-C $.retry.retry_count: retry_count={retry_count} 超过 "
                f"max_retries={max_retries}，应走 fallback"
            )

    token_usage = message.get("token_usage")
    if isinstance(token_usage, Mapping):
        prompt_tokens = token_usage.get("prompt_tokens")
        completion_tokens = token_usage.get("completion_tokens")
        total_tokens = token_usage.get("total_tokens")
        counts = (prompt_tokens, completion_tokens, total_tokens)
        if all(isinstance(value, int) and not isinstance(value, bool) for value in counts):
            expected_total = prompt_tokens + completion_tokens
            if total_tokens != expected_total:
                errors.append(
                    "R-D $.token_usage.total_tokens: "
                    f"total_tokens={total_tokens}，应等于 prompt_tokens+completion_tokens="
                    f"{expected_total}"
                )

    payload = message.get("payload")
    content = payload.get("content") if isinstance(payload, Mapping) else None
    if isinstance(content, Mapping) and content.get("event") == "knowledge_refused":
        refuse_reason = content.get("refuse_reason")
        if not isinstance(refuse_reason, str) or not refuse_reason.strip():
            errors.append(
                "R-E $.payload.content.refuse_reason: knowledge_refused "
                "必须提供非空拒答原因"
            )
        if isinstance(claims, list) and claims:
            errors.append(
                "R-E $.claims: knowledge_refused 不得携带 claims；拒答表示无内容"
            )

    if (
        isinstance(content, Mapping)
        and content.get("event") == "product_ready"
        and content.get("knowledge_point_match") is False
    ):
        errors.append(
            "R-F $.payload.content.knowledge_point_match: "
            "knowledge_point_match=false 时不得输出 product_ready"
        )

    return errors


def validate_message(message: Any) -> list[str]:
    """Return all schema and business-rule errors; an empty list means valid."""

    return _schema_errors(message) + _business_rule_errors(message)


def validate_file(path: Path | str) -> list[str]:
    """Load a UTF-8 JSON message and return validation errors."""

    message_path = Path(path)
    try:
        with message_path.open(encoding="utf-8") as message_file:
            message = json.load(message_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"JSON $: {message_path}: {exc}"]
    return validate_message(message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate one REF protocol message")
    parser.add_argument("message", type=Path, help="UTF-8 JSON message file")
    args = parser.parse_args(argv)

    errors = validate_file(args.message)
    if errors:
        print("FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
