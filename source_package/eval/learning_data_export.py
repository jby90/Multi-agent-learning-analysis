"""Export replayable P7 learning-data samples from the frozen trace dataset."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from agents.diagnosis_agent import load_profiles
from eval.trace_dataset import canonical_json, load_dataset


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "交付物" / "学情数据样例"
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ACCEPTED_DECISIONS = frozenset({"approve", "approve_with_fix"})


@dataclass(frozen=True, slots=True)
class LearningSampleSelection:
    case_id: str
    slug: str
    expected_profile_id: str
    expected_learning_path: str
    rationale: str


DEFAULT_SELECTIONS = (
    LearningSampleSelection(
        case_id="E2E-008",
        slug="01_planner_new_rebuttal_corrected",
        expected_profile_id="planner_new",
        expected_learning_path="rebuttal_corrected",
        rationale="新入职生产计划员首次答错，经数据反证后改正的完整高光链路",
    ),
    LearningSampleSelection(
        case_id="E2E-012",
        slug="02_craft_engineer_direct_correct",
        expected_profile_id="craft_engineer",
        expected_learning_path="direct_correct",
        rationale="工艺工程师首次答对并正常进阶的完整对照链路",
    ),
    LearningSampleSelection(
        case_id="E2E-030",
        slug="03_line_leader_second_wrong_step_down",
        expected_profile_id="line_leader",
        expected_learning_path="second_wrong_step_down",
        rationale="班组长连续答错后降维重学的完整链路",
    ),
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _repository_path(repository_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe trace path: {relative}")
    root = Path(repository_root).resolve()
    path = root.joinpath(*pure.parts).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"trace path escapes repository: {relative}")
    return path


def _portable_repository_path(path: Path, repository_root: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(repository_root).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _payload(message: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return None, {}
    content = payload.get("content")
    return payload.get("type"), content if isinstance(content, Mapping) else {}


def _raw_messages(raw_trace: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    try:
        lines = raw_trace.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"raw trace is not UTF-8: {exc}") from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw trace line {index} is invalid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"raw trace line {index} must be an object")
        messages.append(value)
    return messages


def _stage_index(messages: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    stages = {
        "diagnosis": [],
        "lecture": [],
        "lecture_evidence": [],
        "task": [],
        "sql_submission": [],
        "sql_execution": [],
        "review": [],
        "rebuttal": [],
        "counter_evidence": [],
        "probe_outcome": [],
        "learning_path": [],
    }
    for message in messages:
        msg_id = message.get("msg_id")
        if not isinstance(msg_id, str):
            continue
        payload_type, content = _payload(message)
        event = content.get("event")
        if payload_type == "profile_assessment":
            stages["diagnosis"].append(msg_id)
        if payload_type == "lecture_note":
            stages["lecture"].append(msg_id)
            evidence = message.get("evidence")
            if isinstance(evidence, list) and evidence:
                stages["lecture_evidence"].append(msg_id)
        if payload_type in {"quiz_set", "practice_guide"} and event != "counter_evidence_ready":
            stages["task"].append(msg_id)
        if event == "student_sql_submitted":
            stages["sql_submission"].append(msg_id)
        if payload_type == "sql_result":
            stages["sql_execution"].append(msg_id)
        if payload_type == "review_verdict":
            stages["review"].append(msg_id)
        if event in {"counter_evidence_ready", "probe_outcome"}:
            stages["rebuttal"].append(msg_id)
        if event == "counter_evidence_ready":
            stages["counter_evidence"].append(msg_id)
        if event == "probe_outcome":
            stages["probe_outcome"].append(msg_id)
        if payload_type == "learning_path_update":
            stages["learning_path"].append(msg_id)
    return stages


def _final_resources(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    accepted_ids: set[str] = set()
    for message in messages:
        payload_type, content = _payload(message)
        verdict = message.get("verdict")
        if payload_type != "review_verdict" or not isinstance(verdict, Mapping):
            continue
        reviewed_msg_id = content.get("reviewed_msg_id")
        if (
            verdict.get("decision") in _ACCEPTED_DECISIONS
            and isinstance(reviewed_msg_id, str)
        ):
            accepted_ids.add(reviewed_msg_id)
    return [message for message in messages if message.get("msg_id") in accepted_ids]


def _build_package(
    row: Mapping[str, Any],
    *,
    selection: LearningSampleSelection,
    profile: Mapping[str, Any],
    dataset_path: str,
    dataset_sha256: str,
) -> dict[str, Any]:
    case = row.get("case")
    messages = row.get("messages")
    if not isinstance(case, Mapping) or not isinstance(messages, list):
        raise ValueError(f"{selection.case_id} is missing case or messages")
    stages = _stage_index(messages)
    required = (
        "diagnosis",
        "lecture",
        "lecture_evidence",
        "task",
        "sql_submission",
        "sql_execution",
        "review",
        "learning_path",
    )
    if selection.expected_learning_path == "rebuttal_corrected":
        required += ("counter_evidence", "probe_outcome")
    stage_presence = {name: bool(stages[name]) for name in stages}
    all_present = all(stage_presence[name] for name in required)
    if not all_present:
        missing = [name for name in required if not stage_presence[name]]
        raise ValueError(
            f"{selection.case_id} is incomplete for official delivery: {missing}"
        )
    resources = _final_resources(messages)
    if not resources:
        raise ValueError(f"{selection.case_id} has no accepted final resources")
    resource_types = {_payload(message)[0] for message in resources}
    resource_categories = {
        "lecture": "lecture_note" in resource_types,
        "task": bool(resource_types & {"quiz_set", "practice_guide"}),
        "sql_result": "sql_result" in resource_types,
    }
    missing_resource_categories = [
        name for name, present in resource_categories.items() if not present
    ]
    if missing_resource_categories:
        raise ValueError(
            f"{selection.case_id} is missing accepted final resources: "
            f"{missing_resource_categories}"
        )
    path_updates = [
        message for message in messages if _payload(message)[0] == "learning_path_update"
    ]
    return {
        "format_version": "p7-learning-data-v1",
        "official_view": "输入画像特征 + 多智能体协同决策中间数据 + 最终生成资源",
        "selection_rationale": selection.rationale,
        "provenance": {
            "dataset_path": dataset_path,
            "dataset_sha256": dataset_sha256,
            "case_id": selection.case_id,
            "attempt": row.get("attempt"),
            "trace_id": row.get("trace_id"),
            "trace_path": row.get("trace_path"),
            "raw_trace_sha256": row.get("trace_sha256"),
            "base_commit": row.get("base_commit"),
            "terminal_state": row.get("terminal_state"),
            "message_count": row.get("message_count"),
            "export_policy": "messages are copied from the frozen successful trace without editing",
        },
        "input_profile": {
            "profile_definition": dict(profile),
            "case_parameters": dict(case),
            "pretest_answers": dict(case.get("answers", {})),
        },
        "collaboration_intermediate_data": {
            "stage_message_ids": stages,
            "messages": messages,
        },
        "final_resources": resources,
        "final_learning_path": path_updates[-1],
        "completeness_check": {
            "required_stages": list(required),
            "stage_presence": stage_presence,
            "accepted_final_resource_categories": resource_categories,
            "all_required_stages_present": all_present,
            "terminal_s10": row.get("terminal_state") == "S10_DONE",
            "raw_trace_verbatim": True,
        },
    }


def _readme(manifest: Mapping[str, Any]) -> bytes:
    lines = [
        "# P7完整学情数据样例",
        "",
        "> 官方交付视图：画像输入 + 协同中间数据 + 最终资源。每组同时保留未经改写的完整trace JSONL，供离线回放与逐消息审计。",
        "",
        f"- 源数据集：`{manifest['dataset_path']}`",
        f"- 数据集SHA-256：`{manifest['dataset_sha256']}`",
        f"- 冻结基线：`{manifest['base_commit']}`",
        f"- 样例数：{len(manifest['samples'])}",
        "",
        "| 样例 | case | 画像 | 路径 | 完整trace SHA-256 |",
        "|---|---|---|---|---|",
    ]
    for sample in manifest["samples"]:
        lines.append(
            f"| `{sample['slug']}` | `{sample['case_id']}` | "
            f"`{sample['profile_id']}` | `{sample['learning_path']}` | "
            f"`{sample['raw_trace_sha256']}` |"
        )
    lines.extend(
        [
            "",
            "每个目录内：",
            "",
            "- `完整trace.jsonl`：从正式成功attempt逐字复制，可直接交给trace回放器。",
            "- `完整学情数据.json`：同一真实trace按官方三段格式建立索引；消息正文不改写。",
            "",
            "完整性门禁覆盖：画像与前测、诊断、讲义及引用、任务、SQL提交与执行、审核、可选反证、最终学习路径、S10终态。",
        ]
    )
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def export_learning_samples(
    dataset_path: Path,
    output_dir: Path,
    *,
    repository_root: Path = ROOT,
    selections: Sequence[LearningSampleSelection] = DEFAULT_SELECTIONS,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    dataset_path = Path(dataset_path)
    output_dir = Path(output_dir)
    repository_root = Path(repository_root)
    if not selections or len({item.case_id for item in selections}) != len(selections):
        raise ValueError("sample selections must be non-empty and unique")
    for selection in selections:
        if not _SAFE_SLUG_RE.fullmatch(selection.slug):
            raise ValueError(f"unsafe sample slug: {selection.slug}")
    dataset_bytes = dataset_path.read_bytes()
    dataset_sha256 = _sha256_bytes(dataset_bytes)
    portable_dataset_path = _portable_repository_path(dataset_path, repository_root)
    dataset = load_dataset(dataset_path)
    rows_by_id = {
        str(row.get("case", {}).get("case_id")): row
        for row in dataset
        if isinstance(row.get("case"), Mapping)
    }
    profiles = profiles if profiles is not None else load_profiles()
    sample_entries: list[dict[str, Any]] = []
    base_commits: set[str] = set()
    for selection in selections:
        row = rows_by_id.get(selection.case_id)
        if row is None:
            raise ValueError(f"selected case missing from dataset: {selection.case_id}")
        case = row["case"]
        profile_id = case.get("profile_id")
        learning_path = case.get("learning_path")
        if profile_id != selection.expected_profile_id:
            raise ValueError(f"{selection.case_id} profile does not match selection")
        if learning_path != selection.expected_learning_path:
            raise ValueError(f"{selection.case_id} learning path does not match selection")
        if row.get("terminal_state") != "S10_DONE":
            raise ValueError(f"{selection.case_id} is not terminal S10_DONE")
        trace_path = _repository_path(repository_root, str(row.get("trace_path")))
        raw_trace = trace_path.read_bytes()
        raw_sha256 = _sha256_bytes(raw_trace)
        if raw_sha256 != row.get("trace_sha256"):
            raise ValueError(f"{selection.case_id} trace SHA-256 mismatch")
        if _raw_messages(raw_trace) != row.get("messages"):
            raise ValueError(f"{selection.case_id} dataset messages differ from raw trace")
        profile = profiles.get(str(profile_id))
        if not isinstance(profile, Mapping):
            raise ValueError(f"profile definition missing: {profile_id}")
        package = _build_package(
            row,
            selection=selection,
            profile=profile,
            dataset_path=portable_dataset_path,
            dataset_sha256=dataset_sha256,
        )
        package_bytes = _json_bytes(package)
        sample_dir = output_dir / selection.slug
        _atomic_write(sample_dir / "完整trace.jsonl", raw_trace)
        _atomic_write(sample_dir / "完整学情数据.json", package_bytes)
        base_commit = str(row.get("base_commit"))
        base_commits.add(base_commit)
        sample_entries.append(
            {
                "slug": selection.slug,
                "case_id": selection.case_id,
                "profile_id": profile_id,
                "learning_path": learning_path,
                "rationale": selection.rationale,
                "trace_id": row.get("trace_id"),
                "message_count": row.get("message_count"),
                "raw_trace_sha256": raw_sha256,
                "package_sha256": _sha256_bytes(package_bytes),
            }
        )
    if len(base_commits) != 1:
        raise ValueError(f"selected samples use different frozen bases: {base_commits}")
    manifest = {
        "format_version": "p7-learning-data-manifest-v1",
        "dataset_path": portable_dataset_path,
        "dataset_sha256": dataset_sha256,
        "base_commit": next(iter(base_commits)),
        "sample_count": len(sample_entries),
        "samples": sample_entries,
    }
    _atomic_write(output_dir / "manifest.json", _json_bytes(manifest))
    _atomic_write(output_dir / "README.md", _readme(manifest))
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从冻结版50组trace导出P7学情数据样例")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    manifest = export_learning_samples(
        args.dataset,
        args.output_dir,
        repository_root=args.repository_root,
    )
    print(canonical_json(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
