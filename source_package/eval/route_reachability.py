"""Gate v3 production diagnosis routes without knowledge/template injection."""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from itertools import product
import json
from pathlib import Path
from typing import Any

from agents.diagnosis_agent import DiagnosisAgent, load_pretest, load_profiles
from agents.diagnostic_router import (
    DiagnosticRouter,
    ProbeResult,
    grade_probe_answer,
    load_diagnostic_probes,
)
from agents.task_agent import TaskAgent
from eval.v3_cases import load_formal_cases, load_gold_standard


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = ROOT / "eval" / "results"


def _content(message: Mapping[str, Any]) -> dict[str, Any]:
    payload = message.get("payload")
    content = payload.get("content") if isinstance(payload, Mapping) else None
    if not isinstance(content, Mapping):
        raise ValueError("diagnosis message has no payload.content")
    return dict(content)


def _answer_space() -> Iterable[dict[str, str]]:
    questions = load_pretest()
    ids = [str(item["question_id"]) for item in questions]
    options = [tuple(str(key) for key in item["options"]) for item in questions]
    for choices in product(*options):
        yield dict(zip(ids, choices, strict=True))


def _probe_results(case: Any) -> tuple[ProbeResult, ...]:
    library = {str(item["probe_id"]): item for item in load_diagnostic_probes()}
    return tuple(
        ProbeResult(
            item.probe_id,
            grade_probe_answer(library[item.probe_id], item.answer),
        )
        for item in case.diagnostic_probe_answers
    )


