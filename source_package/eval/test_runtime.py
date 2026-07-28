from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from agents.diagnosis_agent import DiagnosisAgent
from agents.rebuttal_generator import RebuttalGenerator
from agents.review_agent import ReviewAgent
from agents.task_agent import TaskAgent
from agents.verification_agent import VerificationAgent
from orchestrator import runtime
from orchestrator.agents_stub import (
    DiagnosisStub,
    ReviewStub,
    TaskStub,
    VerificationStub,
    build_stubs,
    profile_loaded_draft,
    student_sql_draft,
)
from orchestrator.bus import MessageBus
from orchestrator.engine import OrchestratorEngine
from orchestrator.llm import LLMResult, TokenUsage
from orchestrator.runtime import build_verification_agent
from orchestrator.transitions import State


def assert_transition(result: Any, transition_id: str) -> dict[str, Any]:
    assert result.bus_result.accepted, result.bus_result.errors
    assert result.transitioned, result.reason
    assert result.transition is not None
    assert result.transition.transition_id == transition_id
    return result.bus_result.message


def drive_to_s4(tmp_path: Path, trace_id: str) -> OrchestratorEngine:
    bus = MessageBus(tmp_path)
    engine = OrchestratorEngine(bus, trace_id, "cs_student")
    stubs = build_stubs(trace_id)
    assert_transition(engine.send(profile_loaded_draft(trace_id)), "T01")
    assert_transition(engine.send(stubs.diagnosis.profile_assessment()), "T02")
    lecture = assert_transition(engine.send(stubs.knowledge.lecture()), "T03")
    assert_transition(
        engine.send(
            stubs.review.verdict("approve", "lecture_note", lecture["msg_id"])
        ),
        "T04",
    )
    task = assert_transition(engine.send(stubs.task.task("quiz_set")), "T09")
    assert_transition(
        engine.send(stubs.review.verdict("approve", "quiz_set", task["msg_id"])),
        "T10",
    )
    assert_transition(
        engine.send(student_sql_draft(trace_id, "H2601五月预处理完成率")),
        "T11",
    )
    return engine


def test_t11_accepts_natural_language_question(tmp_path: Path) -> None:
    draft = student_sql_draft(
        "trace-question", "H2601五月预处理完成率"
    )
    bus_result = MessageBus(tmp_path).send(draft)

    assert draft["payload"]["content"]["question"] == "H2601五月预处理完成率"
    assert bus_result.accepted, bus_result.errors


def test_student_sql_draft_without_question_remains_backward_compatible() -> None:
    draft = student_sql_draft("trace-legacy")

    assert draft["payload"]["content"] == {
        "event": "student_sql_submitted",
        "source": "student",
    }


def test_production_factory_returns_real_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warmup_calls: list[str] = []
    monkeypatch.setenv("REF_READER_PASSWORD", "test-only")
    monkeypatch.setattr(
        runtime,
        "warm_default_client",
        lambda: warmup_calls.append("warm"),
        raising=False,
    )

    assert isinstance(build_verification_agent("trace-live"), VerificationAgent)
    assert warmup_calls == ["warm"]


def test_stub_factory_remains_deterministic() -> None:
    stubs = build_stubs("trace-stub")

    assert isinstance(stubs.diagnosis, DiagnosisStub)
    assert isinstance(stubs.task, TaskStub)
    assert isinstance(stubs.verification, VerificationStub)
    assert isinstance(stubs.review, ReviewStub)


def test_p4_p5_factories_warm_and_inject_llm_into_all_model_backed_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warmup_calls: list[str] = []
    fake_llm = lambda **_: None
    monkeypatch.setattr(
        runtime,
        "warm_default_client",
        lambda: warmup_calls.append("warm"),
        raising=False,
    )
    monkeypatch.setattr(runtime, "call_llm", fake_llm)

    diagnosis = runtime.build_diagnosis_agent("trace-diagnosis-factory")
    task = runtime.build_task_agent("trace-task-factory")

    assert isinstance(diagnosis, DiagnosisAgent)
    assert isinstance(task, TaskAgent)
    assert diagnosis._llm_call is fake_llm
    assert task._llm_call is fake_llm
    assert warmup_calls == ["warm", "warm"]

    review = runtime.build_review_agent("trace-review-factory")
    rebuttal = runtime.build_rebuttal_generator("trace-rebuttal-factory")

    assert isinstance(review, ReviewAgent)
    assert isinstance(rebuttal, RebuttalGenerator)
    assert warmup_calls == ["warm", "warm", "warm", "warm"]


