"""Offline v3 TRACE splitter and fail-closed two-mode recomputation CLI."""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from eval.v3_cases import load_gold_standard
from eval.v3_metrics import (
    FinalReviewIncomplete,
    build_adaptation_nodes,
    build_coverage_cells,
    build_fact_units,
    compute_v3_metrics,
)


ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def load_run_directory(path: Path, *, require_complete: bool = True) -> list[dict[str, Any]]:
    path = Path(path)
    manifest = _read_json(path / "run_manifest.json")
    if manifest.get("route_mode") != "production":
        raise ValueError(f"formal run is not production-routed: {path}")
    if require_complete and (
        manifest.get("case_count") != 50 or manifest.get("completed_count") != 50
    ):
        raise ValueError(f"formal run must contain 50 completed cases: {path}")
    rows: list[dict[str, Any]] = []
    for item in manifest.get("cases", []):
        case_id = str(item.get("case_id") or "")
        result = _read_json(path / "cases" / case_id / "case_result.json")
        if result.get("route_mode") != "production":
            raise ValueError(f"{case_id} is not production-routed")
        rows.append(dict(result))
    if require_complete and len(rows) != 50:
        raise ValueError(f"formal run does not expose 50 case results: {path}")
    return rows


def _human_template(
    facts: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    cells: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "instructions": {
            "fact_units": "SUPPORTED/HALLUCINATION；两人不一致时填写 adjudicator",
            "adaptation_transactions": "0/1；两人不一致时填写 adjudicator_mismatch",
            "coverage_cells": "0/1；两人不一致时填写 adjudicator_pass",
            "do_not_edit_ids": True,
        },
        "fact_units": [
            {
                "content_unit_id": row["content_unit_id"],
                "case_id": row["case_id"],
                "content_text": row["content_text"],
                "reviewer_a": None,
                "reviewer_b": None,
                "adjudicator": None,
                "reason": "",
            }
            for row in facts
        ],
        "adaptation_transactions": [
            {
                "first_gen_transaction_id": tx_id,
                "case_id": next(
                    row["case_id"] for row in nodes if row["first_gen_transaction_id"] == tx_id
                ),
                "reviewer_a_mismatch": None,
                "reviewer_b_mismatch": None,
                "adjudicator_mismatch": None,
            }
            for tx_id in sorted(
                {row["first_gen_transaction_id"] for row in nodes if row["first_gen_transaction_id"]}
            )
        ],
        "coverage_cells": [
            {
                "case_id": row["case_id"],
                "knowledge_point": row["knowledge_point"],
                "difficulty": row["difficulty"],
                "reviewer_a_pass": None,
                "reviewer_b_pass": None,
                "adjudicator_pass": None,
            }
            for row in cells
        ],
    }


def recompute(
    run_dirs: list[Path],
    output_dir: Path,
    *,
    mode: str,
    human_review_path: Path | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for path in run_dirs:
        runs.extend(load_run_directory(path, require_complete=require_complete))
    seeds = {str(run.get("seed_id") or "") for run in runs}
    if require_complete and seeds != {"seed_A", "seed_B"}:
        raise ValueError("formal recomputation requires isolated seed_A and seed_B runs")
    gold = list(load_gold_standard().values())
    facts = build_fact_units(runs, mode="AUTO_PRELIMINARY")
    nodes = build_adaptation_nodes(runs, gold, mode="AUTO_PRELIMINARY")
    cells = build_coverage_cells(runs, gold, mode="AUTO_PRELIMINARY")
    automatic_preliminary = compute_v3_metrics(
        deepcopy(facts),
        deepcopy(nodes),
        deepcopy(cells),
        mode="AUTO_PRELIMINARY",
    )
    human_review = _read_json(human_review_path) if human_review_path else None
    combined = compute_v3_metrics(
        facts,
        nodes,
        cells,
        mode=mode,
        human_review=human_review,
    )
    seed_reports: dict[str, Any] = {}
    for seed in sorted(seeds):
        seed_facts = [row for row in facts if row["seed_id"] == seed]
        seed_nodes = [row for row in nodes if row["seed_id"] == seed]
        seed_cells = [
            row
            for row in cells
            if any(
                run.get("seed_id") == seed and run.get("case_id") == row["case_id"]
                for run in runs
            )
        ]
        # Final labels were applied to the shared rows above, so no second
        # human merge is needed for a per-seed report.
        seed_reports[seed] = compute_v3_metrics(
            seed_facts,
            seed_nodes,
            seed_cells,
            mode="AUTO_PRELIMINARY",
        )
        seed_reports[seed]["mode"] = mode
        seed_reports[seed]["label_status"] = combined["label_status"]
    report = {
        "mode": mode,
        "run_directories": [str(Path(path).resolve()) for path in run_dirs],
        "seed_case_counts": dict(
            sorted(
                (seed, sum(run.get("seed_id") == seed for run in runs))
                for seed in seeds
            )
        ),
        "combined": combined,
        "automatic_preliminary": automatic_preliminary,
        "by_seed": seed_reports,
        "row_counts": {
            "fact_units": len(facts),
            "adaptation_nodes": len(nodes),
            "coverage_cells": len(cells),
        },
    }
    output_dir = Path(output_dir)
    _write_json(output_dir / "facts_v3.json", facts)
    _write_json(output_dir / "adaptation_nodes_v3.json", nodes)
    _write_json(output_dir / "coverage_30_cells_v3.json", cells)
    _write_json(output_dir / f"metrics_{mode.lower()}_v3.json", report)
    if mode == "AUTO_PRELIMINARY":
        _write_json(output_dir / "human_review_template_v3.json", _human_template(facts, nodes, cells))
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    labels = {
        "final_hallucination_rate": "最终发布幻觉率",
        "difficulty_adaptation_accuracy": "画像—资源难度适配准确率",
        "strict_closed_loop_coverage": "主域严格闭环覆盖率",
        "hallucination_interception_rate": "幻觉拦截率（辅助）",
        "native_teaching_adaptation_mismatch_rate": "原生教学适配失配率（辅助，低为好）",
    }
    lines = [
        f"# v3 五指标复算｜{report['mode']}",
        "",
        "> 自动初算不是最终成绩；最终模式仅在双人复核、必要仲裁和覆盖门禁齐全后生成。",
        "",
    ]
    for scope, result in [("两轮合并", report["combined"]), *report["by_seed"].items()]:
        lines.extend([f"## {scope}", "", "| 指标 | 分子 | 分母 | 百分比 |", "|---|---:|---:|---:|"])
        for key, value in result["metrics"].items():
            lines.append(
                f"| {labels[key]} | {value['numerator']} | {value['denominator']} | {value['percentage']:.4f}% |"
            )
        lines.append("")
    return "\n".join(lines)


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
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    try:
        report = recompute(
            args.run_dir,
            args.output_dir,
            mode=args.mode,
            human_review_path=args.human_review,
            require_complete=not args.allow_incomplete,
        )
    except FinalReviewIncomplete as exc:
        parser.error(str(exc))
    markdown_path = args.output_dir / f"metrics_{args.mode.lower()}_v3.md"
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
