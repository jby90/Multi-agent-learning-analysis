from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from agents.validate_message import validate_message
from orchestrator import demo
from orchestrator.agents_stub import (
    build_stubs,
    path_update_draft,
    probe_outcome_draft,
    profile_loaded_draft,
    student_answer_draft,
    student_sql_draft,
)
from orchestrator.bus import BusResult, MessageBus, TraceRoutingError
from orchestrator.engine import (
    GUARDS,
    InvariantViolation,
    OrchestratorEngine,
    TransitionResolutionError,
    validate_guard_registry,
)
from orchestrator.transitions import (
    DynamicTarget,
    State,
    TRANSITIONS,
    UnknownPayloadType,
    resolve_downstream,
    resolve_producer,
)


def control_draft(trace_id: str, action: str) -> dict:
    return {
        "trace_id": trace_id,
        "agent": "system",
        "role": "system",
        "payload": {"type": "control", "content": {"action": action}},
        "evidence": [],
        "timestamp": "2026-07-14T10:00:00+08:00",
    }


def read_trace(bus: MessageBus, trace_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in bus.trace_path(trace_id).read_text(encoding="utf-8").splitlines()
    ]


EXPECTED_TRANSITIONS = (
    ("T01", State.S0_INIT, "画像载入成功", State.S1_DIAGNOSIS, "profile_loaded"),
    (
        "T02",
        State.S1_DIAGNOSIS,
        "学情报告过总线校验",
        State.S2_KNOWLEDGE,
        "diagnosis_ready",
    ),
    ("T03", State.S2_KNOWLEDGE, "讲义产出", State.S5_REVIEW, "lecture_produced"),
    (
        "T04",
        State.S5_REVIEW,
        "verdict=approve ∧ 被审对象=讲义",
        State.S3_TASK,
        "approved_lecture",
    ),
    (
        "T05",
        State.S5_REVIEW,
        "verdict=reject ∧ retry_count<max",
        State.S6_DEBATE,
        "review_rejected_below_limit",
    ),
    (
        "T06",
        State.S6_DEBATE,
        "re_verdict=approve(辩护成立)",
        DynamicTarget.ORIGINAL_DOWNSTREAM,
        "re_verdict_approved",
    ),
    (
        "T07",
        State.S6_DEBATE,
        "re_verdict=reject(辩护不成立)",
        DynamicTarget.ORIGINAL_PRODUCER,
        "re_verdict_rejected_below_limit",
    ),
    (
        "T08",
        (State.S5_REVIEW, State.S6_DEBATE),
        "retry_count≥max_retries",
        DynamicTarget.FALLBACK,
        "retry_limit_reached",
    ),
    ("T09", State.S3_TASK, "任务产出", State.S5_REVIEW, "task_produced"),
    (
        "T10",
        State.S5_REVIEW,
        "approve ∧ 被审对象=任务",
        State.S7_STUDENT,
        "approved_task",
    ),
    (
        "T11",
        State.S7_STUDENT,
        "学生提交SQL类操作",
        State.S4_VERIFY,
        "student_sql_submitted",
    ),
    (
        "T12",
        State.S4_VERIFY,
        "查询执行完毕",
        State.S5_REVIEW,
        "query_completed",
    ),
    (
        "T13",
        State.S5_REVIEW,
        "approve ∧ 被审对象=数据结论",
        State.S9_PATH_UPDATE,
        "approved_sql_result",
    ),
    (
        "T14",
        State.S7_STUDENT,
        "学生答题：正确",
        State.S9_PATH_UPDATE,
        "student_answer_correct",
    ),
    (
        "T15",
        State.S7_STUDENT,
        "学生答题：首次错误",
        State.S8_PROBE,
        "student_answer_first_wrong",
    ),
    (
        "T16",
        State.S8_PROBE,
        "反证任务生成→学生执行→修正后答对",
        State.S9_PATH_UPDATE,
        "probe_corrected",
    ),
    (
        "T17",
        State.S8_PROBE,
        "反证后二次仍错",
        State.S2_KNOWLEDGE,
        "probe_second_wrong",
    ),
    (
        "T18",
        State.S8_PROBE,
        "错因未命中词表M-01~M-05",
        State.S7_STUDENT,
        "misconception_unknown",
    ),
    (
        "T19",
        State.S9_PATH_UPDATE,
        "路径更新完毕 ∧ 还有下一环节",
        State.S3_TASK,
        "path_has_next",
    ),
    (
        "T20",
        State.S9_PATH_UPDATE,
        "学习目标达成",
        State.S10_DONE,
        "learning_goal_achieved",
    ),
    (
        "T21",
        State.S4_VERIFY,
        "查询未形成可审核结论，返回调整后重试",
        State.S7_STUDENT,
        "verification_failed",
    ),
)


def test_transition_table_is_exact_design05_order() -> None:
    actual = tuple(
        (row.transition_id, row.from_state, row.trigger, row.to_state, row.guard)
        for row in TRANSITIONS
    )

    assert actual == EXPECTED_TRANSITIONS


@pytest.mark.parametrize(
    ("payload_type", "expected"),
    (
        ("lecture_note", State.S3_TASK),
        ("quiz_set", State.S7_STUDENT),
        ("practice_guide", State.S7_STUDENT),
        ("sql_result", State.S9_PATH_UPDATE),
    ),
)
def test_resolve_downstream_is_explicit(payload_type: str, expected: State) -> None:
    assert resolve_downstream(payload_type) is expected


