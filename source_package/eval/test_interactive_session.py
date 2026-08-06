from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agents.knowledge_agent import KnowledgeAgent
from agents.misconception_relations import RelationIntegrityError
from agents.rebuttal_generator import RebuttalGenerator
from agents.sandbox import (
    QueryExecutionError,
    QueryResult,
    QueryTimeoutError,
    validate_and_rewrite,
)
from agents.review_agent import ReviewAgent
from agents.task_agent import TaskAgent, load_task_catalog
from eval.test_demo_session import ScriptedLLM, _review_reject_fixture
from orchestrator.demo_session import DemoSessionError
from orchestrator.interactive_session import (
    InteractiveSessionError,
    InteractiveSessionManager,
    build_http_server,
    main,
)
from orchestrator.llm import LLMCallError, LLMResult, TokenUsage
from orchestrator.review_flow import ReviewFlowInterrupted, ReviewFlowTerminal


SYSTEM_ERROR_COPY = "内容生成服务暂时不可用，本次学习已安全结束，请稍后重新开始。"


class RecordingExecutor:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, executed_sql: str) -> QueryResult:
        self.sql.append(executed_sql)
        return QueryResult(
            columns=("plan_qty", "actual_qty"),
            rows=({"plan_qty": "1855.06", "actual_qty": "1156.87"},),
            elapsed_ms=3,
        )


class CatalogExecutor:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self._results: dict[str, QueryResult] = {}
        for entry in load_task_catalog().templates.values():
            decision = validate_and_rewrite(entry.standard_sql)
            assert decision.allowed
            executed_sql = str(decision.executed_sql)
            first_row = entry.expected_rows[0]
            self._results.setdefault(
                executed_sql,
                QueryResult(
                    columns=tuple(first_row),
                    rows=tuple(dict(row) for row in entry.expected_rows),
                    elapsed_ms=3,
                ),
            )

    def execute(self, executed_sql: str) -> QueryResult:
        self.sql.append(executed_sql)
        try:
            return self._results[executed_sql]
        except KeyError as exc:
            raise AssertionError(
                "the deterministic catalog executor received an unknown query"
            ) from exc


class EmptyExecutor(RecordingExecutor):
    def execute(self, executed_sql: str) -> QueryResult:
        self.sql.append(executed_sql)
        return QueryResult(
            columns=("plan_qty", "actual_qty"),
            rows=({"plan_qty": None, "actual_qty": None},),
            elapsed_ms=2,
        )


class TimeoutExecutor(RecordingExecutor):
    def execute(self, executed_sql: str) -> QueryResult:
        self.sql.append(executed_sql)
        raise QueryTimeoutError("query exceeded the five-second limit")


class FailingExecutor(RecordingExecutor):
    def execute(self, executed_sql: str) -> QueryResult:
        self.sql.append(executed_sql)
        raise QueryExecutionError("database connection was lost")


def forbidden_llm(**_: Any) -> Any:
    raise AssertionError("creating a session must not call the LLM")


class FollowUpLLM:
    def __init__(self, *responses: dict[str, str]) -> None:
        self.responses = [dict(response) for response in responses]
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected follow-up model call")
        return LLMResult(
            data=self.responses.pop(0),
            model="fixed-follow-up-stub",
            latency_ms=4,
            token_usage=TokenUsage(10, 6, 16),
            attempts=1,
        )


def follow_up_response(
    assessment: str,
    question: str,
    target: str = "M-01",
    next_target: str | None = None,
) -> dict[str, str]:
    return {
        "assessment": assessment,
        "diagnosed_misconception": target,
        "next_target_misconception": (
            next_target
            if next_target is not None
            else target if question else "NO_NEXT_TARGET"
        ),
        "question": question,
    }


def mastered_follow_up() -> FollowUpLLM:
    return FollowUpLLM(
        follow_up_response(
            "mastered",
            "再核对一次，真实完成情况应由哪一类数据说明？",
        ),
        follow_up_response("mastered", ""),
    )


def support_then_mastered_follow_up() -> FollowUpLLM:
    return FollowUpLLM(
        follow_up_response(
            "needs_support",
            "对照计划量与实际完成量，哪一个能说明已经做了多少？",
        ),
        follow_up_response("mastered", ""),
    )


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def start_sql_session(
    tmp_path: Path,
    executor: Any,
    *,
    follow_up_llm: FollowUpLLM | None = None,
) -> tuple[InteractiveSessionManager, str]:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up_llm,
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    manager.advance(session_id)
    return manager, session_id


def test_create_session_uses_t01_and_waits_for_real_pretest_input(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=forbidden_llm,
        executor_factory=RecordingExecutor,
    )

    created = manager.create_session("planner_new")
    state = manager.get_state(created["session_id"])

    assert created["session_id"] == state["session_id"]
    assert state["state"] == "S1_DIAGNOSIS"
    assert state["awaiting"] == "pretest"
    assert state["profile"]["profile_id"] == "planner_new"
    messages = read_trace(Path(state["trace_path"]))
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in messages
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions == ["T01"]


