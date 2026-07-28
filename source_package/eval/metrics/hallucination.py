"""P7 hallucination rate with claim-level evidence links."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any

from eval.metrics import fixed_rate


DETERMINISTIC_KINDS = frozenset({"fact", "data_conclusion"})
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])[-+]?(?:\d+(?:\.\d+)?|\.\d+)%?"
)
_PLAIN_NUMBER_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d+)?|\.\d+)$")
_MONTH_LABEL_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}-(?:0[1-9]|1[0-2])(?!\d)"
)
TOLERANCE = Decimal("0.0001")


def _case_id(row: Mapping[str, Any]) -> str:
    case = row.get("case")
    value = case.get("case_id") if isinstance(case, Mapping) else None
    return str(value or "")


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    value = payload.get("content") if isinstance(payload, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _numeric_tokens(text: str) -> tuple[tuple[str, Decimal, bool], ...]:
    values: list[tuple[str, Decimal, bool]] = []
    month_spans = tuple(match.span() for match in _MONTH_LABEL_RE.finditer(text))
    for match in _NUMBER_RE.finditer(text):
        if any(
            match.start() >= start and match.end() <= end
            for start, end in month_spans
        ):
            continue
        token = match.group(0)
        percentage = token.endswith("%")
        try:
            value = Decimal(token[:-1] if percentage else token)
        except InvalidOperation:
            continue
        values.append((token, value, percentage))
    return tuple(values)


def _month_labels(value: Any) -> tuple[str, ...]:
    labels: list[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            labels.extend(_month_labels(item))
    elif isinstance(value, list):
        for item in value:
            labels.extend(_month_labels(item))
    elif isinstance(value, str):
        labels.extend(match.group(0) for match in _MONTH_LABEL_RE.finditer(value))
    return tuple(labels)


def _row_numbers(value: Any) -> tuple[Decimal, ...]:
    numbers: list[Decimal] = []
    if isinstance(value, Mapping):
        for item in value.values():
            numbers.extend(_row_numbers(item))
    elif isinstance(value, list):
        for item in value:
            numbers.extend(_row_numbers(item))
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, (int, float, Decimal)):
        try:
            numbers.append(Decimal(str(value)))
        except InvalidOperation:
            pass
    elif isinstance(value, str) and _PLAIN_NUMBER_RE.fullmatch(value.strip()):
        try:
            numbers.append(Decimal(value.strip()))
        except InvalidOperation:
            pass
    return tuple(numbers)


def _sql_month_labels(message: Mapping[str, Any]) -> tuple[str, ...]:
    content = _content(message)
    labels = list(_month_labels(content.get("rows")))
    if labels:
        return tuple(labels)
    for evidence in _items(message.get("evidence")):
        if evidence.get("kind") != "sql_query" or not isinstance(evidence.get("quote"), str):
            continue
        try:
            parsed = json.loads(str(evidence["quote"]))
        except json.JSONDecodeError:
            continue
        labels.extend(
            _month_labels(parsed.get("rows") if isinstance(parsed, Mapping) else parsed)
        )
    return tuple(labels)


def _sql_numbers(message: Mapping[str, Any]) -> tuple[Decimal, ...]:
    content = _content(message)
    numbers = list(_row_numbers(content.get("rows")))
    row_count = content.get("row_count")
    if isinstance(row_count, int) and not isinstance(row_count, bool):
        numbers.append(Decimal(row_count))
    if numbers:
        return tuple(numbers)
    for evidence in _items(message.get("evidence")):
        if evidence.get("kind") != "sql_query" or not isinstance(evidence.get("quote"), str):
            continue
        try:
            parsed = json.loads(str(evidence["quote"]))
        except json.JSONDecodeError:
            continue
        numbers.extend(_row_numbers(parsed.get("rows") if isinstance(parsed, Mapping) else parsed))
    return tuple(numbers)


def _near(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= TOLERANCE


def _unmatched_data_numbers(
    claim_text: str,
    message: Mapping[str, Any],
) -> list[str]:
    question = _content(message).get("question")
    question_tokens = _numeric_tokens(question) if isinstance(question, str) else ()
    question_months = set(_month_labels(question)) if isinstance(question, str) else set()
    sql_months = set(_sql_month_labels(message))
    sql_numbers = _sql_numbers(message)
    unmatched = [
        label
        for label in _month_labels(claim_text)
        if label not in question_months and label not in sql_months
    ]
    for token, value, percentage in _numeric_tokens(claim_text):
        if any(_near(value, question_value) for _, question_value, _ in question_tokens):
            continue
        normalized = value / Decimal(100) if percentage else value
        if not any(_near(normalized, actual) for actual in sql_numbers):
            unmatched.append(token)
    return unmatched


def _event_sort_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("case_id", ""),
        event.get("msg_id", ""),
        event.get("event_type", ""),
        event.get("claim_index") if isinstance(event.get("claim_index"), int) else -1,
    )


def hallucination_report(dataset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    denominator = 0
    template_fallback_products = 0
    for row in dataset:
        case_id = _case_id(row)
        trace_id = str(row.get("trace_id", ""))
        messages = _items(row.get("messages"))
        degraded_ids = {
            str(message.get("msg_id"))
            for message in messages
            if _content(message).get("generated_by") == "template_fallback"
        }
        template_fallback_products += len(degraded_ids)
        for message in messages:
            msg_id = str(message.get("msg_id", ""))
            if msg_id in degraded_ids:
                continue
            claims = _items(message.get("claims"))
            evidence = _items(message.get("evidence"))
            for claim_index, claim in enumerate(claims):
                kind = claim.get("kind")
                text = claim.get("text")
                if kind not in DETERMINISTIC_KINDS or not isinstance(text, str):
                    continue
                denominator += 1
                supports = [
                    item
                    for item in evidence
                    if isinstance(item.get("supports_claim"), str)
                    and str(item["supports_claim"]).strip() == text.strip()
                ]
                evidence_ref = (
                    str(supports[0].get("ref")) if supports and supports[0].get("ref") is not None else None
                )
                if not supports:
                    events.append(
                        {
                            "case_id": case_id,
                            "trace_id": trace_id,
                            "msg_id": msg_id,
                            "claim_index": claim_index,
                            "claim_text": text,
                            "event_type": "claim_without_evidence",
                            "evidence_ref": None,
                        }
                    )
                if kind == "data_conclusion":
                    unmatched = _unmatched_data_numbers(text, message)
                    if unmatched:
                        events.append(
                            {
                                "case_id": case_id,
                                "trace_id": trace_id,
                                "msg_id": msg_id,
                                "claim_index": claim_index,
                                "claim_text": text,
                                "event_type": "data_conclusion_number_mismatch",
                                "evidence_ref": evidence_ref,
                                "unmatched_numbers": unmatched,
                            }
                        )
            validation = _content(message).get("quote_validation")
            failures = validation.get("failures") if isinstance(validation, Mapping) else None
            for failure in _items(failures):
                if failure.get("reason") != "invalid_sentence_ref":
                    continue
                claim_text = str(failure.get("claim_text", ""))
                claim_index = next(
                    (
                        index
                        for index, claim in enumerate(claims)
                        if claim.get("text") == claim_text
                    ),
                    None,
                )
                events.append(
                    {
                        "case_id": case_id,
                        "trace_id": trace_id,
                        "msg_id": msg_id,
                        "claim_index": claim_index,
                        "claim_text": claim_text,
                        "event_type": "invalid_sentence_ref",
                        "evidence_ref": None,
                        "sentence_ref": failure.get("sentence_ref"),
                    }
                )
        for review in messages:
            verdict = review.get("verdict")
            content = _content(review)
            reviewed_msg_id = content.get("reviewed_msg_id")
            if not isinstance(verdict, Mapping) or reviewed_msg_id in degraded_ids:
                continue
            for hit in _items(verdict.get("rule_hits")):
                if hit.get("rule_id") != "R-02":
                    continue
                events.append(
                    {
                        "case_id": case_id,
                        "trace_id": trace_id,
                        "msg_id": str(review.get("msg_id", "")),
                        "claim_index": None,
                        "claim_text": None,
                        "event_type": "review_r02_unsupported",
                        "evidence_ref": hit.get("evidence_ref"),
                        "reviewed_msg_id": reviewed_msg_id,
                        "reason": hit.get("reason"),
                    }
                )
    events.sort(key=_event_sort_key)
    return {
        "denominator_claims": denominator,
        "numerator_events": len(events),
        "rate": fixed_rate(len(events), denominator),
        "denominator_zero": denominator == 0,
        "template_fallback_products": template_fallback_products,
        "events": events,
    }