@pytest.mark.parametrize(
    ("payload_type", "expected"),
    (
        ("lecture_note", State.S2_KNOWLEDGE),
        ("quiz_set", State.S3_TASK),
        ("practice_guide", State.S3_TASK),
        ("sql_result", State.S4_VERIFY),
    ),
)
def test_resolve_producer_is_explicit(payload_type: str, expected: State) -> None:
    assert resolve_producer(payload_type) is expected


def test_dynamic_resolvers_reject_unknown_payload_type() -> None:
    with pytest.raises(UnknownPayloadType, match="unknown"):
        resolve_downstream("unknown")
    with pytest.raises(UnknownPayloadType, match="unknown"):
        resolve_producer("unknown")


def test_bus_assigns_envelope_and_rejection_consumes_step(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path)
    first = bus.send(control_draft("trace-bus", action="first"))
    invalid = control_draft("trace-bus", action="bad")
    invalid.pop("timestamp")
    second = bus.send(invalid)
    third = bus.send(control_draft("trace-bus", action="third"))

    assert first.accepted and first.message["step"] == 1
    assert not second.accepted and second.message["step"] == 2
    assert second.message["rejected_by_bus"] is True
    assert third.accepted and third.message["step"] == 3
    messages = [
        json.loads(line)
        for line in bus.trace_path("trace-bus")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [message["step"] for message in messages] == [1, 2, 3]


def test_bus_rejects_sender_owned_step_and_msg_id(tmp_path: Path) -> None:
    draft = control_draft("trace-forged", action="forged") | {
        "step": 88,
        "msg_id": "forged",
    }

    result = MessageBus(tmp_path).send(draft)

    assert not result.accepted
    assert result.message["step"] == 1
    assert result.message["msg_id"] == "trace-forged-001"
    assert any("reserved" in error for error in result.errors)


def test_closed_trace_rejects_but_logs_later_send(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path)
    assert bus.send(control_draft("trace-closed", action="start")).accepted
    bus.close_trace("trace-closed", "S10_DONE")

    result = bus.send(control_draft("trace-closed", action="late"))

    assert not result.accepted
    assert result.message["step"] == 2
    assert result.message["rejected_by_bus"] is True
    assert any("S10_DONE" in error for error in result.errors)
    messages = [
        json.loads(line)
        for line in bus.trace_path("trace-closed")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(messages) == 2


def test_bus_rejects_trace_id_that_can_escape_trace_directory(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "traces"
    bus = MessageBus(trace_dir)

    with pytest.raises(TraceRoutingError, match="safe identifier"):
        bus.send(control_draft("../escaped", action="escape"))

    assert not (tmp_path / "escaped.jsonl").exists()


@pytest.mark.parametrize("trace_id", ("NUL", "con", "COM1", "lpt9", "AUX.log"))
def test_bus_rejects_windows_reserved_device_trace_ids(
    tmp_path: Path, trace_id: str
) -> None:
    bus = MessageBus(tmp_path)

    with pytest.raises(TraceRoutingError, match="Windows device"):
        bus.send(control_draft(trace_id, action="reserved"))


def test_new_bus_rejects_reuse_of_existing_trace_file(tmp_path: Path) -> None:
    first_bus = MessageBus(tmp_path)
    assert first_bus.send(control_draft("trace-existing", action="first")).accepted
    second_bus = MessageBus(tmp_path)

    with pytest.raises(TraceRoutingError, match="already exists"):
        second_bus.send(control_draft("trace-existing", action="second"))

    assert len(read_trace(first_bus, "trace-existing")) == 1


def test_engine_starts_at_s0_and_trace_starts_with_session_start(
    tmp_path: Path,
) -> None:
    bus = MessageBus(tmp_path)

    engine = OrchestratorEngine(bus, "trace-engine", "cs_student")

    first = read_trace(bus, "trace-engine")[0]
    assert engine.state is State.S0_INIT
    assert engine.state_history == (State.S0_INIT,)
    assert first["step"] == 1
    assert first["payload"]["content"] == {
        "action": "session_start",
        "state": "S0_INIT",
        "student_profile_ref": "cs_student",
    }
    assert validate_message(first) == []


def test_t01_writes_system_transition_before_state_commit(tmp_path: Path) -> None:
    bus = MessageBus(tmp_path)
    engine = OrchestratorEngine(bus, "trace-t01", "cs_student")

    result = engine.send(control_draft("trace-t01", action="profile_loaded"))

    assert result.transition is not None
    assert result.transition.transition_id == "T01"
    assert result.transitioned
    assert engine.state is State.S1_DIAGNOSIS
    transition_message = read_trace(bus, "trace-t01")[-1]
    content = transition_message["payload"]["content"]
    assert content["transition_id"] == "T01"
    assert content["from_state"] == "S0_INIT"
    assert content["to_state"] == "S1_DIAGNOSIS"
    assert content["trigger"] == "画像载入成功"
    assert content["based_on_msg_id"] == result.bus_result.message["msg_id"]
    assert validate_message(transition_message) == []


class RejectTransitionBus(MessageBus):
    def send(self, draft: Mapping[str, Any]) -> BusResult:
        content = draft.get("payload", {}).get("content", {})
        if "transition_id" in content:
            rejected = deepcopy(dict(draft))
            rejected.pop("timestamp", None)
            return super().send(rejected)
        return super().send(draft)


def test_state_is_not_committed_when_transition_record_is_rejected(
    tmp_path: Path,
) -> None:
    bus = RejectTransitionBus(tmp_path)
    engine = OrchestratorEngine(bus, "trace-atomic", "cs_student")

    result = engine.send(control_draft("trace-atomic", action="profile_loaded"))

    assert not result.transitioned
    assert engine.state is State.S0_INIT
    assert engine.state_history == (State.S0_INIT,)
    assert read_trace(bus, "trace-atomic")[-1]["rejected_by_bus"] is True


def test_cross_trace_input_is_logged_but_cannot_advance_engine(
    tmp_path: Path,
) -> None:
    bus = MessageBus(tmp_path)
    engine = OrchestratorEngine(bus, "trace-engine-a", "cs_student")

    result = engine.send(profile_loaded_draft("trace-engine-b"))

    assert result.bus_result.accepted
    assert not result.transitioned
    assert result.reason == "trace_id_mismatch"
    assert engine.state is State.S0_INIT
    assert len(read_trace(bus, "trace-engine-a")) == 1
    assert len(read_trace(bus, "trace-engine-b")) == 1


def test_t01_requires_system_control_actor(tmp_path: Path) -> None:
    engine, _, _ = build_runtime(tmp_path, "trace-envelope-t01")
    wrong_actor = profile_loaded_draft(engine.trace_id)
    wrong_actor["agent"] = "task"

    result = engine.send(wrong_actor)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert engine.state is State.S0_INIT


def test_all_five_stubs_and_control_helpers_pass_the_real_bus(
    tmp_path: Path,
) -> None:
    trace_id = "trace-stubs"
    bus = MessageBus(tmp_path)
    stubs = build_stubs(trace_id)
    messages = [
        profile_loaded_draft(trace_id),
        stubs.diagnosis.profile_assessment(),
        stubs.knowledge.lecture(),
        stubs.task.task("quiz_set"),
        stubs.task.task("practice_guide"),
        stubs.verification.sql_result(),
        stubs.review.verdict("approve", "lecture_note", "reviewed-001"),
        stubs.knowledge.rebuttal("reviewed-001"),
        stubs.review.re_verdict("reject", "lecture_note", "reviewed-001"),
        stubs.diagnosis.probe("M-01"),
        student_sql_draft(trace_id),
        student_answer_draft(trace_id, "wrong"),
        probe_outcome_draft(trace_id, "correct", "M-01"),
        path_update_draft(trace_id, has_next=False, learning_goal_achieved=True),
    ]

    for draft in messages:
        assert "step" not in draft
        assert "msg_id" not in draft
        result = bus.send(draft)
        assert result.accepted, result.errors
        assert validate_message(result.message) == []


def assert_transition(result: Any, transition_id: str) -> dict[str, Any]:
    assert result.bus_result.accepted, result.bus_result.errors
    assert result.transitioned, result.reason
    assert result.transition is not None
    assert result.transition.transition_id == transition_id
    return result.bus_result.message


def build_runtime(
    tmp_path: Path, trace_id: str
) -> tuple[OrchestratorEngine, MessageBus, Any]:
    bus = MessageBus(tmp_path)
    engine = OrchestratorEngine(bus, trace_id, "cs_student")
    return engine, bus, build_stubs(trace_id)


def drive_to_s2(engine: OrchestratorEngine, stubs: Any) -> None:
    assert_transition(engine.send(profile_loaded_draft(engine.trace_id)), "T01")
    assert_transition(engine.send(stubs.diagnosis.profile_assessment()), "T02")


def drive_to_s7(engine: OrchestratorEngine, stubs: Any) -> None:
    drive_to_s2(engine, stubs)
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


def replay_states(messages: list[dict[str, Any]]) -> list[str]:
    states = [messages[0]["payload"]["content"]["state"]]
    states.extend(
        message["payload"]["content"]["to_state"]
        for message in messages
        if message.get("role") == "system"
        and "transition_id" in message.get("payload", {}).get("content", {})
        and not message.get("rejected_by_bus", False)
    )
    return states


def test_happy_path_and_trace_replay_are_exact(tmp_path: Path) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-happy")
    drive_to_s7(engine, stubs)
    assert_transition(engine.send(student_sql_draft(engine.trace_id)), "T11")
    sql_result = assert_transition(
        engine.send(stubs.verification.sql_result()), "T12"
    )
    assert_transition(
        engine.send(
            stubs.review.verdict("approve", "sql_result", sql_result["msg_id"])
        ),
        "T13",
    )
    assert_transition(
        engine.send(
            path_update_draft(
                engine.trace_id, has_next=False, learning_goal_achieved=True
            )
        ),
        "T20",
    )
    expected = [
        "S0_INIT",
        "S1_DIAGNOSIS",
        "S2_KNOWLEDGE",
        "S5_REVIEW",
        "S3_TASK",
        "S5_REVIEW",
        "S7_STUDENT",
        "S4_VERIFY",
        "S5_REVIEW",
        "S9_PATH_UPDATE",
        "S10_DONE",
    ]

    messages = read_trace(bus, engine.trace_id)
    assert [state.value for state in engine.state_history] == expected
    assert replay_states(messages) == expected
    assert [message["step"] for message in messages] == list(
        range(1, len(messages) + 1)
    )
    message_ids = {message["msg_id"] for message in messages}
    for message in messages:
        assert validate_message(message) == []
        content = message.get("payload", {}).get("content", {})
        if "transition_id" in content:
            assert content["based_on_msg_id"] in message_ids


def test_t14_correct_student_answer_reaches_path_update(tmp_path: Path) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-t14")
    drive_to_s7(engine, stubs)

    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "correct")), "T14"
    )

    assert engine.state is State.S9_PATH_UPDATE


