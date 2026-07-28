"""Exact knowledge-point coverage and chunk-utilization reports."""

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


def coverage_report(
    dataset: Sequence[Mapping[str, Any]],
    core_points: Sequence[str],
    chunk_ids: Sequence[str] = (),
) -> dict[str, Any]:
    evidence: dict[str, list[dict[str, Any]]] = {point: [] for point in core_points}
    retrieved: set[str] = set()
    for row in dataset:
        case = row.get("case")
        case_id = str(case.get("case_id", "")) if isinstance(case, Mapping) else ""
        trace_id = str(row.get("trace_id", ""))
        for message in _items(row.get("messages")):
            if message.get("role") != "produce":
                continue
            content = _content(message)
            coverage = content.get("coverage")
            if isinstance(coverage, list):
                for value in coverage:
                    if isinstance(value, str) and value in evidence:
                        evidence[value].append(
                            {
                                "case_id": case_id,
                                "trace_id": trace_id,
                                "msg_id": message.get("msg_id"),
                                "coverage_value": value,
                            }
                        )
            retrieved_ids = content.get("retrieved_chunk_ids")
            if isinstance(retrieved_ids, list):
                retrieved.update(
                    str(value) for value in retrieved_ids if isinstance(value, str)
                )
    covered_points = [
        {
            "knowledge_point": point,
            "evidence": sorted(
                evidence[point],
                key=lambda item: (item["case_id"], str(item["msg_id"])),
            ),
        }
        for point in core_points
        if evidence[point]
    ]
    uncovered = [point for point in core_points if not evidence[point]]
    return {
        "covered": len(covered_points),
        "total": len(core_points),
        "rate": fixed_rate(len(covered_points), len(core_points)),
        "covered_points": covered_points,
        "uncovered_points": uncovered,
        "retrieved_chunk_ids": sorted(retrieved),
        "never_retrieved_chunk_ids": [item for item in chunk_ids if item not in retrieved],
    }
