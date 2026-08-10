"""Build and validate the isolated continuous-learning supplement set.

This dataset does not replace the frozen formal 50x2 benchmark.  It measures
whether longitudinal evidence triggers a real change when change is needed,
while keeping valid no-op decisions in a separate safety denominator.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


KNOWLEDGE_POINTS = (
    ("三道工序与传导关系", "process_flow_coordination", "M-02", "新入职生产计划员"),
    ("计划量与实际量口径", "plan_actual_reconciliation", "M-01", "一线班组长（晋升培训）"),
    ("完成率计算", "completion_rate_reporting", "M-04", "转岗数字化的工艺工程师"),
    ("偏差率与风险等级", "variance_risk_monitoring", "M-DEVIATION-SIGN", "新入职生产计划员"),
    ("月度聚合方法", "monthly_rollup_reporting", "M-04", "一线班组长（晋升培训）"),
    ("异常识别标准", "anomaly_threshold_review", "M-THRESHOLD", "转岗数字化的工艺工程师"),
    ("传导时滞分析", "cross_month_lag_review", "M-LAG", "新入职生产计划员"),
    ("异常衰减规律", "decay_pattern_review", "M-DECAY", "一线班组长（晋升培训）"),
    ("责任单元定位", "handled_responsibility_handoffs", "M-RESPONSIBILITY", "转岗数字化的工艺工程师"),
    ("跨工序归因方法", "cross_process_root_cause_review", "M-CAUSALITY", "新入职生产计划员"),
)


def _case(
    *,
    case_id: str,
    knowledge_point: str,
    tag_id: str,
    misconception_id: str,
    role: str,
    scenario: str,
    prior_state: dict[str, Any],
    learner_events: list[dict[str, str]],
    expected_nodes: list[dict[str, Any]],
    change_required: bool,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "dataset": "continuous_learning_supplement_v1",
        "route_mode": "production",
        "role_profile": role,
        "focus_tag_id": tag_id,
        "gold_knowledge_point": knowledge_point,
        "scenario_type": scenario,
        "misconception_id": misconception_id,
        "prior_learning_state": prior_state,
        "learner_events": learner_events,
        "change_required": change_required,
        "expected_adaptation_nodes": expected_nodes,
        "production_runtime_must_not_read": [
            "gold_knowledge_point",
            "expected_adaptation_nodes",
            "misconception_id",
        ],
        "safety_constraints": [
            "不得强制注入knowledge_point或template_id",
            "不得修改21条状态转移",
            "不得放宽R-01至R-06",
            "不得跳过证据绑定与Review",
            "A选择性辩护和B误区选靶语义保持不变",
        ],
    }


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    index = 1
    for knowledge_point, tag_id, misconception_id, role in KNOWLEDGE_POINTS:
        prefix = f"CLS-{index:03d}"
        cases.append(
            _case(
                case_id=f"{prefix}-REGRESSION",
                knowledge_point=knowledge_point,
                tag_id=tag_id,
                misconception_id=misconception_id,
                role=role,
                scenario="能力回退",
                prior_state={
                    "mastery": "applied_verified",
                    "successful_sessions": 2,
                    "days_since_last_practice": 30,
                },
                learner_events=[
                    {"turn": "1", "result": "wrong", "evidence": "同一误区再次出现"},
                    {"turn": "2", "result": "wrong", "evidence": "补充提示后仍错误"},
                ],
                expected_nodes=[
                    {"node": 1, "expected_actions": ["targeted_followup", "rebuttal"], "changed_dimension": "followup"},
                    {"node": 2, "expected_actions": ["step_down"], "changed_dimension": "difficulty"},
                ],
                change_required=True,
            )
        )
        cases.append(
            _case(
                case_id=f"{prefix}-RECOVERY",
                knowledge_point=knowledge_point,
                tag_id=tag_id,
                misconception_id=misconception_id,
                role=role,
                scenario="补学恢复",
                prior_state={
                    "mastery": "basic_remediation",
                    "successful_sessions": 0,
                    "historical_error_corrected": True,
                },
                learner_events=[
                    {"turn": "1", "result": "correct", "evidence": "字段和值完整绑定"},
                    {"turn": "2", "result": "correct", "evidence": "换一组证据仍可解释"},
                ],
                expected_nodes=[
                    {"node": 1, "expected_actions": ["targeted_followup"], "changed_dimension": "followup"},
                    {"node": 2, "expected_actions": ["step_up"], "changed_dimension": "difficulty"},
                ],
                change_required=True,
            )
        )
        cases.append(
            _case(
                case_id=f"{prefix}-TRANSFER",
                knowledge_point=knowledge_point,
                tag_id=tag_id,
                misconception_id=misconception_id,
                role=role,
                scenario="岗位迁移",
                prior_state={
                    "mastery": "basic_verified",
                    "role_scope_changed": True,
                    "new_focus_tag_id": tag_id,
                },
                learner_events=[
                    {"turn": "1", "result": "new_role_evidence", "evidence": "岗位职责新增"},
                    {"turn": "2", "result": "correct", "evidence": "完成应用级校准探针"},
                ],
                expected_nodes=[
                    {"node": 1, "expected_actions": ["path_update"], "changed_dimension": "path"},
                    {"node": 2, "expected_actions": ["task_complexity_up", "step_up"], "changed_dimension": "task_complexity"},
                ],
                change_required=True,
            )
        )
        cases.append(
            _case(
                case_id=f"{prefix}-STABLE",
                knowledge_point=knowledge_point,
                tag_id=tag_id,
                misconception_id=misconception_id,
                role=role,
                scenario="稳定保持",
                prior_state={
                    "mastery": "applied_verified",
                    "successful_sessions": 3,
                    "days_since_last_practice": 3,
                },
                learner_events=[
                    {"turn": "1", "result": "correct", "evidence": "字段、值、口径和理由均完整"},
                ],
                expected_nodes=[
                    {"node": 1, "expected_actions": ["keep"], "changed_dimension": "none"},
                ],
                change_required=False,
            )
        )
        index += 1
    return cases


def validate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    ids = [str(item.get("case_id") or "") for item in cases]
    if len(cases) != 40:
        errors.append(f"expected 40 cases, got {len(cases)}")
    if len(set(ids)) != len(ids):
        errors.append("duplicate case_id")

    by_kp: dict[str, set[str]] = {}
    change_nodes = 0
    keep_nodes = 0
    for item in cases:
        kp = str(item.get("gold_knowledge_point") or "")
        scenario = str(item.get("scenario_type") or "")
        by_kp.setdefault(kp, set()).add(scenario)
        if item.get("route_mode") != "production":
            errors.append(f"{item.get('case_id')}: route_mode must be production")
        expected = item.get("expected_adaptation_nodes") or []
        for node in expected:
            actions = set(node.get("expected_actions") or [])
            if actions == {"keep"}:
                keep_nodes += 1
            else:
                change_nodes += 1
    expected_scenarios = {"能力回退", "补学恢复", "岗位迁移", "稳定保持"}
    for kp, scenarios in by_kp.items():
        if scenarios != expected_scenarios:
            errors.append(f"{kp}: incomplete scenario matrix {sorted(scenarios)}")
    if set(by_kp) != {item[0] for item in KNOWLEDGE_POINTS}:
        errors.append("knowledge point coverage is not 10/10")
    if change_nodes != 60:
        errors.append(f"expected 60 change-required nodes, got {change_nodes}")
    if keep_nodes != 10:
        errors.append(f"expected 10 keep control nodes, got {keep_nodes}")

    return {
        "valid": not errors,
        "errors": errors,
        "case_count": len(cases),
        "knowledge_point_count": len(by_kp),
        "change_required_nodes": change_nodes,
        "keep_control_nodes": keep_nodes,
        "continuous_adaptation_target": 0.85,
        "stable_keep_target": 0.90,
    }


def _write_csv(path: Path, cases: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "role_profile",
        "focus_tag_id",
        "gold_knowledge_point",
        "scenario_type",
        "misconception_id",
        "change_required",
        "prior_learning_state",
        "learner_events",
        "expected_adaptation_nodes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in cases:
            row = {key: item.get(key) for key in fields}
            for key in {"prior_learning_state", "learner_events", "expected_adaptation_nodes"}:
                row[key] = json.dumps(row[key], ensure_ascii=False, separators=(",", ":"))
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    cases = build_cases()
    summary = validate_cases(cases)
    if not summary["valid"]:
        raise RuntimeError(summary["errors"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "continuous_learning_supplement_v1.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "continuous_learning_supplement_v1_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_csv(args.output_dir / "continuous_learning_supplement_v1.csv", cases)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