def verification_failure_draft(trace_id: str, event: str) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {
                "event": event,
                "student_message": "本次查询未形成可审核的数据结论。",
            },
        },
        "evidence": [],
        "claims": [],
        "timestamp": "2026-07-24T10:00:00+08:00",
    }


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
def test_s4_known_verification_failure_has_auditable_retry_transition(
    tmp_path: Path,
    event: str,
    expected_outcome: str,
) -> None:
    engine, bus, stubs = build_runtime(tmp_path, f"trace-s4-{event}")
    drive_to_s7(engine, stubs)
    assert_transition(engine.send(student_sql_draft(engine.trace_id)), "T11")

    result = engine.send(verification_failure_draft(engine.trace_id, event))

    assert_transition(result, "T21")
    assert result.reason is None
    assert engine.state is State.S7_STUDENT
    transition_content = read_trace(bus, engine.trace_id)[-1]["payload"]["content"]
    assert transition_content["outcome"] == expected_outcome
    assert transition_content["based_on_msg_id"] == result.bus_result.message["msg_id"]


def test_s4_unknown_verification_event_is_not_misclassified_as_a_safe_result(
    tmp_path: Path,
) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-s4-unknown-event")
    drive_to_s7(engine, stubs)
    assert_transition(engine.send(student_sql_draft(engine.trace_id)), "T11")

    result = engine.send(
        verification_failure_draft(engine.trace_id, "unexpected_internal_event")
    )

    assert result.bus_result.accepted
    assert not result.transitioned
    assert result.reason == "no_matching_transition"
    assert engine.state is State.S4_VERIFY


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("agent", "diagnosis"),
        ("role", "probe"),
        ("payload_type", "quiz_set"),
    ),
)
def test_t21_guard_requires_exact_verification_result_envelope(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    engine, _, stubs = build_runtime(tmp_path, f"trace-t21-envelope-{field}")
    drive_to_s7(engine, stubs)
    assert_transition(engine.send(student_sql_draft(engine.trace_id)), "T11")
    draft = verification_failure_draft(engine.trace_id, "sandbox_rejected")
    if field == "payload_type":
        draft["payload"]["type"] = value
    else:
        draft[field] = value

    result = engine.send(draft)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert result.reason == "no_matching_transition"
    assert engine.state is State.S4_VERIFY


def test_t21_guard_is_s4_only_and_does_not_capture_probe_audits(
    tmp_path: Path,
) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-t21-s4-only")
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")),
        "T15",
    )
    assert engine.state is State.S8_PROBE

    result = engine.send(
        verification_failure_draft(engine.trace_id, "sandbox_rejected")
    )

    assert result.bus_result.accepted
    assert not result.transitioned
    assert result.reason == "no_matching_transition"
    assert engine.state is State.S8_PROBE
    transition_ids = [
        message["payload"]["content"].get("transition_id")
        for message in read_trace(bus, engine.trace_id)
        if message.get("role") == "system"
    ]
    assert "T21" not in transition_ids


