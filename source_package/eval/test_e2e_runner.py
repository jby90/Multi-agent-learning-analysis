from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.validate_message import validate_message
from eval.case_matrix import EvaluationCase, PRETEST_PATTERNS
from eval.e2e_runner import (
    FROZEN_BASE_COMMIT,
    RunPaths,
    RunnerDependencies,
    _EvaluationRuntime,
    _assert_frozen_tree,
    _new_attempt_numbers,
    derive_step_down_target,
    execute_case,
    run_case,
)
from eval.test_demo_session import RecordingExecutor, ScriptedLLM, llm_result
from eval.trace_dataset import load_attempt_records
from orchestrator.llm import LLMResult
from orchestrator.outcomes import Outcome, OutcomeError
from orchestrator.transitions import State


EXPECTED_PREFIX = (
    "T01",
    "T02",
    "T03",
    "T04",
    "T09",
    "T10",
    "T11",
    "T12",
    "T13",
    "T19",
    "T09",
    "T10",
)


def test_runner_defaults_to_final_frozen_system() -> None:
    final_system_commit = "release-1.1.0"

    dependencies = RunnerDependencies()

    assert FROZEN_BASE_COMMIT == final_system_commit
    assert dependencies.base_commit == final_system_commit
    assert dependencies.verify_frozen_tree is True


def test_frozen_tree_uses_injected_review_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("eval.e2e_runner.subprocess.run", fake_run)

    _assert_frozen_tree(tmp_path, "review-candidate-sha")

    assert calls[0] == [
        "git",
        "diff",
        "--quiet",
        "review-candidate-sha",
        "--",
        "agents",
        "orchestrator",
        "frontend",
    ]


def _case(path: str) -> EvaluationCase:
    pattern = {
        "direct_correct": "A5",
        "rebuttal_corrected": "A3",
        "second_wrong_step_down": "A0",
    }[path]
    return EvaluationCase(
        case_id="E2E-001",
        profile_id="planner_new",
        knowledge_point="计划量与实际量口径",
        pretest_pattern=pattern,
        answers=PRETEST_PATTERNS[pattern],
        task_template_id="T-01",
        learning_path=path,
        misconception_id=None if path == "direct_correct" else "M-01",
        manual_review=True,
    )


def _dependencies(llm: Any | None = None) -> RunnerDependencies:
    return RunnerDependencies(
        llm_call=llm or ScriptedLLM(),
        executor_factory=RecordingExecutor,
        mode="live",
        base_commit=FROZEN_BASE_COMMIT,
        verify_frozen_tree=False,
    )


def _verification_failure(trace_id: str, event: str) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {
                "event": event,
                "question": "固定桩验证问题",
                "family": "Q1",
                "student_message": "本次查询未形成可审核的数据结论。",
            },
        },
        "evidence": [],
        "claims": [],
        "timestamp": "2026-07-24T10:00:00+08:00",
    }


def _runtime_ready_for_sql(
    tmp_path: Path,
    trace_id: str,
) -> tuple[_EvaluationRuntime, dict[str, Any], dict[str, Any]]:
    case = _case("direct_correct")
    runtime = _EvaluationRuntime(
        case,
        trace_id=trace_id,
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )
    diagnosis = runtime.load_and_diagnose()
    runtime.teach(case.knowledge_point, diagnosis)
    task = runtime.task_and_review(case.task_template_id, diagnosis)
    return runtime, diagnosis, task


