from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from eval.learning_data_export import (
    DEFAULT_SELECTIONS,
    LearningSampleSelection,
    export_learning_samples,
)
from eval.trace_dataset import canonical_json


def _message(
    trace_id: str,
    step: int,
    *,
    agent: str,
    role: str,
    payload_type: str,
    content: dict,
    verdict: dict | None = None,
    evidence: list[dict] | None = None,
) -> dict:
    value = {
        "msg_id": f"{trace_id}-{step:03d}",
        "trace_id": trace_id,
        "step": step,
        "agent": agent,
        "role": role,
        "payload": {"type": payload_type, "content": content},
    }
    if verdict is not None:
        value["verdict"] = verdict
    if evidence is not None:
        value["evidence"] = evidence
    return value


def _fixture(repository_root: Path) -> tuple[Path, bytes]:
    trace_id = "trace-export-001"
    messages = [
        _message(
            trace_id,
            1,
            agent="diagnosis",
            role="produce",
            payload_type="profile_assessment",
            content={"event": "diagnosis_ready"},
        ),
        _message(
            trace_id,
            2,
            agent="knowledge",
            role="produce",
            payload_type="lecture_note",
            content={"event": "product_ready", "lecture_md": "真实讲义"},
            evidence=[
                {
                    "kind": "kb_chunk",
                    "ref": "KB-001",
                    "quote": "真实引文",
                    "supports_claim": "真实结论",
                }
            ],
        ),
        _message(
            trace_id,
            3,
            agent="review",
            role="verdict",
            payload_type="review_verdict",
            content={"event": "review_complete", "reviewed_msg_id": f"{trace_id}-002"},
            verdict={"decision": "approve", "rule_hits": []},
        ),
        _message(
            trace_id,
            4,
            agent="task",
            role="produce",
            payload_type="quiz_set",
            content={"event": "product_ready", "questions": []},
        ),
        _message(
            trace_id,
            5,
            agent="review",
            role="verdict",
            payload_type="review_verdict",
            content={"event": "review_complete", "reviewed_msg_id": f"{trace_id}-004"},
            verdict={"decision": "approve", "rule_hits": []},
        ),
        _message(
            trace_id,
            6,
            agent="system",
            role="system",
            payload_type="control",
            content={"event": "student_sql_submitted", "sql": "SELECT 1"},
        ),
        _message(
            trace_id,
            7,
            agent="verification",
            role="produce",
            payload_type="sql_result",
            content={"event": "query_completed", "rows": [{"value": 1}]},
        ),
        _message(
            trace_id,
            8,
            agent="review",
            role="verdict",
            payload_type="review_verdict",
            content={"event": "review_complete", "reviewed_msg_id": f"{trace_id}-007"},
            verdict={"decision": "approve", "rule_hits": []},
        ),
        _message(
            trace_id,
            9,
            agent="task",
            role="probe",
            payload_type="quiz_set",
            content={"event": "counter_evidence_ready"},
        ),
        _message(
            trace_id,
            10,
            agent="system",
            role="system",
            payload_type="control",
            content={"event": "probe_outcome", "corrected": True},
        ),
        _message(
            trace_id,
            11,
            agent="system",
            role="system",
            payload_type="learning_path_update",
            content={"event": "path_updated", "difficulty_action": "keep"},
        ),
        _message(
            trace_id,
            12,
            agent="system",
            role="system",
            payload_type="control",
            content={"transition_id": "T20"},
        ),
    ]
    raw_trace = "".join(canonical_json(message) + "\n" for message in messages).encode(
        "utf-8"
    )
    trace_path = repository_root / "runs" / "trace.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_bytes(raw_trace)
    row = {
        "case": {
            "case_id": "E2E-001",
            "profile_id": "planner_new",
            "knowledge_point": "计划量与实际量口径",
            "learning_path": "rebuttal_corrected",
            "answers": {"PT-1": "B"},
        },
        "attempt": 1,
        "trace_id": trace_id,
        "trace_path": "runs/trace.jsonl",
        "trace_sha256": hashlib.sha256(raw_trace).hexdigest(),
        "base_commit": "a" * 40,
        "terminal_state": "S10_DONE",
        "transition_sequence": ["T20"],
        "message_count": len(messages),
        "messages": messages,
    }
    dataset = repository_root / "dataset.jsonl"
    dataset.write_text(canonical_json(row) + "\n", encoding="utf-8", newline="")
    return dataset, raw_trace