def test_t21_clears_abandoned_review_retry_context_before_student_retries(
    tmp_path: Path,
) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-s4-fresh-retry")
    product = drive_to_review(engine, stubs, "sql_result")
    assert_transition(
        engine.send(
            stubs.review.verdict("reject", "sql_result", product["msg_id"])
        ),
        "T05",
    )
    assert_transition(
        engine.send(
            stubs.review.re_verdict("reject", "sql_result", product["msg_id"])
        ),
        "T07",
    )

    abandoned = assert_transition(
        engine.send(
            verification_failure_draft(engine.trace_id, "sandbox_rejected")
        ),
        "T21",
    )
    assert abandoned["retry"]["retry_count"] == 1
    assert engine.state is State.S7_STUDENT

    assert_transition(engine.send(student_sql_draft(engine.trace_id)), "T11")
    fresh_product = assert_transition(
        engine.send(stubs.verification.sql_result()),
        "T12",
    )
    assert "retry" not in fresh_product
    assert_transition(
        engine.send(
            stubs.review.verdict(
                "reject",
                "sql_result",
                fresh_product["msg_id"],
            )
        ),
        "T05",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("agent", "task"),
        ("role", "produce"),
        ("payload_type", "quiz_set"),
        ("source", "teacher"),
    ),
)
def test_t11_requires_complete_student_control_envelope(
    tmp_path: Path, field: str, value: str
) -> None:
    engine, _, stubs = build_runtime(tmp_path, f"trace-envelope-t11-{field}")
    drive_to_s7(engine, stubs)
    draft = student_sql_draft(engine.trace_id)
    if field == "payload_type":
        draft["payload"]["type"] = value
    elif field == "source":
        draft["payload"]["content"]["source"] = value
    else:
        draft[field] = value

    result = engine.send(draft)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert engine.state is State.S7_STUDENT


@pytest.mark.parametrize("answer_result", ("correct", "wrong"))
def test_student_answer_guards_require_student_source(
    tmp_path: Path, answer_result: str
) -> None:
    engine, _, stubs = build_runtime(
        tmp_path, f"trace-envelope-answer-{answer_result}"
    )
    drive_to_s7(engine, stubs)
    draft = student_answer_draft(engine.trace_id, answer_result)
    draft["payload"]["content"]["source"] = "teacher"

    result = engine.send(draft)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert engine.state is State.S7_STUDENT


def test_t19_path_with_next_step_returns_to_task(tmp_path: Path) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-t19")
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "correct")), "T14"
    )

    assert_transition(
        engine.send(
            path_update_draft(
                engine.trace_id, has_next=True, learning_goal_achieved=False
            )
        ),
        "T19",
    )

    assert engine.state is State.S3_TASK