@pytest.mark.parametrize(
    ("event", "expected_outcome"),
    (
        ("refuse_out_of_scope", Outcome.SAFE_REJECTED),
        ("sandbox_rejected", Outcome.SAFE_REJECTED),
        ("template_authority_rejected", Outcome.SAFE_REJECTED),
        ("query_empty", Outcome.SAFE_REJECTED),
        ("query_timeout", Outcome.EXTERNAL_UNAVAILABLE),
        ("query_failed", Outcome.EXTERNAL_UNAVAILABLE),
    ),
)
def test_sql_runner_stops_with_explicit_outcome_after_t21(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    expected_outcome: Outcome,
) -> None:
    runtime, diagnosis, task = _runtime_ready_for_sql(
        tmp_path / event,
        f"p7-s4-{event.replace('_', '-')}-a01",
    )
    monkeypatch.setattr(
        runtime.verification,
        "answer",
        lambda *_args, **_kwargs: _verification_failure(runtime.trace_id, event),
    )

    with pytest.raises(OutcomeError) as raised:
        runtime.sql_and_review(task, diagnosis)

    assert raised.value.outcome is expected_outcome
    assert raised.value.event == event
    assert runtime.engine.state is State.S7_STUDENT
    messages = [
        json.loads(line)
        for line in runtime.bus.trace_path(runtime.trace_id)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    transitions = [
        message["payload"]["content"]["transition_id"]
        for message in messages
        if "transition_id" in message.get("payload", {}).get("content", {})
    ]
    assert transitions[-2:] == ["T11", "T21"]
    failed_product = next(
        message
        for message in reversed(messages)
        if message.get("agent") == "verification"
    )
    assert not any(
        message.get("payload", {}).get("content", {}).get("reviewed_msg_id")
        == failed_product["msg_id"]
        for message in messages
    )


def _grounded_lecture(runtime: _EvaluationRuntime) -> dict[str, Any]:
    chunk = next(item for item in runtime.chunks if item.chunk_id == "KB-003")
    quote = chunk.sentences[0]
    atom = quote.replace("**", "").replace("__", "").replace("`", "")
    return {
        "payload": {
            "type": "lecture_note",
            "content": {
                "lecture_md": atom,
                "knowledge_point": "完成率计算",
                "responsibility_scope": ["完成率计算", "计划量与实际量口径"],
                "coverage": ["完成率计算", "计划量与实际量口径"],
            },
        },
        "claims": [{"text": atom, "kind": "fact"}],
        "evidence": [
            {
                "kind": "kb_chunk",
                "ref": "KB-003",
                "quote": quote,
                "supports_claim": atom,
            }
        ],
    }


def _expected_retrieved_chunk_ids(
    runtime: _EvaluationRuntime,
    target_chunk_id: str,
) -> list[str]:
    target = next(
        chunk for chunk in runtime.chunks if chunk.chunk_id == target_chunk_id
    )
    return [target.chunk_id, *target.prerequisites]


def test_runner_remembers_only_mechanically_grounded_approved_coverage(
    tmp_path: Path,
) -> None:
    runtime = _EvaluationRuntime(
        _case("direct_correct"),
        trace_id="p7-grounded-memory-a01",
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )

    runtime._remember_approved_coverage(_grounded_lecture(runtime), "approve")

    assert runtime.learned_knowledge_points == ["完成率计算"]


def test_runner_does_not_learn_from_approve_with_fix(
    tmp_path: Path,
) -> None:
    runtime = _EvaluationRuntime(
        _case("direct_correct"),
        trace_id="p7-fixed-memory-a01",
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )

    runtime._remember_approved_coverage(
        _grounded_lecture(runtime),
        "approve_with_fix",
    )

    assert runtime.learned_knowledge_points == []


@pytest.mark.parametrize(
    ("learning_path", "expected_suffix"),
    [
        ("direct_correct", ("T14", "T20")),
        ("rebuttal_corrected", ("T15", "T16", "T20")),
        (
            "second_wrong_step_down",
            ("T15", "T17", "T03", "T04", "T09", "T10", "T14", "T20"),
        ),
    ],
)
def test_driver_uses_frozen_engine_for_each_approved_path(
    learning_path: str,
    expected_suffix: tuple[str, ...],
    tmp_path: Path,
) -> None:
    result = run_case(
        _case(learning_path),
        attempt=1,
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )

    assert result.terminal_state == "S10_DONE"
    assert result.transition_sequence == EXPECTED_PREFIX + expected_suffix
    assert result.state_sequence[-1] == "S10_DONE"
    assert result.trace_path.exists()
    messages = [
        json.loads(line)
        for line in result.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(messages) == result.message_count
    assert all(validate_message(message) == [] for message in messages)
    assert {message["agent"] for message in messages} >= {
        "diagnosis",
        "knowledge",
        "task",
        "verification",
        "review",
    }
    reviewed = {
        message["payload"]["content"].get("reviewed_msg_id")
        for message in messages
        if message["payload"]["type"] == "review_verdict"
    }
    displayed_products = {
        message["msg_id"]
        for message in messages
        if message["agent"] in {"knowledge", "task", "verification"}
        and message["role"] == "produce"
    }
    assert displayed_products.issubset(reviewed)


def test_driver_selects_completion_assets_from_diagnosed_difficulty(
    tmp_path: Path,
) -> None:
    case = EvaluationCase(
        case_id="E2E-011",
        profile_id="planner_new",
        knowledge_point="完成率计算",
        pretest_pattern="A5",
        answers=PRETEST_PATTERNS["A5"],
        task_template_id="T-02",
        learning_path="direct_correct",
        misconception_id=None,
        manual_review=True,
    )

    paths = RunPaths.under(tmp_path)
    runtime = _EvaluationRuntime(
        case,
        trace_id="p7-e2e-011-a01",
        paths=paths,
        dependencies=_dependencies(),
    )
    diagnosis = runtime.load_and_diagnose()
    lecture = runtime.teach(case.knowledge_point, diagnosis)
    task = runtime.task_and_review(case.task_template_id, diagnosis)
    messages = [
        json.loads(line)
        for line in runtime.bus.trace_path(runtime.trace_id).read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    tasks = [
        message
        for message in messages
        if message.get("agent") == "task" and message.get("role") == "produce"
    ]

    assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
        _expected_retrieved_chunk_ids(runtime, "KB-003-A")
    )
    assert task["payload"]["content"]["template_id"] == "T-02-A"
    assert tasks
    assert {
        (message["payload"]["content"]["template_id"], message["payload"]["content"]["difficulty"])
        for message in tasks
    } == {("T-02-A", "applied")}


def test_driver_selects_plan_actual_assets_from_each_diagnosed_difficulty(
    tmp_path: Path,
) -> None:
    scenarios = (
        ("E2E-008", "planner_new", "A3", "basic", "KB-002", "T-01"),
        ("E2E-006", "planner_new", "A5", "applied", "KB-002-A", "T-01-A"),
        ("E2E-051", "craft_engineer", "A5", "advanced", "KB-002-B", "T-01-B"),
    )
    matched = 0

    for case_id, profile_id, pattern, difficulty, chunk_id, template_id in scenarios:
        case = EvaluationCase(
            case_id=case_id,
            profile_id=profile_id,
            knowledge_point="计划量与实际量口径",
            pretest_pattern=pattern,
            answers=PRETEST_PATTERNS[pattern],
            task_template_id="T-01",
            learning_path="direct_correct",
            misconception_id=None,
            manual_review=True,
        )
        paths = RunPaths.under(tmp_path / case_id)
        runtime = _EvaluationRuntime(
            case,
            trace_id=f"p7-{case_id.lower()}-a01",
            paths=paths,
            dependencies=_dependencies(),
        )

        diagnosis = runtime.load_and_diagnose()
        lecture = runtime.teach(case.knowledge_point, diagnosis)
        task = runtime.task_and_review(case.task_template_id, diagnosis)

        assert diagnosis["payload"]["content"]["difficulty"] == difficulty
        assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
            _expected_retrieved_chunk_ids(runtime, chunk_id)
        )
        assert task["payload"]["content"]["template_id"] == template_id
        assert task["payload"]["content"]["difficulty"] == difficulty
        matched += 1

    assert matched / len(scenarios) == 1.0


def test_driver_selects_process_propagation_assets_from_each_diagnosed_difficulty(
    tmp_path: Path,
) -> None:
    scenarios = (
        ("E2E-052", "planner_new", "A3", "basic", "KB-001", "T-03"),
        ("E2E-053", "planner_new", "A5", "applied", "KB-001-A", "T-03-A"),
        ("E2E-054", "craft_engineer", "A5", "advanced", "KB-001-B", "T-03-B"),
    )
    matched = 0

    for case_id, profile_id, pattern, difficulty, chunk_id, template_id in scenarios:
        case = EvaluationCase(
            case_id=case_id,
            profile_id=profile_id,
            knowledge_point="三道工序与传导关系",
            pretest_pattern=pattern,
            answers=PRETEST_PATTERNS[pattern],
            task_template_id="T-03",
            learning_path="direct_correct",
            misconception_id=None,
            manual_review=True,
        )
        paths = RunPaths.under(tmp_path / case_id)
        runtime = _EvaluationRuntime(
            case,
            trace_id=f"p7-{case_id.lower()}-a01",
            paths=paths,
            dependencies=_dependencies(),
        )

        diagnosis = runtime.load_and_diagnose()
        lecture = runtime.teach(case.knowledge_point, diagnosis)
        task = runtime.task_and_review(case.task_template_id, diagnosis)

        assert diagnosis["payload"]["content"]["difficulty"] == difficulty
        assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
            _expected_retrieved_chunk_ids(runtime, chunk_id)
        )
        assert task["payload"]["content"]["template_id"] == template_id
        assert task["payload"]["content"]["difficulty"] == difficulty
        matched += 1

    assert matched / len(scenarios) == 1.0