def _official_default_fixture(repository_root: Path) -> tuple[Path, dict[str, bytes]]:
    dataset, _ = _fixture(repository_root)
    base_row = json.loads(dataset.read_text("utf-8"))
    frozen_base = "release-fixture"
    cases = (
        ("E2E-008", "planner_new", "rebuttal_corrected", 12),
        ("E2E-012", "craft_engineer", "direct_correct", 12),
        ("E2E-030", "line_leader", "second_wrong_step_down", 45),
    )
    rows = []
    traces = {}
    for case_id, profile_id, learning_path, message_count in cases:
        row = json.loads(json.dumps(base_row))
        trace_id = f"trace-export-{case_id.lower()}"
        id_map = {
            message["msg_id"]: f"{trace_id}-{message['step']:03d}"
            for message in row["messages"]
        }
        for message in row["messages"]:
            message["trace_id"] = trace_id
            message["msg_id"] = id_map[message["msg_id"]]
            content = message["payload"]["content"]
            reviewed_msg_id = content.get("reviewed_msg_id")
            if reviewed_msg_id is not None:
                content["reviewed_msg_id"] = id_map[reviewed_msg_id]
        for step in range(len(row["messages"]) + 1, message_count + 1):
            row["messages"].append(
                _message(
                    trace_id,
                    step,
                    agent="system",
                    role="system",
                    payload_type="control",
                    content={"event": "audit_checkpoint"},
                )
            )
        row["case"] = {
            "case_id": case_id,
            "profile_id": profile_id,
            "knowledge_point": "计划量与实际量口径",
            "learning_path": learning_path,
            "answers": {"PT-1": "B"},
        }
        row["attempt"] = 1
        row["trace_id"] = trace_id
        row["trace_path"] = f"runs/{case_id}.jsonl"
        row["base_commit"] = frozen_base
        row["terminal_state"] = "S10_DONE"
        row["message_count"] = message_count
        case_trace = "".join(
            canonical_json(message) + "\n" for message in row["messages"]
        ).encode("utf-8")
        (repository_root / row["trace_path"]).write_bytes(case_trace)
        row["trace_sha256"] = hashlib.sha256(case_trace).hexdigest()
        rows.append(row)
        traces[case_id] = case_trace
    dataset.write_text(
        "".join(canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
        newline="",
    )
    return dataset, traces


def test_export_preserves_raw_trace_and_builds_official_three_part_view(
    tmp_path: Path,
) -> None:
    dataset, raw_trace = _fixture(tmp_path)
    output = tmp_path / "delivery"
    selection = LearningSampleSelection(
        case_id="E2E-001",
        slug="01_planner_new_rebuttal_corrected",
        expected_profile_id="planner_new",
        expected_learning_path="rebuttal_corrected",
        rationale="新计划员答错后经数据反证改正",
    )

    manifest = export_learning_samples(
        dataset,
        output,
        repository_root=tmp_path,
        selections=(selection,),
        profiles={"planner_new": {"profile_id": "planner_new", "title": "新计划员"}},
    )

    assert manifest["dataset_path"] == "dataset.jsonl"
    sample_dir = output / selection.slug
    assert (sample_dir / "完整trace.jsonl").read_bytes() == raw_trace
    package = json.loads((sample_dir / "完整学情数据.json").read_text("utf-8"))
    assert package["input_profile"]["profile_definition"]["title"] == "新计划员"
    assert package["input_profile"]["pretest_answers"] == {"PT-1": "B"}
    assert len(package["collaboration_intermediate_data"]["messages"]) == 12
    assert [item["msg_id"] for item in package["final_resources"]] == [
        f"{package['provenance']['trace_id']}-002",
        f"{package['provenance']['trace_id']}-004",
        f"{package['provenance']['trace_id']}-007",
    ]
    assert package["final_learning_path"]["payload"]["content"]["event"] == "path_updated"
    assert package["completeness_check"]["all_required_stages_present"] is True
    assert package["completeness_check"]["stage_presence"]["lecture_evidence"] is True
    assert package["completeness_check"]["stage_presence"]["counter_evidence"] is True
    assert package["completeness_check"]["stage_presence"]["probe_outcome"] is True
    assert manifest["samples"][0]["raw_trace_sha256"] == hashlib.sha256(
        raw_trace
    ).hexdigest()
    assert "画像输入 + 协同中间数据 + 最终资源" in (
        output / "README.md"
    ).read_text("utf-8")


def test_default_export_covers_three_profiles_and_paths_with_complete_line_leader(
    tmp_path: Path,
) -> None:
    dataset, traces = _official_default_fixture(tmp_path)
    output = tmp_path / "delivery"

    manifest = export_learning_samples(
        dataset,
        output,
        repository_root=tmp_path,
        profiles={
            "planner_new": {"profile_id": "planner_new", "title": "新计划员"},
            "craft_engineer": {
                "profile_id": "craft_engineer",
                "title": "工艺工程师",
            },
            "line_leader": {"profile_id": "line_leader", "title": "班组长"},
        },
    )

    assert [
        (
            selection.case_id,
            selection.expected_profile_id,
            selection.expected_learning_path,
        )
        for selection in DEFAULT_SELECTIONS
    ] == [
        ("E2E-008", "planner_new", "rebuttal_corrected"),
        ("E2E-012", "craft_engineer", "direct_correct"),
        ("E2E-030", "line_leader", "second_wrong_step_down"),
    ]
    assert manifest["sample_count"] == 3
    assert manifest["base_commit"] == "release-fixture"
    assert {sample["profile_id"] for sample in manifest["samples"]} == {
        "planner_new",
        "craft_engineer",
        "line_leader",
    }
    assert {sample["learning_path"] for sample in manifest["samples"]} == {
        "rebuttal_corrected",
        "direct_correct",
        "second_wrong_step_down",
    }
    assert manifest["samples"][2]["case_id"] == "E2E-030"
    assert manifest["samples"][2]["message_count"] == 45
    assert json.loads((output / "manifest.json").read_text("utf-8")) == manifest

    line_leader_selection = DEFAULT_SELECTIONS[2]
    line_leader_dir = output / line_leader_selection.slug
    assert (line_leader_dir / "完整trace.jsonl").read_bytes() == traces["E2E-030"]
    package = json.loads((line_leader_dir / "完整学情数据.json").read_text("utf-8"))
    assert package["provenance"]["case_id"] == "E2E-030"
    assert package["provenance"]["attempt"] == 1
    assert package["provenance"]["terminal_state"] == "S10_DONE"
    assert package["provenance"]["message_count"] == 45
    assert len(package["collaboration_intermediate_data"]["messages"]) == 45
    assert package["completeness_check"]["all_required_stages_present"] is True
    assert package["completeness_check"]["terminal_s10"] is True


def test_export_rejects_trace_that_no_longer_matches_frozen_hash(tmp_path: Path) -> None:
    dataset, _ = _fixture(tmp_path)
    (tmp_path / "runs" / "trace.jsonl").write_text("tampered\n", encoding="utf-8")
    selection = LearningSampleSelection(
        case_id="E2E-001",
        slug="sample",
        expected_profile_id="planner_new",
        expected_learning_path="rebuttal_corrected",
        rationale="hash gate",
    )

    try:
        export_learning_samples(
            dataset,
            tmp_path / "delivery",
            repository_root=tmp_path,
            selections=(selection,),
            profiles={"planner_new": {"profile_id": "planner_new"}},
        )
    except ValueError as exc:
        assert "trace SHA-256 mismatch" in str(exc)
    else:
        raise AssertionError("tampered raw trace must be rejected")


def test_export_rejects_rebuttal_path_without_probe_outcome(tmp_path: Path) -> None:
    dataset, _ = _fixture(tmp_path)
    row = json.loads(dataset.read_text("utf-8"))
    for message in row["messages"]:
        content = message["payload"]["content"]
        if content.get("event") == "probe_outcome":
            content["event"] = "unrelated_event"
    raw_trace = "".join(
        canonical_json(message) + "\n" for message in row["messages"]
    ).encode("utf-8")
    (tmp_path / row["trace_path"]).write_bytes(raw_trace)
    row["trace_sha256"] = hashlib.sha256(raw_trace).hexdigest()
    dataset.write_text(canonical_json(row) + "\n", encoding="utf-8", newline="")
    selection = LearningSampleSelection(
        case_id="E2E-001",
        slug="sample",
        expected_profile_id="planner_new",
        expected_learning_path="rebuttal_corrected",
        rationale="probe completeness gate",
    )

    with pytest.raises(ValueError, match="probe_outcome"):
        export_learning_samples(
            dataset,
            tmp_path / "delivery",
            repository_root=tmp_path,
            selections=(selection,),
            profiles={"planner_new": {"profile_id": "planner_new"}},
        )