def drive_to_review(
    engine: OrchestratorEngine, stubs: Any, payload_type: str
) -> dict[str, Any]:
    drive_to_s2(engine, stubs)
    lecture = assert_transition(engine.send(stubs.knowledge.lecture()), "T03")
    if payload_type == "lecture_note":
        return lecture
    assert_transition(
        engine.send(
            stubs.review.verdict("approve", "lecture_note", lecture["msg_id"])
        ),
        "T04",
    )
    task_type = payload_type if payload_type in {"quiz_set", "practice_guide"} else "quiz_set"
    task = assert_transition(engine.send(stubs.task.task(task_type)), "T09")
    if payload_type in {"quiz_set", "practice_guide"}:
        return task
    assert_transition(
        engine.send(stubs.review.verdict("approve", task_type, task["msg_id"])),
        "T10",
    )
    assert_transition(engine.send(student_sql_draft(engine.trace_id)), "T11")
    return assert_transition(engine.send(stubs.verification.sql_result()), "T12")


def regeneration_draft(stubs: Any, payload_type: str) -> dict[str, Any]:
    if payload_type == "lecture_note":
        return stubs.knowledge.lecture()
    if payload_type in {"quiz_set", "practice_guide"}:
        return stubs.task.task(payload_type)
    return stubs.verification.sql_result()


def rebuttal_draft(
    stubs: Any, payload_type: str, product_msg_id: str
) -> dict[str, Any]:
    if payload_type == "lecture_note":
        return stubs.knowledge.rebuttal(product_msg_id)
    if payload_type in {"quiz_set", "practice_guide"}:
        return stubs.task.rebuttal(product_msg_id)
    return stubs.verification.rebuttal(product_msg_id)


@pytest.mark.parametrize(
    ("payload_type", "expected_state"),
    (
        ("lecture_note", State.S3_TASK),
        ("quiz_set", State.S7_STUDENT),
        ("practice_guide", State.S7_STUDENT),
        ("sql_result", State.S9_PATH_UPDATE),
    ),
)
def test_t06_re_verdict_approve_resolves_original_downstream(
    tmp_path: Path, payload_type: str, expected_state: State
) -> None:
    engine, _, stubs = build_runtime(tmp_path, f"trace-t06-{payload_type}")
    product = drive_to_review(engine, stubs, payload_type)
    assert_transition(
        engine.send(
            stubs.review.verdict("reject", payload_type, product["msg_id"])
        ),
        "T05",
    )
    rebuttal = engine.send(rebuttal_draft(stubs, payload_type, product["msg_id"]))
    assert rebuttal.bus_result.accepted and not rebuttal.transitioned

    assert_transition(
        engine.send(
            stubs.review.re_verdict("approve", payload_type, product["msg_id"])
        ),
        "T06",
    )

    assert engine.state is expected_state


def test_review_guards_require_review_agent_and_review_payload(tmp_path: Path) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-envelope-review")
    product = drive_to_review(engine, stubs, "lecture_note")
    wrong_actor = stubs.review.verdict(
        "approve", "lecture_note", product["msg_id"]
    )
    wrong_actor["agent"] = "system"

    result = engine.send(wrong_actor)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert engine.state is State.S5_REVIEW


@pytest.mark.parametrize(
    ("payload_type", "expected_state", "product_transition"),
    (
        ("lecture_note", State.S2_KNOWLEDGE, "T03"),
        ("quiz_set", State.S3_TASK, "T09"),
        ("practice_guide", State.S3_TASK, "T09"),
        ("sql_result", State.S4_VERIFY, "T12"),
    ),
)
def test_t07_returns_to_producer_and_engine_injects_retry_metadata(
    tmp_path: Path,
    payload_type: str,
    expected_state: State,
    product_transition: str,
) -> None:
    engine, _, stubs = build_runtime(tmp_path, f"trace-t07-{payload_type}")
    product = drive_to_review(engine, stubs, payload_type)
    assert_transition(
        engine.send(
            stubs.review.verdict("reject", payload_type, product["msg_id"])
        ),
        "T05",
    )

    assert_transition(
        engine.send(
            stubs.review.re_verdict("reject", payload_type, product["msg_id"])
        ),
        "T07",
    )
    assert engine.state is expected_state

    regenerated = assert_transition(
        engine.send(regeneration_draft(stubs, payload_type)), product_transition
    )
    assert regenerated["retry"]["retry_count"] == 1
    assert regenerated["retry"]["in_reply_to"] == product["msg_id"]
    assert engine.state is State.S5_REVIEW


def drive_to_third_review_reject(
    engine: OrchestratorEngine,
    stubs: Any,
    fallback: str | None,
) -> Any:
    drive_to_s2(engine, stubs)
    product = assert_transition(
        engine.send(stubs.knowledge.lecture(fallback=fallback)), "T03"
    )
    for expected_retry in (1, 2):
        assert_transition(
            engine.send(
                stubs.review.verdict("reject", "lecture_note", product["msg_id"])
            ),
            "T05",
        )
        rebuttal = engine.send(stubs.knowledge.rebuttal(product["msg_id"]))
        assert rebuttal.bus_result.accepted and not rebuttal.transitioned
        assert_transition(
            engine.send(
                stubs.review.re_verdict(
                    "reject", "lecture_note", product["msg_id"]
                )
            ),
            "T07",
        )
        product = assert_transition(engine.send(stubs.knowledge.lecture()), "T03")
        assert product["retry"]["retry_count"] == expected_retry

    third_reject = stubs.review.verdict(
        "reject", "lecture_note", product["msg_id"]
    )
    third_reject["retry"] = {"retry_count": 0, "max_retries": 2}
    return engine.send(third_reject)