def test_driver_selects_deviation_risk_assets_from_each_diagnosed_difficulty(
    tmp_path: Path,
) -> None:
    scenarios = (
        ("E2E-018", "line_leader", "A2", "basic", "KB-004", "T-10"),
        ("E2E-016", "planner_new", "A5", "applied", "KB-004-A", "T-10-A"),
        ("E2E-055", "craft_engineer", "A5", "advanced", "KB-004-B", "T-10-B"),
    )
    matched = 0

    for case_id, profile_id, pattern, difficulty, chunk_id, template_id in scenarios:
        case = EvaluationCase(
            case_id=case_id,
            profile_id=profile_id,
            knowledge_point="偏差率与风险等级",
            pretest_pattern=pattern,
            answers=PRETEST_PATTERNS[pattern],
            task_template_id="T-10",
            learning_path="direct_correct",
            misconception_id=None,
            manual_review=True,
        )
        paths = RunPaths.under(tmp_path / case_id)
        runtime = _EvaluationRuntime(
            case,
            trace_id=f"p7-{case_id.lower()}-a01",
            paths=paths,
            dependencies=_dependencies(),
        )

        diagnosis = runtime.load_and_diagnose()
        lecture = runtime.teach(case.knowledge_point, diagnosis)
        task = runtime.task_and_review(case.task_template_id, diagnosis)

        assert diagnosis["payload"]["content"]["difficulty"] == difficulty
        assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
            _expected_retrieved_chunk_ids(runtime, chunk_id)
        )
        assert task["payload"]["content"]["template_id"] == template_id
        assert task["payload"]["content"]["difficulty"] == difficulty
        matched += 1

    assert matched / len(scenarios) == 1.0


