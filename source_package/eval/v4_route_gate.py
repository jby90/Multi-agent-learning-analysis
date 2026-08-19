"""Production-faithful route gate for the frozen v4 50-case library."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from agents.diagnosis_agent import load_profiles
from agents.kb_loader import require_valid_chunks
from agents.knowledge_scope import CHUNK_DIRECTORY
from agents.persona_router import (
    PersonaDiagnosticRouter,
    apply_calibration,
    calibration_probe_for,
    load_dependencies,
    resolve_experience_tag_point,
)
from eval.v4_cases import load_formal_cases, load_gold_standard


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "config" / "domains" / "production_progress" / "task_manifest.json"
)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _chunk_records() -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": str(chunk.chunk_id),
            "knowledge_point": str(chunk.knowledge_point),
            "prerequisites": [str(value) for value in chunk.prerequisites],
        }
        for chunk in require_valid_chunks(CHUNK_DIRECTORY)
    ]


def audit_routes() -> dict[str, Any]:
    cases = [asdict(case) for case in load_formal_cases()]
    gold = load_gold_standard()
    profiles = load_profiles()
    router = PersonaDiagnosticRouter(profiles)
    dependencies = load_dependencies()
    chunks = _chunk_records()
    template_map = _read(MANIFEST_PATH)["diagnostic_template_ids"]
    details: list[dict[str, Any]] = []
    coverage_cells: set[tuple[str, str]] = set()

    for case in cases:
        case_id = str(case["case_id"])
        expected = gold[case_id]
        target = str(expected["target_knowledge_point"])
        scope = set(profiles[str(case["profile_id"])]["knowledge_scope"])
        tags = list(case.get("experience_tags", ()))
        focus = resolve_experience_tag_point(tags[0]) if tags else None
        route = router.build_plan(
            str(case["profile_id"]),
            dict(case["pretest_answers"]),
            dependencies=dependencies,
            experience_tag_point=focus,
            chunk_records=chunks,
        )
        expected_probe_ids = [
            str(item["probe_id"])
            for item in case.get("diagnostic_probe_answers", ())
        ]
        comparison_route = router.build_plan(
            str(case["profile_id"]),
            dict(case["pretest_answers"]),
            dependencies=dependencies,
            experience_tag_point=focus,
            chunk_records=chunks,
        )
        actual_probe_ids: list[str] = []
        if focus:
            probe = calibration_probe_for(focus)
            if probe is not None:
                actual_probe_ids = [str(probe["probe_id"])]
                if expected_probe_ids == actual_probe_ids:
                    apply_calibration(route, focus, actual_probe_ids[0], True)
                    apply_calibration(
                        comparison_route, focus, actual_probe_ids[0], True
                    )

        plan = list(route["knowledge_point_plan"])
        target_items = [
            (position, item)
            for position, item in enumerate(plan, start=1)
            if item.get("knowledge_point") == target
            and item.get("tier") != "prerequisite_lift"
        ]
        target_position = target_items[0][0] if len(target_items) == 1 else None
        target_item = target_items[0][1] if len(target_items) == 1 else {}
        actual_initial = str(target_item.get("initial_difficulty") or "")
        actual_initial_template = str(
            template_map.get(target, {}).get(actual_initial, "")
        )
        actual_final = {"basic": "applied", "applied": "advanced"}.get(
            actual_initial, actual_initial
        )
        actual_final_template = str(template_map.get(target, {}).get(actual_final, ""))
        route_points = [str(item["knowledge_point"]) for item in plan]

        checks = {
            "profile_scope": target in scope,
            "target_reachable": len(target_items) == 1,
            # Compare the complete post-calibration plan.  A previous gate
            # treated every calibrated route as deterministic without actually
            # rebuilding it, which left the most important production branch
            # unproved.
            "route_deterministic": deepcopy(route) == comparison_route,
            "probe_sequence": actual_probe_ids == expected_probe_ids,
            "route_points": route_points == expected["expected_route_points"],
            "target_position": target_position
            == int(expected["expected_target_plan_position"]),
            "initial_difficulty": actual_initial
            == expected["expected_initial_difficulty"],
            "final_difficulty": actual_final == expected["expected_final_difficulty"],
            "initial_template": actual_initial_template
            == expected["expected_initial_template"],
            "final_template": actual_final_template
            == expected["expected_final_template"],
            "correct_not_blind_spot": all(
                not evidence.get("is_correct")
                or evidence.get("knowledge_point") not in route.get("blind_spots", [])
                for evidence in route.get("route_evidence", [])
            ),
        }
        if checks["target_reachable"]:
            coverage_cells.add((target, actual_initial))
            coverage_cells.add((target, actual_final))
        details.append(
            {
                "case_id": case_id,
                "profile_id": case["profile_id"],
                "target_knowledge_point": target,
                "learner_script_id": case["learner_script_id"],
                "target_plan_position": target_position,
                "route_points": route_points,
                "actual_initial_difficulty": actual_initial,
                "actual_final_difficulty": actual_final,
                "actual_initial_template": actual_initial_template,
                "actual_final_template": actual_final_template,
                "actual_probe_ids": actual_probe_ids,
                "checks": checks,
                "passed": all(checks.values()),
            }
        )

    point_counts = Counter(
        row["target_knowledge_point"] for row in details
    )
    profile_counts = Counter(row["profile_id"] for row in details)
    script_counts = Counter(row["learner_script_id"] for row in details)
    summary_checks = {
        "case_count_50": len(details) == 50,
        "ten_points_five_each": len(point_counts) == 10
        and set(point_counts.values()) == {5},
        "three_personas_present": set(profile_counts)
        == {"planner_new", "craft_engineer", "line_leader"},
        "five_scenarios_ten_each": len(script_counts) == 5
        and set(script_counts.values()) == {10},
        "thirty_coverage_cells": len(coverage_cells) == 30,
        "all_cases_pass": all(row["passed"] for row in details),
    }
    return {
        "status": "passed" if all(summary_checks.values()) else "failed",
        "summary_checks": summary_checks,
        "case_count": len(details),
        "passed_count": sum(row["passed"] for row in details),
        "profile_counts": dict(sorted(profile_counts.items())),
        "knowledge_point_counts": dict(sorted(point_counts.items())),
        "script_counts": dict(sorted(script_counts.items())),
        "coverage_cell_count": len(coverage_cells),
        "coverage_cells": [list(value) for value in sorted(coverage_cells)],
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_routes()
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
