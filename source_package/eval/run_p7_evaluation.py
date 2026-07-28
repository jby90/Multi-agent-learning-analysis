"""Compute and render the complete deterministic P7 evaluation bundle."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

from agents.diagnosis_agent import load_profiles
from agents.kb_loader import require_valid_chunks
from eval.blind_review_sample import (
    build_blind_key,
    build_blind_sample,
    parse_blind_form,
    render_blind_form,
)
from eval.case_matrix import EvaluationCase, load_case_matrix
from eval.cost_report import cost_report
from eval.metrics.adaptation import adaptation_report
from eval.metrics.coverage import coverage_report
from eval.metrics.hallucination import hallucination_report
from eval.metrics.summary import refusal_report
from eval.trace_dataset import canonical_json, load_dataset


ROOT = Path(__file__).resolve().parents[1]
PRICING_PATH = ROOT / "eval" / "pricing_qwen3_cn.json"
CHUNK_DIR = ROOT / "agents" / "knowledge_base" / "chunks"
DEFAULT_OUTPUT_DIR = ROOT / "eval" / "results"
DEFAULT_REPORT = ROOT / "docs" / "评测报告.md"
DEFAULT_BLIND_FORM = ROOT / "eval" / "blind_review_form.md"
METRIC_FILES = {
    "hallucination": "hallucination.json",
    "adaptation_auto": "adaptation_auto.json",
    "coverage": "coverage.json",
    "refusal": "refusal.json",
    "cost": "cost.json",
}


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    output_dir: Path
    report_path: Path
    blind_form_path: Path
    exit_code: int


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _case_value(case: EvaluationCase | Mapping[str, Any], field: str) -> Any:
    if isinstance(case, EvaluationCase):
        return getattr(case, field)
    return case.get(field)


def _fixed_decimal(value: Decimal) -> str:
    return f"{value:.6f}"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _dataset_bytes(dataset: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(canonical_json(row) + "\n" for row in dataset).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _reproduction_instructions(output_dir: Path) -> tuple[str, str]:
    resolved_output = output_dir.resolve()
    try:
        relative_output = resolved_output.relative_to(ROOT.resolve())
    except ValueError:
        execution_note = (
            "从本报告所在输出目录执行；执行环境必须能导入本项目"
            "（仓库根目录已加入 PYTHONPATH 或项目已安装）"
        )
        dataset_path = Path("dataset.jsonl")
        recomputed_dir = Path("recomputed")
    else:
        execution_note = "从仓库根目录执行"
        dataset_path = relative_output / "dataset.jsonl"
        recomputed_dir = relative_output / "recomputed"

    def quote(path: Path) -> str:
        return "'" + path.as_posix().replace("'", "''") + "'"

    command = (
        "python -m eval.run_p7_evaluation "
        f"--dataset {quote(dataset_path)} "
        f"--output-dir {quote(recomputed_dir)} "
        f"--report {quote(recomputed_dir / 'official_report.md')} "
        f"--blind-form {quote(recomputed_dir / 'blind_review_form.md')}"
    )
    return execution_note, command


def _core_points(
    matrix: Sequence[EvaluationCase | Mapping[str, Any]],
) -> tuple[str, ...]:
    points: list[str] = []
    for case in matrix:
        point = _case_value(case, "knowledge_point")
        if isinstance(point, str) and point and point not in points:
            points.append(point)
    if not points:
        raise ValueError("matrix contains no core knowledge points")
    return tuple(points)


def _compute_metrics(
    dataset: Sequence[Mapping[str, Any]],
    *,
    core_points: Sequence[str],
    chunk_ids: Sequence[str],
    pricing: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        "hallucination": hallucination_report(dataset),
        "adaptation_auto": adaptation_report(dataset),
        "coverage": coverage_report(dataset, core_points, chunk_ids),
        "refusal": refusal_report(dataset),
        "cost": cost_report(dataset, pricing),
    }


def _thresholds(metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    hallucination = metrics["hallucination"]
    adaptation = metrics["adaptation_auto"]
    coverage = metrics["coverage"]
    hallucination_rate = Decimal(str(hallucination["rate"]))
    adaptation_rate = Decimal(str(adaptation["rate"]))
    coverage_rate = Decimal(str(coverage["rate"]))
    hallucination_valid = not bool(hallucination.get("denominator_zero"))
    adaptation_valid = int(adaptation.get("total_products", 0)) > 0
    coverage_valid = int(coverage.get("total", 0)) > 0
    return {
        "hallucination": {
            "value": str(hallucination["rate"]),
            "standard": "<0.050000",
            "excellence": "<0.030000",
            "standard_pass": hallucination_valid
            and hallucination_rate < Decimal("0.05"),
            "excellence_pass": hallucination_valid
            and hallucination_rate < Decimal("0.03"),
        },
        "adaptation_auto": {
            "value": str(adaptation["rate"]),
            "standard": ">=0.850000",
            "excellence": ">=0.900000",
            "standard_pass": adaptation_valid
            and adaptation_rate >= Decimal("0.85"),
            "excellence_pass": adaptation_valid
            and adaptation_rate >= Decimal("0.90"),
        },
        "coverage": {
            "value": str(coverage["rate"]),
            "standard": ">=0.900000",
            "excellence": ">=0.950000",
            "standard_pass": coverage_valid and coverage_rate >= Decimal("0.90"),
            "excellence_pass": coverage_valid
            and coverage_rate >= Decimal("0.95"),
        },
    }


def _sample_dataset(
    dataset: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        row for row in dataset if _mapping(row.get("case")).get("manual_review") is True
    )


def _absolute_rate_gap(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    return _fixed_decimal(abs(Decimal(str(left["rate"])) - Decimal(str(right["rate"]))))


def _sample_comparison(
    full: Mapping[str, Mapping[str, Any]],
    sample: Mapping[str, Mapping[str, Any]],
    *,
    full_size: int,
    sample_size: int,
) -> dict[str, Any]:
    return {
        "full_size": full_size,
        "sample_size": sample_size,
        "hallucination_full_rate": full["hallucination"]["rate"],
        "hallucination_sample_rate": sample["hallucination"]["rate"],
        "hallucination_rate_absolute_gap": _absolute_rate_gap(
            full["hallucination"], sample["hallucination"]
        ),
        "adaptation_full_rate": full["adaptation_auto"]["rate"],
        "adaptation_sample_rate": sample["adaptation_auto"]["rate"],
        "adaptation_rate_absolute_gap": _absolute_rate_gap(
            full["adaptation_auto"], sample["adaptation_auto"]
        ),
        "coverage_full_rate": full["coverage"]["rate"],
        "coverage_sample_rate": sample["coverage"]["rate"],
        "coverage_rate_absolute_gap": _absolute_rate_gap(
            full["coverage"], sample["coverage"]
        ),
        "refusal_full_rate": full["refusal"]["rate"],
        "refusal_sample_rate": sample["refusal"]["rate"],
        "refusal_rate_absolute_gap": _absolute_rate_gap(
            full["refusal"], sample["refusal"]
        ),
    }


def _sample_audit(sample: Sequence[Mapping[str, Any]], *, full_size: int) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for row in sample:
        case = _mapping(row.get("case"))
        messages = row.get("messages")
        cases.append(
            {
                "case_id": case.get("case_id"),
                "profile_id": case.get("profile_id"),
                "knowledge_point": case.get("knowledge_point"),
                "learning_path": case.get("learning_path"),
                "trace_id": row.get("trace_id"),
                "trace_sha256": row.get("trace_sha256"),
                "terminal_state": row.get("terminal_state"),
                "message_count": row.get("message_count", len(messages) if isinstance(messages, list) else 0),
                "transition_sequence": row.get("transition_sequence", []),
            }
        )
    return {
        "audit_scope": "固定manual_review分层样本的机器结构审计；内容质量需查看对应完整trace人工复核",
        "sample_size": len(sample),
        "sample_ratio": _fixed_decimal(
            Decimal(len(sample)) / Decimal(full_size) if full_size else Decimal(0)
        ),
        "all_terminal_s10": all(item["terminal_state"] == "S10_DONE" for item in cases),
        "profiles": dict(sorted(Counter(str(item["profile_id"]) for item in cases).items())),
        "learning_paths": dict(
            sorted(Counter(str(item["learning_path"]) for item in cases).items())
        ),
        "cases": cases,
    }


def _manual_adaptation(
    form_text: str,
    *,
    material: Sequence[Mapping[str, Any]],
    material_sha256: str,
    key: Sequence[Mapping[str, str]],
    auto_report: Mapping[str, Any],
) -> dict[str, Any]:
    blind_ids = tuple(str(row["blind_id"]) for row in material)
    ratings = parse_blind_form(
        form_text, expected_ids=blind_ids, source_sha256=material_sha256
    )
    completed = {key_: value for key_, value in ratings.items() if value is not None}
    base: dict[str, Any] = {
        "total_ratings": len(ratings),
        "completed_ratings": len(completed),
        "ratings": ratings,
        "human_rate": None,
        "automatic_rate_same_sample": None,
        "absolute_gap": None,
        "status": "pending" if not completed else "incomplete",
    }
    if len(completed) != len(ratings):
        return base

    human_matched = sum(value == "适配" for value in completed.values())
    human_rate = Decimal(human_matched) / Decimal(len(ratings))
    selected_msg_ids = {row["msg_id"] for row in key}
    auto_items = [
        item
        for item in auto_report.get("items", [])
        if isinstance(item, Mapping) and item.get("product_msg_id") in selected_msg_ids
    ]
    if len(auto_items) != len(selected_msg_ids):
        raise ValueError("blind key does not map one automatic verdict per sampled product")
    auto_matched = sum(bool(item.get("matched")) for item in auto_items)
    auto_rate = Decimal(auto_matched) / Decimal(len(auto_items))
    gap = abs(human_rate - auto_rate)
    base.update(
        {
            "human_matched": human_matched,
            "human_rate": _fixed_decimal(human_rate),
            "automatic_matched_same_sample": auto_matched,
            "automatic_rate_same_sample": _fixed_decimal(auto_rate),
            "absolute_gap": _fixed_decimal(gap),
            "status": "divergent" if gap > Decimal("0.10") else "consistent",
            "human_standard_pass": human_rate >= Decimal("0.85"),
            "human_excellence_pass": human_rate >= Decimal("0.90"),
        }
    )
    return base


def _failed_items(
    thresholds: Mapping[str, Mapping[str, Any]], manual: Mapping[str, Any]
) -> list[str]:
    labels = {
        "hallucination": "幻觉率",
        "adaptation_auto": "自动适配率",
        "coverage": "覆盖率",
    }
    failed = [
        f"{labels[key]}未达标准线（实测{value['value']}，标准{value['standard']}）"
        for key, value in thresholds.items()
        if not value["standard_pass"]
    ]
    if manual.get("status") in {"consistent", "divergent"} and not manual.get(
        "human_standard_pass", False
    ):
        failed.append(
            f"人工适配率未达标准线（实测{manual['human_rate']}，标准>=0.850000）"
        )
    return failed


def _evidence_lines(metrics: Mapping[str, Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    hallucinations = metrics["hallucination"].get("events", [])
    if hallucinations:
        for event in hallucinations[:3]:
            lines.append(
                f"- 幻觉事件：`{event.get('case_id')}` / `{event.get('msg_id')}` / "
                f"`{event.get('event_type')}` / claim=`{event.get('claim_text')}`"
            )
    else:
        lines.append("- 幻觉事件：本批次为0，无事件可抽样；分母与全部claim仍见 `hallucination.json`。")
    count = 0
    for point in metrics["coverage"].get("covered_points", []):
        for evidence in point.get("evidence", []):
            lines.append(
                f"- 覆盖证据：`{point.get('knowledge_point')}` ← "
                f"`{evidence.get('case_id')}` / `{evidence.get('msg_id')}` / "
                f"coverage=`{evidence.get('coverage_value')}`"
            )
            count += 1
            if count == 3:
                return lines
    if count == 0:
        lines.append("- 覆盖证据：本批次无知识点命中。")
    return lines


def _render_report(
    *,
    metrics: Mapping[str, Mapping[str, Any]],
    thresholds: Mapping[str, Mapping[str, Any]],
    manual: Mapping[str, Any],
    comparison: Mapping[str, Any],
    audit: Mapping[str, Any],
    reproducibility: Mapping[str, Any],
    failed: Sequence[str],
    reproduction_execution_note: str,
    reproduction_command: str,
) -> str:
    status = manual["status"]
    if status == "pending":
        manual_text = "人工盲评待回填；当前仅报告自动适配率，不冒充最终人工口径。"
    elif status == "incomplete":
        manual_text = (
            f"人工盲评仅回填 {manual['completed_ratings']}/{manual['total_ratings']}，未形成有效人工率。"
        )
    elif status == "divergent":
        manual_text = (
            f"人工率 {manual['human_rate']}，同样本自动率 "
            f"{manual['automatic_rate_same_sample']}，绝对差 {manual['absolute_gap']}；"
            "超过10个百分点，必须停下报告分歧。"
        )
    else:
        manual_text = (
            f"人工率 {manual['human_rate']}，同样本自动率 "
            f"{manual['automatic_rate_same_sample']}，绝对差 {manual['absolute_gap']}。"
        )
    cost = metrics["cost"]
    failure_lines = (
        [f"- {item}" for item in failed]
        if failed
        else ["- 当前输入数据的自动三项指标均达到标准线。"]
    )
    evidence_lines = _evidence_lines(metrics)
    report = [
        "# P7评测报告",
        "",
        f"> 本报告仅汇总当前输入数据集（{comparison['full_size']}组）与本轮计算结果。",
        "",
        "## 结论与阈值",
        "",
        "| 指标 | 实测 | 标准线 | 内测超额线 | 标准 | 超额 |",
        "|---|---:|---:|---:|:---:|:---:|",
        f"| 幻觉率 | {thresholds['hallucination']['value']} | <0.050000 | <0.030000 | {'✅' if thresholds['hallucination']['standard_pass'] else '❌'} | {'✅' if thresholds['hallucination']['excellence_pass'] else '❌'} |",
        f"| 自动适配率 | {thresholds['adaptation_auto']['value']} | >=0.850000 | >=0.900000 | {'✅' if thresholds['adaptation_auto']['standard_pass'] else '❌'} | {'✅' if thresholds['adaptation_auto']['excellence_pass'] else '❌'} |",
        f"| 覆盖率 | {thresholds['coverage']['value']} | >=0.900000 | >=0.950000 | {'✅' if thresholds['coverage']['standard_pass'] else '❌'} | {'✅' if thresholds['coverage']['excellence_pass'] else '❌'} |",
        f"| 拒答率（交叉观察） | {metrics['refusal']['rate']} | — | — | — | — |",
        "",
        manual_text,
        "",
        "## 指标口径与证据链抽样",
        "",
        f"- 幻觉率：{metrics['hallucination']['numerator_events']}/{metrics['hallucination']['denominator_claims']}；模板降级产物 {metrics['hallucination']['template_fallback_products']} 份，未混入主指标。",
        f"- 自动适配率：{metrics['adaptation_auto']['matched']}/{metrics['adaptation_auto']['total_products']}。",
        f"- 覆盖率：{metrics['coverage']['covered']}/{metrics['coverage']['total']}；从未检索切片 {len(metrics['coverage']['never_retrieved_chunk_ids'])} 个。",
        *evidence_lines,
        "",
        "完整逐条证据见本轮输出目录中的 `hallucination.json`、`adaptation_auto.json` 和 `coverage.json`。",
        "",
        f"## 当前输入数据（{comparison['full_size']}组）与固定抽检",
        "",
        f"- 全量：{comparison['full_size']}组自包含trace。固定抽检：{comparison['sample_size']}组（占比 {audit['sample_ratio']}）。",
        f"- 抽检全部终态S10：{'是' if audit['all_terminal_s10'] else '否'}。",
        f"- 当前抽检画像分布：`{canonical_json(audit['profiles'])}`；路径分布：`{canonical_json(audit['learning_paths'])}`。",
        f"- 全量/抽检绝对差：幻觉 {comparison['hallucination_rate_absolute_gap']}，适配 {comparison['adaptation_rate_absolute_gap']}，覆盖 {comparison['coverage_rate_absolute_gap']}，拒答 {comparison['refusal_rate_absolute_gap']}。",
        "- 每组trace哈希、消息数与转移序列见本轮 `sample_audit.json`。",
        "",
        "## 本轮工件",
        "",
        "- 输入快照：`dataset.jsonl`。",
        "- 自动指标：`hallucination.json`、`adaptation_auto.json`、`coverage.json`、`refusal.json`、`cost.json`。",
        "- 阈值与人工口径：`thresholds.json`、`adaptation_manual.json`。",
        "- 抽检与盲评：`full_vs_sample.json`、`sample_audit.json`、`blind_review_sample.jsonl`、`blind_review_key.json`。",
        "- 可复现性：`reproducibility.json`。",
        "",
        "## 成本账",
        "",
        f"- 当前数据集输入/输出拆分：输入 {cost['prompt_tokens']} / 输出 {cost['completion_tokens']}。",
        f"- 计价：阿里云百炼中国内地公开原价，检索日 {cost['pricing_basis']['retrieved_at']}；缓存提示词仍按标准输入价保守计费，不计免费额度/活动优惠。",
        f"- 传统实训：¥{cost['traditional_training_comparison']['cost_per_learner_cny']}/学员，仅为当前计价配置中的人工粗估，不是trace实测。",
        "- 分模型、分画像、分用例明细见本轮 `cost.json`。",
        "",
        "## 教师盲评",
        "",
        f"- 状态：{manual_text}",
        "- 材料由当前输入数据集生成；公开材料不含来源ID、模型、难度裁决或审核结论，私钥映射只用于审计。",
        "- 教师在本轮盲评表回填适配结论与理由后，按下方命令复跑即可计算人工率。",
        "",
        "## 三层自证与复算命令",
        "",
        f"- 当前输入数据集 SHA-256：`{reproducibility['dataset_sha256']}`。",
        f"- 指标独立计算两遍逐位一致：{reproducibility['two_pass_identical']}；摘要 `{reproducibility['first_pass_sha256']}`。",
        "- 指标与源数据之间保留 case_id、trace_id、msg_id 及 claim/coverage 字段。",
        "- 三指标、拒答率、全量与当前固定抽检子集同时报告。",
        "",
        "### 复算命令",
        "",
        f"{reproduction_execution_note}：",
        "",
        "```powershell",
        reproduction_command,
        "```",
        "",
        "## 未达标项",
        "",
        *failure_lines,
    ]
    return "\n".join(report).rstrip() + "\n"


def run_evaluation(
    dataset: Sequence[Mapping[str, Any]],
    output_dir: Path,
    *,
    report_path: Path | None = None,
    blind_form_path: Path | None = None,
    matrix: Sequence[EvaluationCase | Mapping[str, Any]] | None = None,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
    pricing: Mapping[str, Any] | None = None,
    core_points: Sequence[str] | None = None,
    chunk_ids: Sequence[str] | None = None,
    blind_size: int = 30,
) -> EvaluationResult:
    output_dir = Path(output_dir)
    report_path = Path(report_path) if report_path else output_dir / "评测报告.md"
    blind_form_path = (
        Path(blind_form_path) if blind_form_path else output_dir / "blind_review_form.md"
    )
    matrix = tuple(matrix) if matrix is not None else load_case_matrix()
    profiles = profiles if profiles is not None else load_profiles()
    if pricing is None:
        pricing = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    if core_points is None:
        core_points = _core_points(matrix)
    if chunk_ids is None:
        chunk_ids = tuple(chunk.chunk_id for chunk in require_valid_chunks(CHUNK_DIR))

    first = _compute_metrics(
        dataset, core_points=core_points, chunk_ids=chunk_ids, pricing=pricing
    )
    second = _compute_metrics(
        dataset, core_points=core_points, chunk_ids=chunk_ids, pricing=pricing
    )
    first_digest = _sha256_bytes(_json_bytes(first))
    second_digest = _sha256_bytes(_json_bytes(second))
    if first_digest != second_digest or canonical_json(first) != canonical_json(second):
        raise RuntimeError("P7 metrics are not deterministic across two passes")

    sample_rows = _sample_dataset(dataset)
    sample_metrics = _compute_metrics(
        sample_rows, core_points=core_points, chunk_ids=chunk_ids, pricing=pricing
    )
    comparison = _sample_comparison(
        first,
        sample_metrics,
        full_size=len(dataset),
        sample_size=len(sample_rows),
    )
    audit = _sample_audit(sample_rows, full_size=len(dataset))
    thresholds = _thresholds(first)

    material = build_blind_sample(
        dataset, matrix, profiles, size=blind_size
    )
    key = build_blind_key(dataset, matrix, size=blind_size)
    material_bytes = _jsonl_bytes(material)
    material_digest = _sha256_bytes(material_bytes)
    expected_ids = tuple(str(row["blind_id"]) for row in material)
    if blind_form_path.exists():
        form_text = blind_form_path.read_text(encoding="utf-8")
        # Parse immediately so an old or edited source digest can never be silently reused.
        parse_blind_form(
            form_text, expected_ids=expected_ids, source_sha256=material_digest
        )
    else:
        form_text = render_blind_form(material, source_sha256=material_digest)
        _atomic_write(blind_form_path, form_text.encode("utf-8"))
    manual = _manual_adaptation(
        form_text,
        material=material,
        material_sha256=material_digest,
        key=key,
        auto_report=first["adaptation_auto"],
    )
    failed = _failed_items(thresholds, manual)

    output_dir.mkdir(parents=True, exist_ok=True)
    legacy_bug_audit = output_dir / "bug_fix_audit.json"
    if legacy_bug_audit.is_file():
        legacy_bug_audit.unlink()
    artifacts: dict[str, bytes] = {
        **{METRIC_FILES[name]: _json_bytes(value) for name, value in first.items()},
        "dataset.jsonl": _dataset_bytes(dataset),
        "thresholds.json": _json_bytes(thresholds),
        "adaptation_manual.json": _json_bytes(manual),
        "full_vs_sample.json": _json_bytes(comparison),
        "sample_audit.json": _json_bytes(audit),
        "blind_review_sample.jsonl": material_bytes,
        "blind_review_key.json": _json_bytes(key),
    }
    for name, content in artifacts.items():
        _atomic_write(output_dir / name, content)
    reproducibility = {
        "dataset_sha256": _sha256_bytes(_dataset_bytes(dataset)),
        "matrix_sha256": _sha256_bytes(
            _json_bytes(
                [
                    case.as_dict() if isinstance(case, EvaluationCase) else dict(case)
                    for case in matrix
                ]
            )
        ),
        "base_commits": sorted(
            {
                str(row.get("base_commit"))
                for row in dataset
                if isinstance(row.get("base_commit"), str)
            }
        ),
        "two_pass_identical": True,
        "first_pass_sha256": first_digest,
        "second_pass_sha256": second_digest,
        "artifact_sha256": {
            name: _sha256_bytes(content) for name, content in sorted(artifacts.items())
        },
    }
    _atomic_write(output_dir / "reproducibility.json", _json_bytes(reproducibility))
    reproduction_execution_note, reproduction_command = (
        _reproduction_instructions(output_dir)
    )
    report = _render_report(
        metrics=first,
        thresholds=thresholds,
        manual=manual,
        comparison=comparison,
        audit=audit,
        reproducibility=reproducibility,
        failed=failed,
        reproduction_execution_note=reproduction_execution_note,
        reproduction_command=reproduction_command,
    )
    _atomic_write(report_path, report.encode("utf-8"))

    if manual["status"] == "divergent":
        exit_code = 2
    elif failed:
        exit_code = 1
    else:
        exit_code = 0
    return EvaluationResult(
        output_dir=output_dir,
        report_path=report_path,
        blind_form_path=blind_form_path,
        exit_code=exit_code,
    )


def _metric_output(
    name: str,
    dataset: Sequence[Mapping[str, Any]],
    matrix: Sequence[EvaluationCase],
    pricing: Mapping[str, Any],
) -> Mapping[str, Any]:
    points = _core_points(matrix)
    chunks = require_valid_chunks(CHUNK_DIR)
    if name == "hallucination":
        return hallucination_report(dataset)
    if name == "adaptation":
        return adaptation_report(dataset)
    if name == "coverage":
        return coverage_report(dataset, points, tuple(chunk.chunk_id for chunk in chunks))
    if name == "refusal":
        return refusal_report(dataset)
    if name == "cost":
        return cost_report(dataset, pricing)
    raise ValueError(f"unsupported metric: {name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--blind-form", type=Path, default=DEFAULT_BLIND_FORM)
    parser.add_argument(
        "--metric",
        choices=("hallucination", "adaptation", "coverage", "refusal", "cost"),
    )
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    matrix = load_case_matrix()
    pricing = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    if args.metric:
        print(canonical_json(_metric_output(args.metric, dataset, matrix, pricing)))
        return 0
    result = run_evaluation(
        dataset,
        args.output_dir,
        report_path=args.report,
        blind_form_path=args.blind_form,
        matrix=matrix,
        profiles=load_profiles(),
        pricing=pricing,
    )
    print(
        canonical_json(
            {
                "output_dir": args.output_dir.as_posix(),
                "report": args.report.as_posix(),
                "blind_form": args.blind_form.as_posix(),
                "exit_code": result.exit_code,
            }
        )
    )
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