def test_driver_selects_monthly_aggregation_assets_from_each_diagnosed_difficulty(
    tmp_path: Path,
) -> None:
    scenarios = (
        ("E2E-024", "line_leader", "A1", "basic", "KB-005", "T-04"),
        ("E2E-021", "line_leader", "A5", "applied", "KB-005-A", "T-04-A"),
        ("E2E-056", "craft_engineer", "A5", "advanced", "KB-005-B", "T-04-B"),
    )
    matched = 0

    for case_id, profile_id, pattern, difficulty, chunk_id, template_id in scenarios:
        case = EvaluationCase(
            case_id=case_id,
            profile_id=profile_id,
            knowledge_point="月度聚合方法",
            pretest_pattern=pattern,
            answers=PRETEST_PATTERNS[pattern],
            task_template_id="T-04",
            learning_path="direct_correct",
            misconception_id=None,
            manual_review=True,
        )
        paths = RunPaths.under(tmp_path / case_id)
        runtime = _EvaluationRuntime(
            case,
            trace_id=f"p7-{case_id.lower()}-a01",
            paths=paths,
            dependencies=_dependencies(),
        )

        diagnosis = runtime.load_and_diagnose()
        lecture = runtime.teach(case.knowledge_point, diagnosis)
        task = runtime.task_and_review(case.task_template_id, diagnosis)

        assert diagnosis["payload"]["content"]["difficulty"] == difficulty
        assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
            _expected_retrieved_chunk_ids(runtime, chunk_id)
        )
        assert task["payload"]["content"]["template_id"] == template_id
        assert task["payload"]["content"]["difficulty"] == difficulty
        matched += 1

    assert matched / len(scenarios) == 1.0


