"""Stepwise interactive sessions over the existing orchestrator engine and bus."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping
from uuid import uuid4
from urllib.parse import parse_qs, unquote, urlsplit

from agents.diagnosis_agent import load_pretest
from agents.kb_loader import KnowledgeChunk
from agents.follow_up_agent import (
    MAX_FOLLOW_UP_ROUNDS,
    FollowUpAgent,
    FollowUpGenerationError,
    FollowUpTurn,
    UNKNOWN_MISCONCEPTION,
    contains_engineering_text,
    normalize_learner_input,
)
from agents.sandbox import (
    QueryExecutionError,
    QueryTimeoutError,
    validate_and_rewrite,
)
from agents.verification_agent import (
    FAMILY_COLUMN_SETS,
    VerificationAgent,
    _normalize_rows,
    _output_aliases,
    _render_claims,
    _result_is_empty,
)
from coordination.contracts import LearningContract
from coordination.evidence_bundle import EvidenceBundle
from coordination.evaluation import build_coordination_evidence
from coordination.parallel import BranchSpec, ParallelStage, ParallelStageExecutor
from coordination.resource_bundle import ResourceBundle
from orchestrator.demo_session import (
    DemoOptions,
    _DemoRuntime,
    _default_executor_factory,
    _generate_reviewable_lecture,
    _path_update_draft,
    _payload_content,
    _profile_loaded_draft,
    _produce_reviewed_product,
    _summary,
    _trace_messages,
)
from orchestrator.llm import LLMResult, call_llm
from orchestrator.outcomes import verification_outcome
from orchestrator.review_flow import (
    ReviewFlowError,
    ReviewFlowInterrupted,
    ReviewFlowTerminal,
    audit_and_review,
)
from orchestrator.agent_events import AGENT_IDS, AgentEventStream
from orchestrator.transitions import State


class InteractiveSessionError(RuntimeError):
    """Raised when an interactive action is not valid for the current session."""


_REVIEW_STOP_COPY = {
    "degrade_to_template": "这份内容多次未通过专业审核，本次学习已安全结束。",
    "human_review": "这份内容需要进一步确认，本次学习已暂停。",
    "refuse": "这份内容未通过专业审核，本次学习已安全结束。",
    "system_error": "内容生成服务暂时不可用，本次学习已安全结束，请稍后重新开始。",
}


def _required_task_draft(
    draft: dict[str, Any] | None,
    message: str,
) -> dict[str, Any]:
    if draft is None:
        raise InteractiveSessionError(message)
    return draft


def _retryable_task_producer(
    initial: dict[str, Any],
    regenerate: Callable[[], dict[str, Any] | None],
    missing_message: str,
) -> Callable[[], dict[str, Any]]:
    pending: dict[str, Any] | None = initial

    def produce() -> dict[str, Any]:
        nonlocal pending
        if pending is not None:
            draft = pending
            pending = None
            return draft
        return _required_task_draft(regenerate(), missing_message)

    return produce


@dataclass(slots=True)
class _InteractiveSession:
    session_id: str
    runtime: _DemoRuntime
    executor: Any
    follow_up_agent: FollowUpAgent
    awaiting: str
    artifact: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    learning_contract: LearningContract | None = None
    evidence_bundle: EvidenceBundle | None = None
    evidence_chunks: tuple[KnowledgeChunk, ...] = ()
    lecture: dict[str, Any] | None = None
    active_task: dict[str, Any] | None = None
    learning_task: dict[str, Any] | None = None
    sql_result: dict[str, Any] | None = None
    task_phase: str = "initial"
    pending_learning_action: str | None = None
    completed_correction: bool = False
    interaction: dict[str, Any] | None = None
    follow_up_round: int = 1
    follow_up_question: str = ""
    follow_up_turns: list[dict[str, Any]] = field(default_factory=list)
    follow_up_target: str | None = None
    follow_up_had_support: bool = False
    generic_fallback_used: bool = False
    recorded_turn_ids: set[str] = field(default_factory=set)
    processed_turn_ids: set[str] = field(default_factory=set)
    advance_lock: Any = field(default_factory=RLock, repr=False)
    follow_up_lock: Any = field(default_factory=RLock, repr=False)
    query_count: int = 0
    outcome: str | None = None
    events: AgentEventStream | None = None
    prefetched_task: dict[str, Any] | None = None
    prefetched_task_generator: Any = None
    prefetched_assessment: dict[str, Any] | None = None
    prefetched_assessment_generator: Any = None
    resource_bundle: ResourceBundle | None = None


class InteractiveSessionManager:
    """Own in-memory interactive sessions while engine and bus own progression."""

    def __init__(
        self,
        *,
        trace_dir: Path,
        cache_dir: Path,
        llm_call: Callable[..., LLMResult] = call_llm,
        follow_up_llm_call: Callable[..., LLMResult] | None = None,
        executor_factory: Callable[[], Any] = _default_executor_factory,
        mode: str | None = None,
    ) -> None:
        self._trace_dir = Path(trace_dir)
        self._cache_dir = Path(cache_dir)
        self._llm_call = llm_call
        self._follow_up_llm_call = follow_up_llm_call
        self._executor_factory = executor_factory
        self._mode = mode or os.environ.get("REF_DEMO_MODE", "live")
        self._evidence_stage_executor = ParallelStageExecutor(max_concurrency=3)
        self._resource_stage_executor = ParallelStageExecutor(max_concurrency=3)
        self._sessions: dict[str, _InteractiveSession] = {}
        self._lock = RLock()

    def create_session(self, profile_id: str) -> dict[str, Any]:
        session_id = uuid4().hex
        trace_id = f"interactive-{session_id}"
        executor = self._executor_factory()
        runtime = _DemoRuntime(
            DemoOptions(
                profile_id=profile_id,
                trace_id=trace_id,
                trace_dir=self._trace_dir,
                cache_dir=self._cache_dir,
            ),
            self._mode,
            self._llm_call,
            lambda: executor,
        )
        follow_up_agent = FollowUpAgent(
            trace_id,
            llm_call=self._follow_up_llm_call or runtime.cache,
        )
        runtime.transition(
            _profile_loaded_draft(
                trace_id,
                runtime.profile,
                runtime.knowledge_dimensions,
            ),
            "T01",
        )
        session = _InteractiveSession(
            session_id=session_id,
            runtime=runtime,
            executor=executor,
            follow_up_agent=follow_up_agent,
            awaiting="pretest",
            events=AgentEventStream(trace_id),
        )
        for agent in sorted(AGENT_IDS):
            session.events.publish(
                agent=agent,
                status="idle",
                activity="standby",
                label="等待调度",
                stage=runtime.engine.state.value,
            )
        session.events.publish(
            agent="diagnosis",
            status="queued",
            activity="pretest_waiting",
            label="等待岗前测评",
            stage=runtime.engine.state.value,
        )
        with self._lock:
            self._sessions[session_id] = session
        return self.get_state(session_id)

    def continue_learning(self, session_id: str) -> dict[str, Any]:
        """Start the next diagnosed knowledge unit without repeating pretest."""
        previous = self._get_session(session_id)
        if (
            previous.runtime.engine.state is not State.S10_DONE
            or previous.awaiting != "done"
            or previous.outcome != "completed"
        ):
            raise InteractiveSessionError("当前学习单元尚未完成，不能进入下一知识点。")
        if previous.diagnosis is None:
            raise InteractiveSessionError("缺少可继承的岗前测评结果，请重新开始。")
        remaining = self._remaining_blind_spots(previous)
        if not remaining:
            raise InteractiveSessionError("当前培养路径已全部完成。")

        created = self.create_session(previous.runtime.options.profile_id)
        next_session = self._get_session(str(created["session_id"]))
        previous_content = _payload_content(previous.diagnosis)
        current_difficulty = _payload_content(
            previous.learning_task or previous.lecture or {}
        ).get("difficulty")
        diagnosis_content = dict(previous_content)
        diagnosis_content.update(
            {
                "event": "diagnosis_ready",
                "blind_spots": remaining,
                "diagnosis_narrative": "",
                "suggestions": [],
                "narrative_fallback": True,
                "narrative_fallback_reason": "continued_learning",
                "llm_latency_ms": 0,
            }
        )
        if current_difficulty in {"basic", "applied", "advanced"}:
            diagnosis_content["difficulty"] = current_difficulty
        diagnosis = next_session.runtime.transition(
            {
                "trace_id": next_session.runtime.options.trace_id,
                "agent": "diagnosis",
                "role": "produce",
                "payload": {
                    "type": "profile_assessment",
                    "content": diagnosis_content,
                },
                "evidence": [],
                "claims": [],
                "student_profile_ref": next_session.runtime.options.profile_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "T02",
        )
        learning_contract = LearningContract.from_diagnosis(
            profile=next_session.runtime.profile,
            diagnosis=diagnosis,
            domain_id=next_session.runtime.task.domain_id,
            domain_package_sha256=next_session.runtime.task.domain_package_sha256,
        )
        next_session.runtime.audit(
            learning_contract.control_draft(next_session.runtime.options.trace_id)
        )
        self._publish_activity(
            next_session,
            "diagnosis",
            "done",
            "continued_assessment",
            "已沿用本轮画像与测评结果",
        )
        self._publish_activity(
            next_session,
            "knowledge",
            "queued",
            "evidence_retrieval",
            f"下一知识点“{remaining[0]}”已进入生成队列",
        )
        next_session.diagnosis = diagnosis
        next_session.learning_contract = learning_contract
        next_session.artifact = diagnosis
        next_session.interaction = {
            "kind": "learning_notice",
            "message": f"已沿用本轮画像与测评结果，下一知识点：{remaining[0]}。",
        }
        next_session.awaiting = "advance"
        next_session.outcome = None
        return self.get_state(next_session.session_id)

    def get_pretest(self, session_id: str) -> list[dict[str, Any]]:
        session = self._get_session(session_id)
        if session.awaiting != "pretest":
            raise InteractiveSessionError("当前步骤不是岗前测评。")
        return [
            {
                "question_id": str(question["question_id"]),
                "knowledge_point": str(question["knowledge_point"]),
                "stem": str(question["stem"]),
                "options": dict(question["options"]),
            }
            for question in load_pretest()
        ]

    def submit_pretest(
        self,
        session_id: str,
        answers: dict[str, str],
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.awaiting != "pretest":
            raise InteractiveSessionError("当前步骤不能提交岗前测评。")
        self._publish_activity(
            session,
            "diagnosis",
            "working",
            "profile_assessment",
            "正在计算知识盲区与难度",
        )
        try:
            diagnosis = session.runtime.transition(
                session.runtime.diagnosis.assess(
                    session.runtime.options.profile_id,
                    answers,
                ),
                "T02",
            )
        except Exception:
            self._publish_activity(
                session,
                "diagnosis",
                "blocked",
                "profile_assessment",
                "诊断未能安全完成",
            )
            raise
        self._publish_activity(
            session,
            "diagnosis",
            "done",
            "profile_assessment",
            "知识盲区与起始难度已确定",
        )
        learning_contract = LearningContract.from_diagnosis(
            profile=session.runtime.profile,
            diagnosis=diagnosis,
            domain_id=session.runtime.task.domain_id,
            domain_package_sha256=session.runtime.task.domain_package_sha256,
        )
        session.runtime.audit(
            learning_contract.control_draft(session.runtime.options.trace_id)
        )
        self._publish_activity(
            session,
            "knowledge",
            "queued",
            "evidence_retrieval",
            "已进入个性化知识生成队列",
        )
        session.diagnosis = diagnosis
        session.learning_contract = learning_contract
        session.artifact = diagnosis
        session.awaiting = "advance"
        session.outcome = None
        return self.get_state(session_id)

    def _produce_reviewed_product(
        self,
        session: _InteractiveSession,
        producer: Callable[[], dict[str, Any]],
        learning_report: dict[str, Any],
        produced_transition: str,
        approved_transition: str,
        *,
        producer_agent: str,
        activity: str,
        working_label: str,
    ) -> dict[str, Any] | None:
        def observe(event: str, details: Mapping[str, Any]) -> None:
            cycle = int(details.get("cycle", 1))
            event_details = {"cycle": cycle, **dict(details)}
            if self._publish_specialist_review_activity(
                session,
                event,
                event_details,
                producer_agent=producer_agent,
            ):
                return
            self._publish_targeted_dispute_specialists(
                session,
                event,
                event_details,
                producer_agent=producer_agent,
            )
            if event == "producer_started":
                self._publish_activity(
                    session,
                    producer_agent,
                    "working",
                    activity,
                    working_label,
                    details=event_details,
                )
            elif event == "product_ready":
                self._publish_activity(
                    session,
                    producer_agent,
                    "collaborating",
                    activity,
                    "候选产物已送交专业审核",
                    peers=("review",),
                    details=event_details,
                )
            elif event == "review_started":
                self._publish_activity(
                    session,
                    "review",
                    "reviewing",
                    "quality_gate",
                    "正在执行事实、难度与安全审查",
                    peers=(producer_agent,),
                    details=event_details,
                )
            elif event == "parallel_review_started":
                self._publish_activity(
                    session,
                    "review",
                    "collaborating",
                    "parallel_quality_review",
                    "事实、教学、数据安全与表达正在四维并行审核",
                    peers=(producer_agent,),
                    details=event_details,
                )
            elif event == "parallel_review_completed":
                self._publish_activity(
                    session,
                    "review",
                    "reviewing",
                    "parallel_quality_review",
                    "四维审核已汇聚，正在执行确定性裁决",
                    peers=(producer_agent,),
                    details=event_details,
                )
            elif event == "review_completed":
                accepted = details.get("transition") == approved_transition
                direct_regeneration = details.get("dispute_route") == "local_regeneration"
                self._publish_activity(
                    session,
                    "review",
                    "approved" if accepted else "waiting",
                    "quality_gate",
                    (
                        "质量门已通过"
                        if accepted
                        else "命中硬规则，准备局部重生成"
                        if direct_regeneration
                        else "发现可辩争议，准备定向复核"
                    ),
                    peers=() if accepted else (producer_agent,),
                    details=event_details,
                )
            elif event == "regeneration_started":
                self._publish_activity(
                    session,
                    "review",
                    "blocked",
                    "deterministic_rejection_route",
                    "硬规则命中，已跳过模型辩论",
                    peers=(producer_agent,),
                    details=event_details,
                )
                self._publish_activity(
                    session,
                    producer_agent,
                    "queued",
                    "local_regeneration",
                    "仅退回当前产物进行局部重生成",
                    peers=("review",),
                    details=event_details,
                )
            elif event == "regeneration_completed":
                self._publish_activity(
                    session,
                    producer_agent,
                    "queued",
                    "local_regeneration",
                    "重生成路由已确认，等待新候选产物",
                    peers=("review",),
                    details=event_details,
                )
            elif event == "debate_started":
                for agent, peer in ((producer_agent, "review"), ("review", producer_agent)):
                    self._publish_activity(
                        session,
                        agent,
                        "debating",
                        "bounded_debate",
                        "正在围绕证据进行有界复核",
                        peers=(peer,),
                        details=event_details,
                    )
            elif event == "debate_completed":
                self._publish_activity(
                    session,
                    "review",
                    "approved" if details.get("transition") in {"T06"} else "waiting",
                    "bounded_debate",
                    "复核已形成裁决",
                    peers=(producer_agent,),
                    details=event_details,
                )

        try:
            product = _produce_reviewed_product(
                session.runtime,
                producer,
                learning_report,
                produced_transition,
                approved_transition,
                on_event=observe,
                learning_contract=session.learning_contract,
            )
        except ReviewFlowTerminal as terminal:
            self._finish_review_stop(
                session,
                terminal.control_message,
                terminal.action,
            )
            return None
        except ReviewFlowInterrupted as interrupted:
            self._finish_review_stop(
                session,
                interrupted.last_message,
                "system_error",
            )
            return None
        self._publish_activity(
            session,
            producer_agent,
            "done",
            activity,
            "产物已通过质量门",
        )
        if _payload_content(product).get("generated_by") == "template_fallback":
            self._finish_review_stop(
                session,
                product,
                "degrade_to_template",
            )
            return None
        return product

    @staticmethod
    def _finish_review_stop(
        session: _InteractiveSession,
        artifact: dict[str, Any],
        action: str,
    ) -> None:
        try:
            student_message = _REVIEW_STOP_COPY[action]
        except KeyError as exc:
            raise InteractiveSessionError(
                "这份内容暂时无法继续，请重新开始本次学习。"
            ) from exc
        session.artifact = artifact
        session.interaction = {
            "kind": "review_notice",
            "message": student_message,
        }
        session.awaiting = "done"
        session.outcome = (
            "system_error" if action == "system_error" else "safe_rejected"
        )
        if session.events is not None:
            session.events.publish(
                agent="review",
                status="blocked",
                activity="quality_gate",
                label="质量门已安全阻断该产物",
                stage=session.runtime.engine.state.value,
            )

    def advance(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        if not session.advance_lock.acquire(blocking=False):
            with session.advance_lock:
                return self.get_state(session_id)
        try:
            return self._advance_locked(session)
        finally:
            session.advance_lock.release()

    def _prepare_evidence_bundle(
        self,
        session: _InteractiveSession,
        diagnosis_content: Mapping[str, Any],
        blind_spots: list[Any],
    ) -> tuple[EvidenceBundle, tuple[KnowledgeChunk, ...]]:
        if session.evidence_bundle is not None:
            return session.evidence_bundle, session.evidence_chunks
        if session.learning_contract is None:
            raise InteractiveSessionError("学习契约缺失，无法准备统一证据包。")
        runtime = session.runtime
        knowledge_point = str(blind_spots[0])
        difficulty = str(diagnosis_content.get("difficulty"))
        keywords = tuple(str(item) for item in blind_spots[:3])

        def retrieve_knowledge() -> dict[str, Any]:
            chunks = runtime.retriever.retrieve(
                knowledge_point,
                difficulty,
                keywords,
            )
            difficulty_fallback = not chunks
            if difficulty_fallback:
                chunks = runtime.retriever.retrieve(
                    knowledge_point,
                    None,
                    keywords,
                )
            return {
                "chunks": chunks,
                "summary": {
                    "chunk_ids": [chunk.chunk_id for chunk in chunks],
                    "chunk_count": len(chunks),
                    "difficulty_fallback": difficulty_fallback,
                },
            }

        def retrieve_business_data() -> dict[str, Any]:
            return runtime.task.diagnosis_evidence(
                knowledge_point,
                difficulty,
            )

        def retrieve_pedagogy() -> dict[str, Any]:
            return {
                "profile_id": str(runtime.profile["profile_id"]),
                "profile_title": str(runtime.profile["title"]),
                "lecture_style": str(runtime.profile["lecture_style"]),
                "difficulty": difficulty,
                "misconceptions": list(session.learning_contract.misconceptions),
            }

        evidence_result = self._evidence_stage_executor.execute(
            ParallelStage(
                stage_id="evidence-bundle",
                branches=(
                    BranchSpec("knowledge", retrieve_knowledge),
                    BranchSpec("business_data", retrieve_business_data),
                    BranchSpec("pedagogy", retrieve_pedagogy),
                ),
            ),
            correlation_id=f"{runtime.options.trace_id}-evidence",
            observer=lambda event, details: self._observe_evidence_stage(
                session,
                event,
                details,
            ),
        )
        if not evidence_result.succeeded:
            raise InteractiveSessionError("统一证据包暂时无法完成，请稍后重试。")
        knowledge_result = evidence_result.require("knowledge")
        chunks = tuple(knowledge_result["chunks"])
        bundle = EvidenceBundle(
            contract_id=session.learning_contract.contract_id,
            knowledge_point=knowledge_point,
            difficulty=difficulty,
            sources={
                "knowledge": knowledge_result["summary"],
                "business_data": evidence_result.require("business_data"),
                "pedagogy": evidence_result.require("pedagogy"),
            },
        )
        runtime.audit(bundle.control_draft(runtime.options.trace_id))
        session.evidence_bundle = bundle
        session.evidence_chunks = chunks
        return bundle, chunks

    def _advance_locked(self, session: _InteractiveSession) -> dict[str, Any]:
        session_id = session.session_id
        if session.awaiting != "advance":
            raise InteractiveSessionError("当前步骤需要先完成学员操作。")
        runtime = session.runtime
        if runtime.engine.state is State.S2_KNOWLEDGE:
            if session.diagnosis is None:
                raise InteractiveSessionError("岗前测评结果缺失。")
            diagnosis_content = _payload_content(session.diagnosis)
            blind_spots = diagnosis_content.get("blind_spots")
            if not isinstance(blind_spots, list) or not blind_spots:
                raise InteractiveSessionError("岗前测评没有产生知识盲区。")
            evidence_bundle, evidence_chunks = self._prepare_evidence_bundle(
                session,
                diagnosis_content,
                blind_spots,
            )
            business_evidence = evidence_bundle.source("business_data")
            def prefetch_task() -> dict[str, Any]:
                return evidence_bundle.bind(
                    runtime.task.generate(str(business_evidence["template_id"]))
                )

            def prefetch_assessment() -> dict[str, Any]:
                return evidence_bundle.bind(
                    runtime.task.generate_assessment(
                        str(business_evidence["template_id"])
                    )
                )

            def produce_lecture() -> dict[str, Any] | None:
                return self._produce_reviewed_product(
                    session,
                    lambda: _generate_reviewable_lecture(
                        runtime,
                        knowledge_point=str(blind_spots[0]),
                        diagnosis_content=diagnosis_content,
                        blind_spots=blind_spots,
                        retrieved_chunks=evidence_chunks,
                        difficulty_fallback=bool(
                            evidence_bundle.source("knowledge")["difficulty_fallback"]
                        ),
                        evidence_bundle=evidence_bundle,
                    ),
                    session.diagnosis,
                    "T03",
                    "T04",
                    producer_agent="knowledge",
                    activity="personalized_lecture",
                    working_label="正在检索证据并生成个性化微课",
                )

            resource_result = self._resource_stage_executor.execute(
                ParallelStage(
                    stage_id="resource-generation",
                    branches=(
                        BranchSpec("knowledge", produce_lecture),
                        BranchSpec("practice", prefetch_task, required=False),
                        BranchSpec(
                            "assessment",
                            prefetch_assessment,
                            required=False,
                        ),
                    ),
                ),
                correlation_id=evidence_bundle.bundle_id,
                observer=lambda event, details: self._observe_resource_stage(
                    session,
                    event,
                    details,
                ),
            )
            lecture_branch = resource_result.branch("knowledge")
            if lecture_branch.status != "succeeded":
                raise InteractiveSessionError(
                    "个性化微课暂时无法生成，请稍后重试。"
                )
            lecture = lecture_branch.value
            task_branch = resource_result.branch("practice")
            if task_branch.status == "succeeded":
                session.prefetched_task = task_branch.value
                session.prefetched_task_generator = type(
                    runtime.task
                ).generate
            else:
                session.prefetched_task = None
                session.prefetched_task_generator = None
                self._publish_activity(
                    session,
                    "task",
                    "queued",
                    "task_design",
                    "并行草稿未完成，将在主链路重试",
                )
            assessment_branch = resource_result.branch("assessment")
            if assessment_branch.status == "succeeded":
                session.prefetched_assessment = assessment_branch.value
                session.prefetched_assessment_generator = type(
                    runtime.task
                ).generate_assessment
            else:
                session.prefetched_assessment = None
                session.prefetched_assessment_generator = None
                self._publish_activity(
                    session,
                    "assessment",
                    "queued",
                    "assessment_design",
                    "分阶测验草稿未完成，将在需要时单分支重试",
                )
            if lecture is None:
                return self.get_state(session_id)
            session.resource_bundle = ResourceBundle.build(
                contract_id=evidence_bundle.contract_id,
                evidence_bundle_id=evidence_bundle.bundle_id,
                products={
                    "knowledge": lecture,
                    "practice": (
                        task_branch.value
                        if task_branch.status == "succeeded"
                        else None
                    ),
                    "assessment": (
                        assessment_branch.value
                        if assessment_branch.status == "succeeded"
                        else None
                    ),
                },
            )
            session.lecture = lecture
            session.artifact = lecture
            session.interaction = None
            self._publish_activity(
                session,
                "task",
                "waiting" if session.prefetched_task is not None else "queued",
                "task_design",
                (
                    "并行草稿已就绪，等待进入质量门"
                    if session.prefetched_task is not None
                    else "等待生成匹配难度的实操任务"
                ),
            )
            self._publish_activity(
                session,
                "assessment",
                "waiting" if session.prefetched_assessment is not None else "queued",
                "assessment_design",
                (
                    "分阶测验已就绪，等待进入理解核对质量门"
                    if session.prefetched_assessment is not None
                    else "等待单分支生成分阶测验"
                ),
            )
            return self.get_state(session_id)
        if runtime.engine.state is State.S3_TASK and session.active_task is None:
            if session.diagnosis is None:
                raise InteractiveSessionError("岗前测评结果缺失。")
            diagnosis_content = _payload_content(session.diagnosis)
            blind_spots = diagnosis_content.get("blind_spots")
            if not isinstance(blind_spots, list) or not blind_spots:
                raise InteractiveSessionError("岗前测评没有产生知识盲区。")
            def produce_task() -> dict[str, Any]:
                try:
                    if session.evidence_bundle is not None:
                        template_id = session.evidence_bundle.source(
                            "business_data"
                        )["template_id"]
                        return session.evidence_bundle.bind(
                            runtime.task.generate(str(template_id))
                        )
                    return runtime.task.generate_for_diagnosis(
                        blind_spots[0], diagnosis_content.get("difficulty")
                    )
                except ValueError as exc:
                    raise InteractiveSessionError(
                        "暂时无法为你匹配合适的训练任务，请稍后重试。"
                    ) from exc

            prefetched = session.prefetched_task
            generator_unchanged = (
                session.prefetched_task_generator
                is type(runtime.task).generate
            )
            session.prefetched_task = None
            session.prefetched_task_generator = None
            task_producer = (
                _retryable_task_producer(
                    prefetched,
                    produce_task,
                    "暂时无法为你匹配合适的训练任务，请稍后重试。",
                )
                if prefetched is not None and generator_unchanged
                else produce_task
            )

            task = self._produce_reviewed_product(
                session,
                task_producer,
                session.diagnosis,
                "T09",
                "T10",
                producer_agent="task",
                activity="task_design",
                working_label="正在匹配场景、难度与数据任务",
            )
            if task is None:
                return self.get_state(session_id)
            session.active_task = task
            session.learning_task = task
            session.task_phase = "initial"
            session.artifact = task
            session.awaiting = "sql"
            self._publish_activity(
                session,
                "verification",
                "waiting",
                "student_submission",
                "等待学员提交只读查询",
            )
            return self.get_state(session_id)
        if runtime.engine.state is State.S9_PATH_UPDATE:
            if session.task_phase == "progression":
                return self._complete_training(session)
            if session.task_phase == "conclusion":
                return self._advance_after_correct_answer(session)
            if session.task_phase == "initial":
                return self._create_conclusion_task(session)
            raise InteractiveSessionError("当前训练进度暂时无法继续，请稍后重试。")
        raise InteractiveSessionError("当前状态没有可推进的教学产物。")

    def _create_conclusion_task(
        self,
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        if session.diagnosis is None:
            raise InteractiveSessionError("岗前测评结果缺失。")
        prefetched = session.prefetched_assessment
        generator_unchanged = (
            session.prefetched_assessment_generator
            is type(session.runtime.task).generate_assessment
        )
        session.prefetched_assessment = None
        session.prefetched_assessment_generator = None
        task_draft = (
            prefetched
            if prefetched is not None and generator_unchanged
            else self._assessment_task_draft(session)
        )
        if task_draft is None:
            raise InteractiveSessionError("暂时无法为你安排结论练习，请稍后重试。")
        runtime = session.runtime
        runtime.transition(
            _path_update_draft(
                runtime.options.trace_id,
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
        self._publish_activity(
            session,
            "assessment",
            "collaborating",
            "assessment_design",
            "预生成分阶测验已进入主链路质量门",
            peers=("task", "review"),
        )
        task = self._produce_reviewed_product(
            session,
            _retryable_task_producer(
                task_draft,
                lambda: self._assessment_task_draft(session),
                "暂时无法为你安排结论练习，请稍后重试。",
            ),
            session.diagnosis,
            "T09",
            "T10",
            producer_agent="task",
            activity="conclusion_task",
            working_label="正在生成基于真实数据的结论练习",
        )
        if task is None:
            return self.get_state(session.session_id)
        self._publish_activity(
            session,
            "assessment",
            "done",
            "assessment_design",
            "分阶测验已通过质量门并交付理解核对",
            peers=("task", "review"),
        )
        session.active_task = task
        session.learning_task = task
        session.task_phase = "conclusion"
        session.artifact = task
        self._start_follow_up(session, task)
        return self.get_state(session.session_id)

    def _advance_after_correct_answer(
        self,
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        if session.pending_learning_action != "step_up":
            raise InteractiveSessionError("请先完成当前练习，再查看下一步训练。")
        if session.diagnosis is None:
            raise InteractiveSessionError("岗前测评结果缺失。")
        task_draft = self._learning_task_draft(session, "step_up")
        if task_draft is None:
            return self._complete_training(session)
        current_content = _payload_content(session.learning_task or {})
        target_content = _payload_content(task_draft)
        runtime = session.runtime
        path = runtime.transition(
            _path_update_draft(
                runtime.options.trace_id,
                has_next=True,
                learning_goal_achieved=False,
                content={
                    "completed_nodes": self._completed_nodes(session),
                    "current_node": "进阶训练",
                    "difficulty_action": "step_up",
                    "previous_difficulty": current_content.get("difficulty"),
                    "difficulty": target_content.get("difficulty"),
                },
            ),
            "T19",
        )
        task = self._produce_reviewed_product(
            session,
            _retryable_task_producer(
                task_draft,
                lambda: self._learning_task_draft(session, "step_up"),
                "暂时无法为你安排进阶训练，请稍后重试。",
            ),
            path,
            "T09",
            "T10",
            producer_agent="task",
            activity="progression_task",
            working_label="正在生成进阶训练任务",
        )
        if task is None:
            return self.get_state(session.session_id)
        session.active_task = task
        session.learning_task = task
        session.task_phase = "progression"
        session.pending_learning_action = None
        session.artifact = task
        session.interaction = {
            "kind": "learning_notice",
            "message": "根据本次作答表现，已为你提高一档难度。",
        }
        session.awaiting = "sql"
        return self.get_state(session.session_id)

    def _complete_training(
        self,
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        if session.diagnosis is None:
            raise InteractiveSessionError("岗前测评结果缺失。")
        evidence_result = session.sql_result
        if evidence_result is None:
            raise InteractiveSessionError("数据实操结果缺失。")
        current_content = _payload_content(session.learning_task or {})
        difficulty_action = (
            "step_up" if session.task_phase == "progression" else "keep"
        )
        runtime = session.runtime
        remaining_blind_spots = self._remaining_blind_spots(session)
        path = runtime.transition(
            _path_update_draft(
                runtime.options.trace_id,
                has_next=False,
                learning_goal_achieved=True,
                content={
                    "completed_nodes": self._completed_nodes(session),
                    "current_node": "培养目标达成",
                    "difficulty_action": difficulty_action,
                    "difficulty": current_content.get("difficulty"),
                    "summary": _summary(
                        runtime.profile,
                        session.diagnosis,
                        evidence_result,
                    ),
                    "curriculum_has_next": bool(remaining_blind_spots),
                    "next_knowledge_point": (
                        remaining_blind_spots[0]
                        if remaining_blind_spots
                        else None
                    ),
                },
            ),
            "T20",
        )
        session.artifact = path
        session.interaction = (
            {
                "kind": "next_learning_step",
                "message": f"下一知识点：{remaining_blind_spots[0]}",
                "knowledge_point": remaining_blind_spots[0],
            }
            if remaining_blind_spots
            else None
        )
        session.pending_learning_action = None
        session.awaiting = "done"
        session.outcome = "completed"
        return self.get_state(session.session_id)

    @staticmethod
    def _remaining_blind_spots(session: _InteractiveSession) -> list[str]:
        if session.diagnosis is None:
            return []
        blind_spots = [
            item.strip()
            for item in _payload_content(session.diagnosis).get("blind_spots", [])
            if isinstance(item, str) and item.strip()
        ]
        if not blind_spots:
            return []
        current = _payload_content(
            session.learning_task or session.lecture or {}
        ).get("knowledge_point")
        remaining = [item for item in blind_spots if item != current]
        if len(remaining) == len(blind_spots):
            return blind_spots[1:]
        return remaining

    @staticmethod
    def _completed_nodes(session: _InteractiveSession) -> list[str]:
        nodes = ["岗前测评", "岗位微课", "数据实操", "结论判断"]
        if session.completed_correction:
            nodes.extend(["反证追问", "修正结论"])
        return nodes

    @staticmethod
    def _learning_task_draft(
        session: _InteractiveSession,
        action: str,
    ) -> dict[str, Any] | None:
        content = _payload_content(session.learning_task or {})
        template_id = content.get("template_id")
        if not isinstance(template_id, str) or not template_id.strip():
            raise InteractiveSessionError("当前训练内容不完整，请稍后重试。")
        try:
            return session.runtime.task.generate_for_learning_action(
                template_id,
                action,
            )
        except ValueError as exc:
            raise InteractiveSessionError(
                "暂时无法为你匹配合适的下一步训练，请稍后重试。"
            ) from exc

    @staticmethod
    def _assessment_task_draft(
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        content = _payload_content(session.learning_task or {})
        template_id = content.get("template_id")
        if not isinstance(template_id, str) or not template_id.strip():
            raise InteractiveSessionError("当前训练内容不完整，请稍后重试。")
        try:
            draft = session.runtime.task.generate_assessment(template_id)
            return (
                session.evidence_bundle.bind(draft)
                if session.evidence_bundle is not None
                else draft
            )
        except ValueError as exc:
            raise InteractiveSessionError(
                "暂时无法为你生成分阶测验，请稍后重试。"
            ) from exc

    def submit_follow_up(
        self,
        session_id: str,
        text: str,
        client_turn_id: str,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        with session.follow_up_lock:
            return self._submit_follow_up_locked(
                session,
                text,
                client_turn_id,
            )

    def _submit_follow_up_locked(
        self,
        session: _InteractiveSession,
        text: str,
        client_turn_id: str,
    ) -> dict[str, Any]:
        session_id = session.session_id
        if (
            isinstance(client_turn_id, str)
            and client_turn_id in session.processed_turn_ids
        ):
            return self.get_state(session_id)
        if session.awaiting != "follow_up":
            raise InteractiveSessionError("当前步骤不能提交这段判断。")
        if (
            not isinstance(client_turn_id, str)
            or not client_turn_id.strip()
            or len(client_turn_id) > 120
        ):
            raise InteractiveSessionError("本次回答未能安全提交，请重试。")
        try:
            answer_text = normalize_learner_input(text)
        except ValueError as exc:
            raise InteractiveSessionError(
                "请用业务或学习语言描述你的判断。"
            ) from exc
        if not session.follow_up_question:
            raise InteractiveSessionError("当前理解核对内容不完整，请稍后重试。")

        session.outcome = None
        runtime = session.runtime
        if runtime.engine.state not in {State.S7_STUDENT, State.S8_PROBE}:
            raise InteractiveSessionError("当前步骤不能提交这段判断。")
        submitted_round = session.follow_up_round
        if not 1 <= submitted_round <= MAX_FOLLOW_UP_ROUNDS:
            raise InteractiveSessionError("本轮理解核对已经结束。")
        current_task = session.learning_task or session.active_task
        if current_task is None:
            raise InteractiveSessionError("当前理解核对内容不完整，请稍后重试。")

        self._publish_activity(
            session,
            "task",
            "working",
            "learner_answer_assessment",
            f"正在评估第 {submitted_round} 轮学员判断",
        )

        if client_turn_id not in session.recorded_turn_ids:
            runtime.audit(
                self._follow_up_submission_draft(
                    session,
                    answer_text,
                    client_turn_id,
                    submitted_round,
                )
            )
            session.recorded_turn_ids.add(client_turn_id)
        terminal_round = submitted_round >= MAX_FOLLOW_UP_ROUNDS
        generation_round = min(
            submitted_round + 1,
            MAX_FOLLOW_UP_ROUNDS,
        )
        try:
            generated = session.follow_up_agent.generate(
                student_answer=answer_text,
                current_task=current_task,
                task_agent=runtime.task,
                round_index=generation_round,
                max_rounds=MAX_FOLLOW_UP_ROUNDS,
                completion_allowed=submitted_round >= 2,
                terminal_round=terminal_round,
            )
            reviewed_product: dict[str, Any] | None = None
            if generated.product is not None:
                generated, reviewed_product = self._review_follow_up_product(
                    session,
                    answer_text=answer_text,
                    current_task=current_task,
                    round_index=generation_round,
                    completion_allowed=submitted_round >= 2,
                    terminal_round=terminal_round,
                    initial=generated,
                )
        except ReviewFlowTerminal as terminal:
            self._finish_review_stop(
                session,
                terminal.control_message,
                terminal.action,
            )
            session.processed_turn_ids.add(client_turn_id)
            return self.get_state(session_id)
        except ReviewFlowInterrupted as interrupted:
            self._finish_review_stop(
                session,
                interrupted.last_message,
                "system_error",
            )
            session.processed_turn_ids.add(client_turn_id)
            return self.get_state(session_id)
        except (FollowUpGenerationError, ReviewFlowError) as exc:
            raise InteractiveSessionError(
                "内容暂时无法继续生成，请稍后重试。"
            ) from exc

        runtime.audit(
            self._follow_up_assessment_draft(
                session,
                generated,
                submitted_round,
            )
        )
        session.follow_up_turns.append(
            {
                "round": submitted_round,
                "question": session.follow_up_question,
                "answer": text.strip(),
            }
        )
        session.follow_up_target = generated.target_misconception

        if generated.assessment == "mastered" and submitted_round >= 2:
            answer = self._finish_mastered_follow_up(session)
            session.artifact = answer
            session.processed_turn_ids.add(client_turn_id)
            return self.get_state(session_id)

        if terminal_round:
            answer = self._finish_unmastered_follow_up(session)
            session.artifact = answer
            session.processed_turn_ids.add(client_turn_id)
            return self.get_state(session_id)

        if reviewed_product is None:
            raise InteractiveSessionError(
                "内容暂时无法继续生成，请稍后重试。"
            )
        question = str(
            _payload_content(reviewed_product).get("question", "")
        ).strip()
        if not question or contains_engineering_text(question):
            raise InteractiveSessionError(
                "内容暂时无法继续生成，请稍后重试。"
            )
        if generated.assessment != "mastered":
            self._record_unmastered_transition(session, generated)
        session.follow_up_round = submitted_round + 1
        session.follow_up_question = question
        session.artifact = reviewed_product
        session.interaction = self._follow_up_interaction(session)
        session.awaiting = "follow_up"
        session.processed_turn_ids.add(client_turn_id)
        return self.get_state(session_id)

    def _review_follow_up_product(
        self,
        session: _InteractiveSession,
        *,
        answer_text: str,
        current_task: Mapping[str, Any],
        round_index: int,
        completion_allowed: bool,
        terminal_round: bool,
        initial: FollowUpTurn,
    ) -> tuple[FollowUpTurn, dict[str, Any]]:
        runtime = session.runtime
        candidate = initial
        first = True

        def produce() -> dict[str, Any]:
            nonlocal candidate, first
            if first:
                first = False
            else:
                candidate = session.follow_up_agent.generate(
                    student_answer=answer_text,
                    current_task=current_task,
                    task_agent=runtime.task,
                    round_index=round_index,
                    max_rounds=MAX_FOLLOW_UP_ROUNDS,
                    review_feedback=(
                        "上一版问题未通过专业审核，请重新组织。",
                    ),
                    completion_allowed=completion_allowed,
                    terminal_round=terminal_round,
                )
                if (
                    candidate.assessment != initial.assessment
                    or candidate.target_misconception
                    != initial.target_misconception
                ):
                    raise FollowUpGenerationError(
                        "regeneration changed the learner assessment"
                    )
            if candidate.product is None:
                raise FollowUpGenerationError(
                    "review regeneration produced no question"
                )
            return candidate.product

        def observe(event: str, details: Mapping[str, Any]) -> None:
            if self._publish_specialist_review_activity(
                session,
                event,
                details,
                producer_agent="task",
            ):
                return
            self._publish_targeted_dispute_specialists(
                session,
                event,
                details,
                producer_agent="task",
            )
            if event == "producer_started":
                self._publish_activity(
                    session,
                    "task",
                    "working",
                    "follow_up_generation",
                    "正在生成自适应追问",
                    details=details,
                )
            elif event == "product_ready":
                self._publish_activity(
                    session,
                    "task",
                    "collaborating",
                    "follow_up_generation",
                    "追问草稿已送交专业审核",
                    peers=("review",),
                    details=details,
                )
            elif event == "review_started":
                self._publish_activity(
                    session,
                    "review",
                    "reviewing",
                    "follow_up_quality_gate",
                    "正在审查追问的事实与教学适配性",
                    peers=("task",),
                    details=details,
                )
            elif event == "parallel_review_started":
                self._publish_activity(
                    session,
                    "review",
                    "collaborating",
                    "parallel_quality_review",
                    "事实、教学、数据安全与表达正在四维并行审核",
                    peers=("task",),
                    details=details,
                )
            elif event == "parallel_review_completed":
                self._publish_activity(
                    session,
                    "review",
                    "reviewing",
                    "parallel_quality_review",
                    "四维审核已汇聚，正在执行确定性裁决",
                    peers=("task",),
                    details=details,
                )
            elif event == "review_completed":
                accepted = details.get("decision") in {"approve", "approve_with_fix"}
                direct_regeneration = details.get("dispute_route") == "local_regeneration"
                self._publish_activity(
                    session,
                    "review",
                    "approved" if accepted else "waiting",
                    "follow_up_quality_gate",
                    (
                        "追问已通过质量门"
                        if accepted
                        else "追问命中硬规则，准备局部重生成"
                        if direct_regeneration
                        else "追问存在可辩争议，准备定向复核"
                    ),
                    peers=() if accepted else ("task",),
                    details=details,
                )
            elif event == "regeneration_started":
                self._publish_activity(
                    session,
                    "review",
                    "blocked",
                    "deterministic_rejection_route",
                    "硬规则命中，追问跳过模型辩论",
                    peers=("task",),
                    details=details,
                )
                self._publish_activity(
                    session,
                    "task",
                    "queued",
                    "local_regeneration",
                    "仅重新生成当前追问",
                    peers=("review",),
                    details=details,
                )
            elif event == "debate_started":
                for agent, peer in (("task", "review"), ("review", "task")):
                    self._publish_activity(
                        session,
                        agent,
                        "debating",
                        "follow_up_debate",
                        "正在围绕追问依据进行有界复核",
                        peers=(peer,),
                        details=details,
                    )

        product = audit_and_review(
            produce,
            audit=runtime.audit,
            review=lambda value: runtime.review.review(
                value,
                learning_report=session.diagnosis,
                student_profile=runtime.profile,
                learned_knowledge_points=runtime.learned_knowledge_points,
                activity_observer=observe,
                learning_contract=session.learning_contract,
            ),
            generate_rebuttal=runtime.rebuttal.generate,
            re_review=lambda value, verdict, rebuttal: runtime.review.re_review(
                value,
                verdict,
                rebuttal,
                learning_report=session.diagnosis,
                student_profile=runtime.profile,
                learned_knowledge_points=runtime.learned_knowledge_points,
                learning_contract=session.learning_contract,
            ),
            max_cycles=(
                session.learning_contract.quality_policy.max_review_cycles
                if session.learning_contract is not None
                else 4
            ),
            terminal_action="refuse",
            on_event=observe,
            quality_policy=(
                session.learning_contract.quality_policy
                if session.learning_contract is not None
                else None
            ),
        )
        self._publish_activity(
            session,
            "task",
            "done",
            "follow_up_generation",
            "下一轮追问已准备完成",
        )
        return candidate, product

    @staticmethod
    def _follow_up_submission_draft(
        session: _InteractiveSession,
        answer_text: str,
        client_turn_id: str,
        round_index: int,
    ) -> dict[str, Any]:
        return {
            "trace_id": session.runtime.options.trace_id,
            "agent": "system",
            "role": "system",
            "payload": {
                "type": "control",
                "content": {
                    "event": "learner_follow_up_submitted",
                    "source": "student",
                    "round": round_index,
                    "answer": answer_text,
                    "client_turn_id": client_turn_id,
                },
            },
            "evidence": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _follow_up_assessment_draft(
        session: _InteractiveSession,
        generated: FollowUpTurn,
        round_index: int,
    ) -> dict[str, Any]:
        return {
            "trace_id": session.runtime.options.trace_id,
            "agent": "diagnosis",
            "role": "probe",
            "payload": {
                "type": "control",
                "content": {
                    "event": "learner_follow_up_assessed",
                    "round": round_index,
                    "assessment": generated.assessment,
                    "target_misconception": generated.target_misconception,
                },
            },
            "evidence": [],
            "model": generated.model,
            "latency_ms": generated.latency_ms,
            "token_usage": generated.token_usage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _student_answer_draft(
        session: _InteractiveSession,
        *,
        answer_result: str,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "event": "student_answer",
            "answer_result": answer_result,
            "answer_mode": "free_text",
            "source": "student",
        }
        if answer_result == "wrong":
            content["requested_action"] = "probe"
        return {
            "trace_id": session.runtime.options.trace_id,
            "agent": "system",
            "role": "system",
            "payload": {"type": "control", "content": content},
            "evidence": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _probe_outcome_draft(
        session: _InteractiveSession,
        *,
        answer_result: str,
    ) -> dict[str, Any]:
        target = session.follow_up_target or UNKNOWN_MISCONCEPTION
        return {
            "trace_id": session.runtime.options.trace_id,
            "agent": "system",
            "role": "system",
            "payload": {
                "type": "control",
                "content": {
                    "event": "probe_outcome",
                    "answer_result": answer_result,
                    "answer_mode": "free_text",
                    "target_misconception": target,
                    "source": "student",
                },
            },
            "evidence": [],
            "probe": {
                "wrong_attempts": 1,
                "target_misconception": target,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _unknown_probe_draft(
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        return {
            "trace_id": session.runtime.options.trace_id,
            "agent": "diagnosis",
            "role": "probe",
            "payload": {
                "type": "probe_questions",
                "content": {
                    "event": "misconception_diagnosed",
                    "target_misconception": UNKNOWN_MISCONCEPTION,
                },
            },
            "evidence": [],
            "probe": {
                "wrong_attempts": 1,
                "questions": [session.follow_up_question],
                "target_misconception": UNKNOWN_MISCONCEPTION,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _record_unmastered_transition(
        self,
        session: _InteractiveSession,
        generated: FollowUpTurn,
    ) -> None:
        runtime = session.runtime
        session.follow_up_had_support = True
        if runtime.engine.state is State.S7_STUDENT:
            runtime.transition(
                self._student_answer_draft(
                    session,
                    answer_result="wrong",
                ),
                "T15",
            )
            if (
                generated.target_misconception == UNKNOWN_MISCONCEPTION
                and not session.generic_fallback_used
            ):
                runtime.transition(
                    self._unknown_probe_draft(session),
                    "T18",
                )
                session.generic_fallback_used = True
        elif runtime.engine.state is not State.S8_PROBE:
            raise InteractiveSessionError("当前理解核对进度无法继续。")

    def _finish_mastered_follow_up(
        self,
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        runtime = session.runtime
        if runtime.engine.state is State.S7_STUDENT:
            answer = runtime.transition(
                self._student_answer_draft(
                    session,
                    answer_result="correct",
                ),
                "T14",
            )
        elif runtime.engine.state is State.S8_PROBE:
            answer = runtime.transition(
                self._probe_outcome_draft(
                    session,
                    answer_result="correct",
                ),
                "T16",
            )
        else:
            raise InteractiveSessionError("当前理解核对进度无法继续。")
        session.pending_learning_action = "step_up"
        session.completed_correction = session.follow_up_had_support
        session.awaiting = "advance"
        if (
            session.follow_up_target == "M-01"
            and session.completed_correction
        ):
            session.interaction = self._collision_interaction(session)
        else:
            session.interaction = {
                "kind": "next_learning_step",
                "message": "你的判断已经能够用数据说明，正在为你安排下一步训练。",
            }
        return answer

    def _finish_unmastered_follow_up(
        self,
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        runtime = session.runtime
        if runtime.engine.state is State.S7_STUDENT:
            runtime.transition(
                self._student_answer_draft(
                    session,
                    answer_result="wrong",
                ),
                "T15",
            )
        if runtime.engine.state is not State.S8_PROBE:
            raise InteractiveSessionError("当前理解核对进度无法继续。")
        answer = runtime.transition(
            self._probe_outcome_draft(
                session,
                answer_result="wrong",
            ),
            "T17",
        )
        session.interaction = {
            "kind": "learning_notice",
            "message": "这个判断还需要再巩固。我们先回顾一个关键点，再重新练习。",
        }
        session.active_task = None
        session.learning_task = None
        session.task_phase = "initial"
        session.pending_learning_action = None
        session.completed_correction = False
        session.awaiting = "advance"
        return answer

    def submit_sql(self, session_id: str, sql: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.awaiting != "sql" or session.active_task is None:
            raise InteractiveSessionError("当前步骤不能提交数据查询。")
        task_content = _payload_content(session.active_task)
        question = str(task_content.get("question", "数据实操"))
        family = str(task_content.get("family", ""))
        runtime = session.runtime
        entry_state = runtime.engine.state
        if entry_state is not State.S7_STUDENT:
            raise InteractiveSessionError("当前状态不能接收数据查询。")
        self._publish_activity(
            session,
            "verification",
            "working",
            "sql_validation",
            "正在执行白名单校验与只读查询",
        )
        session.outcome = None
        submission_draft = {
            "trace_id": runtime.options.trace_id,
            "agent": "system",
            "role": "system",
            "payload": {
                "type": "control",
                "content": {
                    "event": "student_sql_submitted",
                    "source": "student",
                    "question": question,
                    "submitted_sql": str(sql),
                },
            },
            "evidence": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        runtime.transition(submission_draft, "T11")
        decision = validate_and_rewrite(sql)
        if decision.allowed:
            allowed_contracts = FAMILY_COLUMN_SETS.get(family, frozenset())
            if _output_aliases(str(sql)) not in allowed_contracts:
                return self._record_sql_rejection(
                    session,
                    question,
                    family,
                    str(sql),
                    "S-04",
                    f"output columns do not match the {family} contract",
                )
            executed_sql = str(decision.executed_sql)
            session.query_count += 1
            query_id = (
                f"{session.runtime.options.trace_id}-sql-"
                f"{session.query_count:02d}"
            )
            try:
                result = session.executor.execute(executed_sql)
            except QueryTimeoutError:
                return self._record_query_failure(
                    session,
                    question=question,
                    family=family,
                    sql=str(sql),
                    executed_sql=executed_sql,
                    query_id=query_id,
                    event="query_timeout",
                    status="timeout",
                    student_message="查询超时，请缩小查询范围。",
                )
            except QueryExecutionError:
                return self._record_query_failure(
                    session,
                    question=question,
                    family=family,
                    sql=str(sql),
                    executed_sql=executed_sql,
                    query_id=query_id,
                    event="query_failed",
                    status="failed",
                    student_message="只读查询未能执行，请稍后重试。",
                )
            normalized_rows = _normalize_rows(result.rows)
            if _result_is_empty(family, normalized_rows):
                return self._record_query_failure(
                    session,
                    question=question,
                    family=family,
                    sql=str(sql),
                    executed_sql=executed_sql,
                    query_id=query_id,
                    event="query_empty",
                    status="empty",
                    student_message="查询无数据，请检查查询条件。",
                    rows=[],
                )
            claim_texts = _render_claims(family, question, normalized_rows)
            quote = VerificationAgent._query_quote(
                str(sql),
                executed_sql,
                normalized_rows,
                "completed",
            )
            result_draft = {
                "trace_id": session.runtime.options.trace_id,
                "agent": "verification",
                "role": "produce",
                "payload": {
                    "type": "sql_result",
                    "content": {
                        "event": "query_completed",
                        "question": question,
                        "family": family,
                        "query_id": query_id,
                        "generated_sql": str(sql),
                        "executed_sql": executed_sql,
                        "sql_source": "student",
                        "columns": list(result.columns),
                        "rows": normalized_rows,
                        "row_count": len(normalized_rows),
                        "query_elapsed_ms": result.elapsed_ms,
                    },
                },
                "evidence": [
                    {
                        "kind": "sql_query",
                        "ref": query_id,
                        "quote": quote,
                        "supports_claim": claim,
                    }
                    for claim in claim_texts
                ],
                "claims": [
                    {"text": claim, "kind": "data_conclusion"}
                    for claim in claim_texts
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if session.diagnosis is None:
                raise InteractiveSessionError("岗前测评结果缺失。")
            sql_result = self._produce_reviewed_product(
                session,
                lambda: result_draft,
                session.diagnosis,
                "T12",
                "T13",
                producer_agent="verification",
                activity="data_verification",
                working_label="正在核验查询结果与数据结论",
            )
            if sql_result is None:
                return self.get_state(session_id)
            session.sql_result = sql_result
            session.awaiting = "advance"
            session.artifact = sql_result
            return self.get_state(session_id)
        return self._record_sql_rejection(
            session,
            question,
            family,
            str(sql),
            str(decision.rule_id),
            str(decision.reason),
        )

    def _record_sql_rejection(
        self,
        session: _InteractiveSession,
        question: str,
        family: str,
        sql: str,
        rule_id: str,
        reason: str,
    ) -> dict[str, Any]:
        rule_messages = {
            "S-01": "只允许查询数据，请使用 SELECT。",
            "S-02": "每次只能提交一条查询语句。",
            "S-03": "查询包含未开放的数据表。",
            "S-04": "查询包含未开放的数据字段。",
            "S-05": "查询包含不安全的操作。",
            "S-07": "查询格式无法识别，请检查语法。",
        }
        failure = self._record_verification_failure(
            session,
            {
                "trace_id": session.runtime.options.trace_id,
                "agent": "verification",
                "role": "produce",
                "payload": {
                    "type": "sql_result",
                    "content": {
                        "event": "sandbox_rejected",
                        "question": question,
                        "family": family,
                        "generated_sql": sql,
                        "sql_source": "student",
                        "rule_id": rule_id,
                        "rule_reason": reason,
                        "student_message": rule_messages.get(
                            rule_id,
                            "查询未通过安全校验，请修改后重试。",
                        ),
                    },
                },
                "evidence": [
                    {"kind": "review_rule", "ref": rule_id, "quote": reason}
                ],
                "claims": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        session.artifact = failure
        return self.get_state(session.session_id)

    def _record_query_failure(
        self,
        session: _InteractiveSession,
        *,
        question: str,
        family: str,
        sql: str,
        executed_sql: str,
        query_id: str,
        event: str,
        status: str,
        student_message: str,
        rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        failure = self._record_verification_failure(
            session,
            {
                "trace_id": session.runtime.options.trace_id,
                "agent": "verification",
                "role": "produce",
                "payload": {
                    "type": "sql_result",
                    "content": {
                        "event": event,
                        "question": question,
                        "family": family,
                        "query_id": query_id,
                        "generated_sql": sql,
                        "executed_sql": executed_sql,
                        "sql_source": "student",
                        "student_message": student_message,
                        **({"rows": rows, "row_count": len(rows)} if rows is not None else {}),
                    },
                },
                "evidence": [
                    {
                        "kind": "sql_query",
                        "ref": query_id,
                        "quote": VerificationAgent._query_quote(
                            sql,
                            executed_sql,
                            rows,
                            status,
                        ),
                    }
                ],
                "claims": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        session.artifact = failure
        return self.get_state(session.session_id)

    @staticmethod
    def _record_verification_failure(
        session: _InteractiveSession,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        event = _payload_content(draft).get("event")
        outcome = verification_outcome(event)
        if outcome is None:
            raise InteractiveSessionError("本次查询结果无法识别，请稍后重试。")
        if session.runtime.engine.state is not State.S4_VERIFY:
            raise InteractiveSessionError("当前状态不能记录查询结果。")
        failure = session.runtime.transition(draft, "T21")
        session.outcome = outcome.value
        if session.events is not None:
            session.events.publish(
                agent="verification",
                status="blocked",
                activity="data_verification",
                label="安全校验已阻断查询" if outcome.value == "safe_rejected" else "数据验证未能完成",
                stage=session.runtime.engine.state.value,
                details={"outcome": outcome.value},
            )
        return failure

    def get_state(self, session_id: str) -> dict[str, Any]:
        session = self._get_session(session_id)
        trace_path = session.runtime.bus.trace_path(
            session.runtime.options.trace_id
        )
        messages = _trace_messages(trace_path)
        events = session.events.after(0) if session.events is not None else []
        return {
            "session_id": session.session_id,
            "trace_id": session.runtime.options.trace_id,
            "trace_path": str(trace_path),
            "state": session.runtime.engine.state.value,
            "awaiting": session.awaiting,
            "outcome": session.outcome,
            "mode": session.runtime.mode,
            "profile": dict(session.runtime.profile),
            "learning_contract": (
                session.learning_contract.as_dict()
                if session.learning_contract is not None
                else None
            ),
            "evidence_bundle": (
                session.evidence_bundle.as_dict()
                if session.evidence_bundle is not None
                else None
            ),
            "resource_bundle": (
                session.resource_bundle.as_dict()
                if session.resource_bundle is not None
                else None
            ),
            "coordination_evidence": build_coordination_evidence(events, messages),
            "messages": messages,
            "artifact": session.artifact,
            "interaction": session.interaction,
        }

    def get_agent_events(
        self,
        session_id: str,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        session = self._get_session(session_id)
        if session.events is None:
            return []
        return session.events.after(max(after_sequence, 0))

    def wait_agent_events(
        self,
        session_id: str,
        after_sequence: int = 0,
        *,
        timeout: float = 15.0,
    ) -> list[dict[str, Any]]:
        session = self._get_session(session_id)
        if session.events is None:
            return []
        return session.events.wait_after(
            max(after_sequence, 0),
            timeout=timeout,
        )

    def _observe_resource_stage(
        self,
        session: _InteractiveSession,
        event: str,
        details: Mapping[str, Any],
    ) -> None:
        """Translate the shared fork/join protocol into Agent lifecycle events."""
        normalized = dict(details)
        if session.evidence_bundle is not None:
            normalized["evidence_bundle_id"] = session.evidence_bundle.bundle_id
            normalized["evidence_source_ids"] = list(
                session.evidence_bundle.sources
            )
        branch_ids = normalized.get("branch_ids")
        branches = normalized.get("branches")
        fan_out = (
            len(branch_ids)
            if isinstance(branch_ids, list)
            else len(branches)
            if isinstance(branches, list)
            else 3
        )
        normalized["fan_out"] = fan_out
        if event == "stage_started":
            normalized["aggregation"] = "pending"
            resource_agents = ("knowledge", "task", "assessment")
            for agent in resource_agents:
                self._publish_activity(
                    session,
                    agent,
                    "collaborating",
                    "parallel_resource_generation",
                    "微课、实操与分阶测验已三路并行派发",
                    peers=tuple(item for item in resource_agents if item != agent),
                    details=normalized,
                )
            return
        branch_id = normalized.get("branch_id")
        branch_agents = {
            "knowledge": "knowledge",
            "practice": "task",
            "assessment": "assessment",
        }
        if branch_id not in branch_agents:
            if event != "stage_completed":
                return
        agent_id = branch_agents.get(str(branch_id), "knowledge")
        peer_ids = tuple(
            agent
            for agent in ("knowledge", "task", "assessment")
            if agent != agent_id
        )
        if event == "branch_started":
            label = {
                "knowledge": "正在检索证据并生成个性化微课",
                "practice": "正在并行准备实操任务草稿",
                "assessment": "正在并行生成匹配难度的分阶测验",
            }[str(branch_id)]
            self._publish_activity(
                session,
                agent_id,
                "working",
                "parallel_resource_generation",
                label,
                peers=peer_ids,
                details={**normalized, "aggregation": "pending"},
            )
            return
        if event == "branch_completed":
            label = {
                "knowledge": "微课分支已完成，等待资源汇聚",
                "practice": "实操草稿已就绪，等待资源汇聚",
                "assessment": "分阶测验已就绪，等待资源汇聚",
            }[str(branch_id)]
            self._publish_activity(
                session,
                agent_id,
                "waiting",
                "parallel_resource_generation",
                label,
                peers=peer_ids,
                details={**normalized, "aggregation": "pending"},
            )
            return
        if event == "branch_failed":
            self._publish_activity(
                session,
                agent_id,
                "blocked",
                "parallel_resource_generation",
                "当前资源分支未完成",
                peers=peer_ids,
                details={**normalized, "aggregation": "pending"},
            )
            return
        if event == "stage_completed":
            normalized.update(
                {
                    "aggregation": "deterministic",
                    "parallel_elapsed_ms": normalized.get("elapsed_ms"),
                }
            )
            safely_stopped = session.awaiting == "done" and session.outcome != "completed"
            self._publish_activity(
                session,
                "knowledge",
                "blocked" if safely_stopped else "done",
                "parallel_resource_generation",
                (
                    "资源并行阶段已安全停止"
                    if safely_stopped
                    else "三路教学资源已汇聚，进入主链路质量门"
                ),
                peers=("task", "assessment"),
                details=normalized,
            )

    def _observe_evidence_stage(
        self,
        session: _InteractiveSession,
        event: str,
        details: Mapping[str, Any],
    ) -> None:
        """Expose three-source evidence fan-out without leaking source content."""
        normalized = dict(details)
        branch_ids = normalized.get("branch_ids")
        branches = normalized.get("branches")
        normalized["fan_out"] = (
            len(branch_ids)
            if isinstance(branch_ids, list)
            else len(branches)
            if isinstance(branches, list)
            else 3
        )
        agents = {
            "knowledge": "knowledge",
            "business_data": "verification",
            "pedagogy": "diagnosis",
        }
        labels = {
            "knowledge": "正在检索领域知识证据",
            "business_data": "正在解析业务数据口径与任务锚点",
            "pedagogy": "正在提取岗位画像与教学策略",
        }
        if event == "stage_started":
            normalized["aggregation"] = "pending"
            for branch_id, agent in agents.items():
                peers = tuple(
                    peer_agent
                    for peer_id, peer_agent in agents.items()
                    if peer_id != branch_id
                )
                self._publish_activity(
                    session,
                    agent,
                    "collaborating",
                    "parallel_evidence_retrieval",
                    "三路证据源已并行派发",
                    peers=peers,
                    details=normalized,
                )
            return
        branch_id = normalized.get("branch_id")
        if branch_id in agents:
            agent = agents[str(branch_id)]
            peers = tuple(value for key, value in agents.items() if key != branch_id)
            if event == "branch_started":
                self._publish_activity(
                    session,
                    agent,
                    "working",
                    "parallel_evidence_retrieval",
                    labels[str(branch_id)],
                    peers=peers,
                    details={**normalized, "aggregation": "pending"},
                )
                return
            if event == "branch_completed":
                self._publish_activity(
                    session,
                    agent,
                    "waiting",
                    "parallel_evidence_retrieval",
                    "本路证据已就绪，等待统一证据包汇聚",
                    peers=peers,
                    details={**normalized, "aggregation": "pending"},
                )
                return
            if event == "branch_failed":
                self._publish_activity(
                    session,
                    agent,
                    "blocked",
                    "parallel_evidence_retrieval",
                    "本路证据未完成，统一证据包已阻断",
                    peers=peers,
                    details={**normalized, "aggregation": "pending"},
                )
                return
        if event == "stage_completed":
            self._publish_activity(
                session,
                "knowledge",
                "done" if normalized.get("succeeded") else "blocked",
                "parallel_evidence_retrieval",
                (
                    "三路证据已汇聚为统一 Evidence Bundle"
                    if normalized.get("succeeded")
                    else "统一证据包未能完成"
                ),
                peers=("verification", "diagnosis"),
                details={
                    **normalized,
                    "aggregation": "deterministic",
                    "parallel_elapsed_ms": normalized.get("elapsed_ms"),
                },
            )

    @staticmethod
    def _publish_activity(
        session: _InteractiveSession,
        agent: str,
        status: str,
        activity: str,
        label: str,
        *,
        peers: tuple[str, ...] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if session.events is None:
            return
        session.events.publish(
            agent=agent,
            status=status,
            activity=activity,
            label=label,
            stage=session.runtime.engine.state.value,
            peers=peers,
            details=details,
        )

    @staticmethod
    def _publish_specialist_review_activity(
        session: _InteractiveSession,
        event: str,
        details: Mapping[str, Any],
        *,
        producer_agent: str,
    ) -> bool:
        if event not in {
            "specialist_review_started",
            "specialist_review_completed",
        }:
            return False
        agent = details.get("agent")
        specialist_labels = {
            "evidence_review": "事实与证据审核执行中",
            "pedagogy_review": "教学适配审核执行中",
            "data_safety_review": "数据与安全审核执行中",
            "readability_review": "表达与可读性审核执行中",
        }
        if agent not in specialist_labels:
            return False
        completed = event == "specialist_review_completed"
        raw_status = details.get("status")
        status = (
            "blocked"
            if completed and raw_status == "blocked"
            else "done" if completed else "working"
        )
        label = details.get("label")
        if not isinstance(label, str) or not label.strip():
            label = specialist_labels[str(agent)]
        InteractiveSessionManager._publish_activity(
            session,
            agent,
            status,
            "specialist_quality_review",
            label,
            peers=(producer_agent, "review"),
            details=details,
        )
        return True

    @staticmethod
    def _publish_targeted_dispute_specialists(
        session: _InteractiveSession,
        event: str,
        details: Mapping[str, Any],
        *,
        producer_agent: str,
    ) -> None:
        if event not in {"debate_started", "debate_completed"}:
            return
        raw_agents = details.get("specialist_agents")
        if not isinstance(raw_agents, list):
            return
        agents = tuple(
            agent
            for agent in raw_agents
            if agent in {"evidence_review", "pedagogy_review"}
        )
        for agent in agents:
            label = (
                "正在针对 R-02 证据边界进行定向复核"
                if agent == "evidence_review"
                else "正在针对 R-03 教学适配进行定向复核"
            )
            if event == "debate_completed":
                label = "定向复核已完成并交回专业审核仲裁"
            InteractiveSessionManager._publish_activity(
                session,
                agent,
                "debating" if event == "debate_started" else "done",
                "targeted_dispute_review",
                label,
                peers=(producer_agent, "review"),
                details=details,
            )

    @staticmethod
    def _start_follow_up(
        session: _InteractiveSession,
        task: Mapping[str, Any],
    ) -> None:
        question = str(_payload_content(task).get("question", "")).strip()
        if not question or contains_engineering_text(question):
            raise InteractiveSessionError(
                "当前理解核对内容不完整，请稍后重试。"
            )
        session.follow_up_round = 1
        session.follow_up_question = question
        session.follow_up_turns.clear()
        session.follow_up_target = None
        session.follow_up_had_support = False
        session.generic_fallback_used = False
        session.processed_turn_ids.clear()
        session.interaction = InteractiveSessionManager._follow_up_interaction(
            session
        )
        session.awaiting = "follow_up"

    @staticmethod
    def _follow_up_interaction(
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        return {
            "kind": "free_text_follow_up",
            "prompt": session.follow_up_question,
            "round": session.follow_up_round,
            "max_rounds": MAX_FOLLOW_UP_ROUNDS,
            "turns": [dict(turn) for turn in session.follow_up_turns],
        }

    @staticmethod
    def _collision_interaction(session: _InteractiveSession) -> dict[str, Any]:
        result = session.sql_result
        rows = _payload_content(result or {}).get("rows")
        row = rows[0] if isinstance(rows, list) and rows else {}
        return {
            "kind": "data_collision",
            "misconception": "计划量与实际完成量的区分",
            "wrong_label": "计划量",
            "wrong_value": str(row.get("plan_qty", "—")),
            "correct_label": "实际完成量",
            "correct_value": str(row.get("actual_qty", "—")),
        }

    def _get_session(self, session_id: str) -> _InteractiveSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise InteractiveSessionError("会话不存在或已失效。")
        return session


class _InteractiveRequestHandler(BaseHTTPRequestHandler):
    manager: InteractiveSessionManager
    max_body_bytes = 1_000_000
    protocol_version = "HTTP/1.1"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        try:
            parts = self._path_parts()
            if (
                len(parts) == 4
                and parts[:2] == ["api", "sessions"]
                and parts[3] == "events"
            ):
                self._send_event_stream(parts[2])
                return
            if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                self._send_json(200, self.manager.get_state(parts[2]))
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "sessions"]
                and parts[3] == "pretest"
            ):
                self._send_json(
                    200,
                    {"questions": self.manager.get_pretest(parts[2])},
                )
                return
            self._send_json(404, {"error": "未找到请求的交互接口。"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except InteractiveSessionError as exc:
            self._send_json(404, {"error": str(exc)})
        except Exception:
            self._send_json(500, {"error": "交互服务暂时不可用。"})

    def _send_event_stream(self, session_id: str) -> None:
        query = parse_qs(urlsplit(self.path).query)
        raw_sequence = self.headers.get("Last-Event-ID")
        if not raw_sequence:
            raw_sequence = query.get("after", ["0"])[0]
        try:
            sequence = max(int(raw_sequence), 0)
        except (TypeError, ValueError):
            sequence = 0

        # Validate before sending headers so an expired session still returns JSON.
        self.manager.get_agent_events(session_id, sequence)
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                events = self.manager.wait_agent_events(
                    session_id,
                    sequence,
                    timeout=15.0,
                )
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    sequence = int(event["sequence"])
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    packet = (
                        f"id: {sequence}\n"
                        "event: agent-activity\n"
                        f"data: {data}\n\n"
                    ).encode("utf-8")
                    self.wfile.write(packet)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def do_POST(self) -> None:
        try:
            parts = self._path_parts()
            body = self._read_json_body()
            if parts == ["api", "sessions"]:
                profile_id = body.get("profile_id")
                if not isinstance(profile_id, str):
                    raise ValueError("请选择有效岗位画像。")
                self._send_json(201, self.manager.create_session(profile_id))
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "sessions"]
                and parts[3] == "pretest"
            ):
                answers = body.get("answers")
                if not isinstance(answers, dict):
                    raise ValueError("请完成全部岗前测评题目。")
                self._send_json(
                    200,
                    self.manager.submit_pretest(parts[2], answers),
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                session_id, action = parts[2], parts[3]
                if action == "advance":
                    self._send_json(200, self.manager.advance(session_id))
                    return
                if action == "continue":
                    self._send_json(
                        201,
                        self.manager.continue_learning(session_id),
                    )
                    return
                if action == "follow-up":
                    text = body.get("text")
                    client_turn_id = body.get("client_turn_id")
                    if not isinstance(text, str) or not isinstance(
                        client_turn_id,
                        str,
                    ):
                        raise ValueError("请填写本轮判断后再提交。")
                    self._send_json(
                        200,
                        self.manager.submit_follow_up(
                            session_id,
                            text,
                            client_turn_id,
                        ),
                    )
                    return
                if action == "sql":
                    sql = body.get("sql")
                    if not isinstance(sql, str):
                        raise ValueError("请输入要执行的查询。")
                    self._send_json(
                        200,
                        self.manager.submit_sql(session_id, sql),
                    )
                    return
            self._send_json(404, {"error": "未找到请求的交互接口。"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except InteractiveSessionError as exc:
            self._send_json(409, {"error": str(exc)})
        except Exception:
            self._send_json(500, {"error": "交互服务暂时不可用。"})

    def _path_parts(self) -> list[str]:
        path = urlsplit(self.path).path
        return [unquote(part) for part in path.split("/") if part]

    def _read_json_body(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("请求长度无效。") from exc
        if length < 0 or length > self.max_body_bytes:
            raise ValueError("请求内容过大。")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8") if raw else "{}")
        if not isinstance(value, dict):
            raise ValueError("请求内容必须是对象。")
        return value

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self._cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The state transition may have completed after a reverse-proxy or
            # browser timeout.  Treat the vanished client as transport noise;
            # the next idempotent state read will recover the canonical result.
            self.close_connection = True

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Last-Event-ID",
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_http_server(
    manager: InteractiveSessionManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    class Handler(_InteractiveRequestHandler):
        pass

    Handler.manager = manager
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="启动船厂数字化岗位培训实操通道",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--trace-dir", type=Path, default=root / "traces")
    parser.add_argument("--cache-dir", type=Path, default=root / "cache")
    args = parser.parse_args(argv)
    manager = InteractiveSessionManager(
        trace_dir=args.trace_dir,
        cache_dir=args.cache_dir,
    )
    server = build_http_server(manager, host=args.host, port=args.port)
    print(f"实操通道已启动：http://{args.host}:{server.server_port}")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
