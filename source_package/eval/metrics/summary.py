"""Cross-metric helpers, including session-level refusal rate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from eval.metrics import fixed_rate


def _items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    value = payload.get("content") if isinstance(payload, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def refusal_report(dataset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    refused_cases: set[str] = set()
    for row in dataset:
        case = row.get("case")
        case_id = str(case.get("case_id", "")) if isinstance(case, Mapping) else ""
        trace_id = str(row.get("trace_id", ""))
        for message in _items(row.get("messages")):
            content = _content(message)
            if content.get("event") == "knowledge_refused":
                event_type = "knowledge_refused"
            elif content.get("student_visible_refusal") is True:
                event_type = "student_visible_refusal"
            else:
                continue
            refused_cases.add(case_id)
            events.append(
                {
                    "case_id": case_id,
                    "trace_id": trace_id,
                    "msg_id": message.get("msg_id"),
                    "event_type": event_type,
                    "reason": content.get("refuse_reason") or content.get("refusal_message"),
                }
            )
    events.sort(key=lambda item: (item["case_id"], str(item["msg_id"]), item["event_type"]))
    return {
        "refused_cases": len(refused_cases),
        "total_cases": len(dataset),
        "rate": fixed_rate(len(refused_cases), len(dataset)),
        "events": events,
    }
