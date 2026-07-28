from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from eval.run_p7_evaluation import run_evaluation


PROFILES = {
    "planner_new": {
        "profile_id": "planner_new",
        "title": "新入职生产计划员",
        "background": "会SQL，不懂工序。",
        "strengths": ["SQL"],
        "gaps_prior": ["口径"],
        "lecture_style": "重讲口径",
    },
    "craft_engineer": {
        "profile_id": "craft_engineer",
        "title": "工艺工程师",
        "background": "懂工艺，不懂数据。",
        "strengths": ["工艺"],
        "gaps_prior": ["数据"],
        "lecture_style": "重讲工具",
    },
    "line_leader": {
        "profile_id": "line_leader",
        "title": "班组长",
        "background": "现场熟，理论弱。",
        "strengths": ["现场"],
        "gaps_prior": ["计算"],
        "lecture_style": "步骤短句",
    },
}


def _pricing() -> dict:
    return {
        "currency": "CNY",
        "unit": "per_million_tokens",
        "region": "中国内地",
        "retrieved_at": "2026-07-17",
        "source_url": "https://help.aliyun.com/zh/model-studio/model-pricing",
        "cache_policy": "standard_input_price_without_cache_discount",
        "prices": {
            "qwen3-32b": {
                "input_cny_per_million": "2",
                "output_cny_per_million": "8",
            },
            "qwen3-235b-a22b": {
                "input_cny_per_million": "2",
                "output_cny_per_million": "8",
            },
        },
        "traditional_training_estimate": {
            "development_hours": "8",
            "hourly_cny": "200",
            "learner_count": 50,
            "note": "人工粗估。",
        },
    }


def _fixture(case_count: int = 3) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    dataset = []
    matrix = []
    profile_ids = tuple(PROFILES)
    for serial in range(1, case_count + 1):
        profile_id = profile_ids[(serial - 1) % len(profile_ids)]
        case_id = f"E2E-{serial:03d}"
        trace_id = f"trace-{serial:03d}"
        case = {
            "case_id": case_id,
            "profile_id": profile_id,
            "knowledge_point": "计划量与实际量口径",
            "learning_path": "direct_correct",
            "manual_review": True,
        }
        matrix.append(case)
        product_id = f"{trace_id}-001"
        dataset.append(
            {
                "case": dict(case),
                "attempt": 1,
                "trace_id": trace_id,
                "trace_path": f"traces/{trace_id}.jsonl",
                "trace_sha256": str(serial) * 64,
                "base_commit": "current-fixture",
                "terminal_state": "S10_DONE",
                "transition_sequence": ["T01", "T02", "T20"],
                "message_count": 2,
                "messages": [
                    {
                        "msg_id": product_id,
                        "trace_id": trace_id,
                        "step": 1,
                        "agent": "knowledge",
                        "role": "produce",
                        "payload": {
                            "type": "lecture_note",
                            "content": {
                                "event": "product_ready",
                                "lecture_md": "计划量与实际量口径讲义。",
                                "coverage": ["计划量与实际量口径"],
                                "retrieved_chunk_ids": ["KB-002"],
                            },
                        },
                        "claims": [{"text": "计划量不等于实际量。", "kind": "fact"}],
                        "evidence": [
                            {
                                "kind": "kb_chunk",
                                "ref": "KB-002",
                                "quote": "计划量与实际量口径不同。",
                                "supports_claim": "计划量不等于实际量。",
                            }
                        ],
                        "model": "qwen3-235b-a22b",
                        "token_usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                        },
                    },
                    {
                        "msg_id": f"{trace_id}-002",
                        "trace_id": trace_id,
                        "step": 2,
                        "agent": "review",
                        "role": "verdict",
                        "payload": {
                            "type": "review_verdict",
                            "content": {"reviewed_msg_id": product_id},
                        },
                        "verdict": {
                            "decision": "approve",
                            "difficulty_action": "keep",
                            "rule_hits": [],
                        },
                        "model": "qwen3-32b",
                        "token_usage": {
                            "prompt_tokens": 50,
                            "completion_tokens": 10,
                            "total_tokens": 60,
                        },
                    },
                ],
            }
        )
    return tuple(dataset), tuple(matrix)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run(dataset: tuple[dict, ...], matrix: tuple[dict, ...], output: Path):
    return run_evaluation(
        dataset,
        output,
        matrix=matrix,
        profiles=PROFILES,
        pricing=_pricing(),
        core_points=("计划量与实际量口径", "完成率计算"),
        chunk_ids=("KB-001", "KB-002"),
        blind_size=3,
    )


def test_run_outputs_stable_json_commands_and_threshold_status(tmp_path: Path) -> None:
    dataset, matrix = _fixture()
    first = _run(dataset, matrix, tmp_path / "one")
    second = _run(dataset, matrix, tmp_path / "two")

    assert first.exit_code == 1  # fixture deliberately misses one core point
    assert _tree_hash(first.output_dir) == _tree_hash(second.output_dir)
    text = first.report_path.read_text(encoding="utf-8")
    assert "# P7" in text
    assert "hallucination.json" in text
    thresholds = json.loads((first.output_dir / "thresholds.json").read_text("utf-8"))
    assert thresholds["coverage"]["standard_pass"] is False
    assert thresholds["hallucination"]["standard_pass"] is True
    assert thresholds["adaptation_auto"]["excellence_pass"] is True
    reproducibility = json.loads(
        (first.output_dir / "reproducibility.json").read_text("utf-8")
    )
    assert reproducibility["two_pass_identical"] is True
    assert reproducibility["dataset_sha256"] in text