@pytest.mark.parametrize("fallback", (None, "degrade_to_template"))
def test_t08_template_fallback_is_explicit_valid_and_returns_downstream(
    tmp_path: Path, fallback: str | None
) -> None:
    engine, bus, stubs = build_runtime(tmp_path, f"trace-t08-template-{fallback}")

    result = drive_to_third_review_reject(engine, stubs, fallback)

    assert_transition(result, "T08")
    assert engine.state is State.S3_TASK
    template_messages = [
        message
        for message in read_trace(bus, engine.trace_id)
        if message.get("payload", {}).get("content", {}).get("generated_by")
        == "template_fallback"
    ]
    assert len(template_messages) == 1
    assert template_messages[0]["payload"]["type"] == "lecture_note"
    assert validate_message(template_messages[0]) == []


@pytest.mark.parametrize(
    ("fallback", "terminal_marker"),
    (
        ("human_review", "human_review_required"),
        ("refuse", "student_visible_refusal"),
    ),
)
def test_t08_terminal_fallbacks_reach_s_fail_with_explicit_marker(
    tmp_path: Path, fallback: str, terminal_marker: str
) -> None:
    engine, bus, stubs = build_runtime(tmp_path, f"trace-t08-{fallback}")

    result = drive_to_third_review_reject(engine, stubs, fallback)

    assert_transition(result, "T08")
    assert engine.state is State.S_FAIL
    terminal_content = read_trace(bus, engine.trace_id)[-1]["payload"]["content"]
    assert terminal_content["fallback_action"] == fallback
    assert terminal_content[terminal_marker] is True
    before_lines = len(read_trace(bus, engine.trace_id))

    late = engine.send(profile_loaded_draft(engine.trace_id))

    assert not late.bus_result.accepted
    assert engine.state is State.S_FAIL
    assert len(read_trace(bus, engine.trace_id)) == before_lines + 1


def test_every_transition_guard_has_an_engine_implementation() -> None:
    assert set(GUARDS) == {transition.guard for transition in TRANSITIONS}


def test_guard_registry_validation_rejects_missing_guard() -> None:
    incomplete = {"profile_loaded": GUARDS["profile_loaded"]}

    with pytest.raises(TransitionResolutionError, match="guard registry mismatch"):
        validate_guard_registry(incomplete, TRANSITIONS)


def test_t15_then_t16_runs_the_corrected_probe_path(tmp_path: Path) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-probe-corrected")
    drive_to_s7(engine, stubs)

    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")), "T15"
    )
    assert engine.state is State.S8_PROBE
    assert_transition(
        engine.send(probe_outcome_draft(engine.trace_id, "correct", "M-01")),
        "T16",
    )

    assert engine.state is State.S9_PATH_UPDATE
    assert [state.value for state in engine.state_history[-3:]] == [
        "S7_STUDENT",
        "S8_PROBE",
        "S9_PATH_UPDATE",
    ]
    assert replay_states(read_trace(bus, engine.trace_id)) == [
        "S0_INIT",
        "S1_DIAGNOSIS",
        "S2_KNOWLEDGE",
        "S5_REVIEW",
        "S3_TASK",
        "S5_REVIEW",
        "S7_STUDENT",
        "S8_PROBE",
        "S9_PATH_UPDATE",
    ]


def test_t17_uses_engine_wrong_count_despite_forged_message_count(
    tmp_path: Path,
) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-probe-wrong")
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")), "T15"
    )

    assert_transition(
        engine.send(
            probe_outcome_draft(
                engine.trace_id, "wrong", "M-01", wrong_attempts=1
            )
        ),
        "T17",
    )

    assert engine.state is State.S2_KNOWLEDGE
    assert read_trace(bus, engine.trace_id)[-1]["payload"]["content"][
        "difficulty_action"
    ] == "step_down"


@pytest.mark.parametrize("answer_result", ("correct", "wrong"))
def test_probe_outcome_guards_require_student_source(
    tmp_path: Path, answer_result: str
) -> None:
    engine, _, stubs = build_runtime(
        tmp_path, f"trace-envelope-probe-{answer_result}"
    )
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")), "T15"
    )
    draft = probe_outcome_draft(engine.trace_id, answer_result, "M-01")
    draft["payload"]["content"]["source"] = "teacher"

    result = engine.send(draft)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert engine.state is State.S8_PROBE


def test_t18_unknown_misconception_returns_to_generic_student_prompt(
    tmp_path: Path,
) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-probe-unknown")
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")), "T15"
    )

    assert_transition(engine.send(stubs.diagnosis.probe("UNKNOWN")), "T18")

    assert engine.state is State.S7_STUDENT


def test_t18_requires_probe_questions_payload(tmp_path: Path) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-envelope-t18")
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")), "T15"
    )
    wrong_payload = stubs.diagnosis.probe("UNKNOWN")
    wrong_payload["payload"]["type"] = "quiz_set"

    result = engine.send(wrong_payload)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert engine.state is State.S8_PROBE


def test_t18_resets_the_first_wrong_boundary_for_the_generic_prompt(
    tmp_path: Path,
) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-probe-unknown-reset")
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")),
        "T15",
    )
    assert_transition(engine.send(stubs.diagnosis.probe("UNKNOWN")), "T18")

    retried = engine.send(student_answer_draft(engine.trace_id, "wrong"))

    assert_transition(retried, "T15")
    assert engine.state is State.S8_PROBE


def test_t18_uses_the_active_domain_misconception_vocabulary(
    tmp_path: Path,
) -> None:
    trace_id = "trace-probe-first-segment"
    bus = MessageBus(tmp_path)
    engine = OrchestratorEngine(
        bus,
        trace_id,
        "cs_student",
        known_misconceptions=("M-FS01",),
    )
    stubs = build_stubs(trace_id)
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "wrong")),
        "T15",
    )

    result = engine.send(stubs.diagnosis.probe("M-FS01"))

    assert result.bus_result.accepted
    assert not result.transitioned
    assert result.reason == "no_matching_transition"
    assert engine.state is State.S8_PROBE


