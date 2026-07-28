"""Run complete P6 demo sessions with production agents and auditable caching."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from agents.diagnosis_agent import DiagnosisAgent, load_profiles
from agents.kb_loader import require_valid_chunks
from agents.knowledge_agent import KnowledgeAgent
from agents.rebuttal_generator import RebuttalGenerator
from agents.retriever import BM25Retriever
from agents.review_agent import ReviewAgent, evaluate_hard_rules
from agents.sandbox import DatabaseSettings, ReadOnlyExecutor
from agents.task_agent import TaskAgent
from agents.verification_agent import VerificationAgent
from orchestrator.bus import MessageBus
from orchestrator.demo_answers import (
    CONCLUSION_TASK_ID,
    CORRECTED_CONCLUSION_RESULT,
    FIRST_CONCLUSION_RESULT,
    FIRST_TASK_ID,
    PRETEST_ANSWERS,
    TARGET_MISCONCEPTION,
)
from orchestrator.demo_cache import DemoLLMCache
from orchestrator.engine import OrchestratorEngine
from orchestrator.llm import LLMResult, call_llm
from orchestrator.review_flow import (
    ReviewFlowError,
    ReviewFlowInterrupted,
    ReviewFlowTerminal,
    produce_and_review,
)
from orchestrator.transitions import State


PROFILE_IDS = ("planner_new", "craft_engineer", "line_leader")
MAX_LECTURE_GENERATION_ATTEMPTS = 3


class DemoSessionError(RuntimeError):
    """Raised when a demo message or state diverges from the approved flow."""


@dataclass(frozen=True, slots=True)
class DemoOptions:
    profile_id: str
    trace_id: str
    trace_dir: Path
    cache_dir: Path


@dataclass(frozen=True, slots=True)
class DemoResult:
    trace_path: Path
    cache_path: Path
    state_sequence: tuple[str, ...]
    transition_sequence: tuple[str, ...]
    message_count: int
    llm_calls: int
    cache_hits: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    content = payload.get("content")
    return content if isinstance(content, Mapping) else {}


def _system_draft(
    trace_id: str,
    payload_type: str,
    content: Mapping[str, Any],
    *,
    student_profile_ref: str | None = None,
    probe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "trace_id": trace_id,
        "agent": "system",
        "role": "system",
        "payload": {"type": payload_type, "content": dict(content)},
        "evidence": [],
        "claims": [],
        "timestamp": _now(),
    }
    if student_profile_ref is not None:
        draft["student_profile_ref"] = student_profile_ref
    if probe is not None:
        draft["probe"] = dict(probe)
    return draft


def _profile_loaded_draft(
    trace_id: str,
    profile: Mapping[str, Any],
    knowledge_dimensions: Sequence[str],
) -> dict[str, Any]:
    return _system_draft(
        trace_id,
        "control",
        {
            "action": "profile_loaded",
            "profile": dict(profile),
            "knowledge_dimensions": list(knowledge_dimensions),
            "training_scene": "船厂数字化岗位培训",
            "assessment_label": "岗前测评",
        },
        student_profile_ref=str(profile["profile_id"]),
    )


def _student_sql_draft(trace_id: str, question: str) -> dict[str, Any]:
    return _system_draft(
        trace_id,
        "control",
        {
            "event": "student_sql_submitted",
            "source": "student",
            "question": question,
        },
    )


def _student_answer_draft(trace_id: str) -> dict[str, Any]:
    return _system_draft(
        trace_id,
        "control",
        {
            "event": "student_answer",
            "answer_result": FIRST_CONCLUSION_RESULT,
            "requested_action": "probe",
            "source": "student",
        },
    )


def _corrected_probe_draft(trace_id: str) -> dict[str, Any]:
    return _system_draft(
        trace_id,
        "control",
        {
            "event": "probe_outcome",
            "answer_result": CORRECTED_CONCLUSION_RESULT,
            "target_misconception": TARGET_MISCONCEPTION,
            "source": "student",
        },
        probe={
            "wrong_attempts": 1,
            "target_misconception": TARGET_MISCONCEPTION,
        },
    )


def _path_update_draft(
    trace_id: str,
    *,
    has_next: bool,
    learning_goal_achieved: bool,
    content: Mapping[str, Any],
) -> dict[str, Any]:
    return _system_draft(
        trace_id,
        "learning_path_update",
        {
            "event": "path_updated",
            "has_next": has_next,
            "learning_goal_achieved": learning_goal_achieved,
            **dict(content),
        },
    )


def _default_executor_factory() -> ReadOnlyExecutor:
    return ReadOnlyExecutor(DatabaseSettings.from_environment())


def _query_id_factory(trace_id: str) -> Callable[[], str]:
    current = 0

    def next_query_id() -> str:
        nonlocal current
        current += 1
        return f"{trace_id}-sql-{current:02d}"

    return next_query_id


class _DemoRuntime:
    def __init__(
        self,
        options: DemoOptions,
        mode: str,
        llm_call: Callable[..., LLMResult],
        executor_factory: Callable[[], Any],
        *,
        task_agent: TaskAgent | None = None,
    ) -> None:
        if options.profile_id not in PROFILE_IDS:
            raise DemoSessionError(f"unsupported profile_id: {options.profile_id}")
        self.options = options
        self.mode = mode
        self.cache = DemoLLMCache(
            options.trace_id,
            options.cache_dir,
            mode,
            llm_call,
        )
        chunks = require_valid_chunks(
            Path(__file__).resolve().parents[1]
            / "agents"
            / "knowledge_base"
            / "chunks"
        )
        self.knowledge_dimensions = tuple(
            dict.fromkeys(chunk.knowledge_point for chunk in chunks)
        )
        self.profile = load_profiles()[options.profile_id]
        try:
            self.task = task_agent or TaskAgent(options.trace_id)
        except (OSError, ValueError) as exc:
            raise DemoSessionError(
                "task domain assets are unavailable"
            ) from exc
        self.bus = MessageBus(options.trace_dir)
        self.engine = OrchestratorEngine(
            self.bus,
            options.trace_id,
            options.profile_id,
            known_misconceptions=self.task.misconception_ids,
        )
        self.diagnosis = DiagnosisAgent(options.trace_id)
        self.retriever = BM25Retriever(chunks)
        self.knowledge = KnowledgeAgent(
            options.trace_id,
            self.retriever,
            llm_call=self.cache,
        )
        self.verification = VerificationAgent(
            options.trace_id,
            llm_call=self.cache,
            executor=executor_factory(),
            query_id_factory=_query_id_factory(options.trace_id),
        )
        self.review = ReviewAgent(options.trace_id, llm_call=self.cache)
        self.rebuttal = RebuttalGenerator(options.trace_id, llm_call=self.cache)
        self.learned_knowledge_points: list[str] = []

    def completion_difficulty_action(self, template_id: str) -> str:
        """Report progression honestly at the current domain's upper boundary."""

        try:
            next_task = self.task.generate_for_learning_action(
                template_id,
                "step_up",
            )
        except ValueError as exc:
            raise DemoSessionError(
                "task domain progression cannot be determined"
            ) from exc
        return "step_up" if next_task is not None else "keep"

    def remember_approved_scope(self, product: Mapping[str, Any]) -> None:
        scope = _payload_content(product).get("responsibility_scope")
        if not isinstance(scope, list):
            return
        for item in scope:
            if (
                isinstance(item, str)
                and item.strip()
                and item not in self.learned_knowledge_points
            ):
                self.learned_knowledge_points.append(item)

    def prepare(self, draft: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.mode != "cached" or "model" not in draft:
            return draft
        prepared = deepcopy(dict(draft))
        payload = prepared.get("payload")
        if not isinstance(payload, dict):
            raise DemoSessionError("model-backed draft has no payload")
        content = payload.get("content")
        if not isinstance(content, dict):
            raise DemoSessionError("model-backed draft has no payload content")
        content["cached"] = True
        return prepared

    def transition(
        self, draft: Mapping[str, Any], transition_id: str
    ) -> dict[str, Any]:
        message, actual = self.send_transition(draft)
        if actual != transition_id:
            raise DemoSessionError(f"expected {transition_id}, got {actual}")
        return message

    def send_transition(
        self, draft: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str]:
        result = self.engine.send(self.prepare(draft))
        if not result.bus_result.accepted:
            raise DemoSessionError(
                f"transition message rejected: {result.bus_result.errors}"
            )
        actual = result.transition.transition_id if result.transition else result.reason
        if not result.transitioned or result.transition is None:
            raise DemoSessionError(f"expected transition, got {actual}")
        return result.bus_result.message, result.transition.transition_id

    def audit(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        before = self.engine.state
        result = self.engine.send(self.prepare(draft))
        if not result.bus_result.accepted:
            raise DemoSessionError(
                f"audit message rejected: {result.bus_result.errors}"
            )
        if result.transitioned or self.engine.state is not before:
            transition_id = (
                result.transition.transition_id if result.transition else "unknown"
            )
            raise DemoSessionError(
                f"audit message unexpectedly triggered {transition_id}"
            )
        return result.bus_result.message

    def resolve_review_fallback(
        self,
        product: Mapping[str, Any],
        trigger: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        product_msg_id = product.get("msg_id")
        trigger_msg_id = trigger.get("msg_id")
        product_payload = product.get("payload")
        product_type = (
            product_payload.get("type")
            if isinstance(product_payload, Mapping)
            else None
        )
        trigger_content = _payload_content(trigger)
        if (
            not isinstance(product_msg_id, str)
            or not isinstance(trigger_msg_id, str)
            or not isinstance(product_type, str)
            or trigger_content.get("reviewed_msg_id") != product_msg_id
        ):
            raise DemoSessionError("review fallback association is invalid")
        trace_path = self.bus.trace_path(self.options.trace_id)
        messages = _trace_messages(trace_path)
        transition_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if _payload_content(messages[index]).get("transition_id") == "T08"
                and _payload_content(messages[index]).get("based_on_msg_id")
                == trigger_msg_id
            ),
            None,
        )
        if transition_index is None:
            raise DemoSessionError("review fallback transition is missing")
        transition_message = messages[transition_index]
        transition_content = _payload_content(messages[transition_index])
        fallback_action = transition_content.get("fallback_action")
        if fallback_action in {"human_review", "refuse"}:
            raise ReviewFlowTerminal(str(fallback_action), transition_message)
        if fallback_action != "degrade_to_template":
            raise DemoSessionError("review fallback action is invalid")
        for message in reversed(messages[:transition_index]):
            content = _payload_content(message)
            retry = message.get("retry")
            payload = message.get("payload")
            if (
                content.get("generated_by") == "template_fallback"
                and isinstance(retry, Mapping)
                and retry.get("in_reply_to") == product_msg_id
                and isinstance(payload, Mapping)
                and payload.get("type") == product_type
            ):
                return message
        return None


def _produce_reviewed_product(
    runtime: _DemoRuntime,
    producer: Callable[[], dict[str, Any]],
    learning_report: Mapping[str, Any],
    produced_transition: str,
    approved_transition: str,
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def review(product: Mapping[str, Any]) -> dict[str, Any]:
        return runtime.review.review(
            product,
            learning_report=learning_report,
            student_profile=runtime.profile,
            learned_knowledge_points=tuple(runtime.learned_knowledge_points),
            activity_observer=(
                (lambda event, details: on_event(event, details))
                if on_event is not None
                else None
            ),
        )

    def re_review(
        product: Mapping[str, Any],
        original: Mapping[str, Any],
        rebuttal: Mapping[str, Any],
    ) -> dict[str, Any]:
        return runtime.review.re_review(
            product,
            original,
            rebuttal,
            learning_report=learning_report,
            student_profile=runtime.profile,
            learned_knowledge_points=tuple(runtime.learned_knowledge_points),
        )

    def remember(
        product: Mapping[str, Any],
        canonical_verdict: Mapping[str, Any],
    ) -> None:
        verdict = canonical_verdict.get("verdict")
        if (
            isinstance(verdict, Mapping)
            and verdict.get("decision") == "approve"
            and _payload_content(product).get("event") == "product_ready"
            and product.get("agent") == "knowledge"
        ):
            runtime.remember_approved_scope(product)

    try:
        return produce_and_review(
            producer,
            produced_transition=produced_transition,
            approved_transition=approved_transition,
            send_transition=runtime.send_transition,
            audit=runtime.audit,
            review=review,
            generate_rebuttal=runtime.rebuttal.generate,
            re_review=re_review,
            resolve_fallback=runtime.resolve_review_fallback,
            on_approved=remember,
            on_event=on_event,
        )
    except (ReviewFlowInterrupted, ReviewFlowTerminal):
        raise
    except ReviewFlowError as exc:
        raise DemoSessionError("review flow could not complete safely") from exc


def _generate_reviewable_lecture(
    runtime: _DemoRuntime,
    *,
    knowledge_point: str,
    diagnosis_content: Mapping[str, Any],
    blind_spots: Sequence[Any],
) -> dict[str, Any]:
    requested_difficulty = str(diagnosis_content.get("difficulty"))
    keywords = tuple(str(item) for item in blind_spots[:3])
    difficulty_fallback = not runtime.retriever.retrieve(
        knowledge_point,
        requested_difficulty,
        keywords,
    )
    generation_difficulty = None if difficulty_fallback else requested_difficulty
    hard_hits: tuple[dict[str, str], ...] = ()
    for _ in range(MAX_LECTURE_GENERATION_ATTEMPTS):
        lecture = runtime.knowledge.generate(
            knowledge_point=knowledge_point,
            student_profile=runtime.profile,
            learning_report_summary=(
                f"{runtime.profile['title']}当前优先补足{knowledge_point}，"
                f"岗前测评难度档为{diagnosis_content.get('difficulty')}。"
            ),
            keywords=keywords,
            difficulty=generation_difficulty,
        )
        if difficulty_fallback:
            lecture = deepcopy(lecture)
            payload = lecture.get("payload")
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, dict):
                raise DemoSessionError("fallback lecture has no payload content")
            content["difficulty_fallback"] = True
        hard_hits = evaluate_hard_rules(lecture)
        if hard_hits:
            continue
        semantic_hit = runtime.review.preflight_r04(lecture)
        if semantic_hit is None:
            return lecture
        hard_hits = (semantic_hit,)
    labels = ", ".join(hit["rule_id"] for hit in hard_hits)
    raise DemoSessionError(
        "lecture failed deterministic review preflight after "
        f"{MAX_LECTURE_GENERATION_ATTEMPTS} attempts: {labels}"
    )


def _trace_messages(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DemoSessionError(f"cannot read trace {path}: {exc}") from exc
    return [json.loads(line) for line in lines if line.strip()]


def _transition_sequence(messages: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    transitions: list[str] = []
    for message in messages:
        if message.get("role") != "system" or message.get("rejected_by_bus"):
            continue
        transition_id = _payload_content(message).get("transition_id")
        if isinstance(transition_id, str):
            transitions.append(transition_id)
    return tuple(transitions)


def _summary(
    profile: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    probe_result: Mapping[str, Any],
) -> str:
    diagnosis_content = _payload_content(diagnosis)
    score = diagnosis_content.get("pretest_score")
    score_text = "岗前测评已完成"
    if isinstance(score, Mapping):
        score_text = f"岗前测评{score.get('correct')}/{score.get('total')}"
    rows = _payload_content(probe_result).get("rows")
    comparison = "已用真实查询区分计划量与实际完成量"
    if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
        plan = rows[0].get("plan_qty")
        actual = rows[0].get("actual_qty")
        comparison = f"反证查询显示计划量{plan}、实际完成量{actual}"
    return (
        f"{profile['title']}完成{score_text}与岗位微课；{comparison}，"
        "修正了计划量与实际量混淆，进入下一阶段培养。"
    )


def run_demo_session(
    options: DemoOptions,
    *,
    llm_call: Callable[..., LLMResult] = call_llm,
    executor_factory: Callable[[], Any] = _default_executor_factory,
) -> DemoResult:
    """Run one complete production-agent session and return audited metadata."""

    mode = os.environ.get("REF_DEMO_MODE", "live")
    runtime = _DemoRuntime(options, mode, llm_call, executor_factory)

    runtime.transition(
        _profile_loaded_draft(
            options.trace_id,
            runtime.profile,
            runtime.knowledge_dimensions,
        ),
        "T01",
    )
    diagnosis = runtime.transition(
        runtime.diagnosis.assess(options.profile_id, PRETEST_ANSWERS),
        "T02",
    )
    diagnosis_content = _payload_content(diagnosis)
    blind_spots = diagnosis_content.get("blind_spots")
    if not isinstance(blind_spots, list) or not blind_spots:
        raise DemoSessionError("diagnosis did not produce a knowledge blind spot")
    knowledge_point = str(blind_spots[0])
    lecture = _produce_reviewed_product(
        runtime,
        lambda: _generate_reviewable_lecture(
            runtime,
            knowledge_point=knowledge_point,
            diagnosis_content=diagnosis_content,
            blind_spots=blind_spots,
        ),
        diagnosis,
        "T03",
        "T04",
    )
    first_task = _produce_reviewed_product(
        runtime,
        lambda: runtime.task.generate(FIRST_TASK_ID),
        diagnosis,
        "T09",
        "T10",
    )
    first_question = _payload_content(first_task).get("question")
    if not isinstance(first_question, str) or not first_question.strip():
        raise DemoSessionError("first task did not contain a question")
    runtime.transition(
        _student_sql_draft(options.trace_id, first_question), "T11"
    )
    first_result = _produce_reviewed_product(
        runtime,
        lambda: runtime.verification.answer(
            first_question,
            query_authority=_payload_content(first_task).get("query_authority"),
        ),
        diagnosis,
        "T12",
        "T13",
    )

    runtime.transition(
        _path_update_draft(
            options.trace_id,
            has_next=True,
            learning_goal_achieved=False,
            content={
                "completed_nodes": ["岗前测评", "岗位微课", "数据实操"],
                "current_node": "结论判断",
                "difficulty_action": "keep",
            },
        ),
        "T19",
    )
    conclusion_task = _produce_reviewed_product(
        runtime,
        lambda: runtime.task.generate(CONCLUSION_TASK_ID),
        diagnosis,
        "T09",
        "T10",
    )
    runtime.transition(_student_answer_draft(options.trace_id), "T15")

    counter_task = runtime.audit(
        runtime.task.counter_evidence(TARGET_MISCONCEPTION)
    )
    counter_question = _payload_content(counter_task).get("question")
    if not isinstance(counter_question, str) or not counter_question.strip():
        raise DemoSessionError("counter-evidence task did not contain a question")
    runtime.audit(_student_sql_draft(options.trace_id, counter_question))
    probe_result = runtime.audit(runtime.verification.answer(counter_question))
    probe_verdict = runtime.review.review(probe_result)
    runtime.audit(probe_verdict)
    if probe_verdict.get("verdict", {}).get("decision") not in {
        "approve",
        "approve_with_fix",
    }:
        raise DemoSessionError("counter-evidence SQL result did not pass review")
    runtime.transition(_corrected_probe_draft(options.trace_id), "T16")

    runtime.transition(
        _path_update_draft(
            options.trace_id,
            has_next=False,
            learning_goal_achieved=True,
            content={
                "completed_nodes": [
                    "岗前测评",
                    "岗位微课",
                    "数据实操",
                    "反证追问",
                    "修正结论",
                ],
                "current_node": "培养目标达成",
                "difficulty_action": "step_up",
                "target_misconception": TARGET_MISCONCEPTION,
                "summary": _summary(runtime.profile, diagnosis, probe_result),
            },
        ),
        "T20",
    )

    if runtime.engine.state is not State.S10_DONE:
        raise DemoSessionError(
            f"session ended at {runtime.engine.state.value}, expected S10_DONE"
        )
    runtime.cache.assert_exhausted()
    trace_path = runtime.bus.trace_path(options.trace_id)
    messages = _trace_messages(trace_path)
    state_sequence = tuple(state.value for state in runtime.engine.state_history)
    transition_sequence = _transition_sequence(messages)
    if len(state_sequence) != len(transition_sequence) + 1:
        raise DemoSessionError("trace transitions cannot reconstruct state history")
    return DemoResult(
        trace_path=trace_path,
        cache_path=runtime.cache.path,
        state_sequence=state_sequence,
        transition_sequence=transition_sequence,
        message_count=len(messages),
        llm_calls=runtime.cache.calls,
        cache_hits=runtime.cache.cache_hits,
    )


def _generated_trace_id(profile_id: str) -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    return f"demo-{profile_id}-{stamp}"


def _result_json(result: DemoResult) -> str:
    return json.dumps(
        {
            "trace_path": str(result.trace_path),
            "cache_path": str(result.cache_path),
            "state_sequence": list(result.state_sequence),
            "transition_sequence": list(result.transition_sequence),
            "message_count": result.message_count,
            "llm_calls": result.llm_calls,
            "cache_hits": result.cache_hits,
        },
        ensure_ascii=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="运行P6真实五智能体端到端演示会话"
    )
    parser.add_argument("--profile", choices=PROFILE_IDS)
    parser.add_argument("--trace-id")
    parser.add_argument("--trace-dir", type=Path, default=root / "traces")
    parser.add_argument("--cache-dir", type=Path, default=root / "cache")
    parser.add_argument("--all", action="store_true", dest="run_all")
    args = parser.parse_args(argv)

    mode = os.environ.get("REF_DEMO_MODE", "live")
    if args.run_all:
        if args.profile or args.trace_id:
            parser.error("--all不能与--profile或--trace-id组合")
        if mode != "live":
            parser.error("--all只用于生成三份live会话")
        jobs = [
            (profile_id, _generated_trace_id(profile_id))
            for profile_id in PROFILE_IDS
        ]
    else:
        if args.profile is None:
            parser.error("单次运行必须提供--profile")
        if mode == "cached" and args.trace_id is None:
            parser.error("cached模式必须提供已有--trace-id")
        trace_id = args.trace_id or _generated_trace_id(args.profile)
        jobs = [(args.profile, trace_id)]

    for profile_id, trace_id in jobs:
        result = run_demo_session(
            DemoOptions(
                profile_id=profile_id,
                trace_id=trace_id,
                trace_dir=args.trace_dir,
                cache_dir=args.cache_dir,
            )
        )
        print(_result_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