def test_pretest_hides_answer_key_and_scores_submitted_choices(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=forbidden_llm,
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("planner_new")["session_id"]

    questions = manager.get_pretest(session_id)
    outcome = manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    assert [question["question_id"] for question in questions] == [
        "PT-1",
        "PT-2",
        "PT-3",
        "PT-4",
        "PT-5",
    ]
    assert all("answer" not in question for question in questions)
    assert all("misconceptions" not in question for question in questions)
    assert outcome["artifact"]["payload"]["content"]["pretest_score"] == {
        "correct": 0,
        "total": 5,
        "rate": 0.0,
    }
    assert outcome["state"] == "S2_KNOWLEDGE"
    assert outcome["awaiting"] == "advance"
    contract = outcome["learning_contract"]
    assert contract["contract_id"].startswith("lc-")
    assert contract["learner"]["profile_id"] == "planner_new"
    assert contract["domain_id"] == "production_progress"
    contract_events = [
        message["payload"]["content"]
        for message in outcome["messages"]
        if message["payload"]["content"].get("event")
        == "learning_contract_ready"
    ]
    assert [event["learning_contract"]["contract_id"] for event in contract_events] == [
        contract["contract_id"]
    ]


def test_advance_returns_reviewed_lecture_then_reviewed_sql_task(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    lecture = manager.advance(session_id)
    task = manager.advance(session_id)

    assert lecture["artifact"]["payload"]["type"] == "lecture_note"
    assert lecture["state"] == "S3_TASK"
    assert task["artifact"]["payload"]["type"] == "quiz_set"
    assert task["artifact"]["payload"]["content"]["family"] == "Q2"
    assert task["state"] == "S7_STUDENT"
    assert task["awaiting"] == "sql"
    evidence_bundle = lecture["evidence_bundle"]
    assert evidence_bundle["bundle_id"].startswith("eb-")
    assert set(evidence_bundle["sources"]) == {
        "knowledge",
        "business_data",
        "pedagogy",
    }
    assert (
        lecture["artifact"]["payload"]["content"]["evidence_bundle_ref"]
        == evidence_bundle["bundle_id"]
    )
    assert (
        task["artifact"]["payload"]["content"]["evidence_bundle_ref"]
        == evidence_bundle["bundle_id"]
    )
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in task["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions == ["T01", "T02", "T03", "T04", "T09", "T10"]


def test_lecture_stage_prefetches_task_on_parallel_branch_without_transition(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    lecture = manager.advance(session_id)
    evidence_events = [
        event
        for event in manager.get_agent_events(session_id)
        if event["activity"] == "parallel_evidence_retrieval"
    ]
    evidence_joined = next(
        event
        for event in evidence_events
        if event["details"].get("aggregation") == "deterministic"
    )
    resource_events = [
        event
        for event in manager.get_agent_events(session_id)
        if event["activity"] == "parallel_resource_generation"
    ]
    task_events = [event for event in resource_events if event["agent"] == "task"]
    assessment_events = [
        event for event in resource_events if event["agent"] == "assessment"
    ]
    joined = next(
        event
        for event in resource_events
        if event["details"].get("aggregation") == "deterministic"
    )
    parallel_review_events = [
        event
        for event in manager.get_agent_events(session_id)
        if event["agent"] == "review"
        and event["activity"] == "parallel_quality_review"
    ]
    specialist_events = [
        event
        for event in manager.get_agent_events(session_id)
        if event["agent"] in {
            "evidence_review",
            "pedagogy_review",
            "data_safety_review",
            "readability_review",
        }
        and event["activity"] == "specialist_quality_review"
    ]

    assert evidence_joined["details"]["stage_id"] == "evidence-bundle"
    assert evidence_joined["details"]["fan_out"] == 3
    assert [
        branch["branch_id"] for branch in evidence_joined["details"]["branches"]
    ] == ["knowledge", "business_data", "pedagogy"]
    assert all(
        branch["status"] == "succeeded"
        for branch in evidence_joined["details"]["branches"]
    )
    assert {
        event["agent"]
        for event in evidence_events
        if event["status"] == "working"
    } == {"knowledge", "verification", "diagnosis"}
    assert [event["status"] for event in task_events] == [
        "collaborating",
        "working",
        "waiting",
    ]
    assert [event["status"] for event in assessment_events] == [
        "collaborating",
        "working",
        "waiting",
    ]
    assert joined["details"]["stage_id"] == "resource-generation"
    assert joined["details"]["fan_out"] == 3
    assert joined["details"]["parallel_elapsed_ms"] >= 0
    assert [
        branch["branch_id"] for branch in joined["details"]["branches"]
    ] == ["knowledge", "practice", "assessment"]
    assert all(
        branch["status"] == "succeeded"
        for branch in joined["details"]["branches"]
    )
    assert [event["status"] for event in parallel_review_events] == [
        "collaborating",
        "reviewing",
    ]
    assert {
        (event["agent"], event["status"])
        for event in specialist_events
    } == {
        ("evidence_review", "working"),
        ("evidence_review", "done"),
        ("pedagogy_review", "working"),
        ("pedagogy_review", "done"),
        ("data_safety_review", "working"),
        ("data_safety_review", "done"),
        ("readability_review", "working"),
        ("readability_review", "done"),
    }
    assert lecture["state"] == "S3_TASK"
    assert lecture["artifact"]["payload"]["type"] == "lecture_note"
    bundle_id = lecture["evidence_bundle"]["bundle_id"]
    assert lecture["artifact"]["payload"]["content"]["evidence_bundle_ref"] == bundle_id
    resource_bundle = lecture["resource_bundle"]
    assert resource_bundle["bundle_id"].startswith("rb-")
    assert resource_bundle["evidence_bundle_id"] == bundle_id
    assert [branch["branch_id"] for branch in resource_bundle["branches"]] == [
        "knowledge",
        "practice",
        "assessment",
    ]
    assert all(
        branch["status"] == "ready"
        for branch in resource_bundle["branches"]
    )
    coordination = lecture["coordination_evidence"]
    assert coordination["measurement_basis"] == "same_run_branch_wall_time_sum"
    assert coordination["summary"]["completed_parallel_stages"] == 3
    assert coordination["summary"]["max_fan_out"] == 4
    assert coordination["summary"]["branch_success_rate"] == 1.0
    assert {stage["stage_id"] for stage in coordination["stages"]} == {
        "evidence-bundle",
        "resource-generation",
        "quality-review-axes",
    }
    evidence_control = next(
        message
        for message in lecture["messages"]
        if message["payload"]["content"].get("event") == "evidence_bundle_ready"
    )
    assert evidence_control["payload"]["content"]["evidence_bundle"]["bundle_id"] == bundle_id
    audited_specialist_verdict = next(
        message
        for message in lecture["messages"]
        if message["payload"]["type"] == "review_verdict"
        and message["payload"]["content"].get("specialist_reviews")
    )
    verdict_content = audited_specialist_verdict["payload"]["content"]
    assert [
        item["agent"] for item in verdict_content["specialist_reviews"]
    ] == [
        "evidence_review",
        "pedagogy_review",
        "data_safety_review",
        "readability_review",
    ]
    assert verdict_content["arbitration"]["mode"] == "deterministic_rule_table"
    assert all(
        message["payload"]["type"] not in {"quiz_set", "practice_guide"}
        for message in lecture["messages"]
    )


def test_required_evidence_branch_failure_blocks_resource_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_business_evidence(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("private provider detail")

    monkeypatch.setattr(TaskAgent, "diagnosis_evidence", fail_business_evidence)
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    with pytest.raises(InteractiveSessionError) as raised:
        manager.advance(session_id)

    assert "provider detail" not in str(raised.value)
    state = manager.get_state(session_id)
    assert state["state"] == "S2_KNOWLEDGE"
    assert state["evidence_bundle"] is None
    evidence_events = [
        event
        for event in manager.get_agent_events(session_id)
        if event["activity"] == "parallel_evidence_retrieval"
    ]
    joined = next(
        event
        for event in evidence_events
        if event["details"].get("aggregation") == "deterministic"
    )
    failed_branch = next(
        branch
        for branch in joined["details"]["branches"]
        if branch["branch_id"] == "business_data"
    )
    assert joined["details"]["succeeded"] is False
    assert failed_branch["required"] is True
    assert failed_branch["status"] == "failed"
    assert not any(
        event["activity"] == "parallel_resource_generation"
        for event in manager.get_agent_events(session_id)
    )
    assert "provider detail" not in json.dumps(evidence_events, ensure_ascii=False)


def test_optional_resource_branch_failure_retries_on_the_main_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = TaskAgent.generate
    calls = 0

    def fail_prefetch_once(
        self: TaskAgent,
        template_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("prefetch provider detail")
        return original(self, template_id, **kwargs)

    monkeypatch.setattr(TaskAgent, "generate", fail_prefetch_once)
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    lecture = manager.advance(session_id)
    task = manager.advance(session_id)

    assert lecture["state"] == "S3_TASK"
    assert task["state"] == "S7_STUDENT"
    assert calls == 2
    resource_events = [
        event
        for event in manager.get_agent_events(session_id)
        if event["activity"] == "parallel_resource_generation"
    ]
    failed = next(event for event in resource_events if event["status"] == "blocked")
    joined = next(
        event
        for event in resource_events
        if event["details"].get("aggregation") == "deterministic"
    )
    task_branch = next(
        branch
        for branch in joined["details"]["branches"]
        if branch["branch_id"] == "practice"
    )
    assert failed["agent"] == "task"
    assert task_branch["required"] is False
    assert task_branch["status"] == "failed"
    assert "provider detail" not in json.dumps(resource_events, ensure_ascii=False)


def test_advance_routes_review_reject_through_shared_debate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedLLM()
    original_review = ReviewAgent.review
    fixture_used = False

    def reject_first_product(
        self: ReviewAgent,
        product: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal fixture_used
        if not fixture_used:
            fixture_used = True
            return _review_reject_fixture(product, "R-02")
        return original_review(self, product, **kwargs)

    monkeypatch.setattr(ReviewAgent, "review", reject_first_product)
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=llm,
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    lecture = manager.advance(session_id)

    assert fixture_used
    assert lecture["artifact"]["payload"]["type"] == "lecture_note"
    assert lecture["state"] == "S3_TASK"
    assert lecture["awaiting"] == "advance"
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in lecture["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions == ["T01", "T02", "T03", "T05", "T06"]
    roles = [message["role"] for message in lecture["messages"]]
    assert "rebuttal" in roles
    assert "re_verdict" in roles
    agent_events = manager.get_agent_events(session_id)
    targeted_events = [
        event
        for event in agent_events
        if event["activity"] == "targeted_dispute_review"
    ]
    assert [(event["agent"], event["status"]) for event in targeted_events] == [
        ("evidence_review", "debating"),
        ("evidence_review", "done"),
    ]
    debate_event = next(
        event
        for event in agent_events
        if event["agent"] == "review"
        and event["activity"] == "bounded_debate"
        and event["status"] == "debating"
    )
    assert debate_event["details"]["dispute_route"] == "targeted_debate"
    assert debate_event["details"]["rule_ids"] == ["R-02"]
    assert debate_event["details"]["specialist_agents"] == ["evidence_review"]
    learner_copy = json.dumps(
        {
            "lecture": lecture["artifact"]["payload"]["content"].get("lecture_md"),
            "interaction": lecture["interaction"],
            "outcome": lecture["outcome"],
        },
        ensure_ascii=False,
    )
    assert not any(
        marker in learner_copy
        for marker in (
            "T05",
            "S6_DEBATE",
            "msg_id",
            "rule_hits",
            "rebuttal",
            "verdict",
            "injected_for_demo",
        )
    )


@pytest.mark.parametrize("failure_stage", ("rebuttal", "re_review"))
def test_review_follow_up_failure_closes_session_in_learning_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    def reject_every_product(
        self: ReviewAgent,
        product: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return _review_reject_fixture(product, "R-01")

    def fail_follow_up(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated review follow-up failure")

    monkeypatch.setattr(ReviewAgent, "review", reject_every_product)
    if failure_stage == "rebuttal":
        monkeypatch.setattr(RebuttalGenerator, "generate", fail_follow_up)
    else:
        monkeypatch.setattr(ReviewAgent, "re_review", fail_follow_up)
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    stopped = manager.advance(session_id)

    assert stopped["state"] == "S6_DEBATE"
    assert stopped["awaiting"] == "done"
    assert stopped["outcome"] == "system_error"
    assert stopped["interaction"] == {
        "kind": "review_notice",
        "message": "内容生成服务暂时不可用，本次学习已安全结束，请稍后重新开始。",
    }
    assert stopped["termination"]["reason_code"] == "model_unavailable"
    assert stopped["termination"]["review_attempts"] >= 1
    assert stopped["termination"]["review_limit"] == 4
    learner_copy = json.dumps(stopped["interaction"], ensure_ascii=False)
    assert not any(
        marker in learner_copy
        for marker in (
            "S6",
            "T05",
            "rebuttal",
            "re_review",
            "rule_hits",
            "verdict",
            "system_error",
        )
    )
    before_retry = stopped["messages"]
    retried = manager.advance(session_id)
    assert retried["awaiting"] == "done"
    assert retried["outcome"] == "system_error"
    assert manager.get_state(session_id)["messages"] == before_retry


def test_review_retry_exhaustion_returns_canonical_template_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_every_product(
        self: ReviewAgent,
        product: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return _review_reject_fixture(product, "R-01")

    monkeypatch.setattr(ReviewAgent, "review", reject_every_product)
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    fallback = manager.advance(session_id)

    assert fallback["state"] == "S3_TASK"
    assert fallback["awaiting"] == "done"
    assert fallback["outcome"] == "safe_rejected"
    assert fallback["interaction"] == {
        "kind": "review_notice",
        "message": "这份内容多次未通过专业审核，本次学习已安全结束。",
    }
    assert fallback["termination"] == {
        "reason_code": "review_exhausted",
        "review_attempts": 3,
        "review_limit": 4,
    }
    assert fallback["artifact"]["payload"]["type"] == "lecture_note"
    assert (
        fallback["artifact"]["payload"]["content"]["generated_by"]
        == "template_fallback"
    )
    fallback_id = fallback["artifact"]["msg_id"]
    assert any(
        message["msg_id"] == fallback_id for message in fallback["messages"]
    )
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in fallback["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions == [
        "T01",
        "T02",
        "T03",
        "T05",
        "T07",
        "T03",
        "T05",
        "T07",
        "T03",
        "T08",
    ]
    with pytest.raises(InteractiveSessionError):
        manager.advance(session_id)
    assert manager.get_state(session_id)["messages"] == fallback["messages"]


def test_review_stop_reports_evidence_insufficient_separately(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    session = manager._get_session(session_id)
    artifact = {
        "verdict": {
            "decision": "reject",
            "rule_hits": [{"rule_id": "R-02"}],
        }
    }

    manager._finish_review_stop(
        session,
        artifact,
        "refuse",
        review_attempts=2,
    )

    assert manager.get_state(session_id)["termination"] == {
        "reason_code": "evidence_insufficient",
        "review_attempts": 2,
        "review_limit": 4,
    }


@pytest.mark.parametrize(
    ("fallback_action", "student_message"),
    (
        (
            "human_review",
            "这份内容需要进一步确认，本次学习已暂停。",
        ),
        (
            "refuse",
            "这份内容未通过专业审核，本次学习已安全结束。",
        ),
    ),
)
def test_terminal_review_fallback_closes_interactive_session_in_learning_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fallback_action: str,
    student_message: str,
) -> None:
    original_generate = KnowledgeAgent.generate

    def generate_with_terminal_fallback(
        self: KnowledgeAgent,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        draft = original_generate(self, *args, **kwargs)
        draft["retry"] = {"fallback": fallback_action}
        return draft

    def reject_every_product(
        self: ReviewAgent,
        product: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return _review_reject_fixture(product, "R-01")

    monkeypatch.setattr(KnowledgeAgent, "generate", generate_with_terminal_fallback)
    monkeypatch.setattr(ReviewAgent, "review", reject_every_product)
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    stopped = manager.advance(session_id)

    assert stopped["state"] == "S_FAIL"
    assert stopped["awaiting"] == "done"
    assert stopped["outcome"] == "safe_rejected"
    assert stopped["interaction"] == {
        "kind": "review_notice",
        "message": student_message,
    }
    assert stopped["artifact"]["payload"]["content"]["fallback_action"] == fallback_action
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in stopped["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-1] == "T08"
    with pytest.raises(InteractiveSessionError):
        manager.advance(session_id)
    assert manager.get_state(session_id)["messages"] == stopped["messages"]


@pytest.mark.parametrize(
    (
        "profile_id",
        "answers",
        "expected_knowledge_point",
        "expected_difficulty",
        "expected_template_id",
    ),
    (
        (
            "planner_new",
            {f"PT-{index}": "D" for index in range(1, 6)},
            "三道工序与传导关系",
            "basic",
            "T-03",
        ),
        (
            "planner_new",
            {"PT-1": "B", "PT-2": "B", "PT-3": "C", "PT-4": "B", "PT-5": "C"},
            "三道工序与传导关系",
            "applied",
            "T-03-A",
        ),
        (
            "craft_engineer",
            {"PT-1": "B", "PT-2": "B", "PT-3": "C", "PT-4": "B", "PT-5": "C"},
            "完成率计算",
            "advanced",
            "T-02-B",
        ),
        (
            "line_leader",
            {f"PT-{index}": "D" for index in range(1, 6)},
            "计划量与实际量口径",
            "basic",
            "T-01",
        ),
    ),
)
def test_diagnosis_result_routes_the_first_interactive_task(
    tmp_path: Path,
    profile_id: str,
    answers: dict[str, str],
    expected_knowledge_point: str,
    expected_difficulty: str,
    expected_template_id: str,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session(profile_id)["session_id"]

    diagnosis = manager.submit_pretest(session_id, answers)
    manager.advance(session_id)
    task = manager.advance(session_id)

    diagnosis_content = diagnosis["artifact"]["payload"]["content"]
    task_content = task["artifact"]["payload"]["content"]
    assert diagnosis_content["blind_spots"][0] == expected_knowledge_point
    assert diagnosis_content["difficulty"] == expected_difficulty
    assert task_content["template_id"] == expected_template_id
    assert task_content["knowledge_point"] == expected_knowledge_point
    assert task_content["difficulty"] == expected_difficulty
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in task["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions == ["T01", "T02", "T03", "T04", "T09", "T10"]


def test_overlapping_advance_requests_share_the_completed_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    original = manager._produce_reviewed_product
    entered = Event()
    release = Event()

    def delayed_review(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_produce_reviewed_product", delayed_review)
    results: list[dict[str, Any]] = []
    errors: list[Exception] = []

    def run_advance() -> None:
        try:
            results.append(manager.advance(session_id))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = Thread(target=run_advance)
    second = Thread(target=run_advance)
    first.start()
    assert entered.wait(timeout=3)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    assert {result["state"] for result in results} == {"S3_TASK"}
    transition_sequences = {
        tuple(
            message["payload"]["content"]["transition_id"]
            for message in result["messages"]
            if message["payload"]["content"].get("transition_id")
        )
        for result in results
    }
    assert transition_sequences == {("T01", "T02", "T03", "T04")}


def test_non_q2_diagnosis_route_preserves_the_sandbox_retry_transition(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    task = manager.advance(session_id)

    rejected = manager.submit_sql(
        session_id,
        "DELETE FROM fact_production_progress",
    )

    assert task["artifact"]["payload"]["content"]["template_id"] == "T-03"
    assert rejected["state"] == "S7_STUDENT"
    assert rejected["awaiting"] == "sql"
    assert rejected["outcome"] == "safe_rejected"
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in rejected["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-2:] == ["T11", "T21"]


def test_diagnosis_routing_failure_uses_learning_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)

    def reject_route(*_: Any, **__: Any) -> dict[str, Any]:
        raise ValueError("unsupported diagnostic difficulty: expert")

    monkeypatch.setattr(
        "agents.task_agent.TaskAgent.generate",
        reject_route,
    )
    before = manager.get_state(session_id)

    with pytest.raises(InteractiveSessionError) as raised:
        manager.advance(session_id)

    message = str(raised.value)
    assert message == "暂时无法为你匹配合适的训练任务，请稍后重试。"
    assert not any(
        forbidden in message
        for forbidden in (
            "diagnostic",
            "difficulty",
            "template",
            "route",
            "T-",
            "S3",
        )
    )
    after = manager.get_state(session_id)
    assert after["state"] == before["state"] == "S3_TASK"
    assert after["awaiting"] == before["awaiting"] == "advance"
    assert after["messages"] == before["messages"]

    monkeypatch.undo()
    retry = manager.advance(session_id)
    assert retry["artifact"]["payload"]["content"]["template_id"] == "T-03"
    assert retry["state"] == "S7_STUDENT"


def test_submit_sql_records_sandbox_rejection_and_returns_to_student_state(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    manager, session_id = start_sql_session(tmp_path, executor)

    rejected = manager.submit_sql(
        session_id,
        "DELETE FROM fact_production_progress",
    )

    content = rejected["artifact"]["payload"]["content"]
    assert content["event"] == "sandbox_rejected"
    assert content["rule_id"] == "S-01"
    assert "只允许查询" in content["student_message"]
    assert rejected["artifact"]["evidence"] == [
        {
            "kind": "review_rule",
            "ref": "S-01",
            "quote": "the AST root must be Select",
        }
    ]
    assert rejected["state"] == "S7_STUDENT"
    assert rejected["awaiting"] == "sql"
    assert rejected["outcome"] == "safe_rejected"
    assert executor.sql == []
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in rejected["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-2:] == ["T11", "T21"]


def test_submit_sql_executes_student_sql_without_text2sql_and_takes_t11_to_t13(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    llm = ScriptedLLM()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=llm,
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    manager.advance(session_id)
    submitted_sql = load_task_catalog().templates["T-01"].standard_sql

    outcome = manager.submit_sql(session_id, submitted_sql)

    content = outcome["artifact"]["payload"]["content"]
    assert content["event"] == "query_completed"
    assert content["generated_sql"] == submitted_sql
    assert content["sql_source"] == "student"
    assert content["rows"] == [
        {"plan_qty": "1855.06", "actual_qty": "1156.87"}
    ]
    assert len(executor.sql) == 1
    assert executor.sql[0].endswith("LIMIT 200")
    assert "FROM fact_production_progress" in executor.sql[0]
    assert outcome["state"] == "S9_PATH_UPDATE"
    assert outcome["awaiting"] == "advance"
    assert not any(
        set(call["json_schema"].get("required", []))
        in ({"family"}, {"sql", "family", "explanation"})
        for call in llm.calls
    )
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in outcome["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-3:] == ["T11", "T12", "T13"]


def test_submit_sql_keeps_empty_results_out_of_the_state_machine(
    tmp_path: Path,
) -> None:
    executor = EmptyExecutor()
    manager, session_id = start_sql_session(tmp_path, executor)

    outcome = manager.submit_sql(
        session_id,
        load_task_catalog().templates["T-01"].standard_sql,
    )

    content = outcome["artifact"]["payload"]["content"]
    assert content["event"] == "query_empty"
    assert content["student_message"] == "查询无数据，请检查查询条件。"
    assert "rule_id" not in content
    assert outcome["state"] == "S7_STUDENT"
    assert outcome["awaiting"] == "sql"
    assert outcome["outcome"] == "safe_rejected"
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in outcome["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-2:] == ["T11", "T21"]


def test_submit_sql_returns_a_teaching_message_on_read_only_timeout(
    tmp_path: Path,
) -> None:
    executor = TimeoutExecutor()
    manager, session_id = start_sql_session(tmp_path, executor)

    outcome = manager.submit_sql(
        session_id,
        load_task_catalog().templates["T-01"].standard_sql,
    )

    content = outcome["artifact"]["payload"]["content"]
    assert content["event"] == "query_timeout"
    assert content["student_message"] == "查询超时，请缩小查询范围。"
    assert outcome["state"] == "S7_STUDENT"
    assert outcome["awaiting"] == "sql"
    assert outcome["outcome"] == "external_unavailable"
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in outcome["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-2:] == ["T11", "T21"]


def test_submit_sql_classifies_query_execution_failure_and_allows_retry(
    tmp_path: Path,
) -> None:
    manager, session_id = start_sql_session(tmp_path, FailingExecutor())

    failed = manager.submit_sql(
        session_id,
        load_task_catalog().templates["T-01"].standard_sql,
    )

    assert failed["artifact"]["payload"]["content"]["event"] == "query_failed"
    assert failed["state"] == "S7_STUDENT"
    assert failed["awaiting"] == "sql"
    assert failed["outcome"] == "external_unavailable"
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in failed["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-2:] == ["T11", "T21"]


def test_submit_sql_can_succeed_after_a_safe_rejection(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    manager, session_id = start_sql_session(tmp_path, executor)

    rejected = manager.submit_sql(
        session_id,
        "DELETE FROM fact_production_progress",
    )
    completed = manager.submit_sql(
        session_id,
        load_task_catalog().templates["T-01"].standard_sql,
    )

    assert rejected["outcome"] == "safe_rejected"
    assert completed["state"] == "S9_PATH_UPDATE"
    assert completed["outcome"] is None
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in completed["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-5:] == ["T11", "T21", "T11", "T12", "T13"]


def test_correct_conclusion_creates_a_reviewed_one_level_harder_task(
    tmp_path: Path,
) -> None:
    executor = CatalogExecutor()
    manager, session_id = start_sql_session(
        tmp_path,
        executor,
        follow_up_llm=mastered_follow_up(),
    )
    catalog = load_task_catalog()
    manager.submit_sql(session_id, catalog.templates["T-01"].standard_sql)
    conclusion = manager.advance(session_id)
    manager.submit_follow_up(
        session_id,
        "应以实际完成量说明真实进度。",
        "correct-conclusion-1",
    )
    answered = manager.submit_follow_up(
        session_id,
        "真实报工形成的实际量才能说明完成情况。",
        "correct-conclusion-2",
    )

    upgraded = manager.advance(session_id)

    conclusion_content = conclusion["artifact"]["payload"]["content"]
    upgraded_content = upgraded["artifact"]["payload"]["content"]
    assert conclusion_content["template_id"] == "T-01"
    assert answered["state"] == "S9_PATH_UPDATE"
    assert answered["interaction"]["kind"] == "next_learning_step"
    assert "回答有效" in answered["interaction"]["feedback"]
    assert "进入下一步训练" in answered["interaction"]["next_step_reason"]
    assert upgraded["state"] == "S7_STUDENT"
    assert upgraded["awaiting"] == "sql"
    assert upgraded["interaction"] == {
        "kind": "learning_notice",
        "message": "根据本次作答表现，已为你提高一档难度。",
    }
    assert upgraded_content["template_id"] == "T-01-A"
    assert upgraded_content["knowledge_point"] == conclusion_content["knowledge_point"]
    assert upgraded_content["difficulty"] == "applied"
    assert upgraded_content["query_authority"]["template_id"] == "T-01-A"
    upgraded_msg_id = upgraded["artifact"]["msg_id"]
    matching_verdicts = [
        message
        for message in upgraded["messages"]
        if message["payload"]["type"] == "review_verdict"
        and message["payload"]["content"].get("reviewed_msg_id")
        == upgraded_msg_id
    ]
    assert len(matching_verdicts) == 1
    assert matching_verdicts[0]["verdict"]["decision"] == "approve"
    assert matching_verdicts[0]["verdict"]["rule_hits"] == []
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in upgraded["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-4:] == ["T14", "T19", "T09", "T10"]

    manager.submit_sql(session_id, catalog.templates["T-01-A"].standard_sql)
    completed = manager.advance(session_id)
    assert completed["state"] == "S10_DONE"
    assert completed["awaiting"] == "done"
    assert completed["outcome"] == "completed"
    assert completed["artifact"]["payload"]["content"]["difficulty_action"] == "keep"
    assert completed["interaction"] == {
        "kind": "next_learning_step",
        "message": "下一知识点：完成率计算",
        "knowledge_point": "完成率计算",
    }

    continued = manager.continue_learning(session_id)
    assert continued["session_id"] != session_id
    assert continued["state"] == "S2_KNOWLEDGE"
    assert continued["awaiting"] == "advance"
    assert continued["profile"]["profile_id"] == "line_leader"
    assert continued["learning_contract"]["target_knowledge_points"][0] == "完成率计算"
    assert continued["learning_contract"]["difficulty"] == "basic"
    continued_diagnosis = (
        manager._get_session(continued["session_id"]).diagnosis or {}
    )["payload"]["content"]
    assert continued_diagnosis["knowledge_point_plan"][0] == {
        "knowledge_point": "计划量与实际量口径",
        "initial_difficulty": "basic",
        "difficulty_source": "diagnosis_baseline",
    }
    assert continued["interaction"] == {
        "kind": "learning_notice",
        "message": "已沿用本轮画像与测评结果，下一知识点：完成率计算。",
    }

    next_lecture = manager.advance(continued["session_id"])
    assert next_lecture["state"] == "S3_TASK"
    assert next_lecture["artifact"]["payload"]["content"]["knowledge_point"] == "完成率计算"


def test_conclusion_task_consumes_the_contract_bound_prefetched_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_strategy = TaskAgent.generate_for_learning_action
    strategy_calls: list[tuple[str, str]] = []

    def record_strategy(
        agent: TaskAgent,
        current_template_id: str,
        action: str,
    ) -> dict[str, Any] | None:
        strategy_calls.append((current_template_id, action))
        return original_strategy(agent, current_template_id, action)

    monkeypatch.setattr(
        TaskAgent,
        "generate_for_learning_action",
        record_strategy,
    )
    executor = CatalogExecutor()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    first_task = manager.advance(session_id)
    first_content = first_task["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[first_content["template_id"]].standard_sql,
    )

    conclusion = manager.advance(session_id)

    conclusion_content = conclusion["artifact"]["payload"]["content"]
    assert first_content["template_id"] == "T-03"
    assert conclusion_content["template_id"] == first_content["template_id"]
    assert conclusion_content["knowledge_point"] == first_content["knowledge_point"]
    assert conclusion_content["difficulty"] == first_content["difficulty"]
    assert conclusion["state"] == "S7_STUDENT"
    assert conclusion["awaiting"] == "follow_up"
    assert conclusion["interaction"]["kind"] == "free_text_follow_up"
    assert conclusion["interaction"]["prompt"] == (
        "根据刚才的三道工序结果，哪一道工序完成率最低，你依据的数值是什么？"
    )
    assert conclusion["interaction"]["prompt"] != conclusion_content["question"]
    assert strategy_calls == []
    assessment_branch = next(
        branch
        for branch in conclusion["resource_bundle"]["branches"]
        if branch["branch_id"] == "assessment"
    )
    assert assessment_branch["status"] == "ready"
    assert assessment_branch["payload_type"] == "quiz_set"
    conclusion_msg_id = conclusion["artifact"]["msg_id"]
    conclusion_verdicts = [
        message
        for message in conclusion["messages"]
        if message["payload"]["type"] == "review_verdict"
        and message["payload"]["content"].get("reviewed_msg_id")
        == conclusion_msg_id
    ]
    assert len(conclusion_verdicts) == 1
    assert conclusion_verdicts[0]["verdict"]["decision"] == "approve"
    source = (
        Path(__file__).resolve().parents[1]
        / "orchestrator"
        / "interactive_session.py"
    ).read_text(encoding="utf-8")
    assert "CONCLUSION_TASK_ID" not in source
    assert "def submit_answer" not in source
    assert '"kind": "choice"' not in source


def test_follow_up_rejects_a_bare_yes_without_calling_the_model(
    tmp_path: Path,
) -> None:
    executor = CatalogExecutor()
    follow_up_llm = FollowUpLLM()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up_llm,
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    task = manager.advance(session_id)
    content = task["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[content["template_id"]].standard_sql,
    )
    before = manager.advance(session_id)

    with pytest.raises(InteractiveSessionError, match="不能只回答"):
        manager.submit_follow_up(session_id, "是的", "bare-yes")

    after = manager.get_state(session_id)
    assert after["interaction"] == before["interaction"]
    assert after["awaiting"] == "follow_up"
    assert follow_up_llm.calls == []


@pytest.mark.parametrize(
    "answer",
    ("不会", "不清楚", "不知道怎么回答", "没学过", "无法判断"),
)
def test_follow_up_treats_explicit_non_answers_as_teaching_signals(
    tmp_path: Path,
    answer: str,
) -> None:
    executor = CatalogExecutor()
    follow_up_llm = FollowUpLLM()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up_llm,
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    task = manager.advance(session_id)
    content = task["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[content["template_id"]].standard_sql,
    )
    before = manager.advance(session_id)

    with pytest.raises(InteractiveSessionError, match="不会|依据|提示"):
        manager.submit_follow_up(session_id, answer, f"non-answer-{answer}")

    after = manager.get_state(session_id)
    assert after["interaction"] == before["interaction"]
    assert follow_up_llm.calls == []


def test_follow_up_rejects_a_copied_question_before_model_assessment(
    tmp_path: Path,
) -> None:
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=FollowUpLLM(),
    )
    task = manager.get_state(session_id)["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[task["template_id"]].standard_sql,
    )
    state = manager.advance(session_id)
    prompt = state["interaction"]["prompt"]

    with pytest.raises(InteractiveSessionError, match="题目|依据"):
        manager.submit_follow_up(session_id, prompt, "copied-question")


@pytest.mark.parametrize("answer", ("今天天气不错", "随便写写", "asdfgh"))
def test_follow_up_rejects_obviously_unrelated_text_before_model_assessment(
    tmp_path: Path,
    answer: str,
) -> None:
    follow_up_llm = FollowUpLLM()
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    task = manager.get_state(session_id)["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[task["template_id"]].standard_sql,
    )
    manager.advance(session_id)

    with pytest.raises(InteractiveSessionError, match="无关|字段和值"):
        manager.submit_follow_up(session_id, answer, f"unrelated-{answer}")
    assert follow_up_llm.calls == []


def test_unresolved_historical_error_blocks_an_unrelated_mastery_result(
    tmp_path: Path,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response(
            "mastered",
            "按船号比较时，查询结果中最低的完成率来自哪艘船？",
            target="M-01",
            next_target="M-04",
        ),
    )
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    task = manager.get_state(session_id)["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[task["template_id"]].standard_sql,
    )
    manager.advance(session_id)
    internal = manager._get_session(session_id)
    internal.follow_up_round = 2
    internal.follow_up_target = "M-01"
    internal.unresolved_misconceptions = {"M-04"}

    state = manager.submit_follow_up(
        session_id,
        "YCL完成率为0.6236，数值来自查询结果。",
        "unrelated-mastery",
    )

    assert state["awaiting"] == "follow_up"
    assert state["interaction"]["round"] == 3
    assert "M-04" in state["remediation_status"]["unresolved_misconceptions"]
    assert manager._get_session(session_id).follow_up_target == "M-04"
    assert "此前" in state["interaction"]["feedback"]


def test_wrong_follow_up_creates_an_auditable_correction_ticket(
    tmp_path: Path,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response(
            "needs_support",
            "查询结果中AZTP和ZZTP的完成率分别是多少？",
            target="M-01",
        )
    )
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    task = manager.get_state(session_id)["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[task["template_id"]].standard_sql,
    )
    manager.advance(session_id)

    state = manager.submit_follow_up(
        session_id,
        "YCL应该比较低。",
        "ticket-open-1",
    )

    assert state["correction_status"] == {
        "open_count": 1,
        "resolved_count": 0,
        "total_count": 1,
    }
    ticket = manager._get_session(session_id).correction_tickets[0]
    assert ticket["source_round"] == 1
    assert ticket["misconception_id"] == "M-01"
    assert ticket["resolved"] is False
    assert ticket["learner_answer_digest"]
    assert "YCL应该比较低" not in str(ticket)
    assert ticket["missing_evidence_fields"]
    request = json.loads(follow_up_llm.calls[0]["user"])
    assert request["required_evidence_fields"] == ticket["missing_evidence_fields"]


def test_oldest_open_correction_ticket_controls_the_next_target_and_gap() -> None:
    manager = InteractiveSessionManager.__new__(InteractiveSessionManager)
    session = SimpleNamespace(
        correction_tickets=[
            {
                "source_round": 1,
                "misconception_id": "M-04",
                "missing_evidence_fields": ["ship_no", "complete_rate"],
                "resolved": False,
            },
            {
                "source_round": 2,
                "misconception_id": "M-01",
                "missing_evidence_fields": ["actual_qty"],
                "resolved": False,
            },
        ]
    )

    target, fields = manager._next_correction_requirements(
        session,
        unresolved={"M-01", "M-04"},
    )

    assert target == "M-04"
    assert fields == ("ship_no", "complete_rate")


def test_targeted_mastery_resolves_only_the_matching_correction_ticket(
    tmp_path: Path,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response("mastered", "", target="M-04"),
    )
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    task = manager.get_state(session_id)["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[task["template_id"]].standard_sql,
    )
    manager.advance(session_id)
    internal = manager._get_session(session_id)
    internal.follow_up_round = 2
    internal.follow_up_target = "M-04"
    internal.unresolved_misconceptions = {"M-04"}
    internal.correction_tickets = [
        {
            "source_round": 1,
            "learner_answer_digest": "digest",
            "missing_evidence_fields": ["ship_no", "complete_rate"],
            "misconception_id": "M-04",
            "correction_evidence": [],
            "resolved": False,
        }
    ]

    state = manager.submit_follow_up(
        session_id,
        "H2601的完成率最低，为0.6236。",
        "ticket-resolve-2",
    )

    assert state["awaiting"] == "advance"
    assert state["correction_status"] == {
        "open_count": 0,
        "resolved_count": 1,
        "total_count": 1,
    }
    ticket = manager._get_session(session_id).correction_tickets[0]
    assert ticket["resolved"] is True
    assert ticket["resolved_round"] == 2
    assert ticket["resolution_evidence"]


def test_second_round_assessment_uses_the_targeted_probe_evidence(
    tmp_path: Path,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response(
            "needs_support",
            "按船号比较时，查询结果中最低的完成率来自哪艘船？",
            target="M-04",
        ),
        follow_up_response("mastered", "", target="M-04"),
    )
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    task = manager.get_state(session_id)["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[task["template_id"]].standard_sql,
    )
    manager.advance(session_id)

    continued = manager.submit_follow_up(
        session_id,
        "完成率可能偏低，但我还没有核对船号。",
        "targeted-evidence-1",
    )
    assert continued["interaction"]["round"] == 2
    assert manager._get_session(session_id).follow_up_target == "M-04"

    completed = manager.submit_follow_up(
        session_id,
        "H2601的完成率最低，为0.6236。",
        "targeted-evidence-2",
    )

    assert completed["awaiting"] == "advance"
    assert "上一处错误" in completed["interaction"]["feedback"]
    ticket = manager._get_session(session_id).correction_tickets[0]
    assert any(
        "船级排名" in point
        for evidence in ticket["resolution_evidence"]
        for point in evidence["expected_points"]
    )


def test_reviewed_field_and_value_correct_a_model_false_negative_with_feedback(
    tmp_path: Path,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response(
            "unknown",
            "查询结果中AZTP和ZZTP的完成率分别是多少？",
            target="UNKNOWN",
            next_target="UNKNOWN",
        )
    )
    executor = CatalogExecutor()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up_llm,
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    task = manager.advance(session_id)
    content = task["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[content["template_id"]].standard_sql,
    )
    manager.advance(session_id)

    continued = manager.submit_follow_up(
        session_id,
        "YCL 0.6236",
        "reviewed-false-negative-1",
    )

    assert continued["awaiting"] == "follow_up"
    assert continued["interaction"]["round"] == 2
    assert continued["interaction"]["turns"][0]["assessment"] == "mastered"
    assert continued["interaction"]["feedback"] == (
        "回答有效：已核验：YCL 的完成率为 0.6236，"
        "与查询结果中的最低值一致。"
    )


def test_follow_up_uses_reviewed_evidence_fallback_when_model_output_fails(
    tmp_path: Path,
) -> None:
    def unavailable_follow_up(**_: Any) -> Any:
        raise RuntimeError("temporary model failure")

    executor = CatalogExecutor()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=unavailable_follow_up,
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    task = manager.advance(session_id)
    content = task["artifact"]["payload"]["content"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[content["template_id"]].standard_sql,
    )
    manager.advance(session_id)

    continued = manager.submit_follow_up(
        session_id,
        "YCL最低，完成率为62.36%，低于另外两道工序。",
        "fallback-turn-1",
    )

    assert continued["state"] == "S7_STUDENT"
    assert continued["awaiting"] == "follow_up"
    assert continued["interaction"]["round"] == 2
    assert len(continued["interaction"]["turns"]) == 1
    assert continued["interaction"]["prompt"] == (
        "查询结果中AZTP和ZZTP的完成率分别是多少？"
    )
    assert continued["artifact"]["model"] == "deterministic-reviewed-answer"
    assert continued["interaction"]["feedback"].startswith("回答有效")

    continued = manager.submit_follow_up(
        session_id,
        "AZTP为0.9149，ZZTP为1.0249。",
        "fallback-turn-2",
    )

    assert continued["state"] == "S7_STUDENT"
    assert continued["awaiting"] == "follow_up"
    assert continued["interaction"]["round"] == 3
    assert len(continued["interaction"]["turns"]) == 2
    assert continued["interaction"]["prompt"] == (
        "按完成率从低到高，三道工序应怎样排序？"
    )

    continued = manager.submit_follow_up(
        session_id,
        "从低到高依次为YCL、AZTP、ZZTP。",
        "fallback-turn-3",
    )

    assert continued["state"] == "S8_PROBE"
    assert continued["awaiting"] == "follow_up"
    assert continued["interaction"]["round"] == 4
    assert len(continued["interaction"]["turns"]) == 3
    assert continued["interaction"]["prompt"] == (
        "请引用当前查询结果中的字段和值说明你的判断？"
    )

    finished = manager.submit_follow_up(
        session_id,
        "ZZTP最高，完成率为1.0249。",
        "fallback-turn-4",
    )

    assert finished["awaiting"] == "advance"
    assert finished["interaction"]["kind"] == "learning_notice"


def test_top_tier_answer_completes_without_claiming_a_fake_increase(
    tmp_path: Path,
) -> None:
    executor = CatalogExecutor()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=mastered_follow_up(),
        executor_factory=lambda: executor,
    )
    session_id = manager.create_session("craft_engineer")["session_id"]
    answers = {
        "PT-1": "B",
        "PT-2": "B",
        "PT-3": "C",
        "PT-4": "B",
        "PT-5": "C",
    }
    manager.submit_pretest(session_id, answers)
    manager.advance(session_id)
    first_task = manager.advance(session_id)
    first_content = first_task["artifact"]["payload"]["content"]
    assert first_content["difficulty"] == "advanced"
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[first_content["template_id"]].standard_sql,
    )
    manager.advance(session_id)
    manager.submit_follow_up(
        session_id,
        "WSB责任单元完成率最低，为0.6218。",
        "top-tier-1",
    )
    manager.submit_follow_up(
        session_id,
        "实际发生的数据能够说明完成情况。",
        "top-tier-2",
    )

    completed = manager.advance(session_id)

    assert completed["state"] == "S10_DONE"
    assert completed["interaction"]["kind"] == "next_learning_step"
    content = completed["artifact"]["payload"]["content"]
    assert content["difficulty_action"] == "keep"
    assert "提高一档难度" not in json.dumps(completed, ensure_ascii=False)
    latest_events = {}
    for event in manager.get_agent_events(session_id):
        latest_events[event["agent"]] = event
    assert not any(
        event["status"] in {"working", "collaborating", "reviewing", "debating"}
        for event in latest_events.values()
    )


def test_progression_selection_failure_is_retryable_and_uses_learning_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=mastered_follow_up(),
    )
    catalog = load_task_catalog()
    manager.submit_sql(session_id, catalog.templates["T-01"].standard_sql)
    manager.advance(session_id)
    manager.submit_follow_up(
        session_id,
        "应以实际完成量说明真实进度。",
        "progression-retry-1",
    )
    manager.submit_follow_up(
        session_id,
        "真实报工形成的实际量才能说明完成情况。",
        "progression-retry-2",
    )
    before = manager.get_state(session_id)

    def reject_learning_action(*_: Any, **__: Any) -> dict[str, Any]:
        raise ValueError("unsupported learning action: step_up")

    monkeypatch.setattr(
        "agents.task_agent.TaskAgent.generate_for_learning_action",
        reject_learning_action,
    )
    with pytest.raises(InteractiveSessionError) as raised:
        manager.advance(session_id)

    message = str(raised.value)
    assert message == "暂时无法为你匹配合适的下一步训练，请稍后重试。"
    assert not any(
        forbidden in message
        for forbidden in (
            "step_up",
            "learning action",
            "template",
            "T-",
            "S9",
        )
    )
    after = manager.get_state(session_id)
    assert after["state"] == before["state"] == "S9_PATH_UPDATE"
    assert after["awaiting"] == before["awaiting"] == "advance"
    assert after["messages"] == before["messages"]

    monkeypatch.undo()
    retry = manager.advance(session_id)
    assert retry["artifact"]["payload"]["content"]["template_id"] == "T-01-A"
    assert retry["current_difficulty"] == "applied"
    assert retry["state"] == "S7_STUDENT"
    assert retry["awaiting"] == "sql"


def test_free_text_support_and_correction_complete_existing_flow(
    tmp_path: Path,
) -> None:
    executor = CatalogExecutor()
    manager, session_id = start_sql_session(
        tmp_path,
        executor,
        follow_up_llm=support_then_mastered_follow_up(),
    )
    catalog = load_task_catalog()
    submitted_sql = catalog.templates["T-01"].standard_sql
    manager.submit_sql(session_id, submitted_sql)

    follow_up = manager.advance(session_id)
    supported = manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "support-correction-1",
    )
    corrected = manager.submit_follow_up(
        session_id,
        "实际完成量才表示真正做了多少。",
        "support-correction-2",
    )
    upgraded = manager.advance(session_id)
    manager.submit_sql(session_id, catalog.templates["T-01-A"].standard_sql)
    completed = manager.advance(session_id)

    assert follow_up["interaction"]["kind"] == "free_text_follow_up"
    assert completed["training_report"]["title"] == "本轮训练报告"
    assert completed["training_report"]["query_count"] == 2
    assert completed["training_report"]["follow_up_rounds"] >= 2
    assert follow_up["awaiting"] == "follow_up"
    assert (
        supported["artifact"]["payload"]["content"]["event"]
        == "follow_up_question_ready"
    )
    assert supported["artifact"]["evidence"]
    assert supported["state"] == "S8_PROBE"
    assert supported["awaiting"] == "follow_up"
    assert corrected["interaction"]["kind"] == "data_collision"
    assert corrected["interaction"]["wrong_value"] == "1855.06"
    assert corrected["interaction"]["correct_value"] == "1156.87"
    assert "回答有效" in corrected["interaction"]["feedback"]
    assert corrected["state"] == "S9_PATH_UPDATE"
    assert upgraded["state"] == "S7_STUDENT"
    assert upgraded["awaiting"] == "sql"
    assert upgraded["artifact"]["payload"]["content"]["template_id"] == "T-01-A"
    assert upgraded["artifact"]["payload"]["content"]["difficulty"] == "applied"
    assert upgraded["interaction"] == {
        "kind": "learning_notice",
        "message": "根据本次作答表现，已为你提高一档难度。",
    }
    upgraded_paths = [
        message["payload"]["content"]
        for message in upgraded["messages"]
        if message["payload"]["type"] == "learning_path_update"
    ]
    assert upgraded_paths[-1]["completed_nodes"] == [
        "岗前测评",
        "岗位微课",
        "数据实操",
        "结论判断",
        "反证追问",
        "修正结论",
    ]
    assert completed["state"] == "S10_DONE"
    assert completed["awaiting"] == "done"
    assert completed["artifact"]["payload"]["content"]["completed_nodes"] == [
        "岗前测评",
        "岗位微课",
        "数据实操",
        "结论判断",
        "反证追问",
        "修正结论",
    ]
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in completed["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions == [
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
        "T15",
        "T16",
        "T19",
        "T09",
        "T10",
        "T11",
        "T12",
        "T13",
        "T20",
    ]


def test_four_unmastered_rounds_reenter_teaching_without_stale_follow_up_state(
    tmp_path: Path,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response(
            "needs_support",
            "你会先区分目标数量与真实报工数量吗？",
        ),
        follow_up_response(
            "needs_support",
            "如果目标尚未报工，能把它算作已经完成吗？",
            next_target="M-04",
        ),
        follow_up_response(
            "needs_support",
            "判断真实进度时，你最终会采用哪一种数量？",
        ),
        follow_up_response("needs_support", ""),
    )
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    standard_sql = load_task_catalog().templates["T-01"].standard_sql
    manager.submit_sql(session_id, standard_sql)
    manager.advance(session_id)

    for index in range(1, 4):
        manager.submit_follow_up(
            session_id,
            "我仍然认为计划量就是完成量。",
            f"relearn-{index}",
        )
    second_wrong = manager.submit_follow_up(
        session_id,
        "我还是不能区分这两个口径。",
        "relearn-4",
    )
    lecture = manager.advance(session_id)
    relearned_task = manager.advance(session_id)

    assert second_wrong["state"] == "S2_KNOWLEDGE"
    assert second_wrong["awaiting"] == "advance"
    assert second_wrong["interaction"]["kind"] == "learning_notice"
    assert "回答部分有效" in second_wrong["interaction"]["feedback"]
    assert "四次核对上限" in second_wrong["interaction"]["next_step_reason"]
    assert lecture["state"] == "S3_TASK"
    assert relearned_task["state"] == "S7_STUDENT"
    assert relearned_task["awaiting"] == "sql"
    assert relearned_task["artifact"]["payload"]["content"]["template_id"] == "T-01"
    manager.submit_sql(session_id, standard_sql)
    conclusion = manager.advance(session_id)
    assert conclusion["state"] == "S7_STUDENT"
    assert conclusion["awaiting"] == "follow_up"
    assert conclusion["interaction"]["kind"] == "free_text_follow_up"


def test_first_t17_creates_a_lower_difficulty_contract_revision(
    tmp_path: Path,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response("needs_support", "三道工序中哪一道完成率最低？"),
        follow_up_response("needs_support", "最低完成率对应的数值是多少？"),
        follow_up_response("needs_support", "另外两道工序的完成率分别是多少？"),
        follow_up_response("needs_support", ""),
    )
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up_llm,
        executor_factory=CatalogExecutor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    answers = {
        "PT-1": "B",
        "PT-2": "B",
        "PT-3": "C",
        "PT-4": "B",
        "PT-5": "C",
    }
    manager.submit_pretest(session_id, answers)
    manager.advance(session_id)
    task = manager.advance(session_id)
    task_content = task["artifact"]["payload"]["content"]
    assert task_content["difficulty"] == "applied"
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[task_content["template_id"]].standard_sql,
    )
    manager.advance(session_id)
    for index in range(1, 5):
        state = manager.submit_follow_up(
            session_id,
            "我还不能根据结果完成判断。",
            f"step-down-{index}",
        )

    assert state["state"] == "S2_KNOWLEDGE"
    assert state["learning_contract"]["difficulty"] == "basic"
    assert state["learning_contract"]["revision"] == 2
    assert state["remediation_status"]["attempts"] == {
        "三道工序与传导关系": 1
    }
    assert state["remediation_status"]["context"]["action"] == "step_down"

    lecture = manager.advance(session_id)
    lecture_content = lecture["artifact"]["payload"]["content"]
    assert lecture_content["difficulty"] == "basic"
    assert lecture["evidence_bundle"]["sources"]["pedagogy"]["remediation"] == {
        "knowledge_point": "三道工序与传导关系",
        "attempt": 1,
        "action": "step_down",
        "from_difficulty": "applied",
        "to_difficulty": "basic",
        "exposed_misconceptions": ["M-01"],
    }
    relearned_task = manager.advance(session_id)
    relearned_content = relearned_task["artifact"]["payload"]["content"]
    assert relearned_content["difficulty"] == "basic"
    assert relearned_content["template_id"] == "T-03"


def test_t17_generation_failure_uses_reviewed_evidence_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session_id = start_sql_session(tmp_path, CatalogExecutor())
    session = manager._get_session(session_id)
    session.runtime.transition(
        manager._student_answer_draft(session, answer_result="wrong"),
        "T15",
    )
    session.runtime.transition(
        manager._probe_outcome_draft(session, answer_result="wrong"),
        "T17",
    )
    manager._apply_t17_remediation(
        session,
        feedback="连续练习后仍需降低难度。",
    )

    def fail_live_lecture(*_: Any, **__: Any) -> dict[str, Any]:
        raise LLMCallError("simulated live generation outage")

    monkeypatch.setattr(
        "orchestrator.interactive_session._generate_reviewable_lecture",
        fail_live_lecture,
    )

    fallback = manager.advance(session_id)
    content = fallback["artifact"]["payload"]["content"]

    assert fallback["state"] == "S3_TASK"
    assert fallback["awaiting"] == "advance"
    assert fallback["outcome"] is None
    assert content["generated_by"] == "evidence_projection_fallback"
    assert content["fallback_reason"] == "remediation_generation_unavailable"
    assert fallback["artifact"]["claims"]
    assert fallback["artifact"]["evidence"]
    assert all(
        claim["kind"] == "fact"
        for claim in fallback["artifact"]["claims"]
    )


def test_second_t17_defers_the_point_instead_of_repeating_forever(
    tmp_path: Path,
) -> None:
    responses = []
    for cycle in range(2):
        responses.extend(
            (
                follow_up_response(
                    "needs_support",
                    f"第{cycle + 1}次补学：哪项结果最需要继续核对？",
                ),
                follow_up_response(
                    "needs_support",
                    f"第{cycle + 1}次补学：对应的数值依据是什么？",
                ),
                follow_up_response(
                    "needs_support",
                    f"第{cycle + 1}次补学：另外两项结果如何比较？",
                ),
                follow_up_response("needs_support", ""),
            )
        )
    scripted_llm = ScriptedLLM()
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=scripted_llm,
        follow_up_llm_call=FollowUpLLM(*responses),
        executor_factory=CatalogExecutor,
    )
    session_id = manager.create_session("planner_new")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )

    for cycle in range(2):
        lecture = manager.advance(session_id)
        if cycle == 1:
            lecture_user = next(
                json.loads(call["user"])
                for call in reversed(scripted_llm.calls)
                if "oneOf" in call["json_schema"]
            )
            assert "第1次补学" in lecture_user["learning_report_summary"]
            assert "更换讲解角度" in lecture_user["learning_report_summary"]
            assert (
                lecture["evidence_bundle"]["sources"]["pedagogy"][
                    "remediation"
                ]["action"]
                == "refresh"
            )
        task = manager.advance(session_id)
        content = task["artifact"]["payload"]["content"]
        manager.submit_sql(
            session_id,
            load_task_catalog().templates[content["template_id"]].standard_sql,
        )
        manager.advance(session_id)
        for index in range(1, 5):
            state = manager.submit_follow_up(
                session_id,
                "我仍然不能依据查询结果完成判断。",
                f"defer-{cycle}-{index}",
            )

    assert state["awaiting"] == "advance"
    assert state["remediation_status"]["attempts"][
        "三道工序与传导关系"
    ] == 2
    assert state["remediation_status"]["deferred_knowledge_points"] == [
        "三道工序与传导关系"
    ]
    assert state["remediation_status"]["context"]["action"] == "deferred"
    assert "延后学习" in state["interaction"]["next_step_reason"]

    next_lecture = manager.advance(session_id)
    assert next_lecture["awaiting"] in {"advance", "done"}
    if next_lecture["awaiting"] == "advance":
        assert (
            next_lecture["evidence_bundle"]["sources"]["pedagogy"][
                "remediation"
            ]
            is None
        )
        assert next_lecture["training_report"] is None
        next_task = manager.advance(session_id)
        assert (
            next_task["artifact"]["payload"]["content"]["knowledge_point"]
            != "三道工序与传导关系"
        )


def test_repeated_sql_failures_escalate_hints_and_step_down_on_fifth_attempt(
    tmp_path: Path,
) -> None:
    manager, session_id = start_sql_session(tmp_path, CatalogExecutor())

    levels = []
    for attempt in range(1, 5):
        state = manager.submit_sql(session_id, "DELETE FROM forbidden_table")
        levels.append(state["sql_support"]["level"])
        assert state["sql_support"]["attempt"] == attempt

    state = manager.submit_sql(session_id, "DELETE FROM forbidden_table")

    assert levels == [
        "self_correction",
        "self_correction",
        "structured_hint",
        "partial_template",
    ]
    assert state["state"] == "S2_KNOWLEDGE"
    assert state["awaiting"] == "advance"
    assert state["remediation_status"]["attempts"] == {
        "计划量与实际量口径": 1
    }
    session = manager._get_session(session_id)
    assert session.sql_failure_count == 0
    assert state["sql_support"] is None
    assert state["interaction"]["kind"] == "learning_notice"
    assert "连续五次" in state["interaction"]["feedback"]


def test_sql_failure_hints_bind_to_task_fields_without_leaking_answer(
    tmp_path: Path,
) -> None:
    manager, session_id = start_sql_session(tmp_path, CatalogExecutor())
    standard_sql = load_task_catalog().templates["T-01"].standard_sql

    for _ in range(3):
        third = manager.submit_sql(session_id, "DELETE FROM forbidden_table")

    structured_hint = third["sql_support"]["hint"]
    assert third["sql_support"]["level"] == "structured_hint"
    assert "plan_qty" in structured_hint
    assert "actual_qty" in structured_hint
    assert "ship_no" in structured_hint
    assert "period_date" in structured_hint
    assert standard_sql not in structured_hint
    assert "1855.06" not in structured_hint
    assert "1156.87" not in structured_hint

    fourth = manager.submit_sql(session_id, "DELETE FROM forbidden_table")
    partial_template = fourth["sql_support"]["hint"]
    assert fourth["sql_support"]["level"] == "partial_template"
    assert "SELECT" in partial_template
    assert "plan_qty" in partial_template
    assert "actual_qty" in partial_template
    assert "ship_no" in partial_template
    assert "period_date" in partial_template
    assert standard_sql not in partial_template
    assert "1855.06" not in partial_template
    assert "1156.87" not in partial_template


@pytest.mark.parametrize("executor", [TimeoutExecutor(), FailingExecutor()])
def test_external_query_failure_does_not_penalize_the_learner(
    tmp_path: Path,
    executor: Any,
) -> None:
    manager, session_id = start_sql_session(tmp_path, executor)
    standard_sql = load_task_catalog().templates["T-01"].standard_sql

    for _ in range(5):
        state = manager.submit_sql(session_id, standard_sql)
        assert state["state"] == "S7_STUDENT"
        assert state["awaiting"] == "sql"
        assert state["sql_support"] is None

    learner_error = manager.submit_sql(session_id, "DELETE FROM forbidden_table")
    assert learner_error["sql_support"]["attempt"] == 1
    assert learner_error["sql_support"]["level"] == "self_correction"


def test_successful_sql_clears_accumulated_failure_support(
    tmp_path: Path,
) -> None:
    manager, session_id = start_sql_session(tmp_path, CatalogExecutor())
    for _ in range(3):
        failed = manager.submit_sql(session_id, "DELETE FROM forbidden_table")
    assert failed["sql_support"]["attempt"] == 3

    completed = manager.submit_sql(
        session_id,
        load_task_catalog().templates["T-01"].standard_sql,
    )

    assert completed["state"] == "S9_PATH_UPDATE"
    assert completed["sql_support"] is None


def test_r04_exhaustion_after_follow_up_finishes_safely_and_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    follow_up_llm = FollowUpLLM(
        follow_up_response(
            "needs_support",
            "你会先区分目标数量与真实报工数量吗？",
        ),
        follow_up_response(
            "needs_support",
            "如果目标尚未报工，能把它算作已经完成吗？",
            next_target="M-04",
        ),
        follow_up_response(
            "needs_support",
            "判断真实进度时，你最终会采用哪一种数量？",
        ),
        follow_up_response("needs_support", ""),
    )
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    standard_sql = load_task_catalog().templates["T-01"].standard_sql
    manager.submit_sql(session_id, standard_sql)
    manager.advance(session_id)
    for index in range(1, 4):
        manager.submit_follow_up(
            session_id,
            "我仍然认为计划量就是完成量。",
            f"r04-stop-{index}",
        )
    before = manager.submit_follow_up(
        session_id,
        "我还是不能区分这两个口径。",
        "r04-stop-4",
    )
    preflight_calls = 0

    def reject_r04(
        self: ReviewAgent,
        product: dict[str, Any],
    ) -> dict[str, str]:
        del self, product
        nonlocal preflight_calls
        preflight_calls += 1
        return {
            "rule_id": "R-04",
            "reason": "该内容与当前学习目标不一致。",
            "evidence_ref": "fixed-r04-stub",
        }

    monkeypatch.setattr(ReviewAgent, "preflight_r04", reject_r04)
    session = manager._get_session(session_id)
    assert session.learning_contract is not None
    expected_calls = session.learning_contract.quality_policy.max_review_cycles

    stopped = manager.advance(session_id)

    assert preflight_calls == expected_calls
    assert stopped["awaiting"] == "done"
    assert stopped["outcome"] == "system_error"
    assert stopped["interaction"] == {
        "kind": "review_notice",
        "message": SYSTEM_ERROR_COPY,
    }
    assert stopped["artifact"] == before["artifact"]
    assert manager._get_session(session_id).pending_learning_action is None
    assert manager.advance(session_id) == stopped
    assert preflight_calls == expected_calls


@pytest.mark.parametrize(
    "failure",
    (
        ReviewFlowTerminal("refuse", {"role": "re_verdict"}),
        ReviewFlowInterrupted({"role": "re_verdict"}),
        RelationIntegrityError("simulated relation mismatch"),
    ),
    ids=("review-exhausted", "review-interrupted", "relation-mismatch"),
)
def test_follow_up_review_exhaustion_keeps_the_approved_question_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=support_then_mastered_follow_up(),
    )
    manager.submit_sql(
        session_id,
        load_task_catalog().templates["T-01"].standard_sql,
    )
    before = manager.advance(session_id)
    original_question = before["interaction"]["prompt"]

    def exhaust_follow_up_review(*_: Any, **__: Any) -> tuple[Any, Any]:
        raise failure

    monkeypatch.setattr(
        manager,
        "_review_follow_up_product",
        exhaust_follow_up_review,
    )

    retained = manager.submit_follow_up(
        session_id,
        "我仍然把计划量当成实际完成量。",
        "retain-approved-follow-up",
    )

    assert retained["awaiting"] == "follow_up"
    assert retained["outcome"] is None
    assert retained["interaction"]["kind"] == "free_text_follow_up"
    assert retained["interaction"]["prompt"] == original_question
    assert retained["interaction"]["retry_required"] is True
    assert "不会结束" in retained["interaction"]["feedback"]
    assert retained["artifact"] == before["artifact"]


def test_follow_up_path_does_not_fall_back_to_the_obsolete_probe_sql_action(
    tmp_path: Path,
) -> None:
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=support_then_mastered_follow_up(),
    )
    standard_sql = load_task_catalog().templates["T-01"].standard_sql
    manager.submit_sql(session_id, standard_sql)
    manager.advance(session_id)
    supported = manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "no-probe-sql-1",
    )

    with pytest.raises(InteractiveSessionError):
        manager.submit_sql(
            session_id,
            "DELETE FROM fact_production_progress",
        )
    unchanged = manager.get_state(session_id)

    assert supported["state"] == "S8_PROBE"
    assert supported["awaiting"] == "follow_up"
    assert unchanged == supported
    transitions = [
        message["payload"]["content"].get("transition_id")
        for message in unchanged["messages"]
        if message["payload"]["content"].get("transition_id")
    ]
    assert transitions[-1] == "T15"
    assert "T21" not in transitions


def test_standard_library_http_supports_create_pretest_and_state_polling(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=forbidden_llm,
        executor_factory=RecordingExecutor,
    )
    server = build_http_server(manager, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    def request(method: str, path: str, body: Any | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        response = urlopen(
            Request(
                f"{base_url}{path}",
                data=data,
                method=method,
                headers={"Content-Type": "application/json"},
            ),
            timeout=3,
        )
        return response.status, json.loads(response.read().decode("utf-8"))

    try:
        create_status, created = request(
            "POST",
            "/api/sessions",
            {"profile_id": "planner_new"},
        )
        session_id = created["session_id"]
        pretest_status, pretest = request(
            "GET",
            f"/api/sessions/{session_id}/pretest",
        )
        submit_status, submitted = request(
            "POST",
            f"/api/sessions/{session_id}/pretest",
            {"answers": {f"PT-{index}": "D" for index in range(1, 6)}},
        )
        state_status, state = request(
            "GET",
            f"/api/sessions/{session_id}",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert create_status == 201
    assert pretest_status == 200
    assert len(pretest["questions"]) == 5
    assert submit_status == 200
    assert submitted["state"] == "S2_KNOWLEDGE"
    assert state_status == 200
    assert state["session_id"] == session_id
    assert state["awaiting"] == "advance"


def test_standard_library_http_exposes_full_interactive_action_flow(
    tmp_path: Path,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=support_then_mastered_follow_up(),
        executor_factory=CatalogExecutor,
    )
    server = build_http_server(manager, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, body: Any | None = None) -> Any:
        response = urlopen(
            Request(
                f"{base_url}{path}",
                data=json.dumps(body or {}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            ),
            timeout=5,
        )
        assert response.status in {200, 201}
        return json.loads(response.read().decode("utf-8"))

    try:
        session_id = post(
            "/api/sessions",
            {"profile_id": "line_leader"},
        )["session_id"]
        post(
            f"/api/sessions/{session_id}/pretest",
            {"answers": {f"PT-{index}": "D" for index in range(1, 6)}},
        )
        post(f"/api/sessions/{session_id}/advance")
        post(f"/api/sessions/{session_id}/advance")
        sql = load_task_catalog().templates["T-01"].standard_sql
        post(f"/api/sessions/{session_id}/sql", {"sql": sql})
        follow_up = post(f"/api/sessions/{session_id}/advance")
        supported = post(
            f"/api/sessions/{session_id}/follow-up",
            {
                "text": "计划量就是已经完成的数量。",
                "client_turn_id": "http-follow-up-1",
            },
        )
        corrected = post(
            f"/api/sessions/{session_id}/follow-up",
            {
                "text": "实际完成量才表示真正做了多少。",
                "client_turn_id": "http-follow-up-2",
            },
        )
        upgraded = post(f"/api/sessions/{session_id}/advance")
        upgraded_sql = load_task_catalog().templates["T-01-A"].standard_sql
        post(f"/api/sessions/{session_id}/sql", {"sql": upgraded_sql})
        completed = post(f"/api/sessions/{session_id}/advance")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert follow_up["interaction"]["kind"] == "free_text_follow_up"
    assert supported["awaiting"] == "follow_up"
    assert corrected["interaction"]["kind"] == "data_collision"
    assert upgraded["artifact"]["payload"]["content"]["template_id"] == "T-01-A"
    assert upgraded["interaction"]["message"] == "根据本次作答表现，已为你提高一档难度。"
    assert completed["state"] == "S10_DONE"


def test_step_up_product_failure_finishes_safely_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=mastered_follow_up(),
    )
    catalog = load_task_catalog()
    manager.submit_sql(session_id, catalog.templates["T-01"].standard_sql)
    manager.advance(session_id)
    manager.submit_follow_up(
        session_id,
        "应以实际完成量说明真实进度。",
        "step-up-stop-1",
    )
    manager.submit_follow_up(
        session_id,
        "真实报工形成的实际量才能说明完成情况。",
        "step-up-stop-2",
    )
    before = manager.get_state(session_id)
    production_calls = 0

    def fail_reviewed_product(*_: Any, **__: Any) -> dict[str, Any]:
        nonlocal production_calls
        production_calls += 1
        raise DemoSessionError("simulated reviewed task failure after T19")

    monkeypatch.setattr(
        "orchestrator.interactive_session._produce_reviewed_product",
        fail_reviewed_product,
    )

    stopped = manager.advance(session_id)

    assert stopped["awaiting"] == "done"
    assert stopped["outcome"] == "system_error"
    assert stopped["interaction"] == {
        "kind": "review_notice",
        "message": SYSTEM_ERROR_COPY,
    }
    assert stopped["artifact"] == before["artifact"]
    assert manager._get_session(session_id).pending_learning_action is None
    latest_events = {}
    for event in manager.get_agent_events(session_id):
        latest_events[event["agent"]] = event
    assert latest_events["review"]["status"] == "blocked"
    assert not any(
        event["status"] in {"working", "collaborating", "reviewing", "debating"}
        for event in latest_events.values()
    )
    assert production_calls == 1
    assert manager.advance(session_id) == stopped
    assert production_calls == 1


@pytest.mark.parametrize("terminal_mode", ("mastered", "unmastered"))
def test_follow_up_terminal_transition_error_finishes_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_mode: str,
) -> None:
    if terminal_mode == "mastered":
        follow_up_llm = mastered_follow_up()
        prior_answers = (("应以实际完成量说明真实进度。", "terminal-stop-1"),)
        final_answer = "真实报工形成的实际量才能说明完成情况。"
        final_turn_id = "terminal-stop-2"
        failing_transition = "T14"
    else:
        follow_up_llm = FollowUpLLM(
            follow_up_response(
                "needs_support",
                "你会先区分目标数量与真实报工数量吗？",
            ),
            follow_up_response(
                "needs_support",
                "如果目标尚未报工，能把它算作已经完成吗？",
                next_target="M-04",
            ),
            follow_up_response(
                "needs_support",
                "判断真实进度时，你最终会采用哪一种数量？",
            ),
            follow_up_response("needs_support", ""),
        )
        prior_answers = tuple(
            ("我仍然认为计划量就是完成量。", f"terminal-stop-{index}")
            for index in range(1, 4)
        )
        final_answer = "我还是不能区分这两个口径。"
        final_turn_id = "terminal-stop-4"
        failing_transition = "T17"

    manager, session_id = start_sql_session(
        tmp_path,
        CatalogExecutor(),
        follow_up_llm=follow_up_llm,
    )
    standard_sql = load_task_catalog().templates["T-01"].standard_sql
    manager.submit_sql(session_id, standard_sql)
    manager.advance(session_id)
    for answer, turn_id in prior_answers:
        manager.submit_follow_up(session_id, answer, turn_id)
    before = manager.get_state(session_id)
    session = manager._get_session(session_id)
    original_transition = session.runtime.transition
    transition_failures = 0

    def fail_terminal_transition(
        draft: dict[str, Any],
        transition_id: str,
    ) -> dict[str, Any]:
        nonlocal transition_failures
        if transition_id == failing_transition:
            transition_failures += 1
            raise DemoSessionError(
                f"simulated terminal transition failure at {transition_id}"
            )
        return original_transition(draft, transition_id)

    monkeypatch.setattr(session.runtime, "transition", fail_terminal_transition)

    stopped = manager.submit_follow_up(
        session_id,
        final_answer,
        final_turn_id,
    )

    assert stopped["awaiting"] == "done"
    assert stopped["outcome"] == "system_error"
    assert stopped["interaction"] == {
        "kind": "review_notice",
        "message": SYSTEM_ERROR_COPY,
    }
    assert stopped["artifact"] == before["artifact"]
    assert session.pending_learning_action is None
    assert transition_failures == 1
    assert manager.submit_follow_up(
        session_id,
        final_answer,
        final_turn_id,
    ) == stopped
    assert transition_failures == 1


def test_http_demo_session_error_returns_structured_system_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=forbidden_llm,
        executor_factory=RecordingExecutor,
    )

    def fail_advance(_: str) -> dict[str, Any]:
        raise DemoSessionError("no_matching_transition at S3_TASK; rule_hits=R-04")

    monkeypatch.setattr(manager, "advance", fail_advance)
    server = build_http_server(manager, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = Request(
        f"http://127.0.0.1:{server.server_port}/api/sessions/session-1/advance",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with pytest.raises(HTTPError) as raised:
            urlopen(request, timeout=3)
        response = raised.value
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert response.code == 409
    assert payload == {
        "error": SYSTEM_ERROR_COPY,
        "outcome": "system_error",
    }
    public_payload = json.dumps(payload, ensure_ascii=False)
    assert not any(
        marker in public_payload
        for marker in (
            "DemoSessionError",
            "no_matching_transition",
            "S3_TASK",
            "R-04",
            "rule_hits",
        )
    )


def test_interactive_server_cli_documents_thin_standard_library_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output
    assert "--trace-dir" in output
    assert "--cache-dir" in output
