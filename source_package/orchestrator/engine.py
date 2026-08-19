"""Explicit transition-table orchestrator engine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from orchestrator.bus import BusResult, MessageBus
from orchestrator.outcomes import verification_outcome
from orchestrator.transitions import (
    DynamicTarget,
    State,
    TRANSITIONS,
    Transition,
    resolve_downstream,
    resolve_producer,
)


class InvariantViolation(RuntimeError):
    """Raised when an input attempts a forbidden engine path."""


class TransitionResolutionError(RuntimeError):
    """Raised when a message ambiguously matches the transition table."""


DEFAULT_FALLBACK = "degrade_to_template"
MAX_RETRIES = 2


@dataclass(frozen=True, slots=True)
class ReviewContext:
    payload_type: str
    producer_agent: str
    product_msg_id: str
    fallback: str = DEFAULT_FALLBACK
    retry_count: int = 0


@dataclass(frozen=True, slots=True)
class EngineResult:
    bus_result: BusResult
    transition: Transition | None
    from_state: State
    to_state: State
    transitioned: bool
    reason: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    content = payload.get("content")
    return content if isinstance(content, Mapping) else {}


def _payload_type(message: Mapping[str, Any]) -> str | None:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("type")
    return value if isinstance(value, str) else None


def _decision(message: Mapping[str, Any]) -> str | None:
    verdict = message.get("verdict")
    if not isinstance(verdict, Mapping):
        return None
    value = verdict.get("decision")
    return value if isinstance(value, str) else None


def _has_envelope(
    message: Mapping[str, Any],
    *,
    agent: str,
    role: str,
    payload_type: str,
) -> bool:
    return (
        message.get("agent") == agent
        and message.get("role") == role
        and _payload_type(message) == payload_type
    )


def _is_student_control(message: Mapping[str, Any]) -> bool:
    return _has_envelope(
        message,
        agent="system",
        role="system",
        payload_type="control",
    ) and _content(message).get("source") == "student"


def _state_matches(transition: Transition, state: State) -> bool:
    source = transition.from_state
    return state in source if isinstance(source, tuple) else source is state


Guard = Callable[["OrchestratorEngine", Mapping[str, Any]], bool]


def guard_profile_loaded(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return _has_envelope(
        message,
        agent="system",
        role="system",
        payload_type="control",
    ) and _content(message).get("action") == "profile_loaded"


def guard_diagnosis_ready(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return (
        message.get("agent") == "diagnosis"
        and message.get("role") == "produce"
        and _payload_type(message) == "profile_assessment"
    )


def guard_lecture_produced(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return (
        message.get("agent") == "knowledge"
        and message.get("role") == "produce"
        and _payload_type(message) == "lecture_note"
        and _content(message).get("event") == "product_ready"
    )


def _approved_review(
    engine: "OrchestratorEngine",
    message: Mapping[str, Any],
    payload_types: set[str],
) -> bool:
    context = engine._review_context
    decision = _decision(message)
    return (
        context is not None
        and context.payload_type in payload_types
        and decision in {"approve", "approve_with_fix"}
        and _review_matches_context(
            engine, message, role="verdict", decision=str(decision)
        )
    )


def _review_matches_context(
    engine: "OrchestratorEngine",
    message: Mapping[str, Any],
    *,
    role: str,
    decision: str,
) -> bool:
    context = engine._review_context
    content = _content(message)
    return (
        context is not None
        and _has_envelope(
            message,
            agent="review",
            role=role,
            payload_type="review_verdict",
        )
        and _decision(message) == decision
        and content.get("reviewed_payload_type") == context.payload_type
        and content.get("reviewed_msg_id") == context.product_msg_id
    )


def guard_approved_lecture(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return _approved_review(engine, message, {"lecture_note"})


def guard_review_rejected_below_limit(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    context = engine._review_context
    return (
        context is not None
        and context.retry_count < MAX_RETRIES
        and _review_matches_context(
            engine, message, role="verdict", decision="reject"
        )
    )


def guard_re_verdict_approved(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return _review_matches_context(
        engine, message, role="re_verdict", decision="approve"
    )


def guard_re_verdict_rejected_below_limit(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    context = engine._review_context
    return (
        context is not None
        and context.retry_count < MAX_RETRIES
        and _review_matches_context(
            engine, message, role="re_verdict", decision="reject"
        )
    )


def guard_retry_limit_reached(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    context = engine._review_context
    if context is None or context.retry_count < MAX_RETRIES:
        return False
    role = str(message.get("role", ""))
    return role in {"verdict", "re_verdict"} and _review_matches_context(
        engine, message, role=role, decision="reject"
    )


def guard_task_produced(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return (
        message.get("agent") == "task"
        and message.get("role") == "produce"
        and _payload_type(message) in {"quiz_set", "practice_guide"}
    )


def guard_approved_task(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return _approved_review(engine, message, {"quiz_set", "practice_guide"})


def guard_student_sql_submitted(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return _is_student_control(message) and _content(message).get(
        "event"
    ) == "student_sql_submitted"


def guard_query_completed(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return (
        message.get("agent") == "verification"
        and message.get("role") == "produce"
        and _payload_type(message) == "sql_result"
        and _content(message).get("event") == "query_completed"
    )


def guard_approved_sql_result(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return _approved_review(engine, message, {"sql_result"})


def guard_student_answer_correct(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    content = _content(message)
    return (
        _is_student_control(message)
        and content.get("event") == "student_answer"
        and content.get("answer_result") == "correct"
    )


def guard_student_answer_first_wrong(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    content = _content(message)
    return (
        engine._wrong_attempts == 0
        and _is_student_control(message)
        and content.get("event") == "student_answer"
        and content.get("answer_result") == "wrong"
        and content.get("requested_action") == "probe"
    )


def guard_probe_corrected(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    content = _content(message)
    return (
        _is_student_control(message)
        and content.get("event") == "probe_outcome"
        and content.get("answer_result") == "correct"
    )


def guard_probe_second_wrong(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    content = _content(message)
    return (
        engine._wrong_attempts >= 1
        and _is_student_control(message)
        and content.get("event") == "probe_outcome"
        and content.get("answer_result") == "wrong"
    )


KNOWN_MISCONCEPTIONS = frozenset({"M-01", "M-02", "M-03", "M-04", "M-05"})


def guard_verification_failed(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    return (
        message.get("agent") == "verification"
        and message.get("role") == "produce"
        and _payload_type(message) == "sql_result"
        and verification_outcome(_content(message).get("event")) is not None
    )


def guard_misconception_unknown(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    content = _content(message)
    target = content.get("target_misconception")
    return (
        _has_envelope(
            message,
            agent="diagnosis",
            role="probe",
            payload_type="probe_questions",
        )
        and content.get("event") == "misconception_diagnosed"
        and isinstance(target, str)
        and target not in engine._known_misconceptions
    )


def guard_path_has_next(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    content = _content(message)
    return (
        _has_envelope(
            message,
            agent="system",
            role="system",
            payload_type="learning_path_update",
        )
        and content.get("event") == "path_updated"
        and content.get("has_next") is True
        and content.get("learning_goal_achieved") is False
    )


def guard_learning_goal_achieved(
    engine: "OrchestratorEngine", message: Mapping[str, Any]
) -> bool:
    content = _content(message)
    return (
        _has_envelope(
            message,
            agent="system",
            role="system",
            payload_type="learning_path_update",
        )
        and content.get("event") == "path_updated"
        and content.get("learning_goal_achieved") is True
        and content.get("has_next") is False
    )


GUARDS: Mapping[str, Guard] = MappingProxyType(
    {
        "profile_loaded": guard_profile_loaded,
        "diagnosis_ready": guard_diagnosis_ready,
        "lecture_produced": guard_lecture_produced,
        "approved_lecture": guard_approved_lecture,
        "review_rejected_below_limit": guard_review_rejected_below_limit,
        "re_verdict_approved": guard_re_verdict_approved,
        "re_verdict_rejected_below_limit": guard_re_verdict_rejected_below_limit,
        "retry_limit_reached": guard_retry_limit_reached,
        "task_produced": guard_task_produced,
        "approved_task": guard_approved_task,
        "student_sql_submitted": guard_student_sql_submitted,
        "query_completed": guard_query_completed,
        "verification_failed": guard_verification_failed,
        "approved_sql_result": guard_approved_sql_result,
        "student_answer_correct": guard_student_answer_correct,
        "student_answer_first_wrong": guard_student_answer_first_wrong,
        "probe_corrected": guard_probe_corrected,
        "probe_second_wrong": guard_probe_second_wrong,
        "misconception_unknown": guard_misconception_unknown,
        "path_has_next": guard_path_has_next,
        "learning_goal_achieved": guard_learning_goal_achieved,
    }
)


def validate_guard_registry(
    guards: Mapping[str, Guard], transitions: list[Transition]
) -> None:
    expected = {transition.guard for transition in transitions}
    actual = set(guards)
    if actual != expected:
        raise TransitionResolutionError(
            "guard registry mismatch: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


validate_guard_registry(GUARDS, TRANSITIONS)


class OrchestratorEngine:
    """Hold state and commit only transitions already persisted by the bus."""

    def __init__(
        self,
        bus: MessageBus,
        trace_id: str,
        student_profile_ref: str,
        *,
        known_misconceptions: Sequence[str] = tuple(KNOWN_MISCONCEPTIONS),
    ) -> None:
        normalized_misconceptions = frozenset(
            value.strip()
            for value in known_misconceptions
            if isinstance(value, str) and value.strip()
        )
        if not normalized_misconceptions:
            raise ValueError("known_misconceptions must contain at least one value")
        self._bus = bus
        self._trace_id = trace_id
        self._known_misconceptions = normalized_misconceptions
        self._state = State.S0_INIT
        self._state_history = [self._state]
        self._review_context: ReviewContext | None = None
        self._regeneration_expected = False
        self._wrong_attempts = 0
        start_result = self._bus.send(
            {
                "trace_id": trace_id,
                "agent": "system",
                "role": "system",
                "payload": {
                    "type": "control",
                    "content": {
                        "action": "session_start",
                        "state": self._state.value,
                        "student_profile_ref": student_profile_ref,
                    },
                },
                "evidence": [],
                "student_profile_ref": student_profile_ref,
                "timestamp": _now(),
            }
        )
        if not start_result.accepted:
            raise RuntimeError(f"session_start rejected: {start_result.errors}")

    @property
    def trace_id(self) -> str:
        return self._trace_id

    @property
    def state(self) -> State:
        return self._state

    @property
    def state_history(self) -> tuple[State, ...]:
        return tuple(self._state_history)

    def send(self, draft: Mapping[str, Any]) -> EngineResult:
        from_state = self._state
        retry_violation = self._sender_owns_regeneration_fields(draft)
        prepared_draft = draft if retry_violation else self._prepare_regeneration(draft)
        bus_result = self._bus.send(prepared_draft)
        if not bus_result.accepted:
            return EngineResult(
                bus_result,
                None,
                from_state,
                from_state,
                False,
                "rejected_by_bus",
            )
        if bus_result.message.get("trace_id") != self._trace_id:
            return EngineResult(
                bus_result,
                None,
                from_state,
                from_state,
                False,
                "trace_id_mismatch",
            )
        if retry_violation:
            raise InvariantViolation(
                "regeneration retry_count and in_reply_to are engine-owned"
            )
        content = _content(bus_result.message)
        if (
            from_state is State.S7_STUDENT
            and content.get("event") == "student_answer"
            and content.get("answer_result") == "wrong"
            and content.get("requested_action") != "probe"
        ):
            raise InvariantViolation("first wrong answer must take T15 to S8")

        matches = [
            transition
            for transition in TRANSITIONS
            if _state_matches(transition, from_state)
            and transition.guard in GUARDS
            and GUARDS[transition.guard](self, bus_result.message)
        ]
        if not matches:
            return EngineResult(
                bus_result,
                None,
                from_state,
                from_state,
                False,
                "no_matching_transition",
            )
        if len(matches) > 1:
            raise TransitionResolutionError(
                f"multiple transitions matched {from_state.value}: "
                f"{[item.transition_id for item in matches]}"
            )

        transition = matches[0]
        to_state = self._resolve_target(transition)
        if transition.transition_id == "T08" and self._review_context is not None:
            if self._review_context.fallback == "degrade_to_template":
                template_result = self._bus.send(self._template_fallback_draft())
                if not template_result.accepted:
                    return EngineResult(
                        bus_result,
                        transition,
                        from_state,
                        from_state,
                        False,
                        "template_fallback_rejected",
                    )

        transition_content: dict[str, Any] = {
            "action": "state_transition",
            "transition_id": transition.transition_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
            "trigger": transition.trigger,
            "based_on_msg_id": bus_result.message["msg_id"],
        }
        if (
            transition.transition_id in {"T04", "T10", "T13"}
            and _decision(bus_result.message) == "approve_with_fix"
        ):
            verdict = bus_result.message.get("verdict")
            if isinstance(verdict, Mapping):
                difficulty_action = verdict.get("difficulty_action")
                if isinstance(difficulty_action, str):
                    transition_content["difficulty_action"] = difficulty_action
        if transition.transition_id == "T08" and self._review_context is not None:
            fallback = self._review_context.fallback
            transition_content["fallback_action"] = fallback
            if fallback == "human_review":
                transition_content["human_review_required"] = True
            elif fallback == "refuse":
                transition_content["student_visible_refusal"] = True
                transition_content["refusal_message"] = "当前请求无法安全完成。"
        elif transition.transition_id == "T21":
            event = _content(bus_result.message).get("event")
            outcome = verification_outcome(event)
            if outcome is None:
                raise TransitionResolutionError(
                    f"T21 has unknown verification event: {event}"
                )
            transition_content["outcome"] = outcome.value
        elif transition.transition_id == "T17":
            # The remediation decision (step_down / refresh / deferred) is
            # computed by the session before firing T17 and carried on the
            # probe-outcome draft.  Fall back to "step_down" for legacy
            # drafts that predate the metadata so the field is always set.
            probe_meta = bus_result.message.get("probe")
            stamped_action = (
                probe_meta.get("difficulty_action")
                if isinstance(probe_meta, Mapping)
                else None
            )
            transition_content["difficulty_action"] = (
                stamped_action if isinstance(stamped_action, str) and stamped_action
                else "step_down"
            )
        elif transition.transition_id == "T18":
            transition_content["fallback_action"] = "generic_probe"
        transition_result = self._bus.send(
            {
                "trace_id": self._trace_id,
                "agent": "system",
                "role": "system",
                "payload": {
                    "type": "control",
                    "content": transition_content,
                },
                "evidence": [],
                "timestamp": _now(),
            }
        )
        if not transition_result.accepted:
            return EngineResult(
                bus_result,
                transition,
                from_state,
                from_state,
                False,
                "transition_record_rejected",
            )

        if transition.transition_id in {"T03", "T09", "T12"}:
            retry = bus_result.message.get("retry")
            fallback = (
                retry.get("fallback", DEFAULT_FALLBACK)
                if isinstance(retry, Mapping)
                else DEFAULT_FALLBACK
            )
            if self._regeneration_expected and self._review_context is not None:
                self._review_context = ReviewContext(
                    payload_type=self._review_context.payload_type,
                    producer_agent=str(bus_result.message.get("agent", "")),
                    product_msg_id=str(bus_result.message["msg_id"]),
                    fallback=self._review_context.fallback,
                    retry_count=self._review_context.retry_count,
                )
                self._regeneration_expected = False
            else:
                self._review_context = ReviewContext(
                    payload_type=_payload_type(bus_result.message) or "",
                    producer_agent=str(bus_result.message.get("agent", "")),
                    product_msg_id=str(bus_result.message["msg_id"]),
                    fallback=str(fallback),
                )
        elif transition.transition_id == "T07" and self._review_context is not None:
            context = self._review_context
            self._review_context = ReviewContext(
                payload_type=context.payload_type,
                producer_agent=context.producer_agent,
                product_msg_id=context.product_msg_id,
                fallback=context.fallback,
                retry_count=context.retry_count + 1,
            )
            self._regeneration_expected = True
        elif transition.transition_id in {
            "T04",
            "T06",
            "T08",
            "T10",
            "T13",
            "T21",
        }:
            self._review_context = None
            self._regeneration_expected = False

        if transition.transition_id == "T10":
            self._wrong_attempts = 0
        elif transition.transition_id == "T14":
            self._wrong_attempts = 0
        elif transition.transition_id == "T15":
            self._wrong_attempts = 1
        elif transition.transition_id == "T16":
            self._wrong_attempts = 0
        elif transition.transition_id == "T17":
            self._wrong_attempts += 1
        elif transition.transition_id == "T18":
            self._wrong_attempts = 0

        self._state = to_state
        self._state_history.append(to_state)
        if to_state in {State.S10_DONE, State.S_FAIL}:
            self._bus.close_trace(self._trace_id, to_state.value)
        return EngineResult(
            bus_result,
            transition,
            from_state,
            to_state,
            True,
        )

    def _prepare_regeneration(
        self, draft: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        context = self._review_context
        if (
            not self._regeneration_expected
            or context is None
            or _payload_type(draft) != context.payload_type
            or draft.get("role") != "produce"
        ):
            return draft
        prepared = deepcopy(dict(draft))
        retry_value = prepared.get("retry")
        retry = dict(retry_value) if isinstance(retry_value, Mapping) else {}
        retry["retry_count"] = context.retry_count
        retry["in_reply_to"] = context.product_msg_id
        retry["fallback"] = context.fallback
        prepared["retry"] = retry
        return prepared

    def _sender_owns_regeneration_fields(
        self, draft: Mapping[str, Any]
    ) -> bool:
        context = self._review_context
        if (
            not self._regeneration_expected
            or context is None
            or _payload_type(draft) != context.payload_type
            or draft.get("role") != "produce"
        ):
            return False
        retry = draft.get("retry")
        return isinstance(retry, Mapping) and bool(
            {"retry_count", "in_reply_to"}.intersection(retry)
        )

    def _resolve_target(self, transition: Transition) -> State:
        target = transition.to_state
        if isinstance(target, State):
            return target
        context = self._review_context
        if context is None:
            raise TransitionResolutionError(
                f"{transition.transition_id} requires review context"
            )
        if target is DynamicTarget.ORIGINAL_DOWNSTREAM:
            return resolve_downstream(context.payload_type)
        if target is DynamicTarget.ORIGINAL_PRODUCER:
            return resolve_producer(context.payload_type)
        if target is DynamicTarget.FALLBACK:
            if context.fallback == "degrade_to_template":
                return resolve_downstream(context.payload_type)
            if context.fallback in {"human_review", "refuse"}:
                return State.S_FAIL
            raise TransitionResolutionError(f"unknown fallback: {context.fallback}")
        raise TransitionResolutionError(
            f"unknown dynamic target: {transition.to_state}"
        )

    def _template_fallback_draft(self) -> dict[str, Any]:
        context = self._review_context
        if context is None:
            raise TransitionResolutionError("template fallback requires review context")
        return {
            "trace_id": self._trace_id,
            "agent": context.producer_agent,
            "role": "produce",
            "payload": {
                "type": context.payload_type,
                "content": {
                    "event": "product_ready",
                    "payload_type": context.payload_type,
                    "generated_by": "template_fallback",
                },
            },
            "evidence": [],
            "retry": {
                "retry_count": context.retry_count,
                "max_retries": MAX_RETRIES,
                "in_reply_to": context.product_msg_id,
                "fallback": context.fallback,
            },
            "timestamp": _now(),
        }