def test_first_wrong_cannot_request_direct_answer(tmp_path: Path) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-no-direct-answer")
    drive_to_s7(engine, stubs)
    before_lines = len(read_trace(bus, engine.trace_id))

    with pytest.raises(InvariantViolation, match="T15"):
        engine.send(
            student_answer_draft(
                engine.trace_id, "wrong", requested_action="give_answer"
            )
        )

    assert engine.state is State.S7_STUDENT
    assert len(read_trace(bus, engine.trace_id)) == before_lines + 1


def test_lecture_cannot_bypass_review_and_reach_student(tmp_path: Path) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-no-review-bypass")
    drive_to_s2(engine, stubs)
    assert_transition(engine.send(stubs.knowledge.lecture()), "T03")
    bypass = control_draft(engine.trace_id, action="show_lecture_to_student")

    result = engine.send(bypass)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert result.reason == "no_matching_transition"
    assert engine.state is State.S5_REVIEW


def test_sender_cannot_set_regeneration_retry_metadata(tmp_path: Path) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-retry-owned")
    product = drive_to_review(engine, stubs, "lecture_note")
    assert_transition(
        engine.send(
            stubs.review.verdict("reject", "lecture_note", product["msg_id"])
        ),
        "T05",
    )
    assert_transition(
        engine.send(
            stubs.review.re_verdict("reject", "lecture_note", product["msg_id"])
        ),
        "T07",
    )
    forged = stubs.knowledge.lecture()
    forged["retry"] = {
        "retry_count": 1,
        "in_reply_to": "forged-product",
        "fallback": "refuse",
    }
    before_lines = len(read_trace(bus, engine.trace_id))

    with pytest.raises(InvariantViolation, match="retry_count"):
        engine.send(forged)

    assert engine.state is State.S2_KNOWLEDGE
    assert len(read_trace(bus, engine.trace_id)) == before_lines + 1


def test_third_debate_entry_is_blocked_by_t08(tmp_path: Path) -> None:
    engine, _, stubs = build_runtime(tmp_path, "trace-no-third-debate")

    result = drive_to_third_review_reject(engine, stubs, None)

    assert_transition(result, "T08")
    assert engine.state_history.count(State.S6_DEBATE) == 2


def test_bus_rejection_never_changes_engine_state(tmp_path: Path) -> None:
    engine, _, _ = build_runtime(tmp_path, "trace-invalid-input")
    invalid = profile_loaded_draft(engine.trace_id)
    invalid.pop("timestamp")

    result = engine.send(invalid)

    assert not result.bus_result.accepted
    assert result.reason == "rejected_by_bus"
    assert engine.state is State.S0_INIT


def test_done_state_is_absorbing_and_late_message_is_logged(tmp_path: Path) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-absorbing")
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "correct")), "T14"
    )
    assert_transition(
        engine.send(
            path_update_draft(
                engine.trace_id, has_next=False, learning_goal_achieved=True
            )
        ),
        "T20",
    )
    before_lines = len(read_trace(bus, engine.trace_id))

    result = engine.send(profile_loaded_draft(engine.trace_id))

    assert not result.bus_result.accepted
    assert result.bus_result.message["rejected_by_bus"] is True
    assert engine.state is State.S10_DONE
    assert len(read_trace(bus, engine.trace_id)) == before_lines + 1


@pytest.mark.parametrize(
    ("has_next", "goal"),
    ((True, False), (False, True)),
)
def test_path_update_guards_require_system_learning_path_message(
    tmp_path: Path, has_next: bool, goal: bool
) -> None:
    engine, _, stubs = build_runtime(
        tmp_path, f"trace-envelope-path-{has_next}-{goal}"
    )
    drive_to_s7(engine, stubs)
    assert_transition(
        engine.send(student_answer_draft(engine.trace_id, "correct")), "T14"
    )
    wrong_actor = path_update_draft(
        engine.trace_id,
        has_next=has_next,
        learning_goal_achieved=goal,
    )
    wrong_actor["agent"] = "task"

    result = engine.send(wrong_actor)

    assert result.bus_result.accepted
    assert not result.transitioned
    assert engine.state is State.S9_PATH_UPDATE


