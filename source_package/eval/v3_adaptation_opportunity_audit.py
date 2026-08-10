"""Audit the structural ceiling of the frozen v3 adaptation benchmark.

This is a diagnostic report, not a replacement KPI.  It makes explicit how
many required adaptation nodes the frozen gold standard itself requires to be
``keep``.  Those nodes remain in the official denominator and cannot enter the
numerator because no difficulty/path/task/follow-up change occurs.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _normalise_sequence(value: Any) -> str:
    return "".join(str(value or "").split())


def _expected_submission_actions(sequence: str, required_nodes: int) -> list[list[str]]:
    """Mirror the frozen semantic-node mapping used by v3_metrics."""

    normalized = _normalise_sequence(sequence)
    submission_count = max(0, required_nodes - 1)
    if normalized.count("wrong") >= 2 and "step_down" in normalized:
        actions = [["targeted_followup"], ["step_down"], ["keep"]]
    elif (
        "wrong→targeted_followup/rebuttal" in normalized
        or "wrong→targeted_followup" in normalized
    ):
        actions = [["targeted_followup", "rebuttal"], ["keep"]]
    else:
        final_action = next(
            (
                item
                for item in reversed(normalized.split("→"))
                if item in {"keep", "step_up", "step_down", "deferred"}
            ),
            "keep",
        )
        actions = [[final_action]]
    while len(actions) < submission_count:
        actions.append(["keep"])
    return actions[:submission_count]


def audit_adaptation_opportunities(
    gold_cases: Iterable[dict[str, Any]],
    *,
    seed_count: int = 2,
    target_rate: float = 0.85,
) -> dict[str, Any]:
    cases = list(gold_cases)
    if seed_count <= 0:
        raise ValueError("seed_count must be positive")
    if not 0 <= target_rate <= 1:
        raise ValueError("target_rate must be between 0 and 1")

    per_seed_required = 0
    per_seed_keep = 0
    sequence_parse_errors: list[dict[str, Any]] = []
    sequence_counts: Counter[str] = Counter()

    for case in cases:
        case_id = str(case.get("case_id") or "")
        sequence = _normalise_sequence(case.get("预期适配序列"))
        try:
            required = int(case.get("预期适配节点数"))
        except (TypeError, ValueError):
            sequence_parse_errors.append({"case_id": case_id, "reason": "invalid_required_node_count"})
            continue
        if required < 0 or not sequence:
            sequence_parse_errors.append({"case_id": case_id, "reason": "invalid_sequence"})
            continue

        expected_actions = _expected_submission_actions(sequence, required)
        keep_count = sum(1 for action_set in expected_actions if action_set == ["keep"])
        if keep_count > required:
            sequence_parse_errors.append({"case_id": case_id, "reason": "keep_count_exceeds_required_nodes"})
            continue

        per_seed_required += required
        per_seed_keep += keep_count
        sequence_counts[sequence] += 1

    total_required = per_seed_required * seed_count
    expected_keep = per_seed_keep * seed_count
    maximum_effective = total_required - expected_keep
    ceiling = (maximum_effective / total_required * 100) if total_required else 0.0
    nodes_required = math.ceil(total_required * target_rate)

    return {
        "mode": "FROZEN_GOLD_STRUCTURAL_AUDIT",
        "case_count": len(cases),
        "seed_count": seed_count,
        "per_seed_required_nodes": per_seed_required,
        "per_seed_expected_keep_nodes": per_seed_keep,
        "total_required_nodes": total_required,
        "expected_keep_nodes": expected_keep,
        "maximum_effective_nodes": maximum_effective,
        "structural_ceiling_percentage": round(ceiling, 4),
        "target_percentage": round(target_rate * 100, 4),
        "nodes_required_for_85_percent": nodes_required,
        "impossible_gap_to_85_percent": max(0, nodes_required - maximum_effective),
        "sequence_counts_per_seed": dict(sorted(sequence_counts.items())),
        "sequence_parse_errors": sequence_parse_errors,
        "interpretation": (
            "Official KPI remains effective_nodes/required_nodes. This diagnostic only shows the "
            "maximum numerator permitted by the frozen expected actions."
        ),
    }


def _markdown(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# v3.2 冻结金标准适配机会审计",
            "",
            "> 本报告是结构诊断，不替代有效自动适配率，也不改变其分子分母。",
            "",
            f"- 正式案例：{report['case_count']} 例 × {report['seed_count']} 个 Seed",
            f"- 应适配节点：{report['total_required_nodes']}",
            f"- 金标准明确要求 `keep`：{report['expected_keep_nodes']}",
            f"- 金标准允许的最大有效节点：{report['maximum_effective_nodes']}",
            f"- 结构性上限：{report['structural_ceiling_percentage']:.4f}%",
            f"- 达到 {report['target_percentage']:.2f}% 所需节点：{report['nodes_required_for_85_percent']}",
            f"- 与冻结金标准的不可兼容缺口：{report['impossible_gap_to_85_percent']} 节点",
            "",
            "结论：若不修改冻结案例或指标定义，就不能把金标准要求的 `keep` 节点计为有效适配。",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--seed-count", type=int, default=2)
    parser.add_argument("--target-rate", type=float, default=0.85)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    gold_cases = json.loads(args.gold.read_text(encoding="utf-8"))
    if not isinstance(gold_cases, list):
        raise ValueError("gold file must contain a JSON list")
    report = audit_adaptation_opportunities(
        gold_cases,
        seed_count=args.seed_count,
        target_rate=args.target_rate,
    )
    if report["sequence_parse_errors"]:
        raise RuntimeError(f"gold sequence audit failed: {report['sequence_parse_errors']}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "adaptation_opportunity_audit_v3_2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "adaptation_opportunity_audit_v3_2.md").write_text(
        _markdown(report) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