def test_p4_p5_factory_trace_ids_flow_through_real_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_llm(**_: Any) -> LLMResult:
        return LLMResult(
            data={"narrative": "答对3/5题。", "suggestions": []},
            model="qwen3-32b",
            latency_ms=1,
            token_usage=TokenUsage(2, 1, 3),
            attempts=1,
        )

    monkeypatch.setattr(runtime, "warm_default_client", lambda: None, raising=False)
    monkeypatch.setattr(runtime, "call_llm", fake_llm)
    diagnosis = runtime.build_diagnosis_agent("trace-diagnosis-factory")
    task = runtime.build_task_agent("trace-task-factory")
    review = runtime.build_review_agent("trace-review-factory")
    rebuttal = runtime.build_rebuttal_generator("trace-rebuttal-factory")
    answers = {
        "PT-1": "B",
        "PT-2": "B",
        "PT-3": "C",
        "PT-4": "B",
        "PT-5": "C",
    }
    product = {
        "msg_id": "trace-source-001",
        "trace_id": "trace-source",
        "step": 1,
        "agent": "knowledge",
        "role": "produce",
        "payload": {
            "type": "lecture_note",
            "content": {
                "event": "product_ready",
                "lecture_md": "完成率为62.36%。",
                "difficulty": "basic",
            },
        },
        "evidence": [],
        "claims": [],
        "timestamp": "2026-07-15T14:00:00+08:00",
    }
    original_verdict = {
        "msg_id": "trace-source-002",
        "trace_id": "trace-source",
        "step": 2,
        "agent": "review",
        "role": "verdict",
        "payload": {
            "type": "review_verdict",
            "content": {
                "event": "review_complete",
                "reviewed_payload_type": "lecture_note",
                "reviewed_msg_id": product["msg_id"],
            },
        },
        "evidence": [{"kind": "review_rule", "ref": "R-04", "quote": "漏报"}],
        "verdict": {
            "decision": "reject",
            "rule_hits": [
                {
                    "rule_id": "R-04",
                    "reason": "漏报确定性结论",
                    "evidence_ref": product["msg_id"],
                }
            ],
            "difficulty_action": "none",
        },
        "timestamp": "2026-07-15T14:00:01+08:00",
    }
    rebuttal_product = deepcopy(product)
    rebuttal_product["trace_id"] = "trace-rebuttal-factory"
    rebuttal_product["msg_id"] = "trace-rebuttal-factory-001"
    rebuttal_verdict = deepcopy(original_verdict)
    rebuttal_verdict["trace_id"] = "trace-rebuttal-factory"
    rebuttal_verdict["msg_id"] = "trace-rebuttal-factory-002"
    rebuttal_verdict["payload"]["content"]["reviewed_msg_id"] = rebuttal_product[
        "msg_id"
    ]

    drafts = [
        diagnosis.assess("planner_new", answers),
        task.generate("T-01"),
        review.review(product),
        rebuttal.generate(rebuttal_product, rebuttal_verdict),
    ]

    assert [draft["trace_id"] for draft in drafts] == [
        "trace-diagnosis-factory",
        "trace-task-factory",
        "trace-review-factory",
        "trace-rebuttal-factory",
    ]


@pytest.mark.parametrize(
    ("event", "expected_outcome"),
    (
        ("refuse_out_of_scope", "safe_rejected"),
        ("sandbox_rejected", "safe_rejected"),
        ("template_authority_rejected", "safe_rejected"),
        ("query_empty", "safe_rejected"),
        ("query_timeout", "external_unavailable"),
        ("query_failed", "external_unavailable"),
    ),
)
def test_failure_events_are_audited_and_return_to_student_retry(
    tmp_path: Path,
    event: str,
    expected_outcome: str,
) -> None:
    trace_id = f"trace-failure-{event.replace('_', '-')}"
    engine = drive_to_s4(tmp_path, trace_id)
    evidence = (
        [{"kind": "review_rule", "ref": "S-01", "quote": "blocked"}]
        if event == "sandbox_rejected"
        else []
    )
    draft = {
        "trace_id": trace_id,
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {
                "event": event,
                "question": "测试问题",
                "family": "OUT_OF_SCOPE" if event == "refuse_out_of_scope" else "Q1",
                "student_message": "本次查询终止。",
                "llm_latency_ms": 10,
            },
        },
        "evidence": evidence,
        "claims": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": "qwen3-235b-a22b",
        "latency_ms": 12,
        "token_usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
    }

    result = engine.send(draft)

    assert result.bus_result.accepted, result.bus_result.errors
    assert result.transitioned is True
    assert result.reason is None
    assert result.transition is not None
    assert result.transition.transition_id == "T21"
    assert engine.state is State.S7_STUDENT
    transition = json.loads(
        (tmp_path / f"{trace_id}.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert transition["payload"]["content"]["outcome"] == expected_outcome
