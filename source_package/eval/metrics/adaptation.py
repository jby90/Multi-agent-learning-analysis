"""Automatic R-03 adaptation rate linked to final review verdicts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from eval.metrics import fixed_rate


PRODUCT_TYPES = frozenset({"lecture_note", "quiz_set", "practice_guide"})
_GAP_RE = re.compile(r"difficulty_gap\s*=\s*(\d+)")


def _items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    value = payload.get("content") if isinstance(payload, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def adaptation_report(dataset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    excluded_items: list[dict[str, Any]] = []
    candidate_products = 0
    for row in dataset:
        case = row.get("case")
        case_id = str(case.get("case_id", "")) if isinstance(case, Mapping) else ""
        profile_id = str(case.get("profile_id", "")) if isinstance(case, Mapping) else ""
        trace_id = str(row.get("trace_id", ""))
        messages = _items(row.get("messages"))
        products = [
            message
            for message in messages
            if message.get("role") == "produce"
            and message.get("payload", {}).get("type") in PRODUCT_TYPES
            and _content(message).get("generated_by") != "template_fallback"
        ]
        candidate_products += len(products)
        reviews: dict[str, Mapping[str, Any]] = {}
        for message in messages:
            if message.get("role") not in {"verdict", "re_verdict"}:
                continue
            reviewed = _content(message).get("reviewed_msg_id")
            if isinstance(reviewed, str):
                previous = reviews.get(reviewed)
                if previous is None or int(message.get("step", 0)) > int(previous.get("step", 0)):
                    reviews[reviewed] = message
        for product in products:
            product_id = str(product.get("msg_id", ""))
            review = reviews.get(product_id)
            base = {
                "case_id": case_id,
                "profile_id": profile_id,
                "trace_id": trace_id,
                "product_msg_id": product_id,
                "payload_type": product.get("payload", {}).get("type"),
            }
            if review is None:
                items.append(
                    {
                        **base,
                        "matched": False,
                        "difficulty_gap": None,
                        "verdict_msg_id": None,
                        "decision": None,
                        "evidence_ref": None,
                        "reason": "missing_review",
                    }
                )
                continue
            verdict = review.get("verdict")
            hits = _items(verdict.get("rule_hits")) if isinstance(verdict, Mapping) else ()
            decision = verdict.get("decision") if isinstance(verdict, Mapping) else None
            if decision == "reject":
                excluded_items.append(
                    {
                        **base,
                        "verdict_msg_id": review.get("msg_id"),
                        "decision": decision,
                        "rule_ids": [
                            str(hit["rule_id"])
                            for hit in hits
                            if isinstance(hit.get("rule_id"), str)
                        ],
                        "reason": "latest_review_rejected",
                    }
                )
                continue
            r03 = next((hit for hit in hits if hit.get("rule_id") == "R-03"), None)
            if r03 is None:
                matched = True
                gap: int | None = 0
                evidence_ref = None
                reason = "R-03未命中"
            else:
                matched = False
                reason = str(r03.get("reason", ""))
                match = _GAP_RE.search(reason)
                gap = int(match.group(1)) if match else None
                evidence_ref = r03.get("evidence_ref")
            items.append(
                {
                    **base,
                    "matched": matched,
                    "difficulty_gap": gap,
                    "verdict_msg_id": review.get("msg_id"),
                    "decision": decision,
                    "evidence_ref": evidence_ref,
                    "reason": reason,
                }
            )
    items.sort(key=lambda item: (item["case_id"], item["product_msg_id"]))
    excluded_items.sort(key=lambda item: (item["case_id"], item["product_msg_id"]))
    matched_count = sum(1 for item in items if item["matched"])
    return {
        "candidate_products": candidate_products,
        "excluded_rejected_products": len(excluded_items),
        "total_products": len(items),
        "matched": matched_count,
        "unmatched": len(items) - matched_count,
        "rate": fixed_rate(matched_count, len(items)),
        "items": items,
        "excluded_items": excluded_items,
    }
