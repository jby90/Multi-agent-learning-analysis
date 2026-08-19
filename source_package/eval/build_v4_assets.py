"""Build the reviewed v4 production-route case assets.

The source workbook is a useful design draft, but its expected first route
does not model the persona router's prerequisite pull-up.  This builder keeps
the approved 10 knowledge points and five learner behaviours while producing
runtime inputs that contain no knowledge-point or template injection.  Gold
expectations are stored in a separate directory and may be used only by the
external learner/evaluator.

Run this script intentionally when freezing a new v4 asset revision.  The
formal runner reads the generated JSON files; it never regenerates them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from agents.kb_loader import require_valid_chunks
from agents.knowledge_scope import CHUNK_DIRECTORY
from agents.persona_router import (
    PersonaDiagnosticRouter,
    apply_calibration,
    calibration_probe_for,
    load_dependencies,
    resolve_experience_tag_point,
)


ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "eval" / "cases" / "v4" / "formal_50_inputs_v4.json"
GOLD_PATH = ROOT / "eval" / "gold" / "v4" / "formal_50_gold_v4.json"
TEMPLATE_PATH = (
    ROOT / "config" / "domains" / "production_progress" / "task_templates.json"
)
MANIFEST_PATH = (
    ROOT / "config" / "domains" / "production_progress" / "task_manifest.json"
)
TAG_PATH = ROOT / "config" / "diagnostic_experience_tags_v3.json"
PROBE_PATH = ROOT / "config" / "diagnostic_probes_v3.json"

LEVELS = ("basic", "applied", "advanced")
SCENARIOS = (
    ("基础覆盖", "S-STEP-UP-B", "basic"),
    ("应用覆盖", "S-STEP-UP-A", "applied"),
    ("基础重学", "S-REFRESH", "basic"),
    ("反证纠偏", "S-REBUTTAL", "applied"),
    ("连错降阶", "S-DOWNSTEP", "applied"),
)

# Every assigned profile owns the target point in its declared knowledge_scope.
# The distribution stays close to the draft workbook while retaining all three
# competition personas: planner=19, craft=19, line leader=12.
POINT_PROFILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("KP-01", "三道工序与传导关系", ("planner_new",) * 5),
    (
        "KP-02",
        "计划量与实际量口径",
        ("line_leader", "craft_engineer", "planner_new", "craft_engineer", "line_leader"),
    ),
    (
        "KP-03",
        "完成率计算",
        ("craft_engineer", "line_leader", "craft_engineer", "line_leader", "craft_engineer"),
    ),
    (
        "KP-04",
        "偏差率与风险等级",
        ("line_leader", "craft_engineer", "line_leader", "craft_engineer", "line_leader"),
    ),
    ("KP-05", "月度聚合方法", ("craft_engineer",) * 5),
    (
        "KP-06",
        "异常识别标准",
        ("line_leader", "craft_engineer", "line_leader", "craft_engineer", "line_leader"),
    ),
    ("KP-07", "传导时滞分析", ("planner_new",) * 5),
    ("KP-08", "异常衰减规律", ("planner_new",) * 5),
    (
        "KP-09",
        "责任单元定位",
        ("line_leader", "craft_engineer", "planner_new", "line_leader", "craft_engineer"),
    ),
    (
        "KP-10",
        "跨工序归因方法",
        ("craft_engineer", "planner_new", "craft_engineer", "planner_new", "craft_engineer"),
    ),
)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _chunk_records() -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": str(chunk.chunk_id),
            "knowledge_point": str(chunk.knowledge_point),
            "prerequisites": [str(value) for value in chunk.prerequisites],
        }
        for chunk in require_valid_chunks(CHUNK_DIRECTORY)
    ]


def _tags_by_point() -> dict[str, str]:
    return {
        str(item["knowledge_point"]): str(item["tag_id"])
        for item in _read(TAG_PATH)["tags"]
    }


def _probes_by_id() -> dict[str, dict[str, Any]]:
    return {str(item["probe_id"]): dict(item) for item in _read(PROBE_PATH)}


def _contracts() -> dict[str, dict[str, Any]]:
    return {
        str(item["template_id"]): dict(item)
        for item in _read(TEMPLATE_PATH)["templates"]
    }


def _render_task(contract: Mapping[str, Any], parameters: Mapping[str, Any]) -> str:
    return str(contract.get("question_template") or "").format(**parameters)


def _step_up(level: str) -> str:
    return LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)]


def _pretest_answers(
    router: PersonaDiagnosticRouter,
    profile_id: str,
    *,
    target_point: str,
    initial_difficulty: str,
) -> dict[str, str]:
    answers: dict[str, str] = {}
    found_target = False
    for question in router.pretest_for(profile_id).questions:
        point = str(question["knowledge_point"])
        answer = str(question["answer"])
        if point == target_point:
            found_target = True
            if initial_difficulty == "basic":
                answer = next(
                    str(option)
                    for option in question["options"]
                    if str(option) != str(question["answer"])
                )
        answers[str(question["question_id"])] = answer
    if not found_target:
        raise ValueError(f"{profile_id} pretest does not cover {target_point}")
    return answers


def build() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    router = PersonaDiagnosticRouter()
    dependencies = load_dependencies()
    chunks = _chunk_records()
    tags = _tags_by_point()
    probes = _probes_by_id()
    manifest = _read(MANIFEST_PATH)
    template_map = manifest["diagnostic_template_ids"]
    task_asset = _read(TEMPLATE_PATH)
    parameters = task_asset["demo_parameters"]
    contracts = _contracts()

    inputs: list[dict[str, Any]] = []
    gold: list[dict[str, Any]] = []
    index = 0
    for point_id, target_point, profiles in POINT_PROFILES:
        for scenario_index, (role, script_id, initial) in enumerate(SCENARIOS):
            index += 1
            case_id = f"E2E-{index:03d}"
            profile_id = profiles[scenario_index]
            answers = _pretest_answers(
                router,
                profile_id,
                target_point=target_point,
                initial_difficulty=initial,
            )
            experience_tags: list[str] = []
            probe_answers: list[dict[str, str]] = []
            focus_point: str | None = None
            if initial == "applied":
                tag_id = tags[target_point]
                experience_tags = [tag_id]
                focus_point = resolve_experience_tag_point(tag_id)
                probe = calibration_probe_for(target_point)
                if probe is None:
                    raise ValueError(f"missing applied calibration probe for {target_point}")
                probe_id = str(probe["probe_id"])
                probe_answers = [
                    {
                        "probe_id": probe_id,
                        "answer": str(probes[probe_id]["gold_answer"]),
                    }
                ]

            route = router.build_plan(
                profile_id,
                answers,
                dependencies=dependencies,
                experience_tag_point=focus_point,
                chunk_records=chunks,
            )
            if probe_answers:
                apply_calibration(
                    route,
                    target_point,
                    probe_answers[0]["probe_id"],
                    True,
                )
            plan = list(route["knowledge_point_plan"])
            matching = [
                (position, item)
                for position, item in enumerate(plan, start=1)
                if item["knowledge_point"] == target_point
                and item.get("tier") != "prerequisite_lift"
            ]
            if len(matching) != 1:
                raise ValueError(
                    f"{case_id} target route is not uniquely reachable: {target_point}"
                )
            target_position, target_item = matching[0]
            if target_item["initial_difficulty"] != initial:
                raise ValueError(
                    f"{case_id} target difficulty mismatch: "
                    f"{target_item['initial_difficulty']} != {initial}"
                )

            final = _step_up(initial)
            initial_template = str(template_map[target_point][initial])
            final_template = str(template_map[target_point][final])
            initial_contract = contracts[initial_template]
            final_contract = contracts[final_template]

            inputs.append(
                {
                    "case_id": case_id,
                    "route_mode": "production",
                    "profile_id": profile_id,
                    "experience_tags": experience_tags,
                    "pretest_answers": answers,
                    "diagnostic_probe_answers": probe_answers,
                    "learner_script_id": script_id,
                }
            )
            gold.append(
                {
                    "case_id": case_id,
                    "knowledge_point_id": point_id,
                    "target_knowledge_point": target_point,
                    "evaluation_role": role,
                    "learner_script_id": script_id,
                    "expected_route_points": [
                        str(item["knowledge_point"]) for item in plan
                    ],
                    "expected_target_plan_position": target_position,
                    "expected_initial_difficulty": initial,
                    "expected_final_difficulty": final,
                    "expected_initial_template": initial_template,
                    "expected_final_template": final_template,
                    "initial_business_task": _render_task(initial_contract, parameters),
                    "initial_standard_sql": str(initial_contract["standard_sql"]),
                    "initial_expected_rows": initial_contract.get("expected_rows", []),
                    "initial_expected_points": list(
                        initial_contract.get("expected_points", [])
                    ),
                    "final_business_task": _render_task(final_contract, parameters),
                    "final_standard_sql": str(final_contract["standard_sql"]),
                    "final_expected_rows": final_contract.get("expected_rows", []),
                    "final_expected_points": list(
                        final_contract.get("expected_points", [])
                    ),
                    "source_design": "正式评测50题_v4.xlsx（按当前生产路由兼容修订）",
                }
            )
    if len(inputs) != 50 or len(gold) != 50:
        raise AssertionError("v4 asset builder must emit exactly 50 cases")
    return inputs, gold


def main() -> int:
    inputs, gold = build()
    _write(INPUT_PATH, inputs)
    _write(GOLD_PATH, gold)
    print(f"wrote {len(inputs)} cases to {INPUT_PATH}")
    print(f"wrote {len(gold)} gold rows to {GOLD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