def exercise_transition(
    tmp_path: Path, transition_id: str
) -> tuple[Any, OrchestratorEngine, MessageBus]:
    engine, bus, stubs = build_runtime(tmp_path, f"trace-row-{transition_id}")
    if transition_id == "T01":
        result = engine.send(profile_loaded_draft(engine.trace_id))
    elif transition_id == "T02":
        assert_transition(engine.send(profile_loaded_draft(engine.trace_id)), "T01")
        result = engine.send(stubs.diagnosis.profile_assessment())
    elif transition_id == "T03":
        drive_to_s2(engine, stubs)
        result = engine.send(stubs.knowledge.lecture())
    elif transition_id in {"T04", "T05", "T06", "T07"}:
        product = drive_to_review(engine, stubs, "lecture_note")
        if transition_id == "T04":
            result = engine.send(
                stubs.review.verdict("approve", "lecture_note", product["msg_id"])
            )
        else:
            reject_result = engine.send(
                stubs.review.verdict(
                    "reject", "lecture_note", product["msg_id"]
                )
            )
            assert_transition(
                reject_result,
                "T05",
            )
            if transition_id == "T05":
                result = reject_result
            else:
                rebuttal = engine.send(stubs.knowledge.rebuttal(product["msg_id"]))
                assert rebuttal.bus_result.accepted and not rebuttal.transitioned
                decision = "approve" if transition_id == "T06" else "reject"
                result = engine.send(
                    stubs.review.re_verdict(
                        decision, "lecture_note", product["msg_id"]
                    )
                )
    elif transition_id == "T08":
        result = drive_to_third_review_reject(engine, stubs, None)
    elif transition_id == "T09":
        lecture = drive_to_review(engine, stubs, "lecture_note")
        assert_transition(
            engine.send(
                stubs.review.verdict(
                    "approve", "lecture_note", lecture["msg_id"]
                )
            ),
            "T04",
        )
        result = engine.send(stubs.task.task("quiz_set"))
    elif transition_id == "T10":
        task = drive_to_review(engine, stubs, "quiz_set")
        result = engine.send(
            stubs.review.verdict("approve", "quiz_set", task["msg_id"])
        )
    elif transition_id in {"T11", "T12", "T21"}:
        drive_to_s7(engine, stubs)
        sql_submit = engine.send(student_sql_draft(engine.trace_id))
        if transition_id == "T11":
            result = sql_submit
        elif transition_id == "T12":
            result = engine.send(stubs.verification.sql_result())
        else:
            result = engine.send(
                verification_failure_draft(engine.trace_id, "sandbox_rejected")
            )
    elif transition_id == "T13":
        sql_result = drive_to_review(engine, stubs, "sql_result")
        result = engine.send(
            stubs.review.verdict("approve", "sql_result", sql_result["msg_id"])
        )
    elif transition_id in {"T14", "T15", "T16", "T17", "T18"}:
        drive_to_s7(engine, stubs)
        if transition_id == "T14":
            result = engine.send(student_answer_draft(engine.trace_id, "correct"))
        else:
            first_wrong = engine.send(
                student_answer_draft(engine.trace_id, "wrong")
            )
            if transition_id == "T15":
                result = first_wrong
            elif transition_id == "T16":
                result = engine.send(
                    probe_outcome_draft(engine.trace_id, "correct", "M-01")
                )
            elif transition_id == "T17":
                result = engine.send(
                    probe_outcome_draft(engine.trace_id, "wrong", "M-01")
                )
            else:
                result = engine.send(stubs.diagnosis.probe("UNKNOWN"))
    else:
        drive_to_s7(engine, stubs)
        assert_transition(
            engine.send(student_answer_draft(engine.trace_id, "correct")), "T14"
        )
        result = engine.send(
            path_update_draft(
                engine.trace_id,
                has_next=transition_id == "T19",
                learning_goal_achieved=transition_id == "T20",
            )
        )
    return result, engine, bus


@pytest.mark.parametrize(
    "transition",
    TRANSITIONS,
    ids=[transition.transition_id for transition in TRANSITIONS],
)
def test_every_transition_row_is_triggered_from_a_legal_s0_prefix(
    tmp_path: Path, transition: Any
) -> None:
    result, _, _ = exercise_transition(tmp_path, transition.transition_id)

    assert result.transition is not None
    assert result.transition.transition_id == transition.transition_id


def test_debate_trace_replays_exactly_and_contains_rebuttal(tmp_path: Path) -> None:
    engine, bus, stubs = build_runtime(tmp_path, "trace-debate-exact")
    product = drive_to_review(engine, stubs, "lecture_note")
    assert_transition(
        engine.send(
            stubs.review.verdict("reject", "lecture_note", product["msg_id"])
        ),
        "T05",
    )
    rebuttal = engine.send(stubs.knowledge.rebuttal(product["msg_id"]))
    assert rebuttal.bus_result.accepted and not rebuttal.transitioned
    assert_transition(
        engine.send(
            stubs.review.re_verdict("approve", "lecture_note", product["msg_id"])
        ),
        "T06",
    )
    messages = read_trace(bus, engine.trace_id)

    assert replay_states(messages) == [
        "S0_INIT",
        "S1_DIAGNOSIS",
        "S2_KNOWLEDGE",
        "S5_REVIEW",
        "S6_DEBATE",
        "S3_TASK",
    ]
    assert sum(message.get("role") == "rebuttal" for message in messages) == 1


@pytest.mark.parametrize(
    ("payload_type", "transition_id", "expected_state"),
    (
        ("lecture_note", "T04", State.S3_TASK),
        ("quiz_set", "T10", State.S7_STUDENT),
        ("sql_result", "T13", State.S9_PATH_UPDATE),
    ),
)
def test_approve_with_fix_uses_existing_approval_transition_and_traces_action(
    tmp_path: Path,
    payload_type: str,
    transition_id: str,
    expected_state: State,
) -> None:
    engine, bus, stubs = build_runtime(
        tmp_path, f"trace-approve-with-fix-{payload_type}"
    )
    product = drive_to_review(engine, stubs, payload_type)
    verdict = stubs.review.verdict(
        "approve_with_fix", payload_type, product["msg_id"]
    )
    verdict["verdict"]["difficulty_action"] = "step_down"

    result = engine.send(verdict)

    assert_transition(result, transition_id)
    assert engine.state is expected_state
    transition_record = read_trace(bus, engine.trace_id)[-1]
    assert transition_record["payload"]["content"]["difficulty_action"] == "step_down"


def test_demo_prints_exact_state_sequence_and_message_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = demo.main(trace_dir=tmp_path, trace_id="trace-demo-test")

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "Trace ID: trace-demo-test",
        "State sequence: S0_INIT -> S1_DIAGNOSIS -> S2_KNOWLEDGE -> "
        "S5_REVIEW -> S3_TASK -> S5_REVIEW -> S7_STUDENT -> S4_VERIFY -> "
        "S5_REVIEW -> S9_PATH_UPDATE -> S10_DONE",
        "Message count: 21",
    ]