def test_ten_case_comparison_artifact_links_selected_source_rows(tmp_path: Path) -> None:
    dataset, matrix = _fixture()
    result = _run(dataset, matrix, tmp_path / "result")

    audit = json.loads((result.output_dir / "sample_audit.json").read_text("utf-8"))
    assert audit["sample_size"] == 3
    assert audit["all_terminal_s10"] is True
    assert [item["case_id"] for item in audit["cases"]] == [
        "E2E-001",
        "E2E-002",
        "E2E-003",
    ]
    comparison = json.loads(
        (result.output_dir / "full_vs_sample.json").read_text("utf-8")
    )
    assert comparison["sample_size"] == 3
    assert comparison["hallucination_rate_absolute_gap"] == "0.000000"


def test_report_uses_only_current_dataset_and_computed_artifacts(tmp_path: Path) -> None:
    dataset, matrix = _fixture()
    result = _run(dataset, matrix, tmp_path / "result")

    text = result.report_path.read_text(encoding="utf-8")
    for historical_text in (
        "historical-evaluation-batch",
        "7cad513",
        "historical-business-baseline",
        "40组成功trace",
        "旧冻结50组",
        "本报告评测获批四项修复后的冻结版系统",
    ):
        assert historical_text not in text
    assert not (result.output_dir / "bug_fix_audit.json").exists()


def test_report_renders_current_artifacts_commands_and_neutral_failures(
    tmp_path: Path,
) -> None:
    dataset, matrix = _fixture()
    result = _run(dataset, matrix, tmp_path / "result")

    text = result.report_path.read_text(encoding="utf-8")
    assert "覆盖率未达标准线" in text
    assert "本轮四项授权" not in text
    assert "仍不修改冻结系统" not in text
    assert "批准样本ID吻合" not in text
    for artifact_name in (
        "dataset.jsonl",
        "hallucination.json",
        "adaptation_auto.json",
        "coverage.json",
        "sample_audit.json",
        "cost.json",
        "reproducibility.json",
    ):
        assert f"`{artifact_name}`" in text
    assert "```powershell\npython -m eval.run_p7_evaluation" in text
    assert "```powershell\n```" not in text
    assert "D:\\ref_multiagent_env" not in text

    dataset_snapshot = result.output_dir / "dataset.jsonl"
    reproducibility = json.loads(
        (result.output_dir / "reproducibility.json").read_text("utf-8")
    )
    assert hashlib.sha256(dataset_snapshot.read_bytes()).hexdigest() == (
        reproducibility["dataset_sha256"]
    )


def test_fifty_case_report_uses_current_sample_facts_only(tmp_path: Path) -> None:
    dataset, matrix = _fixture(case_count=50)
    result = _run(dataset, matrix, tmp_path / "result")

    text = result.report_path.read_text(encoding="utf-8")
    audit = json.loads((result.output_dir / "sample_audit.json").read_text("utf-8"))
    assert audit["sample_size"] == 50
    assert "approved_selection_match" not in audit
    assert "批准样本ID吻合" not in text
    assert "全量：50组" in text


def test_reused_output_directory_removes_legacy_bug_audit(tmp_path: Path) -> None:
    dataset, matrix = _fixture()
    output = tmp_path / "result"
    output.mkdir()
    legacy_audit = output / "bug_fix_audit.json"
    legacy_audit.write_text('{"stale": true}\n', encoding="utf-8")

    _run(dataset, matrix, output)

    assert not legacy_audit.exists()


def test_external_report_reproduction_command_runs_with_documented_import_setup(
    tmp_path: Path,
) -> None:
    dataset, matrix = _fixture(case_count=50)
    output = tmp_path / "external-result"
    result = _run(dataset, matrix, output)
    text = result.report_path.read_text(encoding="utf-8")
    command = text.split("```powershell\n", 1)[1].split("\n```", 1)[0].strip()

    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (str(repository_root), environment.get("PYTHONPATH")),
        )
    )
    environment["PATH"] = os.pathsep.join(
        (str(Path(sys.executable).parent), environment["PATH"])
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=output,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr or completed.stdout
    assert (output / "recomputed" / "official_report.md").is_file()
    assert "执行环境必须能导入本项目" in text
    assert "仓库根目录已加入 PYTHONPATH 或项目已安装" in text


def test_manual_gap_over_ten_points_stops_with_divergence(tmp_path: Path) -> None:
    dataset, matrix = _fixture()
    output = tmp_path / "result"
    first = _run(dataset, matrix, output)
    form = first.blind_form_path
    text = form.read_text(encoding="utf-8").replace(
        "结论（适配/不适配）：", "结论（适配/不适配）：不适配"
    )
    form.write_text(text, encoding="utf-8")

    second = _run(dataset, matrix, output)

    assert second.exit_code == 2
    manual = json.loads((output / "adaptation_manual.json").read_text("utf-8"))
    assert manual["status"] == "divergent"
    assert manual["human_rate"] == "0.000000"
    assert manual["automatic_rate_same_sample"] == "1.000000"
    assert manual["absolute_gap"] == "1.000000"
    assert "必须停下报告分歧" in second.report_path.read_text(encoding="utf-8")