def test_driver_selects_anomaly_identification_assets_from_each_diagnosed_difficulty(
    tmp_path: Path,
) -> None:
    scenarios = (
        ("E2E-029", "planner_new", "A1", "basic", "KB-006", "T-05"),
        ("E2E-026", "planner_new", "A5", "applied", "KB-006-A", "T-05-A"),
        ("E2E-057", "craft_engineer", "A5", "advanced", "KB-006-B", "T-05-B"),
    )
    matched = 0

    for case_id, profile_id, pattern, difficulty, chunk_id, template_id in scenarios:
        case = EvaluationCase(
            case_id=case_id,
            profile_id=profile_id,
            knowledge_point="异常识别标准",
            pretest_pattern=pattern,
            answers=PRETEST_PATTERNS[pattern],
            task_template_id="T-05",
            learning_path="direct_correct",
            misconception_id=None,
            manual_review=True,
        )
        paths = RunPaths.under(tmp_path / case_id)
        runtime = _EvaluationRuntime(
            case,
            trace_id=f"p7-{case_id.lower()}-a01",
            paths=paths,
            dependencies=_dependencies(),
        )

        diagnosis = runtime.load_and_diagnose()
        lecture = runtime.teach(case.knowledge_point, diagnosis)
        task = runtime.task_and_review(case.task_template_id, diagnosis)

        assert diagnosis["payload"]["content"]["difficulty"] == difficulty
        assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
            _expected_retrieved_chunk_ids(runtime, chunk_id)
        )
        assert task["payload"]["content"]["template_id"] == template_id
        assert task["payload"]["content"]["difficulty"] == difficulty
        matched += 1

    assert matched / len(scenarios) == 1.0


def test_driver_selects_propagation_lag_assets_from_each_diagnosed_difficulty(
    tmp_path: Path,
) -> None:
    scenarios = (
        ("E2E-035", "line_leader", "A0", "basic", "KB-007", "T-07"),
        ("E2E-058", "planner_new", "A5", "applied", "KB-007-A", "T-07-A"),
        ("E2E-059", "craft_engineer", "A5", "advanced", "KB-007-B", "T-07-B"),
    )
    matched = 0

    for case_id, profile_id, pattern, difficulty, chunk_id, template_id in scenarios:
        case = EvaluationCase(
            case_id=case_id,
            profile_id=profile_id,
            knowledge_point="传导时滞分析",
            pretest_pattern=pattern,
            answers=PRETEST_PATTERNS[pattern],
            task_template_id="T-07",
            learning_path="direct_correct",
            misconception_id=None,
            manual_review=True,
        )
        paths = RunPaths.under(tmp_path / case_id)
        runtime = _EvaluationRuntime(
            case,
            trace_id=f"p7-{case_id.lower()}-a01",
            paths=paths,
            dependencies=_dependencies(),
        )

        diagnosis = runtime.load_and_diagnose()
        lecture = runtime.teach(case.knowledge_point, diagnosis)
        task = runtime.task_and_review(case.task_template_id, diagnosis)

        assert diagnosis["payload"]["content"]["difficulty"] == difficulty
        assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
            _expected_retrieved_chunk_ids(runtime, chunk_id)
        )
        assert task["payload"]["content"]["template_id"] == template_id
        assert task["payload"]["content"]["difficulty"] == difficulty
        matched += 1

    assert matched / len(scenarios) == 1.0


