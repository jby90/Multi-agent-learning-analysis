"""Offline recomputation adapter for production-routed v4 formal journeys.

V4 evaluates a target knowledge point through a real curriculum route.  A
single formal case may therefore contain prerequisite sessions before its
target session.  Facts are counted from every published session, while the
frozen adaptation scenario and the designated 10x3 coverage cell are scored
against the unique target session.  This preserves the approved metric
formulas without pretending that the first prerequisite is the target.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from eval.v3_metrics import (
    FinalReviewIncomplete,
    build_adaptation_nodes,
    build_coverage_cells,
    build_fact_units,
    compute_v3_metrics,
)
from eval.v3_recompute import _human_template, _read_json, _write_json, render_markdown
from eval.v4_cases import load_gold_standard
from eval.v4_workbook import export_v4_workbook, read_human_review_workbook


SCRIPT_EXPECTATIONS: dict[str, tuple[str, int]] = {
    "S-STEP-UP-B": (
        "initial_route(basic) → learner_correct → step_up → complete",
        2,
    ),
    "S-STEP-UP-A": (
        "initial_route(applied) → learner_correct → step_up → complete",
        2,
    ),
    "S-REFRESH": (
        "initial_route(basic) → wrong → targeted_followup/rebuttal → wrong "
        "→ refresh → correct → step_up → complete",
        # Preserve the approved semantic-node denominator: same-tier refresh
        # is one adaptation opportunity, not three retry-level nodes.
        2,
    ),
    "S-REBUTTAL": (
        "initial_route(applied) → wrong → targeted_followup/rebuttal "
        "→ corrected → step_up → complete",
        3,
    ),
    "S-DOWNSTEP": (
        "initial_route(applied) → wrong → targeted_followup/rebuttal → wrong "
        "→ step_down → correct → step_up → complete",
        4,
    ),
}

# The three designated cases per point prove the basic, applied and advanced
# cells respectively.  Refresh and down-step remain pressure tests and cannot
# silently inflate coverage.
COVERAGE_SCRIPT_DIFFICULTY = {
    "S-STEP-UP-B": "BASIC",
    "S-STEP-UP-A": "APPLIED",
    "S-REBUTTAL": "ADVANCED",
}


@dataclass(frozen=True, slots=True)
class NormalizedV4Case:
    fact_runs: tuple[dict[str, Any], ...]
    target_run: dict[str, Any]


def _selected_point(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in messages:
        payload = message.get("payload")
        if not isinstance(payload, Mapping) or payload.get("type") != "profile_assessment":
            continue
        content = payload.get("content")
        if isinstance(content, Mapping):
            return str(content.get("selected_knowledge_point") or "")
    return ""


def normalize_v4_case(case: Mapping[str, Any]) -> NormalizedV4Case:
    """Flatten a V4 journey without losing its prerequisite/target boundary."""

    if str(case.get("route_mode") or "") != "production":
        raise ValueError(f"{case.get('case_id')} is not production-routed")
    sessions = case.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError(f"{case.get('case_id')} has no completed sessions")
    target = str(case.get("target_knowledge_point") or "")
    fact_runs: list[dict[str, Any]] = []
    target_runs: list[dict[str, Any]] = []
    for index, session in enumerate(sessions, start=1):
        if not isinstance(session, Mapping):
            raise ValueError(f"{case.get('case_id')} session {index} is invalid")
        messages = session.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"{case.get('case_id')} session {index} has no TRACE")
        normalized = {
            "run_id": str(case.get("run_id") or ""),
            "seed_id": str(case.get("seed_id") or ""),
            "case_id": str(case.get("case_id") or ""),
            "session_id": str(session.get("session_id") or ""),
            "route_mode": "production",
            "profile_id": str(case.get("profile_id") or ""),
            "messages": list(messages),
            "journey_session_index": index,
            "journey_knowledge_point": str(session.get("knowledge_point") or ""),
            "target_knowledge_point": target,
        }
        fact_runs.append(normalized)
        session_point = str(session.get("knowledge_point") or "")
        selected_point = _selected_point(messages)
        if session_point == target and selected_point == target:
            target_runs.append(normalized)
    if len(target_runs) != 1:
        raise ValueError(
            f"{case.get('case_id')} must expose exactly one target session for "
            f"{target!r}; found {len(target_runs)}"
        )
    return NormalizedV4Case(tuple(fact_runs), target_runs[0])


def normalize_v4_gold(
    gold: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Translate frozen V4 gold into the established metric input vocabulary."""

    rows: list[dict[str, Any]] = []
    for case_id in sorted(gold):
        item = gold[case_id]
        script_id = str(item.get("learner_script_id") or "")
        if script_id not in SCRIPT_EXPECTATIONS:
            raise ValueError(f"{case_id} has unsupported learner script {script_id}")
        sequence, node_count = SCRIPT_EXPECTATIONS[script_id]
        point_id = str(item.get("knowledge_point_id") or "")
        coverage_difficulty = COVERAGE_SCRIPT_DIFFICULTY.get(script_id)
        rows.append(
            {
                "case_id": case_id,
                "目标知识点": str(item.get("target_knowledge_point") or ""),
                "预期初始难度": str(item.get("expected_initial_difficulty") or ""),
                "预期最终难度": str(item.get("expected_final_difficulty") or ""),
                "预期适配序列": sequence,
                "预期适配节点数": node_count,
                "计入覆盖率": "是" if coverage_difficulty else "否",
                "覆盖格": (
                    f"{point_id}-{coverage_difficulty}" if coverage_difficulty else ""
                ),
                "learner_script_id": script_id,
                "expected_target_plan_position": item.get(
                    "expected_target_plan_position"
                ),
            }
        )
    return rows


