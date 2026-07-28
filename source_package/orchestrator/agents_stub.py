"""Deterministic, protocol-valid stubs for end-to-end orchestrator tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _draft(
    trace_id: str,
    agent: str,
    role: str,
    payload_type: str,
    content: dict[str, Any],
    *,
    evidence: list[dict[str, Any]] | None = None,
    verdict: dict[str, Any] | None = None,
    retry: dict[str, Any] | None = None,
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "trace_id": trace_id,
        "agent": agent,
        "role": role,
        "payload": {"type": payload_type, "content": content},
        "evidence": evidence or [],
        "timestamp": _now(),
    }
    if verdict is not None:
        message["verdict"] = verdict
    if retry is not None:
        message["retry"] = retry
    if probe is not None:
        message["probe"] = probe
    return message


@dataclass(frozen=True, slots=True)
class DiagnosisStub:
    trace_id: str

    def profile_assessment(self) -> dict[str, Any]:
        return _draft(
            self.trace_id,
            "diagnosis",
            "produce",
            "profile_assessment",
            {"event": "diagnosis_ready", "knowledge_gap": "M-01"},
        )

    def probe(self, misconception: str) -> dict[str, Any]:
        return _draft(
            self.trace_id,
            "diagnosis",
            "probe",
            "probe_questions",
            {
                "event": "misconception_diagnosed",
                "target_misconception": misconception,
            },
            probe={
                "wrong_attempts": 1,
                "questions": ["请对照计划量与实际量。"],
                "target_misconception": misconception,
            },
        )


@dataclass(frozen=True, slots=True)
class KnowledgeStub:
    trace_id: str

    def lecture(self, fallback: str | None = None) -> dict[str, Any]:
        retry = {"fallback": fallback} if fallback is not None else None
        return _draft(
            self.trace_id,
            "knowledge",
            "produce",
            "lecture_note",
            {"event": "product_ready", "payload_type": "lecture_note"},
            retry=retry,
        )

    def rebuttal(self, product_msg_id: str) -> dict[str, Any]:
        return _draft(
            self.trace_id,
            "knowledge",
            "rebuttal",
            "rebuttal_case",
            {"event": "rebuttal_ready", "product_msg_id": product_msg_id},
            retry={"in_reply_to": product_msg_id},
        )


@dataclass(frozen=True, slots=True)
class TaskStub:
    trace_id: str

    def task(
        self,
        payload_type: str = "quiz_set",
        fallback: str | None = None,
    ) -> dict[str, Any]:
        retry = {"fallback": fallback} if fallback is not None else None
        return _draft(
            self.trace_id,
            "task",
            "produce",
            payload_type,
            {"event": "product_ready", "payload_type": payload_type},
            retry=retry,
        )

    def rebuttal(self, product_msg_id: str) -> dict[str, Any]:
        return _draft(
            self.trace_id,
            "task",
            "rebuttal",
            "rebuttal_case",
            {"event": "rebuttal_ready", "product_msg_id": product_msg_id},
            retry={"in_reply_to": product_msg_id},
        )


@dataclass(frozen=True, slots=True)
class VerificationStub:
    trace_id: str

    def sql_result(self, fallback: str | None = None) -> dict[str, Any]:
        retry = {"fallback": fallback} if fallback is not None else None
        return _draft(
            self.trace_id,
            "verification",
            "produce",
            "sql_result",
            {"event": "query_completed", "payload_type": "sql_result"},
            retry=retry,
        )

    def rebuttal(self, product_msg_id: str) -> dict[str, Any]:
        return _draft(
            self.trace_id,
            "verification",
            "rebuttal",
            "rebuttal_case",
            {"event": "rebuttal_ready", "product_msg_id": product_msg_id},
            retry={"in_reply_to": product_msg_id},
        )


@dataclass(frozen=True, slots=True)
class ReviewStub:
    trace_id: str

    def verdict(
        self,
        decision: str,
        payload_type: str,
        reviewed_msg_id: str,
    ) -> dict[str, Any]:
        return self._review_message(
            "verdict", decision, payload_type, reviewed_msg_id
        )

    def re_verdict(
        self,
        decision: str,
        payload_type: str,
        reviewed_msg_id: str,
    ) -> dict[str, Any]:
        return self._review_message(
            "re_verdict", decision, payload_type, reviewed_msg_id
        )

    def _review_message(
        self,
        role: str,
        decision: str,
        payload_type: str,
        reviewed_msg_id: str,
    ) -> dict[str, Any]:
        return _draft(
            self.trace_id,
            "review",
            role,
            "review_verdict",
            {
                "event": "review_complete",
                "reviewed_payload_type": payload_type,
                "reviewed_msg_id": reviewed_msg_id,
            },
            evidence=[
                {
                    "kind": "review_rule",
                    "ref": "R-03",
                    "quote": "产物必须匹配学习路径。",
                }
            ],
            verdict={
                "decision": decision,
                "rule_hits": [
                    {
                        "rule_id": "R-03",
                        "reason": "固定桩裁决",
                        "evidence_ref": reviewed_msg_id,
                    }
                ],
                "difficulty_action": "none",
            },
        )


@dataclass(frozen=True, slots=True)
class StubSet:
    diagnosis: DiagnosisStub
    knowledge: KnowledgeStub
    task: TaskStub
    verification: VerificationStub
    review: ReviewStub


def build_stubs(trace_id: str) -> StubSet:
    return StubSet(
        diagnosis=DiagnosisStub(trace_id),
        knowledge=KnowledgeStub(trace_id),
        task=TaskStub(trace_id),
        verification=VerificationStub(trace_id),
        review=ReviewStub(trace_id),
    )


def profile_loaded_draft(trace_id: str) -> dict[str, Any]:
    return _draft(
        trace_id,
        "system",
        "system",
        "control",
        {"action": "profile_loaded"},
    )


def student_sql_draft(
    trace_id: str, question: str | None = None
) -> dict[str, Any]:
    content = {"event": "student_sql_submitted", "source": "student"}
    if question is not None:
        content["question"] = question
    return _draft(
        trace_id,
        "system",
        "system",
        "control",
        content,
    )


def student_answer_draft(
    trace_id: str,
    answer_result: str,
    *,
    requested_action: str = "probe",
    wrong_attempts: int | None = None,
) -> dict[str, Any]:
    probe = {"wrong_attempts": wrong_attempts} if wrong_attempts is not None else None
    return _draft(
        trace_id,
        "system",
        "system",
        "control",
        {
            "event": "student_answer",
            "answer_result": answer_result,
            "requested_action": requested_action,
            "source": "student",
        },
        probe=probe,
    )


def probe_outcome_draft(
    trace_id: str,
    answer_result: str,
    misconception: str,
    *,
    wrong_attempts: int | None = None,
) -> dict[str, Any]:
    probe_count = wrong_attempts if wrong_attempts is not None else 1
    return _draft(
        trace_id,
        "system",
        "system",
        "control",
        {
            "event": "probe_outcome",
            "answer_result": answer_result,
            "target_misconception": misconception,
            "source": "student",
        },
        probe={
            "wrong_attempts": probe_count,
            "target_misconception": misconception,
        },
    )


def path_update_draft(
    trace_id: str,
    *,
    has_next: bool,
    learning_goal_achieved: bool,
) -> dict[str, Any]:
    return _draft(
        trace_id,
        "system",
        "system",
        "learning_path_update",
        {
            "event": "path_updated",
            "has_next": has_next,
            "learning_goal_achieved": learning_goal_achieved,
        },
    )