@pytest.mark.parametrize(
    ("knowledge_point", "requested_template_id", "expected_assets"),
    (
        (
            "异常衰减规律",
            "T-08-DECAY",
            (
                ("basic", "KB-008", "T-08-DECAY"),
                ("applied", "KB-008-A", "T-08-DECAY-A"),
                ("advanced", "KB-008-B", "T-08-DECAY-B"),
            ),
        ),
        (
            "责任单元定位",
            "T-06",
            (
                ("basic", "KB-009", "T-06"),
                ("applied", "KB-009-A", "T-06-A"),
                ("advanced", "KB-009-B", "T-06-B"),
            ),
        ),
        (
            "跨工序归因方法",
            "T-08",
            (
                ("basic", "KB-010", "T-08-ATTR"),
                ("applied", "KB-010-A", "T-08"),
                ("advanced", "KB-010-B", "T-09"),
            ),
        ),
    ),
)
def test_driver_selects_remaining_transmission_assets_from_each_diagnosed_difficulty(
    tmp_path: Path,
    knowledge_point: str,
    requested_template_id: str,
    expected_assets: tuple[tuple[str, str, str], ...],
) -> None:
    diagnostic_inputs = {
        "basic": ("line_leader", "A0"),
        "applied": ("planner_new", "A5"),
        "advanced": ("craft_engineer", "A5"),
    }

    for index, (difficulty, chunk_id, template_id) in enumerate(
        expected_assets, start=1
    ):
        profile_id, pattern = diagnostic_inputs[difficulty]
        case = EvaluationCase(
            case_id=f"E2E-TG-{index}",
            profile_id=profile_id,
            knowledge_point=knowledge_point,
            pretest_pattern=pattern,
            answers=PRETEST_PATTERNS[pattern],
            task_template_id=requested_template_id,
            learning_path="direct_correct",
            misconception_id=None,
            manual_review=True,
        )
        paths = RunPaths.under(tmp_path / f"{knowledge_point}-{difficulty}")
        runtime = _EvaluationRuntime(
            case,
            trace_id=f"transmission-{index}-a01",
            paths=paths,
            dependencies=_dependencies(),
        )

        diagnosis = runtime.load_and_diagnose()
        lecture = runtime.teach(case.knowledge_point, diagnosis)
        task = runtime.task_and_review(case.task_template_id, diagnosis)

        assert diagnosis["payload"]["content"]["difficulty"] == difficulty
        assert lecture["payload"]["content"]["retrieved_chunk_ids"] == (
            _expected_retrieved_chunk_ids(runtime, chunk_id)
        )
        assert task["payload"]["content"]["template_id"] == template_id
        assert task["payload"]["content"]["difficulty"] == difficulty


def test_driver_holds_out_and_reinjects_basic_anomaly_teaching_facts(
    tmp_path: Path,
) -> None:
    case = EvaluationCase(
        case_id="E2E-029",
        profile_id="planner_new",
        knowledge_point="异常识别标准",
        pretest_pattern="A1",
        answers=PRETEST_PATTERNS["A1"],
        task_template_id="T-05",
        learning_path="direct_correct",
        misconception_id=None,
        manual_review=True,
    )
    llm = ScriptedLLM()
    runtime = _EvaluationRuntime(
        case,
        trace_id="p7-e2e-029-teaching-facts-a01",
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(llm),
    )

    diagnosis = runtime.load_and_diagnose()
    lecture = runtime.teach(case.knowledge_point, diagnosis)
    knowledge_request = next(
        call for call in llm.calls if "oneOf" in call["json_schema"]
    )

    for held_value in ("88%", "103%", "62.36%", "90.61%"):
        assert held_value not in knowledge_request["user"]
    content = lecture["payload"]["content"]
    assert content["teaching_fact_cards"] == [
        {
            "chunk_id": "KB-006",
            "card_id": "KB-006-BASIC-ANOMALY-RANGE",
            "card_version": "1.0.0",
            "fact_ids": ["TF-006-001", "TF-006-002"],
        }
    ]
    for fact_id, expected_text in (
        (
            "TF-006-001",
            "本项目岗位培训与评测采用的月完成率正常波动区间为88%—103%。",
        ),
        (
            "TF-006-002",
            (
                "月完成率低于88%时，先作为偏低信号观察，不能仅凭单月数值直接定性；"
                "还须结合持续性或伴随的风险记录。"
            ),
        ),
    ):
        assert fact_id in content["teaching_fact_cards"][0]["fact_ids"]
        assert expected_text in content["lecture_md"]
        assert {"text": expected_text, "kind": "fact"} in lecture["claims"]
        assert {
            "kind": "kb_chunk",
            "ref": "KB-006",
            "quote": expected_text,
            "supports_claim": expected_text,
        } in lecture["evidence"]


