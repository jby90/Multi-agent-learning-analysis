"""Explain every failed v3.2 strict-coverage gate from production TRACE.

This module is diagnostic only.  It does not alter the frozen metric formula,
the production route, or any Agent output.  Its purpose is to turn a failed
coverage cell into an actionable, trace-backed failure classification before
runtime code is changed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from eval.v3_cases import load_gold_standard
from eval.v3_metrics import build_coverage_cells
from eval.v3_recompute import load_run_directory


RESOURCE_TYPES = ("lecture_note", "practice_guide", "quiz_set")
GATE_FIELDS = (
    "route_reachable",
    "route_evidence_valid",
    "learning_contract_match",
    "profile_resource_match",
    "lecture_pass",
    "practice_pass",
    "quiz_pass",
    "feedback_pass",
    "review_pass",
    "evidence_pass",
)


def _items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    value = payload.get("content") if isinstance(payload, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _payload_type(message: Mapping[str, Any]) -> str:
    payload = message.get("payload")
    return str(payload.get("type") or "") if isinstance(payload, Mapping) else ""


def _decision(message: Mapping[str, Any]) -> str:
    verdict = message.get("verdict")
    return str(verdict.get("decision") or "") if isinstance(verdict, Mapping) else ""


def _review_index(
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for message in messages:
        if message.get("role") not in {"verdict", "re_verdict"}:
            continue
        reviewed = str(_content(message).get("reviewed_msg_id") or "")
        if reviewed:
            result.setdefault(reviewed, []).append(message)
    return {
        key: tuple(sorted(value, key=lambda item: int(item.get("step") or 0)))
        for key, value in result.items()
    }


def _resource_diagnosis(
    messages: Sequence[Mapping[str, Any]],
    *,
    resource_type: str,
    knowledge_point: str,
    difficulty: str,
) -> dict[str, Any]:
    reviews = _review_index(messages)
    products = [
        message for message in messages if _payload_type(message) == resource_type
    ]
    exact = [
        message
        for message in products
        if str(_content(message).get("knowledge_point") or "") == knowledge_point
        and str(_content(message).get("difficulty") or "") == difficulty
    ]
    approved = [
        message
        for message in exact
        if any(
            _decision(review) == "approve"
            for review in reviews.get(str(message.get("msg_id") or ""), ())
        )
    ]
    if approved:
        status = "approved_exact"
    elif exact:
        reviewed = any(
            reviews.get(str(message.get("msg_id") or ""), ()) for message in exact
        )
        status = "review_not_approved" if reviewed else "generated_not_reviewed"
    elif products:
        status = "generated_wrong_target_or_difficulty"
    else:
        status = "not_generated"
    return {
        "status": status,
        "generated_count": len(products),
        "exact_count": len(exact),
        "approved_exact_count": len(approved),
        "approved_msg_ids": [str(message.get("msg_id") or "") for message in approved],
    }


def _profile_binding_diagnosis(
    run: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    knowledge_point: str,
    difficulty: str,
) -> dict[str, Any]:
    profile_id = str(run.get("profile_id") or "")
    reviews = _review_index(messages)
    approved = []
    for message in messages:
        if _payload_type(message) not in RESOURCE_TYPES:
            continue
        if str(_content(message).get("knowledge_point") or "") != knowledge_point:
            continue
        if str(_content(message).get("difficulty") or "") != difficulty:
            continue
        if any(
            _decision(review) == "approve"
            for review in reviews.get(str(message.get("msg_id") or ""), ())
        ):
            approved.append(message)

    details = []
    for message in approved:
        top_level = str(message.get("student_profile_ref") or "")
        nested = str(_content(message).get("student_profile_ref") or "")
        if top_level == profile_id and profile_id:
            status = "matched"
        elif nested == profile_id and profile_id:
            status = "nested_only"
        elif not top_level and not nested:
            status = "missing"
        else:
            status = "mismatch"
        details.append(
            {
                "msg_id": str(message.get("msg_id") or ""),
                "resource_type": _payload_type(message),
                "top_level_ref": top_level,
                "nested_ref": nested,
                "status": status,
            }
        )
    if not profile_id:
        classification = "run_profile_missing"
    elif not approved:
        classification = "no_approved_exact_resource"
    elif all(item["status"] == "matched" for item in details):
        classification = "matched"
    elif any(item["status"] == "nested_only" for item in details):
        classification = "schema_location_mismatch"
    elif any(item["status"] == "mismatch" for item in details):
        classification = "profile_ref_mismatch"
    else:
        classification = "profile_ref_missing"
    return {
        "classification": classification,
        "profile_id": profile_id,
        "resources": details,
    }


def audit_coverage_cells(
    runs: Sequence[Mapping[str, Any]],
    gold_rows: Sequence[Mapping[str, Any]],
    cells: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a per-cell failure matrix plus aggregate failure causes."""

    coverage_cells = list(cells or build_coverage_cells(runs, gold_rows))
    runs_by_key = {
        (str(run.get("seed_id") or ""), str(run.get("case_id") or "")): run
        for run in runs
    }
    rows = []
    failure_causes: Counter[str] = Counter()
    gate_pass_counts: Counter[str] = Counter()
    for cell in coverage_cells:
        seed_id = str(cell.get("seed_id") or "")
        case_id = str(cell.get("case_id") or "")
        run = runs_by_key.get((seed_id, case_id))
        if run is None:
            raise ValueError(f"coverage cell has no matching run: {seed_id}/{case_id}")
        messages = list(_items(run.get("messages")))
        knowledge_point = str(cell.get("knowledge_point") or "")
        difficulty = str(cell.get("difficulty") or "")
        resources = {
            resource_type: _resource_diagnosis(
                messages,
                resource_type=resource_type,
                knowledge_point=knowledge_point,
                difficulty=difficulty,
            )
            for resource_type in RESOURCE_TYPES
        }
        profile_binding = _profile_binding_diagnosis(
            run,
            messages,
            knowledge_point=knowledge_point,
            difficulty=difficulty,
        )
        failed_gates = [
            field for field in GATE_FIELDS if int(cell.get(field) or 0) != 1
        ]
        for field in GATE_FIELDS:
            gate_pass_counts[field] += int(cell.get(field) or 0)
        causes = []
        if "route_reachable" in failed_gates:
            causes.append("route_wrong_or_unreachable")
        if "route_evidence_valid" in failed_gates:
            causes.append("route_evidence_missing")
        if "learning_contract_match" in failed_gates:
            causes.append("learning_contract_missing_or_mismatch")
        if "profile_resource_match" in failed_gates:
            causes.append(profile_binding["classification"])
        for resource_type, gate in (
            ("lecture_note", "lecture_pass"),
            ("practice_guide", "practice_pass"),
            ("quiz_set", "quiz_pass"),
        ):
            if gate in failed_gates:
                causes.append(f"{resource_type}:{resources[resource_type]['status']}")
        if "feedback_pass" in failed_gates:
            causes.append("feedback_missing")
        if "evidence_pass" in failed_gates:
            causes.append("approved_fact_evidence_missing")
        unique_causes = list(dict.fromkeys(causes))
        failure_causes.update(unique_causes)
        rows.append(
            {
                "coverage_cell_id": str(cell.get("coverage_cell_id") or ""),
                "seed_id": seed_id,
                "case_id": case_id,
                "knowledge_point": knowledge_point,
                "difficulty": difficulty,
                "cell_pass": int(cell.get("cell_pass") or 0),
                "failed_gates": failed_gates,
                "failure_causes": unique_causes,
                "profile_binding": profile_binding,
                "resources": resources,
            }
        )
    total = len(rows)
    return {
        "scope": "v3.2 strict coverage diagnostic; metric formula unchanged",
        "cell_count": total,
        "cell_pass_count": sum(row["cell_pass"] for row in rows),
        "gate_pass_counts": {
            field: {
                "passed": gate_pass_counts[field],
                "total": total,
            }
            for field in GATE_FIELDS
        },
        "failure_cause_counts": dict(failure_causes.most_common()),
        "cells": rows,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# v3.2 严格覆盖格缺口审计",
        "",
        "> 本报告只解释生产 TRACE 中的门禁缺口，不修改五指标公式、状态机或 Agent 输出。",
        "",
        f"- 覆盖格：{report['cell_count']}",
        f"- 当前完整通过：{report['cell_pass_count']}",
        "",
        "## 门禁通过情况",
        "",
        "| 门禁 | 通过 | 总数 |",
        "|---|---:|---:|",
    ]
    for gate, value in report["gate_pass_counts"].items():
        lines.append(f"| `{gate}` | {value['passed']} | {value['total']} |")
    lines.extend(["", "## 缺口原因", "", "| 原因 | 覆盖格数 |", "|---|---:|"])
    for cause, count in report["failure_cause_counts"].items():
        lines.append(f"| `{cause}` | {count} |")
    lines.extend(
        [
            "",
            "## 逐格明细",
            "",
            "| Seed | 案例 | 知识点 | 难度 | 失败门禁 | 原因 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in report["cells"]:
        lines.append(
            "| {seed_id} | {case_id} | {knowledge_point} | {difficulty} | {gates} | {causes} |".format(
                **row,
                gates="<br>".join(row["failed_gates"]) or "—",
                causes="<br>".join(row["failure_causes"]) or "—",
            )
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    runs: list[dict[str, Any]] = []
    for run_dir in args.run_dir:
        runs.extend(
            load_run_directory(
                run_dir,
                require_complete=not args.allow_incomplete,
            )
        )
    report = audit_coverage_cells(runs, list(load_gold_standard().values()))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "coverage_gap_audit_v3_2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "coverage_gap_audit_v3_2.md").write_text(
        render_markdown(report),
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "cells"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