def load_v4_run_directory(
    path: Path, *, require_complete: bool = True
) -> list[dict[str, Any]]:
    path = Path(path)
    manifest = _read_json(path / "run_manifest.json")
    if manifest.get("route_mode") != "production":
        raise ValueError(f"formal run is not production-routed: {path}")
    if require_complete and (
        manifest.get("case_count") != 50 or manifest.get("completed_count") != 50
    ):
        raise ValueError(f"formal V4 run must contain 50 completed cases: {path}")
    rows: list[dict[str, Any]] = []
    for entry in manifest.get("cases", []):
        case_id = str(entry.get("case_id") or "")
        rows.append(_read_json(path / "cases" / case_id / "case_result.json"))
    if require_complete and len(rows) != 50:
        raise ValueError(f"formal V4 run does not expose 50 case results: {path}")
    return rows


def recompute_v4(
    run_dirs: Sequence[Path],
    output_dir: Path,
    *,
    mode: str,
    human_review_path: Path | None = None,
    human_review_workbook: Path | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        cases.extend(load_v4_run_directory(run_dir, require_complete=require_complete))
    seeds = {str(case.get("seed_id") or "") for case in cases}
    if require_complete and seeds != {"seed_A", "seed_B"}:
        raise ValueError("formal V4 recomputation requires isolated seed_A and seed_B")

    normalized = [normalize_v4_case(case) for case in cases]
    fact_runs = [run for case in normalized for run in case.fact_runs]
    target_runs = [case.target_run for case in normalized]
    gold_rows = normalize_v4_gold(load_gold_standard())

    facts = build_fact_units(fact_runs, mode="AUTO_PRELIMINARY")
    nodes = build_adaptation_nodes(target_runs, gold_rows, mode="AUTO_PRELIMINARY")
    cells = build_coverage_cells(target_runs, gold_rows, mode="AUTO_PRELIMINARY")
    automatic = compute_v3_metrics(
        deepcopy(facts), deepcopy(nodes), deepcopy(cells), mode="AUTO_PRELIMINARY"
    )
    if human_review_path and human_review_workbook:
        raise ValueError("choose either JSON or workbook human review, not both")
    human_review = (
        _read_json(human_review_path)
        if human_review_path
        else read_human_review_workbook(human_review_workbook)
        if human_review_workbook
        else None
    )
    combined = compute_v3_metrics(
        facts, nodes, cells, mode=mode, human_review=human_review
    )

    by_seed: dict[str, Any] = {}
    for seed in sorted(seeds):
        by_seed[seed] = compute_v3_metrics(
            [row for row in facts if row["seed_id"] == seed],
            [row for row in nodes if row["seed_id"] == seed],
            [row for row in cells if row["seed_id"] == seed],
            mode="AUTO_PRELIMINARY",
        )
        by_seed[seed]["mode"] = mode
        by_seed[seed]["label_status"] = combined["label_status"]

    report = {
        "mode": mode,
        "metric_adapter": "v4-target-session-v1",
        "scope_note": (
            "事实单元覆盖完整真实路由旅程；正式适配节点与10×3覆盖格按唯一目标知识点会话复算。"
        ),
        "run_directories": [str(Path(path).resolve()) for path in run_dirs],
        "seed_case_counts": {
            seed: sum(str(case.get("seed_id") or "") == seed for case in cases)
            for seed in sorted(seeds)
        },
        "combined": combined,
        "automatic_preliminary": automatic,
        "by_seed": by_seed,
        "row_counts": {
            "journey_sessions_for_fact_units": len(fact_runs),
            "target_sessions": len(target_runs),
            "fact_units": len(facts),
            "adaptation_nodes": len(nodes),
            "coverage_cells": len(cells),
        },
    }
    output_dir = Path(output_dir)
    _write_json(output_dir / "facts_v4.json", facts)
    _write_json(output_dir / "adaptation_nodes_v4.json", nodes)
    _write_json(output_dir / "coverage_30_cells_v4.json", cells)
    _write_json(output_dir / f"metrics_{mode.lower()}_v4.json", report)
    (output_dir / f"metrics_{mode.lower()}_v4.md").write_text(
        render_markdown(report).replace("# v3.2", "# v4"), encoding="utf-8"
    )
    if mode == "AUTO_PRELIMINARY":
        _write_json(
            output_dir / "human_review_template_v4.json",
            _human_template(facts, nodes, cells),
        )
    workbook_name = (
        "03_TRACE字段与指标计算模板_v4_自动初算.xlsx"
        if mode == "AUTO_PRELIMINARY"
        else "03_TRACE字段与指标计算模板_v4_最终结果.xlsx"
    )
    export_v4_workbook(
        output_dir / workbook_name,
        report,
        facts,
        nodes,
        cells,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("AUTO_PRELIMINARY", "FINAL_HUMAN_REVIEWED"),
        required=True,
    )
    parser.add_argument("--human-review", type=Path)
    parser.add_argument("--human-review-workbook", type=Path)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    try:
        report = recompute_v4(
            args.run_dir,
            args.output_dir,
            mode=args.mode,
            human_review_path=args.human_review,
            human_review_workbook=args.human_review_workbook,
            require_complete=not args.allow_incomplete,
        )
    except (ValueError, FinalReviewIncomplete) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