def test_step_down_targets_follow_the_atomic_transmission_prerequisites() -> None:
    assert derive_step_down_target("传导时滞分析") == (
        "三道工序与传导关系",
        "T-03",
    )
    assert derive_step_down_target("异常衰减规律") == (
        "传导时滞分析",
        "T-07",
    )
    assert derive_step_down_target("责任单元定位") == (
        "月度聚合方法",
        "T-04",
    )
    assert derive_step_down_target("跨工序归因方法") == (
        "责任单元定位",
        "T-06",
    )
    assert derive_step_down_target("计划量与实际量口径") == (
        "计划量与实际量口径",
        "T-01",
    )


class FailingKnowledgeLLM(ScriptedLLM):
    def __call__(self, **request: Any) -> LLMResult:
        if "oneOf" in request["json_schema"]:
            raise RuntimeError("deliberate knowledge failure")
        return super().__call__(**request)


def test_failed_attempt_preserves_partial_trace_and_never_rewrites_it(
    tmp_path: Path,
) -> None:
    paths = RunPaths.under(tmp_path)

    record = execute_case(
        _case("direct_correct"),
        attempt=1,
        paths=paths,
        dependencies=_dependencies(FailingKnowledgeLLM()),
    )

    assert record.status == "failed"
    assert record.outcome == "system_error"
    assert record.error_type == "RuntimeError"
    assert record.error_message == "deliberate knowledge failure"
    archived_trace = tmp_path.joinpath(*Path(record.trace_path).parts)
    assert archived_trace.exists()
    before = archived_trace.read_bytes()
    assert record.message_count >= 3
    assert load_attempt_records(paths.ledger_path) == (record,)
    assert archived_trace.read_bytes() == before
    error_path = archived_trace.with_name("error.json")
    assert error_path.exists()
    error_record = json.loads(error_path.read_text(encoding="utf-8"))
    assert error_record["outcome"] == "system_error"
    assert "deliberate knowledge failure" in error_record["error_message"]


def test_successful_attempt_is_logged_as_completed(tmp_path: Path) -> None:
    record = execute_case(
        _case("direct_correct"),
        attempt=1,
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )

    assert record.status == "succeeded"
    assert record.outcome == "completed"


def test_explicit_verification_outcome_is_preserved_in_attempt_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stop_after_safe_rejection(*_args: Any, **_kwargs: Any) -> None:
        raise OutcomeError(Outcome.SAFE_REJECTED, event="sandbox_rejected")

    monkeypatch.setattr("eval.e2e_runner.run_case", stop_after_safe_rejection)

    record = execute_case(
        _case("direct_correct"),
        attempt=1,
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )

    assert record.status == "failed"
    assert record.outcome == "safe_rejected"
    error_path = (
        tmp_path.joinpath(*Path(record.trace_path).parts).parent / "error.json"
    )
    assert json.loads(error_path.read_text(encoding="utf-8"))["outcome"] == (
        "safe_rejected"
    )