def _pre_probe_compatibility(cases: Iterable[Any]) -> dict[str, Any]:
    """Detect probe-family choices production cannot distinguish before probing."""

    def probe_family(probe_id: str) -> str:
        return probe_id.rsplit("-", 1)[0] if probe_id.startswith("DP-") else probe_id

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        observable = json.dumps(
            {
                "profile_id": case.profile_id,
                "experience_tags": sorted(case.experience_tags),
                "pretest_answers": dict(sorted(case.pretest_answers.items())),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        groups[observable].append(
            {
                "case_id": case.case_id,
                "probe_ids": [item.probe_id for item in case.diagnostic_probe_answers],
                "probe_families": sorted(
                    {
                        probe_family(item.probe_id)
                        for item in case.diagnostic_probe_answers
                    }
                ),
            }
        )
    conflicts: list[dict[str, Any]] = []
    for observable, rows in groups.items():
        requested = {tuple(row["probe_families"]) for row in rows}
        if len(requested) > 1:
            conflicts.append(
                {
                    "observable_input": json.loads(observable),
                    "case_count": len(rows),
                    "cases": rows,
                }
            )
    return {
        "observable_group_count": len(groups),
        "conflicting_group_count": len(conflicts),
        "affected_case_count": sum(item["case_count"] for item in conflicts),
        "maximum_distinguishable_routes": len(groups),
        "compatible": not conflicts,
        "conflicts": conflicts,
    }


@lru_cache(maxsize=1)
def audit_route_reachability() -> dict[str, Any]:
    """Run exhaustive pretest plus all 50 frozen production-input routes."""

    router = DiagnosticRouter()
    task = TaskAgent("route-reachability-v3")
    profiles = load_profiles()
    plan_counts: dict[str, int] = defaultdict(int)
    exhaustive_scenarios = 0

    for profile_id in sorted(profiles):
        diagnosis = DiagnosisAgent(f"route-exhaustive-{profile_id}")
        for answers in _answer_space():
            exhaustive_scenarios += 1
            content = _content(diagnosis.assess(profile_id, answers))
            for item in content["knowledge_point_plan"]:
                plan_counts[str(item["knowledge_point"])] += 1

    gold = load_gold_standard()
    formal_cases = load_formal_cases()
    pre_probe = _pre_probe_compatibility(formal_cases)
    formal_rows: list[dict[str, Any]] = []
    first_counts: dict[str, int] = defaultdict(int)
    evidence_valid_count = 0
    deterministic_count = 0
    for case in formal_cases:
        probes = _probe_results(case)
        first = _content(
            DiagnosisAgent(f"route-formal-{case.case_id}").assess(
                case.profile_id,
                case.pretest_answers,
                probe_results=probes,
                experience_tags=case.experience_tags,
            )
        )
        repeated = _content(
            DiagnosisAgent(f"route-formal-repeat-{case.case_id}").assess(
                case.profile_id,
                case.pretest_answers,
                probe_results=probes,
                experience_tags=case.experience_tags,
            )
        )
        selected = str(first.get("selected_knowledge_point") or "")
        difficulty = str(first.get("selected_difficulty") or "")
        item = next(
            (
                value
                for value in first["knowledge_point_plan"]
                if value.get("plan_item_id") == first.get("selected_plan_item_id")
            ),
            {},
        )
        evidence_ids = item.get("evidence_ids") if isinstance(item, Mapping) else []
        evidence_valid = bool(
            selected
            and evidence_ids
            and item.get("route_reason")
            and item.get("initial_difficulty") == difficulty
        )
        deterministic = (
            first["knowledge_point_plan"] == repeated["knowledge_point_plan"]
            and first.get("selected_knowledge_point")
            == repeated.get("selected_knowledge_point")
            and first.get("selected_difficulty") == repeated.get("selected_difficulty")
        )
        route = task.diagnosis_evidence(selected, difficulty)
        expected = gold[case.case_id]
        expected_point = str(expected["目标知识点"])
        expected_difficulty = str(expected["预期初始难度"])
        expected_template = str(expected["预期初始模板"])
        row = {
            "case_id": case.case_id,
            "profile_id": case.profile_id,
            "experience_tags": list(case.experience_tags),
            "route_mode": case.route_mode,
            "probe_ids": [probe.probe_id for probe in probes],
            "selected_plan_item_id": first.get("selected_plan_item_id"),
            "selected_knowledge_point": selected,
            "selected_difficulty": difficulty,
            "route_reason": item.get("route_reason"),
            "route_evidence_ids": list(evidence_ids or []),
            "route_evidence_valid": evidence_valid,
            "deterministic": deterministic,
            "actual_template_id": route["template_id"],
            "expected_knowledge_point": expected_point,
            "expected_difficulty": expected_difficulty,
            "expected_template_id": expected_template,
            "knowledge_point_match": selected == expected_point,
            "difficulty_match": difficulty == expected_difficulty,
            "template_match": route["template_id"] == expected_template,
            "forced_knowledge_point": False,
            "forced_template_id": False,
        }
        formal_rows.append(row)
        first_counts[selected] += 1
        evidence_valid_count += int(evidence_valid)
        deterministic_count += int(deterministic)

    points = list(router.core_knowledge_points)
    reachable = [point for point in points if first_counts[point] > 0]
    unreachable = [point for point in points if first_counts[point] == 0]
    route_matches = sum(row["knowledge_point_match"] for row in formal_rows)
    all_gates = (
        pre_probe["compatible"]
        and pre_probe["maximum_distinguishable_routes"] >= len(points)
        and len(reachable) == len(points)
        and not unreachable
        and evidence_valid_count == 50
        and deterministic_count == 50
        and route_matches == 50
        and all(not row["forced_knowledge_point"] for row in formal_rows)
        and all(not row["forced_template_id"] for row in formal_rows)
    )
    return {
        "audit_type": "v3_2_production_route_reachability",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules": {
            "forced_knowledge_point_allowed": False,
            "forced_template_id_allowed": False,
            "uses_production_profiles": True,
            "uses_five_question_pretest": True,
            "maximum_fixed_probes": 2,
            "selection_semantics": "first pending knowledge_point_plan item",
        },
        "scenario_space": {
            "profile_count": len(profiles),
            "valid_answer_combinations_per_profile": 4 ** len(load_pretest()),
            "exhaustive_pretest_scenarios": exhaustive_scenarios,
            "formal_case_count": len(formal_rows),
        },
        "summary": {
            "core_knowledge_points": len(points),
            "first_unit_reachable_count": len(reachable),
            "unreachable_count": len(unreachable),
            "traceable_selection_count": evidence_valid_count,
            "traceable_selection_rate": round(evidence_valid_count / 50, 4),
            "deterministic_case_count": deterministic_count,
            "formal_route_matches": route_matches,
            "forced_knowledge_point_count": 0,
            "forced_template_id_count": 0,
            "pre_probe_observable_group_count": pre_probe["observable_group_count"],
            "pre_probe_conflicting_group_count": pre_probe["conflicting_group_count"],
            "pre_probe_affected_case_count": pre_probe["affected_case_count"],
            "real_route_reachable_upper_bound": min(
                len(points), pre_probe["maximum_distinguishable_routes"]
            ),
            "status": "passed" if all_gates else "failed",
        },
        "pre_probe_compatibility": pre_probe,
        "first_unit_reachable_points": reachable,
        "unreachable_points": unreachable,
        "point_details": {
            point: {
                "first_unit_reachable": bool(first_counts[point]),
                "formal_scenario_count": first_counts[point],
                "exhaustive_plan_scenario_count": plan_counts[point],
            }
            for point in points
        },
        "formal_case_audit": formal_rows,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# v3.2 真实路由可达性门禁",
        "",
        "> 正式输入仅包含岗位画像、5题前测和最多2道冻结探针；金标准仅在路由完成后离线合并。",
        "",
        "## 结果摘要",
        "",
        f"- 预先喂入指定探针后的离线路由可达：{summary['first_unit_reachable_count']}/{summary['core_knowledge_points']}",
        f"- 生产路由可区分上限：{summary['real_route_reachable_upper_bound']}/{summary['core_knowledge_points']}",
        f"- 探针选择冲突：{summary['pre_probe_conflicting_group_count']}组，影响{summary['pre_probe_affected_case_count']}例",
        f"- 离线预喂模式中的不可达：{summary['unreachable_count']}",
        f"- 路由证据可追溯：{summary['traceable_selection_count']}/50",
        f"- 相同输入计划一致：{summary['deterministic_case_count']}/50",
        f"- 正式案例路由匹配：{summary['formal_route_matches']}/50",
        f"- 强制知识点/模板注入：{summary['forced_knowledge_point_count']}/{summary['forced_template_id_count']}",
        f"- 门禁状态：**{summary['status']}**",
        "",
        "## 知识点可达性",
        "",
        "| 知识点 | 可达 | 正式案例数 |",
        "|---|---:|---:|",
    ]
    for point, detail in report["point_details"].items():
        lines.append(
            f"| {point} | {'是' if detail['first_unit_reachable'] else '否'} | {detail['formal_scenario_count']} |"
        )
    mismatches = [
        row
        for row in report["formal_case_audit"]
        if not (
            row["knowledge_point_match"]
            and row["difficulty_match"]
            and row["template_match"]
        )
    ]
    lines.extend(["", "## 尚未通过的正式路由", ""])
    if not mismatches:
        lines.append("- 无")
    else:
        for row in mismatches:
            lines.append(
                f"- {row['case_id']}: 实际 {row['selected_knowledge_point']}/{row['selected_difficulty']}/{row['actual_template_id']}"
            )
    lines.append("")
    return "\n".join(lines)


def write_report(output_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    report = audit_route_reachability()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "route_reachability_v3_2.json"
    markdown_path = output_dir / "route_reachability_v3_2.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR / "route_reachability_v3_2",
    )
    args = parser.parse_args()
    json_path, markdown_path, report = write_report(args.output_dir)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"json={json_path}")
    print(f"markdown={markdown_path}")
    return 0 if report["summary"]["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
