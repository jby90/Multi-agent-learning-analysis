"""Declarative state topology from docs/设计05_编排器状态机.md §2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class State(str, Enum):
    S0_INIT = "S0_INIT"
    S1_DIAGNOSIS = "S1_DIAGNOSIS"
    S2_KNOWLEDGE = "S2_KNOWLEDGE"
    S3_TASK = "S3_TASK"
    S4_VERIFY = "S4_VERIFY"
    S5_REVIEW = "S5_REVIEW"
    S6_DEBATE = "S6_DEBATE"
    S7_STUDENT = "S7_STUDENT"
    S8_PROBE = "S8_PROBE"
    S9_PATH_UPDATE = "S9_PATH_UPDATE"
    S10_DONE = "S10_DONE"
    S_FAIL = "S_FAIL"


class DynamicTarget(str, Enum):
    ORIGINAL_DOWNSTREAM = "original_downstream"
    ORIGINAL_PRODUCER = "original_producer"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class Transition:
    transition_id: str
    from_state: State | tuple[State, ...]
    trigger: str
    to_state: State | DynamicTarget
    guard: str


# T01-T20 are the original design05 §2.2 rows. T21 closes the documented
# S4 verification-failure path without allowing an unreviewed data conclusion
# into S5.
TRANSITIONS: list[Transition] = [
    Transition("T01", State.S0_INIT, "画像载入成功", State.S1_DIAGNOSIS, "profile_loaded"),
    Transition(
        "T02",
        State.S1_DIAGNOSIS,
        "学情报告过总线校验",
        State.S2_KNOWLEDGE,
        "diagnosis_ready",
    ),
    Transition("T03", State.S2_KNOWLEDGE, "讲义产出", State.S5_REVIEW, "lecture_produced"),
    Transition(
        "T04",
        State.S5_REVIEW,
        "verdict=approve ∧ 被审对象=讲义",
        State.S3_TASK,
        "approved_lecture",
    ),
    Transition(
        "T05",
        State.S5_REVIEW,
        "verdict=reject ∧ retry_count<max",
        State.S6_DEBATE,
        "review_rejected_below_limit",
    ),
    Transition(
        "T06",
        State.S6_DEBATE,
        "re_verdict=approve(辩护成立)",
        DynamicTarget.ORIGINAL_DOWNSTREAM,
        "re_verdict_approved",
    ),
    Transition(
        "T07",
        State.S6_DEBATE,
        "re_verdict=reject(辩护不成立)",
        DynamicTarget.ORIGINAL_PRODUCER,
        "re_verdict_rejected_below_limit",
    ),
    Transition(
        "T08",
        (State.S5_REVIEW, State.S6_DEBATE),
        "retry_count≥max_retries",
        DynamicTarget.FALLBACK,
        "retry_limit_reached",
    ),
    Transition("T09", State.S3_TASK, "任务产出", State.S5_REVIEW, "task_produced"),
    Transition(
        "T10",
        State.S5_REVIEW,
        "approve ∧ 被审对象=任务",
        State.S7_STUDENT,
        "approved_task",
    ),
    Transition(
        "T11",
        State.S7_STUDENT,
        "学生提交SQL类操作",
        State.S4_VERIFY,
        "student_sql_submitted",
    ),
    Transition(
        "T12",
        State.S4_VERIFY,
        "查询执行完毕",
        State.S5_REVIEW,
        "query_completed",
    ),
    Transition(
        "T13",
        State.S5_REVIEW,
        "approve ∧ 被审对象=数据结论",
        State.S9_PATH_UPDATE,
        "approved_sql_result",
    ),
    Transition(
        "T14",
        State.S7_STUDENT,
        "学生答题：正确",
        State.S9_PATH_UPDATE,
        "student_answer_correct",
    ),
    Transition(
        "T15",
        State.S7_STUDENT,
        "学生答题：首次错误",
        State.S8_PROBE,
        "student_answer_first_wrong",
    ),
    Transition(
        "T16",
        State.S8_PROBE,
        "反证任务生成→学生执行→修正后答对",
        State.S9_PATH_UPDATE,
        "probe_corrected",
    ),
    Transition(
        "T17",
        State.S8_PROBE,
        "反证后二次仍错",
        State.S2_KNOWLEDGE,
        "probe_second_wrong",
    ),
    Transition(
        "T18",
        State.S8_PROBE,
        "错因未命中词表M-01~M-05",
        State.S7_STUDENT,
        "misconception_unknown",
    ),
    Transition(
        "T19",
        State.S9_PATH_UPDATE,
        "路径更新完毕 ∧ 还有下一环节",
        State.S3_TASK,
        "path_has_next",
    ),
    Transition(
        "T20",
        State.S9_PATH_UPDATE,
        "学习目标达成",
        State.S10_DONE,
        "learning_goal_achieved",
    ),
    Transition(
        "T21",
        State.S4_VERIFY,
        "查询未形成可审核结论，返回调整后重试",
        State.S7_STUDENT,
        "verification_failed",
    ),
]


DOWNSTREAM_BY_PAYLOAD: Mapping[str, State] = MappingProxyType(
    {
        "lecture_note": State.S3_TASK,
        "quiz_set": State.S7_STUDENT,
        "practice_guide": State.S7_STUDENT,
        "sql_result": State.S9_PATH_UPDATE,
    }
)

PRODUCER_BY_PAYLOAD: Mapping[str, State] = MappingProxyType(
    {
        "lecture_note": State.S2_KNOWLEDGE,
        "quiz_set": State.S3_TASK,
        "practice_guide": State.S3_TASK,
        "sql_result": State.S4_VERIFY,
    }
)


class UnknownPayloadType(ValueError):
    """Raised when a dynamic transition has no documented payload mapping."""


def _resolve(mapping: Mapping[str, State], payload_type: str) -> State:
    try:
        return mapping[payload_type]
    except KeyError as exc:
        raise UnknownPayloadType(f"unknown payload type: {payload_type}") from exc


def resolve_downstream(payload_type: str) -> State:
    """Resolve T06/T08 downstream without a default branch."""

    return _resolve(DOWNSTREAM_BY_PAYLOAD, payload_type)


def resolve_producer(payload_type: str) -> State:
    """Resolve T07 producer state without a default branch."""

    return _resolve(PRODUCER_BY_PAYLOAD, payload_type)