def test_real_t21_attempt_archives_trace_and_preserves_safe_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def sandbox_rejection(
        agent: Any,
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return _verification_failure(agent._trace_id, "sandbox_rejected")

    monkeypatch.setattr(
        "eval.e2e_runner.VerificationAgent.answer",
        sandbox_rejection,
    )

    record = execute_case(
        _case("direct_correct"),
        attempt=1,
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )

    assert record.status == "failed"
    assert record.outcome == "safe_rejected"
    assert record.transition_sequence[-2:] == ("T11", "T21")
    trace_path = tmp_path.joinpath(*Path(record.trace_path).parts)
    messages = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    failed_product = next(
        message
        for message in reversed(messages)
        if message.get("agent") == "verification"
    )
    assert any(
        message.get("payload", {}).get("content", {}).get("transition_id")
        == "T21"
        for message in messages
    )
    assert not any(
        message.get("payload", {}).get("content", {}).get("reviewed_msg_id")
        == failed_product["msg_id"]
        for message in messages
    )


def test_unknown_verification_event_fails_closed_as_system_error_without_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown_result(agent: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _verification_failure(agent._trace_id, "unexpected_internal_event")

    monkeypatch.setattr(
        "eval.e2e_runner.VerificationAgent.answer",
        unknown_result,
    )

    record = execute_case(
        _case("direct_correct"),
        attempt=1,
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(),
    )

    assert record.status == "failed"
    assert record.outcome == "system_error"
    assert record.error_message == (
        "expected transition at S4_VERIFY: no_matching_transition"
    )
    trace_path = tmp_path.joinpath(*Path(record.trace_path).parts)
    messages = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    failed_product = next(
        message
        for message in reversed(messages)
        if message.get("agent") == "verification"
    )
    assert record.transition_sequence[-1] == "T11"
    assert "T21" not in record.transition_sequence
    assert not any(
        message.get("payload", {}).get("content", {}).get("reviewed_msg_id")
        == failed_product["msg_id"]
        for message in messages
    )


def test_attempt_trace_ids_are_unique(tmp_path: Path) -> None:
    first = run_case(_case("direct_correct"), 1, RunPaths.under(tmp_path), _dependencies())
    second = run_case(_case("direct_correct"), 2, RunPaths.under(tmp_path), _dependencies())
    assert first.trace_id == "p7-e2e-001-a01"
    assert second.trace_id == "p7-e2e-001-a02"
    assert first.trace_path != second.trace_path


def test_retry_budget_starts_after_all_preserved_historical_attempts() -> None:
    assert _new_attempt_numbers((1, 2, 3), max_new_attempts=3) == (4, 5, 6)
    assert _new_attempt_numbers((), max_new_attempts=1) == (1,)


def test_formal_run_directory_can_be_isolated_from_preflight_results(
    tmp_path: Path,
) -> None:
    paths = RunPaths.under(tmp_path, Path("eval/results/live_50"))

    assert paths.trace_dir == (tmp_path / "eval/results/live_50/traces").resolve()
    assert paths.cache_dir == (tmp_path / "eval/results/live_50/cache").resolve()
    assert paths.ledger_path == (
        tmp_path / "eval/results/live_50/run_ledger.jsonl"
    ).resolve()


class TaskContextScriptedLLM(ScriptedLLM):
    def __call__(self, **request: Any) -> LLMResult:
        required = set(request["json_schema"].get("required", []))
        if required == {"contextualized_stem", "guide_intro"}:
            self.calls.append(request)
            context = json.loads(
                str(request["user"])
                .split("[标准题面]", 1)[1]
                .split("[画像JSON]", 1)[0]
            )
            standard_stem = str(context["standard_stem"])
            return llm_result(
                {
                    "contextualized_stem": f"作为新入职生产计划员，请完成岗位核对：{standard_stem}",
                    "guide_intro": "结合计划岗位的口径核对职责完成查询。",
                },
                str(request["model"]),
            )
        return super().__call__(**request)


def test_driver_wires_shared_live_cache_into_task_contextualization(
    tmp_path: Path,
) -> None:
    llm = TaskContextScriptedLLM()

    result = run_case(
        _case("direct_correct"),
        attempt=1,
        paths=RunPaths.under(tmp_path),
        dependencies=_dependencies(llm),
    )

    observed_schemas = {
        frozenset(request["json_schema"].get("required", []))
        for request in llm.calls
    }
    assert frozenset({"contextualized_stem", "guide_intro"}) in observed_schemas

    messages = [
        json.loads(line)
        for line in result.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    task_products = [
        message
        for message in messages
        if message.get("agent") == "task" and message.get("role") == "produce"
    ]
    assert task_products
    for product in task_products:
        content = product["payload"]["content"]
        assert content["contextualize_fallback"] is False
        assert content["contextualize_fallback_reason"] is None
        assert content["guide_intro"]
        assert content["contextualized_stem"] != content["standard_stem"]
        assert product["token_usage"]["total_tokens"] > 0
        assert product["latency_ms"] > 0
