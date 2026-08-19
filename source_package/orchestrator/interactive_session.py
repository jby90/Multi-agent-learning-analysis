"""Stepwise interactive sessions over the existing orchestrator engine and bus."""

from __future__ import annotations

import argparse
import logging
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4
from urllib.parse import parse_qs, unquote, urlsplit

from agents.diagnosis_agent import load_pretest
from agents.diagnostic_router import (
    ProbeResult,
    grade_probe_answer,
    probes_for_diagnosis,
)
from agents.review_agent import _verdict_draft
from agents.persona_router import (
    PersonaDiagnosticRouter,
    apply_calibration,
    calibration_probe_for,
    load_dependencies,
    load_persona_pretest,
    resolve_experience_tag_point,
)
from agents.kb_loader import KnowledgeChunk
from agents.knowledge_scope import prerequisite_scaffolds, responsibility_scope
from agents.follow_up_agent import (
    _is_meaningless_short_answer,
    MAX_FOLLOW_UP_ROUNDS,
    FOLLOW_UP_LAYER_NAMES,
    FollowUpAgent,
    FollowUpGenerationError,
    FollowUpTurn,
    UNKNOWN_MISCONCEPTION,
    _evidence_items,
    _expected_points,
    _expected_rows,
    _responsibility_scope,
    ambiguous_dimension_reference,
    contains_engineering_text,
    deterministic_follow_up_route,
    normalize_learner_input,
    resolve_follow_up_layer,
    reviewed_answer_confirmation,
    reviewed_answer_correction,
    reviewed_plan_actual_correction,
    reviewed_row_value_correction,
)
from agents.misconception_relations import (
    RelationIntegrityError,
    RoutingPolicy,
    default_relation_index,
    default_relation_support_points,
)
from agents.review_agent import evaluate_hard_rules
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
    query_authority_issue,
)
from agents.query_authority import query_authority_from_mapping
from coordination.contracts import LearningContract
from coordination.evidence_bundle import EvidenceBundle
from coordination.evaluation import build_coordination_evidence
from coordination.parallel import BranchSpec, ParallelStage, ParallelStageExecutor
from coordination.resource_bundle import ResourceBundle
from orchestrator.demo_session import (
    DemoSessionError,
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
from orchestrator.llm import LLMCallError, LLMResult, call_llm
from orchestrator.auth_http import AuthHttpError, AuthHttpHandler
from orchestrator.learning_records import (
    LearningRecordStore,
    build_learning_record,
    tier_from_status,
)
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


def _select_remediation_chunks(
    chunks: Sequence[KnowledgeChunk],
    *,
    excluded_chunk_ids: set[str],
    knowledge_point: str,
) -> tuple[KnowledgeChunk, ...]:
    """Refresh teaching content without dropping the current knowledge point.

    A lower-band chunk may already have appeared only as a prerequisite of the
    previous higher-band lesson.  Such an appearance must not exclude it when
    it becomes the primary chunk after T17; otherwise only a prerequisite from
    another knowledge point can survive and the fail-closed knowledge gate will
    correctly refuse the lesson.
    """

    original = tuple(chunks)
    target_chunks = tuple(
        chunk for chunk in original if chunk.knowledge_point == knowledge_point
    )
    fresh_targets = tuple(
        chunk
        for chunk in target_chunks
        if chunk.chunk_id not in excluded_chunk_ids
    )
    selected_targets = fresh_targets or target_chunks[:1]
    support_chunks = tuple(
        chunk for chunk in original if chunk.knowledge_point != knowledge_point
    )
    return selected_targets + support_chunks


@dataclass(frozen=True, slots=True)
class FollowUpReviewPolicy:
    """Internal-only switches for deterministic innovation A ablations."""

    rebuttal_budget: int = 1
    feedback_mode: str = "mapped"

    def __post_init__(self) -> None:
        if (
            isinstance(self.rebuttal_budget, bool)
            or not isinstance(self.rebuttal_budget, int)
            or self.rebuttal_budget not in {0, 1, 4}
        ):
            raise ValueError("rebuttal_budget must be one of 0, 1, or 4")
        if self.feedback_mode not in {"generic", "mapped"}:
            raise ValueError("feedback_mode must be generic or mapped")


_REVIEW_STOP_COPY = {
    "degrade_to_template": "这份内容多次未通过专业审核，本次学习已安全结束。",
    "human_review": "这份内容需要进一步确认，本次学习已暂停。",
    "refuse": "这份内容未通过专业审核，本次学习已安全结束。",
    "system_error": "内容生成服务暂时不可用，本次学习已安全结束，请稍后重新开始。",
}

_GENERIC_FOLLOW_UP_REVIEW_FEEDBACK = (
    "上一版问题未通过专业审核，请重新组织。",
)
_MAPPED_FOLLOW_UP_REVIEW_FEEDBACK = {
    "R-01": "让题目中的数据口径与当前学习任务保持一致。",
    "R-02": "让问题与所给专业材料之间的支持关系更明确。",
    "R-03": "保持学习目标和既定难度档不变，调整问题的表达、铺垫和认知负荷。",
    "R-04": "只使用已经提供且能够核验的材料重新组织问题。",
    "R-05": "只围绕已经确认有效的学习结果重新组织问题。",
}

_INITIAL_FOLLOW_UP_QUESTIONS = {
    "Q1": "根据刚才的查询结果，实际完成量是多少，它能说明怎样的完成情况？",
    "Q2": "根据刚才的查询结果，计划量与实际完成量分别是多少，哪一个表示已经完成的数量？",
    "Q3": "根据刚才的查询结果，该工序的完成率是多少，这个数值说明了怎样的完成情况？",
    "Q4": "根据刚才的月度序列，哪一个月的完成率变化最明显，你依据的数值是什么？",
    "Q5": "根据刚才的船号对比，哪一艘船的完成率最低，你依据的数值是什么？",
    "Q6": "根据刚才的三道工序结果，哪一道工序完成率最低，你依据的数值是什么？",
    "Q7": "根据刚才的责任单元对比，哪个责任单元完成率最低，你依据的数值是什么？",
}
_FOLLOW_UP_FOCUS = {
    "Q1": "核对实际完成量及其业务含义",
    "Q2": "区分计划量与实际完成量",
    "Q3": "解释完成率与计划目标的关系",
    "Q4": "识别月份、完成率和变化趋势",
    "Q5": "核对船号与完成率的对应关系",
    "Q6": "核对工序与完成率的对应关系",
    "Q7": "核对责任单元与完成率的对应关系",
}


_INITIAL_FOLLOW_UP_ROW_PINNED_QUESTIONS = {
    "Q5": "根据刚才的对比结果，哪一行完成率最低，你依据的船号、月份和数值是什么？",
    "Q6": "根据刚才的三道工序×月份结果，哪一行完成率最低，你依据的工序、月份和数值是什么？",
    "Q7": "根据刚才的责任单元对比，哪一行完成率最低，你依据的责任单元和数值是什么？",
}


def _initial_follow_up_question(task: Mapping[str, Any]) -> str:
    """Turn a reviewed data task into one evidence-citing reflection prompt."""

    content = _payload_content(task)
    family = content.get("family")
    if isinstance(family, str):
        question = _INITIAL_FOLLOW_UP_QUESTIONS.get(family)
        if question is not None:
            evidence = _evidence_items(task)
            if ambiguous_dimension_reference(question, evidence):
                pinned = _INITIAL_FOLLOW_UP_ROW_PINNED_QUESTIONS.get(family)
                if pinned is not None:
                    return pinned
            return question
    return "根据刚才的查询结果，你能引用至少一项数据说明自己的判断吗？"


def _learner_context(session: "_InteractiveSession") -> dict[str, str]:
    """闭环六：画像出题上下文（背景/讲义风格），缺失字段一律省略。"""

    profile = session.runtime.profile
    if not isinstance(profile, Mapping):
        return {}
    context: dict[str, str] = {}
    for key in ("profile_id", "title", "background", "lecture_style"):
        value = str(profile.get(key) or "").strip()
        if value:
            context[key] = value
    return context


def _lecture_digest(
    session: "_InteractiveSession",
    *,
    limit: int = 1200,
) -> str:
    """闭环六：微课要点摘要；延期讲义/无讲义时返回空串（降级输入不静默失败）。"""

    lecture = session.lecture
    if not isinstance(lecture, Mapping):
        return ""
    content = _payload_content(lecture)
    if content.get("lecture_deferred") is True:
        return ""
    markdown = str(content.get("lecture_md") or "").strip()
    if not markdown:
        return ""
    if len(markdown) <= limit:
        return markdown
    return markdown[:limit].rstrip() + "…"


def _data_digest(
    session: "_InteractiveSession",
    *,
    limit: int = 900,
) -> str:
    """闭环六：学员当前查询结果摘要（data_present 画像即其唯一数据面）。"""

    result = session.sql_result
    if not isinstance(result, Mapping):
        return ""
    content = _payload_content(result)
    columns = content.get("columns")
    rows = content.get("rows")
    parts: list[str] = []
    if isinstance(columns, list) and columns:
        parts.append("列：" + "、".join(str(column) for column in columns))
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            pair_text = "；".join(
                f"{key}={value}" for key, value in row.items()
            )
            if pair_text:
                parts.append(pair_text)
    digest = "\n".join(parts)
    if len(digest) <= limit:
        return digest
    return digest[:limit].rstrip() + "…"


def _follow_up_task_context(task: Mapping[str, Any] | None) -> dict[str, str]:
    if task is None:
        return {}
    content = _payload_content(task)
    prompt = next(
        (
            str(content.get(field)).strip()
            for field in ("contextualized_stem", "question", "standard_stem")
            if isinstance(content.get(field), str) and str(content.get(field)).strip()
        ),
        "",
    )
    family = str(content.get("family") or "").strip()
    context: dict[str, str] = {}
    if prompt:
        context["task_prompt"] = prompt
    focus = _FOLLOW_UP_FOCUS.get(family)
    if focus:
        context["focus"] = focus
    knowledge_point = content.get("knowledge_point")
    if isinstance(knowledge_point, str) and knowledge_point.strip():
        context["task_knowledge_point"] = knowledge_point.strip()
    difficulty = content.get("difficulty")
    if isinstance(difficulty, str) and difficulty.strip():
        context["task_difficulty"] = difficulty.strip()
    return context


DIFFICULTY_LABELS = {"basic": "基础", "applied": "应用", "advanced": "进阶"}


def _difficulty_label(difficulty: str) -> str:
    """学员可读的难度档中文名（供提示文案直接拼接）。"""
    return DIFFICULTY_LABELS.get(difficulty, difficulty)


def _knowledge_point_initial_difficulty(
    diagnosis_content: Mapping[str, Any],
    knowledge_point: str,
) -> str:
    """Resolve one knowledge point's immutable starting difficulty."""

    plan = diagnosis_content.get("knowledge_point_plan")
    if isinstance(plan, list):
        for item in plan:
            if not isinstance(item, Mapping):
                continue
            if item.get("knowledge_point") != knowledge_point:
                continue
            difficulty = item.get("initial_difficulty")
            if difficulty in {"basic", "applied", "advanced"}:
                return str(difficulty)
    fallback = diagnosis_content.get("difficulty")
    if fallback in {"basic", "applied", "advanced"}:
        return str(fallback)
    raise InteractiveSessionError("缺少下一知识点的初始难度，请重新完成岗前测评。")


_DONT_KNOW_ANSWERS = {
    "不知道",
    "不知道啊",
    "不知道呀",
    "不会",
    "不会啊",
    "不清楚",
    "不知道怎么回答",
    "不知道如何回答",
    "没学过",
    "没学过这个",
    "无法判断",
    "不晓得",
    "不懂",
}


def _is_dont_know_follow_up_answer(value: str) -> bool:
    """学员明确表示不会——放行并转入引导（不再是拦截词）。"""

    compact = "".join(value.split()).rstrip("。！!？?")
    return compact in _DONT_KNOW_ANSWERS


def _is_vacuous_follow_up_answer(value: str) -> bool:
    compact = "".join(value.split()).rstrip("。！!？?")
    return compact in {
        "是",
        "是的",
        "否",
        "不是",
        "不是的",
        "对",
        "对的",
        "不对",
        "正确",
        "错误",
        "同意",
        "不同意",
        "知道",
    }


def _follow_up_match_key(value: str) -> str:
    """Return a punctuation-insensitive key for copy/repetition gates."""

    return "".join(
        character.casefold()
        for character in value
        if character.isalnum()
    )


def _is_obviously_unrelated_follow_up_answer(value: str) -> bool:
    compact = _follow_up_match_key(value)
    return compact in {
        _follow_up_match_key(item)
        for item in (
            "今天天气不错",
            "随便写写",
            "和题目无关",
            "不相关",
            "测试一下",
            "asdfgh",
            "abcdef",
        )
    }


def _feedback_for_re_verdict(
    audited_message: Mapping[str, Any],
    product: Mapping[str, Any],
) -> tuple[str, ...]:
    """Map only a matching, audited reject re-verdict to safe teaching text."""

    if (
        not isinstance(audited_message, Mapping)
        or not isinstance(product, Mapping)
    ):
        return ()
    payload = audited_message.get("payload")
    content = payload.get("content") if isinstance(payload, Mapping) else None
    product_payload = product.get("payload")
    product_type = (
        product_payload.get("type")
        if isinstance(product_payload, Mapping)
        else None
    )
    verdict = audited_message.get("verdict")
    if (
        audited_message.get("agent") != "review"
        or audited_message.get("role") != "re_verdict"
        or audited_message.get("trace_id") != product.get("trace_id")
        or not isinstance(product.get("msg_id"), str)
        or not isinstance(product_type, str)
        or not isinstance(payload, Mapping)
        or payload.get("type") != "review_verdict"
        or not isinstance(content, Mapping)
        or content.get("reviewed_msg_id") != product.get("msg_id")
        or content.get("reviewed_payload_type") != product_type
        or not isinstance(verdict, Mapping)
        or verdict.get("decision") != "reject"
    ):
        return ()
    raw_hits = verdict.get("rule_hits")
    if not isinstance(raw_hits, list) or not raw_hits:
        return ()
    mapped: list[str] = []
    for hit in raw_hits:
        if not isinstance(hit, Mapping):
            return ()
        rule_id = hit.get("rule_id")
        feedback = _MAPPED_FOLLOW_UP_REVIEW_FEEDBACK.get(rule_id)
        if feedback is None:
            return _GENERIC_FOLLOW_UP_REVIEW_FEEDBACK
        if feedback not in mapped:
            mapped.append(feedback)
    return tuple(mapped)


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
    follow_up_submission_count: int = 0
    follow_up_layer: int = 1
    follow_up_question: str = ""
    follow_up_turns: list[dict[str, Any]] = field(default_factory=list)
    follow_up_target: str | None = None
    unresolved_misconceptions: set[str] = field(default_factory=set)
    correction_tickets: list[dict[str, Any]] = field(default_factory=list)
    remediation_attempts: dict[str, int] = field(default_factory=dict)
    deferred_knowledge_points: list[str] = field(default_factory=list)
    remediation_excluded_chunk_ids: set[str] = field(default_factory=set)
    remediation_context: dict[str, Any] | None = None
    probed_misconceptions: set[str] = field(default_factory=set)
    covered_relation_points: set[str] = field(default_factory=set)
    follow_up_had_support: bool = False
    generic_fallback_used: bool = False
    recorded_turn_ids: set[str] = field(default_factory=set)
    processed_turn_ids: set[str] = field(default_factory=set)
    advance_lock: Any = field(default_factory=RLock, repr=False)
    follow_up_lock: Any = field(default_factory=RLock, repr=False)
    query_count: int = 0
    sql_failure_count: int = 0
    sql_support: dict[str, Any] | None = None
    outcome: str | None = None
    termination: dict[str, Any] | None = None
    events: AgentEventStream | None = None
    prefetched_task: dict[str, Any] | None = None
    prefetched_task_generator: Any = None
    prefetched_assessment: dict[str, Any] | None = None
    prefetched_assessment_generator: Any = None
    resource_bundle: ResourceBundle | None = None
    training_report: dict[str, Any] | None = None
    pretest_answers: dict[str, str] | None = None
    pending_diagnostic_probes: tuple[dict[str, Any], ...] = ()
    diagnostic_probe_queue: tuple[dict[str, Any], ...] = ()
    # 闭环三：v4 校准探针上下文（None=非校准等待态）
    persona_calibration: dict[str, Any] | None = None
    # 跨难度档已问追问累积器：升档(step_up)后保留，让同句模板兜底问题
    # 不在更高难度档原样重问；T17 重练与切换知识点时清空。
    cross_phase_questions: list[str] = field(default_factory=list)
    diagnostic_probe_results: list[ProbeResult] = field(default_factory=list)
    experience_tags: tuple[str, ...] = ()
    # 0818 需求 5/6：本轮知识点学习起止时间（完成时算时长）与登录账号归属
    learning_started_at: str = ""
    account: dict[str, Any] | None = None
    # 优化9：最近活动时刻（UTC）——"在线学员会话"按 10 分钟活跃窗口统计，
    # 避免历史遗留的未完成会话（浏览器关闭即弃）永远计入在线数。
    last_active_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


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
        persona_routing: bool = False,
        follow_up_review_policy: FollowUpReviewPolicy | None = None,
        routing_policy: RoutingPolicy | None = None,
        records_dir: Path | None = None,
    ) -> None:
        self._trace_dir = Path(trace_dir)
        self._cache_dir = Path(cache_dir)
        self._llm_call = llm_call
        self._follow_up_llm_call = follow_up_llm_call
        self._executor_factory = executor_factory
        self._mode = mode or os.environ.get("REF_DEMO_MODE", "live")
        # 闭环二：画像领域路由 v4（生产入口开启；测试默认走 v3 冻结通道）
        self._persona_routing = persona_routing
        # 0818 需求 5/6：学习记录落盘（None=不记录，测试默认关闭）
        self._learning_records = (
            LearningRecordStore(Path(records_dir)) if records_dir else None
        )
        self._persona_router: PersonaDiagnosticRouter | None = None
        self._persona_dependencies: dict[str, list[str]] | None = None
        self._persona_chunk_records: list[dict[str, Any]] | None = None
        if follow_up_review_policy is not None and not isinstance(
            follow_up_review_policy,
            FollowUpReviewPolicy,
        ):
            raise ValueError(
                "follow_up_review_policy must be a FollowUpReviewPolicy"
            )
        self._follow_up_review_policy = (
            follow_up_review_policy or FollowUpReviewPolicy()
        )
        if routing_policy is not None and not isinstance(
            routing_policy,
            RoutingPolicy,
        ):
            raise ValueError("routing_policy must be a RoutingPolicy")
        self._routing_policy = routing_policy or RoutingPolicy()
        self._evidence_stage_executor = ParallelStageExecutor(max_concurrency=3)
        self._resource_stage_executor = ParallelStageExecutor(max_concurrency=3)
        self._sessions: dict[str, _InteractiveSession] = {}
        self._lock = RLock()

    def create_session(
        self,
        profile_id: str,
        *,
        experience_tags: Sequence[str] = (),
        account: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            experience_tags=tuple(str(item).strip() for item in experience_tags),
            learning_started_at=datetime.now(timezone.utc).isoformat(),
            account=dict(account) if account else None,
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
            and not (
                previous.awaiting == "done"
                and previous.outcome == "completed"
            )
        ):
            raise InteractiveSessionError("当前学习单元尚未完成，不能进入下一知识点。")
        if previous.diagnosis is None:
            raise InteractiveSessionError("缺少可继承的岗前测评结果，请重新开始。")
        previous_content = _payload_content(previous.diagnosis)
        if str(previous_content.get("router_version", "")).startswith("persona-router"):
            # v4 续学按 plan_item_id 剔除已完成条目：按需抬前置条目与标准条目
            # 同名不同条目，按知识点名排除会误跳过提升条目。
            completed_ids = {
                str(item)
                for item in previous_content.get("completed_plan_item_ids", [])
                if isinstance(item, str)
            }
            if previous_content.get("selected_plan_item_id"):
                completed_ids.add(str(previous_content["selected_plan_item_id"]))
            continued_plan = [
                dict(item)
                for item in previous_content.get("knowledge_point_plan", [])
                if isinstance(item, Mapping)
                and item.get("mastery_status") in {"needs_training", "pending_training"}
                and str(item.get("plan_item_id")) not in completed_ids
            ]
            if not continued_plan:
                raise InteractiveSessionError("当前培养路径已全部完成。")
            remaining = [str(item["knowledge_point"]) for item in continued_plan]
            selected_item = continued_plan[0]
            next_knowledge_point = str(selected_item["knowledge_point"])
            next_difficulty = str(selected_item["initial_difficulty"])
        else:
            remaining = self._remaining_blind_spots(previous)
            if not remaining:
                raise InteractiveSessionError("当前培养路径已全部完成。")
            next_knowledge_point = remaining[0]
            next_difficulty = _knowledge_point_initial_difficulty(
                previous_content,
                next_knowledge_point,
            )
            continued_plan = [
                dict(item)
                for item in previous_content.get("knowledge_point_plan", [])
                if isinstance(item, Mapping)
                and item.get("knowledge_point") in remaining
                and item.get("mastery_status") in {"needs_training", "pending_training"}
            ]
            selected_item = continued_plan[0] if continued_plan else None

        created = self.create_session(
            previous.runtime.options.profile_id,
            experience_tags=previous.experience_tags,
            account=previous.account,
        )
        next_session = self._get_session(str(created["session_id"]))
        # 续学即新一轮知识点的学习起点（create_session 已置为当下时刻，
        # 这里显式声明语义，防止后续重构丢失）
        next_session.learning_started_at = datetime.now(timezone.utc).isoformat()
        diagnosis_content = dict(previous_content)
        extra_content: dict[str, Any] = {}
        if str(previous_content.get("router_version", "")).startswith("persona-router"):
            extra_content["completed_plan_item_ids"] = sorted(completed_ids)
        diagnosis_content.update(
            {
                "event": "diagnosis_ready",
                "blind_spots": remaining,
                **extra_content,
                "difficulty": next_difficulty,
                "knowledge_point_plan": continued_plan,
                "selected_plan_item_id": (
                    selected_item.get("plan_item_id") if selected_item else None
                ),
                "selected_knowledge_point": next_knowledge_point,
                "selected_difficulty": next_difficulty,
                "diagnosis_narrative": "",
                "suggestions": [],
                "narrative_fallback": True,
                "narrative_fallback_reason": "continued_learning",
                "llm_latency_ms": 0,
            }
        )
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
            use_selected_route=True,
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
        if self._persona_routing:
            questions = load_persona_pretest(
                session.runtime.options.profile_id
            ).questions
        else:
            questions = load_pretest()
        return [
            {
                "question_id": str(question["question_id"]),
                "knowledge_point": str(question["knowledge_point"]),
                "stem": str(question["stem"]),
                "options": dict(question["options"]),
            }
            for question in questions
        ]

    def submit_pretest(
        self,
        session_id: str,
        answers: dict[str, str],
        *,
        probe_results: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.awaiting != "pretest":
            raise InteractiveSessionError("当前步骤不能提交岗前测评。")
        if self._persona_routing:
            return self._submit_persona_pretest(session, answers)
        self._publish_activity(
            session,
            "diagnosis",
            "working",
            "profile_assessment",
            "正在计算知识盲区与难度",
        )
        try:
            preliminary = session.runtime.diagnosis.assess(
                session.runtime.options.profile_id,
                answers,
                probe_results=probe_results or (),
                experience_tags=session.experience_tags,
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
        session.pretest_answers = dict(answers)
        if probe_results is None:
            preliminary_content = _payload_content(preliminary)
            has_wrong_pretest = any(
                item.get("evidence_source") == "pretest"
                and item.get("is_correct") is False
                for item in preliminary_content.get("route_evidence", [])
                if isinstance(item, Mapping)
            )
            selected_point = preliminary_content.get(
                "recommended_probe_knowledge_point"
            ) or preliminary_content.get("selected_knowledge_point")
            pending = (
                probes_for_diagnosis(
                    session.runtime.options.profile_id,
                    answers,
                    session.experience_tags,
                )
                if not has_wrong_pretest
                and isinstance(selected_point, str)
                and selected_point.strip()
                else ()
            )
            if pending:
                session.diagnostic_probe_queue = pending
                session.diagnostic_probe_results = []
                session.pending_diagnostic_probes = pending[:1]
                session.artifact = preliminary
                session.awaiting = "diagnostic_probe"
                session.interaction = {
                    "kind": "supplemental_diagnosis",
                    "title": "补充诊断",
                    # 队列里可能只有应用档探针（该知识点无基础探针或已被
                    # 前测覆盖），学员看到的序号按队列位置计，不按难度档计。
                    "probe_step": 1,
                    "probe_total": len(pending),
                    "message": "岗前测评未暴露明确错题，请再回答1-2道小题帮助确认学习起点。",
                    "questions": [
                        {
                            "probe_id": str(item["probe_id"]),
                            "knowledge_point": str(item["knowledge_point"]),
                            "difficulty": str(item["difficulty"]),
                            "stem": str(item["stem"]),
                        }
                        for item in pending[:1]
                    ],
                    "provisional_route": {
                        "knowledge_point": selected_point,
                        "difficulty": preliminary_content.get("selected_difficulty"),
                        "reason": (
                            preliminary_content.get("probe_recommendation_evidence")
                            or {}
                        ).get("route_reason"),
                        "evidence_source": (
                            preliminary_content.get("probe_recommendation_evidence")
                            or {}
                        ).get("evidence_source"),
                        "evidence_ids": (
                            preliminary_content.get("probe_recommendation_evidence")
                            or {}
                        ).get("evidence_ids", []),
                    },
                }
                self._publish_activity(
                    session,
                    "diagnosis",
                    "working",
                    "supplemental_diagnosis",
                    "等待完成补充小题",
                )
                return self.get_state(session_id)
        return self._finalize_diagnosis(session, preliminary)

    def _persona_router_ready(self) -> PersonaDiagnosticRouter:
        if self._persona_router is None:
            self._persona_router = PersonaDiagnosticRouter()
        if self._persona_dependencies is None:
            self._persona_dependencies = load_dependencies()
        if self._persona_chunk_records is None:
            from agents.kb_loader import require_valid_chunks
            from agents.knowledge_scope import CHUNK_DIRECTORY

            self._persona_chunk_records = [
                {
                    "chunk_id": str(chunk.chunk_id),
                    "knowledge_point": str(chunk.knowledge_point),
                    "prerequisites": [str(item) for item in chunk.prerequisites],
                }
                for chunk in require_valid_chunks(CHUNK_DIRECTORY)
            ]
        return self._persona_router

    def _persona_build(
        self,
        session: _InteractiveSession,
        answers: Mapping[str, str],
    ) -> tuple[dict[str, Any], str | None, Any, list[dict[str, Any]]]:
        """v4：评分 + 构建培养清单（无副作用，可重复调用供校准回写复用）。"""
        profile_id = session.runtime.options.profile_id
        router = self._persona_router_ready()
        pretest = router.pretest_for(profile_id)
        evidence = pretest.score(answers)
        focus_point = None
        for tag_id in session.experience_tags or ():
            focus_point = resolve_experience_tag_point(str(tag_id))
            if focus_point:
                break
        result = router.build_plan(
            profile_id,
            answers,
            dependencies=self._persona_dependencies or {},
            experience_tag_point=focus_point,
            chunk_records=self._persona_chunk_records or (),
        )
        return result, focus_point, pretest, evidence

    def _persona_draft(
        self,
        session: _InteractiveSession,
        result: Mapping[str, Any],
        pretest: Any,
        evidence: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """v4：组装诊断 draft 消息（不 transition，探针等待态与终化共用）。"""
        profile_id = session.runtime.options.profile_id
        wrong_points = {
            str(item["knowledge_point"]) for item in evidence if not item["is_correct"]
        }
        hit_misconceptions: list[str] = []
        for question in pretest.questions:
            if str(question["knowledge_point"]) in wrong_points:
                for code in question.get("misconceptions", []):
                    if str(code) not in hit_misconceptions:
                        hit_misconceptions.append(str(code))
        total = len(evidence)
        correct = sum(1 for item in evidence if item["is_correct"])
        content: dict[str, Any] = {
            "event": "diagnosis_ready",
            "profile_id": profile_id,
            "blind_spots": list(result["blind_spots"]),
            "hit_misconceptions": hit_misconceptions,
            "difficulty": result["selected_difficulty"],
            "pretest_baseline_difficulty": result["selected_difficulty"],
            "pretest_score": {
                "correct": correct,
                "total": total,
                "rate": round(correct / total, 4) if total else 0.0,
            },
            "knowledge_point_plan": result["knowledge_point_plan"],
            "selected_plan_item_id": result["selected_plan_item_id"],
            "selected_knowledge_point": result["selected_knowledge_point"],
            "selected_difficulty": result["selected_difficulty"],
            "route_evidence": result["route_evidence"],
            "clamped_prerequisites": result["clamped_prerequisites"],
            "diagnostic_probe_count": 1 if result.get("calibration") else 0,
            "router_version": result["router_version"],
            "pretest_version": result["pretest_version"],
            "experience_tags": list(session.experience_tags or ()),
            "diagnosis_narrative": "",
            "suggestions": [],
            "narrative_fallback": True,
            "narrative_fallback_reason": "persona_router_v4",
            "llm_latency_ms": 0,
        }
        if result.get("calibration"):
            content["calibration"] = result["calibration"]
        return {
            "trace_id": session.runtime.options.trace_id,
            "agent": "diagnosis",
            "role": "produce",
            "payload": {"type": "profile_assessment", "content": content},
            "evidence": [],
            "claims": [],
            "student_profile_ref": profile_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _submit_persona_pretest(
        self,
        session: _InteractiveSession,
        answers: Mapping[str, str],
    ) -> dict[str, Any]:
        """闭环二/三：v4 画像领域路由 + 关注点校准探针（成对替换）。"""
        self._publish_activity(
            session,
            "diagnosis",
            "working",
            "profile_assessment",
            "正在按岗位学习领域计算培养清单与起点档位",
        )
        try:
            result, focus_point, pretest, evidence = self._persona_build(session, answers)
        except Exception:
            self._publish_activity(
                session,
                "diagnosis",
                "blocked",
                "profile_assessment",
                "诊断未能安全完成",
            )
            raise
        session.pretest_answers = dict(answers)

        # 闭环三：选了训练关注点 → 1 道应用档校准探针（蓝图 3.5）
        if focus_point:
            probe = calibration_probe_for(focus_point)
            if probe is not None:
                session.diagnostic_probe_queue = (probe,)
                session.diagnostic_probe_results = []
                session.pending_diagnostic_probes = (probe,)
                session.persona_calibration = {
                    "probe_id": str(probe["probe_id"]),
                    "knowledge_point": focus_point,
                }
                session.artifact = self._persona_draft(session, result, pretest, evidence)
                session.awaiting = "diagnostic_probe"
                session.outcome = None
                session.interaction = {
                    "kind": "supplemental_diagnosis",
                    "title": "校准探针",
                    "probe_step": 1,
                    "probe_total": 1,
                    "message": (
                        "为确认「" + focus_point + "」的学习起点，"
                        "请回答 1 道应用档小题（答对从应用档起步，答错从基础档起步）。"
                    ),
                    "questions": [
                        {
                            "probe_id": str(probe["probe_id"]),
                            "knowledge_point": str(probe["knowledge_point"]),
                            "difficulty": str(probe["difficulty"]),
                            "stem": str(probe["stem"]),
                        }
                    ],
                    "provisional_route": {
                        "knowledge_point": focus_point,
                        "difficulty": result["selected_difficulty"],
                        "reason": (
                            "训练关注点已加入培养计划；系统会先补齐必要前置知识，"
                            "校准探针用于确认关注点的起点档位。"
                        ),
                        "evidence_source": "calibration_probe",
                        "evidence_ids": [],
                    },
                }
                self._publish_activity(
                    session,
                    "diagnosis",
                    "working",
                    "supplemental_diagnosis",
                    "等待完成校准探针",
                )
                return self.get_state(session.session_id)

        return self._finalize_diagnosis(
            session, self._persona_draft(session, result, pretest, evidence)
        )

    def _submit_persona_calibration(
        self,
        session: _InteractiveSession,
        answers: Mapping[str, str],
    ) -> dict[str, Any]:
        """闭环三：校准探针判分 → 四象限回写 → 终化诊断。"""
        calibration = session.persona_calibration or {}
        probe_id = str(calibration.get("probe_id", ""))
        expected = {
            str(item["probe_id"]): item
            for item in session.diagnostic_probe_queue
        }
        if set(answers) != set(expected):
            raise InteractiveSessionError("请完成当前校准探针作答。")
        probe = expected[probe_id]
        is_correct = grade_probe_answer(probe, str(answers[probe_id]))
        if session.pretest_answers is None:
            raise InteractiveSessionError("缺少前测答案，无法完成校准。")
        result, _focus, pretest, evidence = self._persona_build(
            session, session.pretest_answers
        )
        focus_point = str(calibration.get("knowledge_point", ""))
        apply_calibration(result, focus_point, probe_id, is_correct)
        session.diagnostic_probe_results = [
            *list(session.diagnostic_probe_results),
            ProbeResult(probe_id, is_correct),
        ]
        session.pending_diagnostic_probes = ()
        session.diagnostic_probe_queue = ()
        session.persona_calibration = None
        self._publish_activity(
            session,
            "diagnosis",
            "working",
            "supplemental_diagnosis",
            "校准完成，正在生成培养清单",
        )
        return self._finalize_diagnosis(
            session, self._persona_draft(session, result, pretest, evidence)
        )

    def _reached_initial_step_up(self, session: _InteractiveSession) -> bool:
        """闭环二遗留核对项（蓝图 3.6 降阶语义，2026-08-17 补修）。

        完成线锚定「前测判定的初始档 + 升一档」，降档补救不降低完成线：
        前测答对（applied 起步）中途降档后，须爬回并通过 advanced 才完成。
        当前任务难度 >= step_up(初始档) 即达线；初始即顶档（advanced）通过即达线。
        """
        if session.diagnosis is None:
            return True
        diagnosis_content = _payload_content(session.diagnosis)
        knowledge_point = self._current_knowledge_point(session)
        try:
            initial = _knowledge_point_initial_difficulty(
                diagnosis_content,
                knowledge_point,
            ) if knowledge_point else (
                diagnosis_content.get("difficulty")
                if diagnosis_content.get("difficulty") in {"basic", "applied", "advanced"}
                else None
            )
        except InteractiveSessionError:
            return True
        if initial not in {"basic", "applied", "advanced"}:
            return True
        required = {"basic": "applied", "applied": "advanced"}.get(initial, "advanced")
        current = _payload_content(session.learning_task or {}).get("difficulty")
        if current not in {"basic", "applied", "advanced"}:
            return True
        return (
            {"basic": 0, "applied": 1, "advanced": 2}[str(current)]
            >= {"basic": 0, "applied": 1, "advanced": 2}[required]
        )

    def _lecture_deferral_applies(
        self, session: _InteractiveSession, knowledge_point: str
    ) -> bool:
        """闭环四（蓝图 3.8）：前测答对点（三级优先级之③档）默认微课懒生成。

        门禁失败后的补课轮（remediation_context 命中本点）不延期——
        按需补课时微课照常生成重讲。v3 通道（persona_routing=False）不延期。
        """
        if not self._persona_routing:
            return False
        remediation = session.remediation_context
        if (
            remediation is not None
            and str(remediation.get("knowledge_point")) == knowledge_point
        ):
            return False
        if session.diagnosis is None:
            return False
        plan = _payload_content(session.diagnosis).get("knowledge_point_plan")
        if not isinstance(plan, list):
            return False
        for item in plan:
            if (
                isinstance(item, Mapping)
                and str(item.get("knowledge_point")) == knowledge_point
            ):
                return item.get("tier") == "correct"
        return False

    def _deferred_lecture_draft(
        self,
        session: _InteractiveSession,
        knowledge_point: str,
        difficulty: str,
    ) -> dict[str, Any]:
        runtime = session.runtime
        content: dict[str, Any] = {
            "event": "product_ready",
            "knowledge_point": knowledge_point,
            "difficulty": difficulty,
            "lecture_md": "",
            "claims": [],
            "evidence": [],
            "lecture_deferred": True,
            "deferred_reason": "pretest_verified",
            "deferred_notice": "已由前测验证，直入实操（未通过将自动配发微课）",
        }
        return {
            "trace_id": runtime.options.trace_id,
            "agent": "knowledge",
            "role": "produce",
            "payload": {"type": "lecture_note", "content": content},
            "evidence": [],
            "claims": [],
            "student_profile_ref": str(runtime.profile["profile_id"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _produce_deferred_lecture(
        self,
        session: _InteractiveSession,
        knowledge_point: str,
        evidence_bundle: Any,
    ) -> dict[str, Any]:
        """闭环四：S2 照常经过（T03→T04），内容为延期标记，零 LLM。

        评审采用确定性 approve verdict（复用 review_agent 的 _verdict_draft
        同构信封），不调用评审 LLM；21 状态转移不删不绕。
        """
        runtime = session.runtime
        draft = evidence_bundle.bind(
            self._deferred_lecture_draft(
                session, knowledge_point, str(evidence_bundle.difficulty)
            )
        )
        product, produced = runtime.send_transition(draft)  # T03（engine 守卫自动选择）
        verdict = _verdict_draft(
            trace_id=runtime.options.trace_id,
            role="verdict",
            product=product,
            hits=[],
            decision="approve",
            difficulty_action="keep",
            clock=lambda: datetime.now(timezone.utc),
        )
        runtime.send_transition(verdict)  # T04（guard_approved_lecture）
        self._publish_activity(
            session,
            "knowledge",
            "done",
            "personalized_lecture",
            "前测已验证，本知识点微课延期（直入实操）",
        )
        self._publish_activity(
            session,
            "review",
            "approved",
            "quality_gate",
            "延期讲义已通过确定性质量门",
        )
        return product

    def _deferred_companion_lecture(
        self,
        session: _InteractiveSession,
        evidence_bundle: Any,
    ) -> dict[str, Any]:
        """闭环四：升档伴随讲义同样懒生成（绑定不转移、零 LLM、零评审 LLM）。

        延期标记不是学员可见内容，不进入评审 LLM；若后续门禁失败，
        补课轮会走完整生成与评审路径。
        """
        return evidence_bundle.bind(
            self._deferred_lecture_draft(
                session,
                str(evidence_bundle.knowledge_point),
                str(evidence_bundle.difficulty),
            )
        )

    def get_diagnostic_probes(self, session_id: str) -> list[dict[str, Any]]:
        session = self._get_session(session_id)
        if session.awaiting != "diagnostic_probe":
            raise InteractiveSessionError("当前步骤不是补充诊断。")
        return [
            {
                "probe_id": str(item["probe_id"]),
                "knowledge_point": str(item["knowledge_point"]),
                "difficulty": str(item["difficulty"]),
                "stem": str(item["stem"]),
            }
            for item in session.pending_diagnostic_probes
        ]

    def submit_diagnostic_probes(
        self,
        session_id: str,
        answers: Mapping[str, str],
    ) -> dict[str, Any]:
        session = self._get_session(session_id)
        if session.awaiting != "diagnostic_probe":
            raise InteractiveSessionError("当前步骤不能提交补充诊断。")
        if self._persona_routing and session.persona_calibration is not None:
            return self._submit_persona_calibration(session, answers)
        expected = {
            str(item["probe_id"]): item for item in session.pending_diagnostic_probes
        }
        if set(answers) != set(expected):
            raise InteractiveSessionError("请完成当前展示的全部固定诊断探针。")
        if session.pretest_answers is None:
            raise InteractiveSessionError("缺少基础前测答案，无法完成补充诊断。")
        results = [
            ProbeResult(probe_id, grade_probe_answer(expected[probe_id], answers[probe_id]))
            for probe_id in expected
        ]
        session.diagnostic_probe_results.extend(results)
        completed_ids = {
            item.probe_id for item in session.diagnostic_probe_results
        }
        remaining = tuple(
            item
            for item in session.diagnostic_probe_queue
            if str(item["probe_id"]) not in completed_ids
        )
        # A failed basic probe already establishes a basic blind spot.  The
        # applied probe is shown only after the basic probe passes, matching
        # the frozen formal input and avoiding an unnecessary second item.
        if all(item.is_correct for item in results) and remaining:
            session.pending_diagnostic_probes = remaining[:1]
            session.interaction = {
                "kind": "supplemental_diagnosis",
                "title": "补充诊断",
                "probe_step": 2,
                "probe_total": len(session.diagnostic_probe_queue),
                "message": "第1道已通过，请完成第2道以确认学习起点。",
                "questions": [
                    {
                        "probe_id": str(item["probe_id"]),
                        "knowledge_point": str(item["knowledge_point"]),
                        "difficulty": str(item["difficulty"]),
                        "stem": str(item["stem"]),
                    }
                    for item in remaining[:1]
                ],
                "provisional_route": {
                    "knowledge_point": str(remaining[0]["knowledge_point"]),
                    "difficulty": str(remaining[0]["difficulty"]),
                    "reason": "第1道已通过，继续回答同一知识点的第2道以确认起点难度。",
                    "evidence_source": "diagnostic_probe",
                    "evidence_ids": sorted(completed_ids),
                },
            }
            return self.get_state(session_id)
        diagnosis = session.runtime.diagnosis.assess(
            session.runtime.options.profile_id,
            session.pretest_answers,
            probe_results=session.diagnostic_probe_results,
            experience_tags=session.experience_tags,
        )
        session.pending_diagnostic_probes = ()
        session.diagnostic_probe_queue = ()
        session.diagnostic_probe_results = []
        return self._finalize_diagnosis(session, diagnosis)

    def _finalize_diagnosis(
        self,
        session: _InteractiveSession,
        diagnosis_draft: dict[str, Any],
    ) -> dict[str, Any]:
        diagnosis = session.runtime.transition(diagnosis_draft, "T02")
        self._publish_activity(
            session,
            "diagnosis",
            "done",
            "profile_assessment",
            "知识路由与起始难度已确定",
        )
        learning_contract = LearningContract.from_diagnosis(
            profile=session.runtime.profile,
            diagnosis=diagnosis,
            domain_id=session.runtime.task.domain_id,
            domain_package_sha256=session.runtime.task.domain_package_sha256,
            use_selected_route=True,
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
        session.interaction = {
            "kind": "diagnostic_route",
            "knowledge_point": _payload_content(diagnosis).get(
                "selected_knowledge_point"
            ),
            "difficulty": _payload_content(diagnosis).get("selected_difficulty"),
            "reason": (
                _payload_content(diagnosis).get("knowledge_point_plan") or [{}]
            )[0].get("route_reason"),
            "evidence_ids": (
                _payload_content(diagnosis).get("knowledge_point_plan") or [{}]
            )[0].get("evidence_ids", []),
        }
        return self.get_state(session.session_id)

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
        review_attempts = 0

        def observe(event: str, details: Mapping[str, Any]) -> None:
            nonlocal review_attempts
            cycle = int(details.get("cycle", 1))
            if event == "review_started":
                review_attempts = max(review_attempts, cycle)
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
            logging.error(
                "ReviewFlowTerminal: trace=%s kp=%s diff=%s action=%s attempts=%d",
                session.runtime.options.trace_id,
                getattr(session, "current_knowledge_point", "?"),
                getattr(session, "current_difficulty", "?"),
                terminal.action,
                review_attempts,
            )
            self._finish_review_stop(
                session,
                terminal.control_message,
                terminal.action,
                review_attempts=review_attempts,
            )
            return None
        except ReviewFlowInterrupted as interrupted:
            logging.error(
                "ReviewFlowInterrupted: trace=%s kp=%s diff=%s",
                session.runtime.options.trace_id,
                getattr(session, "current_knowledge_point", "?"),
                getattr(session, "current_difficulty", "?"),
            )
            self._finish_review_stop(
                session,
                interrupted.last_message,
                "system_error",
                review_attempts=review_attempts,
            )
            return None
        except DemoSessionError:
            logging.error(
                "DemoSessionError: trace=%s kp=%s diff=%s",
                session.runtime.options.trace_id,
                getattr(session, "current_knowledge_point", "?"),
                getattr(session, "current_difficulty", "?"),
            )
            self._finish_system_error(session, review_attempts=review_attempts)
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
                review_attempts=review_attempts,
            )
            return None
        return product

    @staticmethod
    def _revise_auxiliary_approve_with_fix(
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create a new, traceable companion artifact for a fixable verdict.

        The actual prerequisite text is deterministically bound before review.
        This revision step preserves that content, records the requested fix and
        gives Review a fresh message/artifact identity.  Exact approval is still
        required; repeated ``approve_with_fix`` exhausts the normal review budget
        and fails closed instead of being published as success.
        """

        revised = deepcopy(dict(product))
        revised.pop("step", None)
        revised.pop("msg_id", None)
        revised.pop("rejected_by_bus", None)
        revised.pop("bus_errors", None)
        payload = revised.get("payload")
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, dict):
            raise ReviewFlowError("auxiliary revision requires payload.content")
        original_msg_id = product.get("msg_id")
        verdict_msg_id = verdict.get("msg_id")
        if not isinstance(original_msg_id, str) or not isinstance(
            verdict_msg_id, str
        ):
            raise ReviewFlowError("auxiliary revision requires canonical message ids")
        original_artifact_id = str(content.get("artifact_id") or original_msg_id)
        rule_hits = verdict.get("verdict", {}).get("rule_hits", ())
        rule_ids = sorted(
            {
                str(hit.get("rule_id"))
                for hit in rule_hits
                if isinstance(hit, Mapping) and hit.get("rule_id")
            }
        )
        content.update(
            {
                "generation_stage": "review_fix_revision",
                "revises_msg_id": original_msg_id,
                "revises_artifact_id": original_artifact_id,
                "revision_reason_msg_id": verdict_msg_id,
                "review_fix_rule_ids": rule_ids,
            }
        )
        revision_digest = sha256(
            f"{original_artifact_id}|{verdict_msg_id}|{'|'.join(rule_ids)}".encode(
                "utf-8"
            )
        ).hexdigest()
        content["artifact_id"] = f"art-{revision_digest[:24]}"
        return revised

    @staticmethod
    def _review_auxiliary_resource(
        session: _InteractiveSession,
        producer: Callable[[], dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Review a resource without adding a state-machine transition.

        Parallel companion resources are evidence for the same learning
        contract, but only the active task is allowed to drive T09/T10.  This
        audit path preserves Review, selective rebuttal and fail-closed
        publication while leaving the published 21 transitions untouched.
        """

        runtime = session.runtime
        try:
            return audit_and_review(
                producer,
                audit=runtime.passive_audit,
                review=lambda product: runtime.review.review(
                    product,
                    learning_report=session.diagnosis,
                    student_profile=runtime.profile,
                    learned_knowledge_points=runtime.learned_knowledge_points,
                    learning_contract=session.learning_contract,
                ),
                generate_rebuttal=runtime.rebuttal.generate,
                re_review=lambda product, verdict, rebuttal: runtime.review.re_review(
                    product,
                    verdict,
                    rebuttal,
                    learning_report=session.diagnosis,
                    student_profile=runtime.profile,
                    learned_knowledge_points=runtime.learned_knowledge_points,
                    learning_contract=session.learning_contract,
                ),
                revise_approve_with_fix=(
                    InteractiveSessionManager._revise_auxiliary_approve_with_fix
                ),
                max_cycles=(
                    session.learning_contract.quality_policy.max_review_cycles
                    if session.learning_contract is not None
                    else 4
                ),
                terminal_action="refuse",
                quality_policy=(
                    session.learning_contract.quality_policy
                    if session.learning_contract is not None
                    else None
                ),
            )
        except (ReviewFlowError, ReviewFlowInterrupted, ReviewFlowTerminal):
            return None

    @staticmethod
    def _companion_lecture_draft(
        session: _InteractiveSession,
        *,
        evidence_bundle: EvidenceBundle,
        evidence_chunks: Sequence[KnowledgeChunk],
    ) -> dict[str, Any]:
        """Generate a same-contract lecture for a non-transitioning branch."""

        runtime = session.runtime
        diagnosis_content = _payload_content(session.diagnosis or {})
        knowledge_point = evidence_bundle.knowledge_point
        blind_spots = [knowledge_point]
        try:
            return _generate_reviewable_lecture(
                runtime,
                knowledge_point=knowledge_point,
                diagnosis_content=diagnosis_content,
                blind_spots=blind_spots,
                retrieved_chunks=evidence_chunks,
                difficulty_fallback=bool(
                    evidence_bundle.source("knowledge")["difficulty_fallback"]
                ),
                evidence_bundle=evidence_bundle,
            )
        except LLMCallError as generation_error:
            fallback = runtime.knowledge.generate_evidence_projection(
                knowledge_point=knowledge_point,
                student_profile=runtime.profile,
                difficulty=evidence_bundle.difficulty,
                retrieved_chunks=evidence_chunks,
                fallback_reason="companion_generation_unavailable",
            )
            fallback = evidence_bundle.bind(fallback)
            if (
                evaluate_hard_rules(fallback)
                or runtime.review.preflight_r04(fallback) is not None
            ):
                raise generation_error
            return fallback

    @staticmethod
    def _settle_active_agent_events(
        session: _InteractiveSession,
        *,
        status: str,
        activity: str,
        label: str,
        exclude: frozenset[str] = frozenset(),
    ) -> None:
        if session.events is None:
            return
        active_statuses = {
            "working",
            "collaborating",
            "reviewing",
            "debating",
        }
        latest_events: dict[str, Mapping[str, Any]] = {}
        for event in session.events.after():
            latest_events[str(event["agent"])] = event
        for agent, event in sorted(latest_events.items()):
            if agent in exclude or event.get("status") not in active_statuses:
                continue
            session.events.publish(
                agent=agent,
                status=status,
                activity=activity,
                label=label,
                stage=session.runtime.engine.state.value,
            )

    @staticmethod
    def _finish_review_stop(
        session: _InteractiveSession,
        artifact: dict[str, Any],
        action: str,
        *,
        review_attempts: int = 0,
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
        session.pending_learning_action = None
        session.outcome = (
            "system_error" if action == "system_error" else "safe_rejected"
        )
        rule_ids: set[str] = set()
        verdict = artifact.get("verdict") if isinstance(artifact, Mapping) else None
        hits = verdict.get("rule_hits") if isinstance(verdict, Mapping) else None
        if isinstance(hits, list):
            for hit in hits:
                if isinstance(hit, Mapping) and isinstance(hit.get("rule_id"), str):
                    rule_ids.add(str(hit["rule_id"]))
        reason_code = (
            "model_unavailable"
            if action == "system_error"
            else "evidence_insufficient"
            if rule_ids.intersection({"R-02", "R-05"})
            else "review_exhausted"
        )
        review_limit = (
            session.learning_contract.quality_policy.max_review_cycles
            if session.learning_contract is not None
            else max(review_attempts, 1)
        )
        session.termination = {
            "reason_code": reason_code,
            "review_attempts": max(0, int(review_attempts)),
            "review_limit": int(review_limit),
        }
        if session.events is not None:
            InteractiveSessionManager._settle_active_agent_events(
                session,
                status="idle",
                activity="session_stopped",
                label="本轮已安全停止，等待重新开始",
                exclude=frozenset({"review"}),
            )
            session.events.publish(
                agent="review",
                status="blocked",
                activity="quality_gate",
                label="质量门已安全阻断该产物",
                stage=session.runtime.engine.state.value,
            )

    @staticmethod
    def _finish_system_error(
        session: _InteractiveSession,
        *,
        review_attempts: int = 0,
    ) -> None:
        InteractiveSessionManager._finish_review_stop(
            session,
            session.artifact or {},
            "system_error",
            review_attempts=review_attempts,
        )

    def _retain_follow_up_after_quality_interruption(
        self,
        session: _InteractiveSession,
        *,
        client_turn_id: str,
    ) -> None:
        """Keep the last approved prompt when a new probe cannot pass Review.

        The rejected or incomplete probe is never shown to the learner.  The
        session remains on the previously approved question so a transient
        generation/review failure cannot terminate an otherwise valid lesson.
        """

        current_task = session.learning_task or session.active_task
        allowed_relation_points = set(
            default_relation_support_points(
                tuple(session.runtime.task.misconception_ids)
            )
        )
        session.covered_relation_points.intersection_update(
            allowed_relation_points
        )
        session.outcome = None
        session.awaiting = "follow_up"
        session.pending_learning_action = None
        session.interaction = {
            "kind": "free_text_follow_up",
            **_follow_up_task_context(current_task),
            "prompt": session.follow_up_question,
            "round": session.follow_up_round,
            "max_rounds": MAX_FOLLOW_UP_ROUNDS,
            "turns": [dict(turn) for turn in session.follow_up_turns],
            "feedback": (
                "本轮新追问暂时未能通过质量检查，系统已保留上一道已审核题目；"
                "你可以结合查询结果重新作答，本次学习不会结束。"
            ),
            "next_step_reason": (
                "新的追问未通过质量门，因此不展示该内容，也不改变当前学习进度。"
            ),
            "retry_required": True,
            "correction_required": bool(session.unresolved_misconceptions),
            "unresolved_misconceptions": sorted(
                session.unresolved_misconceptions
            ),
        }
        session.processed_turn_ids.add(client_turn_id)
        self._settle_active_agent_events(
            session,
            status="waiting",
            activity="follow_up_retry",
            label="上一道已审核题目仍然有效，等待学员重新作答",
        )
        self._publish_activity(
            session,
            "review",
            "blocked",
            "follow_up_quality_gate",
            "新追问未通过质量门，已保留上一道已审核题目",
            peers=("task",),
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
        difficulty = session.learning_contract.difficulty
        keywords = tuple(str(item) for item in blind_spots[:3])
        active_remediation = (
            dict(session.remediation_context)
            if session.remediation_context is not None
            and session.remediation_context.get("knowledge_point") == knowledge_point
            and session.remediation_context.get("action") != "deferred"
            else None
        )
        excluded_chunk_ids = (
            set(session.remediation_excluded_chunk_ids)
            if active_remediation is not None
            else set()
        )

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
            original_chunks = tuple(chunks)
            if excluded_chunk_ids:
                chunks = _select_remediation_chunks(
                    original_chunks,
                    excluded_chunk_ids=excluded_chunk_ids,
                    knowledge_point=knowledge_point,
                )
            return {
                "chunks": chunks,
                "summary": {
                    "chunk_ids": [chunk.chunk_id for chunk in chunks],
                    "chunk_count": len(chunks),
                    "responsibility_scope": list(
                        responsibility_scope(
                            knowledge_point,
                            difficulty,
                            chunks=chunks,
                        )
                    ),
                    "prerequisite_bindings": [
                        dict(item)
                        for item in prerequisite_scaffolds(
                            knowledge_point,
                            difficulty,
                            chunks=chunks,
                        )
                    ],
                    "difficulty_fallback": difficulty_fallback,
                    "remediation_refresh": active_remediation is not None,
                    "excluded_previous_chunks": sorted(
                        excluded_chunk_ids
                    ),
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
                "remediation": active_remediation,
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
        if session.awaiting == "done" and session.outcome in {"system_error", "completed"}:
            return self.get_state(session_id)
        if session.awaiting != "advance":
            raise InteractiveSessionError("当前步骤需要先完成学员操作。")
        runtime = session.runtime
        if runtime.engine.state is State.S2_KNOWLEDGE:
            if session.diagnosis is None:
                raise InteractiveSessionError("岗前测评结果缺失。")
            diagnosis_content = _payload_content(session.diagnosis)
            blind_spots = self._effective_blind_spots(session)
            if not blind_spots:
                diagnosis_content = _payload_content(session.diagnosis)
                score = diagnosis_content.get("pretest_score")
                session.training_report = {
                    "title": "本轮训练报告",
                    "knowledge_point": (
                        session.deferred_knowledge_points[-1]
                        if session.deferred_knowledge_points
                        else "本轮知识点"
                    ),
                    "initial_difficulty": diagnosis_content.get("difficulty"),
                    "final_difficulty": (
                        session.learning_contract.difficulty
                        if session.learning_contract is not None
                        else diagnosis_content.get("difficulty")
                    ),
                    "pretest_score": (
                        dict(score) if isinstance(score, Mapping) else None
                    ),
                    "query_count": session.query_count,
                    "follow_up_rounds": session.follow_up_submission_count,
                    "completed_correction": False,
                    "achievement": "已完成一次降阶补学；未掌握内容已安全转入后续补学清单。",
                    "next_knowledge_point": None,
                    "deferred_knowledge_points": list(
                        session.deferred_knowledge_points
                    ),
                }
                # 0818 需求 3/5/6：延期完成同样出增强报告并落学习记录
                self._enrich_report_and_record_learning(
                    session,
                    outcome="completed_with_deferred",
                )
                session.awaiting = "done"
                session.outcome = "completed_with_deferred"
                session.interaction = {
                    "kind": "learning_notice",
                    "message": "本知识点已转入后续补学清单，本轮学习安全结束。",
                    "next_step_reason": "同一知识点已经完成一次补学，继续重复不会增加有效证据。",
                }
                return self.get_state(session_id)
            if not isinstance(blind_spots, list):
                raise InteractiveSessionError("岗前测评没有产生知识盲区。")
            evidence_bundle, evidence_chunks = self._prepare_evidence_bundle(
                session,
                diagnosis_content,
                blind_spots,
            )
            business_evidence = evidence_bundle.source("business_data")
            def prefetch_task() -> dict[str, Any]:
                active_task = evidence_bundle.bind(
                    runtime.task.generate(
                        str(business_evidence["template_id"]),
                        diagnostic_difficulty=evidence_bundle.difficulty,
                        student_profile=runtime.profile,
                    )
                )
                practice_guide = evidence_bundle.bind(
                    runtime.task.generate_practice_guide(
                        str(business_evidence["template_id"]),
                        diagnostic_difficulty=evidence_bundle.difficulty,
                        student_profile=runtime.profile,
                    )
                )
                return {
                    "active_task": active_task,
                    "practice_guide": practice_guide,
                }

            def prefetch_assessment() -> dict[str, Any]:
                return evidence_bundle.bind(
                    runtime.task.generate_assessment(
                        str(business_evidence["template_id"]),
                        diagnostic_difficulty=evidence_bundle.difficulty,
                        student_profile=runtime.profile,
                    )
                )

            def generate_lecture_with_remediation_fallback() -> dict[str, Any]:
                try:
                    return _generate_reviewable_lecture(
                        runtime,
                        knowledge_point=str(blind_spots[0]),
                        diagnosis_content=diagnosis_content,
                        blind_spots=blind_spots,
                        retrieved_chunks=evidence_chunks,
                        difficulty_fallback=bool(
                            evidence_bundle.source("knowledge")[
                                "difficulty_fallback"
                            ]
                        ),
                        evidence_bundle=evidence_bundle,
                    )
                except LLMCallError as generation_error:
                    fallback_reason = (
                        "remediation_generation_unavailable"
                        if session.remediation_context is not None
                        else "primary_generation_unavailable"
                    )
                    fallback = runtime.knowledge.generate_evidence_projection(
                        knowledge_point=str(blind_spots[0]),
                        student_profile=runtime.profile,
                        difficulty=evidence_bundle.difficulty,
                        retrieved_chunks=evidence_chunks,
                        fallback_reason=fallback_reason,
                    )
                    fallback = evidence_bundle.bind(fallback)
                    if (
                        evaluate_hard_rules(fallback)
                        or runtime.review.preflight_r04(fallback) is not None
                    ):
                        raise generation_error
                    return fallback

            def produce_lecture() -> dict[str, Any] | None:
                # 闭环四：前测答对点微课懒生成（S2 照常 T03/T04，零 LLM）
                if self._lecture_deferral_applies(session, str(blind_spots[0])):
                    return self._produce_deferred_lecture(
                        session, str(blind_spots[0]), evidence_bundle
                    )
                return self._produce_reviewed_product(
                    session,
                    generate_lecture_with_remediation_fallback,
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
                session.prefetched_task = task_branch.value["active_task"]
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
            approved_practice = (
                self._review_auxiliary_resource(
                    session,
                    lambda: task_branch.value["practice_guide"],
                )
                if task_branch.status == "succeeded"
                else None
            )
            session.resource_bundle = ResourceBundle.build(
                contract_id=evidence_bundle.contract_id,
                evidence_bundle_id=evidence_bundle.bundle_id,
                products={
                    "knowledge": lecture,
                    "practice": (
                        approved_practice
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
            # 闭环四：延期讲义携带"直入实操"提示（蓝图 3.8 UI 口径）
            session.interaction = (
                {
                    "kind": "lecture_deferred",
                    "message": _payload_content(lecture).get("deferred_notice"),
                }
                if _payload_content(lecture).get("lecture_deferred")
                else None
            )
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
        if (
            runtime.engine.state is State.S7_STUDENT
            and session.awaiting == "advance"
            and self._persona_routing
            and str(runtime.profile.get("practice_mode")) == "data_present"
        ):
            # data_present 延时代执行：S3 下发时设 advance（学员读讲义），
            # 点击进入练习后此处代理标准查询→追问
            template_id = str(
                _payload_content(session.active_task or {}).get("template_id") or ""
            )
            standard_sql = ""
            if template_id.strip():
                try:
                    entry = runtime.task._catalog.templates.get(template_id)
                    if entry is not None:
                        standard_sql = str(entry.standard_sql)
                except Exception:
                    standard_sql = ""
            if isinstance(standard_sql, str) and standard_sql.strip():
                self._publish_activity(
                    session,
                    "verification",
                    "working",
                    "sql_validation",
                    "已按岗位模式代为执行标准查询（白名单与只读校验照常进行）",
                )
                return self.submit_sql(session_id, standard_sql, _proxy=True)
            raise InteractiveSessionError("当前岗位模式的标准查询暂不可用。")

        if runtime.engine.state is State.S3_TASK and session.active_task is None:
            if session.diagnosis is None:
                raise InteractiveSessionError("岗前测评结果缺失。")
            diagnosis_content = _payload_content(session.diagnosis)
            blind_spots = self._effective_blind_spots(session)
            if not blind_spots:
                raise InteractiveSessionError("岗前测评没有产生知识盲区。")
            def produce_task() -> dict[str, Any]:
                try:
                    if session.evidence_bundle is not None:
                        template_id = session.evidence_bundle.source(
                            "business_data"
                        )["template_id"]
                        return session.evidence_bundle.bind(
                            runtime.task.generate(
                                str(template_id),
                                diagnostic_difficulty=(
                                    session.evidence_bundle.difficulty
                                ),
                                student_profile=runtime.profile,
                            )
                        )
                    return runtime.task.generate_for_diagnosis(
                        blind_spots[0],
                        diagnosis_content.get("difficulty"),
                        student_profile=runtime.profile,
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
            # data_present：设 advance 而非 sql——学员先读讲义，点击进入练习后
            # 下一次 advance 由 S7 分支代执行标准查询
            if (
                self._persona_routing
                and str(runtime.profile.get("practice_mode")) == "data_present"
            ):
                session.awaiting = "advance"
            else:
                session.awaiting = "sql"
            # 闭环五（蓝图 3.7）：data_present 画像任务下发后系统立即代执行
            # 标准查询——白名单/只读/超时门禁照走，学员直接看结果表进追问。
            # data_present 代执行已移至 S7+advance 分支（延迟到学员点击进入练习）
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
                result = self._create_conclusion_task(session)
                if session.task_phase == "conclusion":
                    session.task_phase = "progression_conclusion"
                return result
            if session.task_phase == "progression_conclusion":
                # 蓝图 3.6（闭环二遗留核对项补修）：v4 通道完成线锚定前测
                # 初始档+1；降档爬升未达线时继续升档。v3 冻结通道行为不变。
                if self._persona_routing and not self._reached_initial_step_up(session):
                    session.interaction = {
                        "kind": "learning_notice",
                        "message": (
                            "降档补学后已重新通过当前档位；"
                            "按你的前测起点，还需通过上一档才算完成本知识点。"
                        ),
                        "next_step_reason": (
                            "完成线按前测判定的起始档锚定，"
                            "降档补救不降低完成要求。"
                        ),
                    }
                    return self._advance_after_correct_answer(session)
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
        logging.info(
            "create_conclusion_task: trace=%s kp=%s diff=%s task_phase=%s",
            session.runtime.options.trace_id,
            getattr(session, "current_knowledge_point", "?"),
            getattr(session, "current_difficulty", "?"),
            session.task_phase,
        )
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
        raw_task_draft = self._learning_task_draft(session, "step_up")
        if raw_task_draft is None:
            return self._complete_training(session)
        current_content = _payload_content(session.learning_task or {})
        target_content = _payload_content(raw_task_draft)
        runtime = session.runtime
        if session.learning_contract is None:
            raise InteractiveSessionError("learning contract is unavailable")
        target_knowledge_point = target_content.get("knowledge_point")
        target_difficulty = target_content.get("difficulty")
        if (
            not isinstance(target_knowledge_point, str)
            or not target_knowledge_point.strip()
            or target_difficulty not in {"basic", "applied", "advanced"}
        ):
            raise InteractiveSessionError("progression target is incomplete")

        # A learning contract is immutable.  Progression therefore creates a
        # new revision and a new evidence bundle instead of silently reusing
        # the initial-difficulty context for the higher-difficulty artefacts.
        session.learning_contract = replace(
            session.learning_contract,
            difficulty=str(target_difficulty),
            revision=session.learning_contract.revision + 1,
        )
        runtime.passive_audit(
            session.learning_contract.control_draft(runtime.options.trace_id)
        )
        session.evidence_bundle = None
        session.evidence_chunks = ()
        evidence_bundle, evidence_chunks = self._prepare_evidence_bundle(
            session,
            _payload_content(session.diagnosis),
            [target_knowledge_point],
        )
        task_draft = evidence_bundle.bind(raw_task_draft)

        # 闭环四：前测答对点的升档伴随讲义同样懒生成（零 LLM）
        companion_lecture = (
            self._deferred_companion_lecture(session, evidence_bundle)
            if self._lecture_deferral_applies(
                session, str(evidence_bundle.knowledge_point)
            )
            else self._review_auxiliary_resource(
                session,
                lambda: self._companion_lecture_draft(
                    session,
                    evidence_bundle=evidence_bundle,
                    evidence_chunks=evidence_chunks,
                ),
            )
        )
        companion_practice = self._review_auxiliary_resource(
            session,
            lambda: evidence_bundle.bind(
                runtime.task.generate_practice_guide(
                    str(target_content["template_id"]),
                    diagnostic_difficulty=str(target_difficulty),
                    student_profile=runtime.profile,
                )
            ),
        )
        companion_assessment = self._review_auxiliary_resource(
            session,
            lambda: evidence_bundle.bind(
                runtime.task.generate_assessment(
                    str(target_content["template_id"]),
                    diagnostic_difficulty=str(target_difficulty),
                    student_profile=runtime.profile,
                )
            ),
        )
        companion_fallback_used = False
        if (
            companion_lecture is None
            or companion_practice is None
            or companion_assessment is None
        ):
            # Companion resources for the higher difficulty failed review.
            # Fall back to current session's approved companions so the
            # step_up proceeds (initial + 1 upgrade = complete).
            companion_lecture = session.lecture or companion_lecture
            companion_practice = session.active_task or companion_practice
            companion_assessment = session.active_task or companion_assessment
            companion_fallback_used = True
        try:
            resource_bundle = ResourceBundle.build(
                contract_id=evidence_bundle.contract_id,
                evidence_bundle_id=evidence_bundle.bundle_id,
                products={
                    "knowledge": companion_lecture,
                    "practice": companion_practice,
                    "assessment": companion_assessment,
                },
            )
        except ValueError:
            # Fallback companions keep the old evidence_bundle_ref, which no
            # longer matches the freshly bound bundle.  Only that case may
            # reuse the session's existing bundle; genuine validation
            # failures on fresh companions still finish safely as before.
            if not companion_fallback_used:
                self._finish_system_error(session)
                return self.get_state(session.session_id)
            resource_bundle = session.resource_bundle
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
            if (
                session.awaiting == "done"
                and session.outcome in {"safe_rejected", "system_error"}
            ):
                session.outcome = "completed"
                session.pending_learning_action = None
                if isinstance(session.interaction, dict):
                    session.interaction = {
                        **session.interaction,
                        "message": (
                            "已完成当前难度训练。进阶内容暂时无法生成，"
                            "本次学习在当前难度完成。"
                        ),
                    }
                return self.get_state(session.session_id)
            return self.get_state(session.session_id)
        session.active_task = task
        session.learning_task = task
        session.lecture = companion_lecture
        session.resource_bundle = resource_bundle
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
        # The progression task was already introduced by a dedicated T19
        # path update whose action is ``step_up``.  Completing that task does
        # not perform a second difficulty change; it only records attainment
        # at the current difficulty.
        difficulty_action = "keep"
        runtime = session.runtime
        remaining_blind_spots = self._remaining_blind_spots(session)
        diagnosis_content = _payload_content(session.diagnosis)
        score = diagnosis_content.get("pretest_score")
        knowledge_point = current_content.get("knowledge_point")
        resolved_knowledge_point = (
            str(knowledge_point)
            if isinstance(knowledge_point, str)
            else "本轮知识点"
        )
        initial_difficulty = (
            _knowledge_point_initial_difficulty(
                diagnosis_content,
                resolved_knowledge_point,
            )
            if isinstance(knowledge_point, str)
            else diagnosis_content.get("difficulty")
        )
        deferred_points = list(session.deferred_knowledge_points)
        achievement = (
            "完成了数据实操、证据核对和结论修正。"
            if session.completed_correction
            else "完成了数据实操和多轮理解核对。"
        )
        if deferred_points:
            achievement = (
                achievement.rstrip("。")
                + "；未掌握内容已转入后续补学清单。"
            )
        session.training_report = {
            "title": "本轮训练报告",
            "knowledge_point": resolved_knowledge_point,
            "initial_difficulty": initial_difficulty,
            "final_difficulty": current_content.get("difficulty"),
            "pretest_score": dict(score) if isinstance(score, Mapping) else None,
            "query_count": session.query_count,
            "follow_up_rounds": session.follow_up_submission_count,
            "completed_correction": session.completed_correction,
            "achievement": achievement,
            "next_knowledge_point": (
                remaining_blind_spots[0] if remaining_blind_spots else None
            ),
            "deferred_knowledge_points": deferred_points,
        }
        # 0818 需求 3：报告增强——每知识点掌握档位 + 常犯错误 + 学习时长；
        # 需求 5/6：同步落盘学习记录（登录账号归属，游客账号字段为空）。
        self._enrich_report_and_record_learning(session)
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
        self._settle_active_agent_events(
            session,
            status="done",
            activity="session_completed",
            label="本轮职责已完成",
        )
        return self.get_state(session.session_id)

    def _enrich_report_and_record_learning(
        self,
        session: _InteractiveSession,
        *,
        outcome: str = "completed",
    ) -> None:
        """报告增强（每知识点档位/常犯错误/时长）+ 学习记录落盘（0818 需求 3/5/6）。

        training_report 需在调用前已构建；落盘失败不影响学习主流程（仅告警）。
        """

        finished_at = datetime.now(timezone.utc).isoformat()
        report = (
            session.training_report
            if isinstance(session.training_report, dict)
            else {}
        )
        diagnosis_content = _payload_content(session.diagnosis or {})
        plan = diagnosis_content.get("knowledge_point_plan")
        completed_point = str(report.get("knowledge_point") or "").strip()
        final_difficulty = str(report.get("final_difficulty") or "").strip()
        completed_tiers = {"basic": 1, "applied": 2, "advanced": 3}
        mastery_plan: list[dict[str, Any]] = []
        for item in (plan if isinstance(plan, list) else []):
            if not isinstance(item, Mapping) or not item.get("knowledge_point"):
                continue
            point = str(item.get("knowledge_point", ""))
            status = item.get("mastery_status")
            row = {
                "knowledge_point": point,
                "tier": tier_from_status(status),
                "mastery_status": status,
            }
            # The diagnosis plan is intentionally immutable routing evidence,
            # so its original ``needs_training`` status is not rewritten when
            # a learner finishes a knowledge point.  The report, however,
            # describes post-training attainment and must overlay the current
            # point with the actually completed difficulty.  Deferred/error
            # exits remain fail-closed and are never promoted here.
            if (
                outcome == "completed"
                and point == completed_point
                and final_difficulty in completed_tiers
            ):
                row["tier"] = completed_tiers[final_difficulty]
                row["mastery_status"] = f"{final_difficulty}_mastered"
            mastery_plan.append(row)
        misconception_counts: dict[str, int] = {}
        wrong_rounds = 0
        examples: list[dict[str, Any]] = []
        for turn in session.follow_up_turns:
            assessment = str(turn.get("assessment", ""))
            if assessment and assessment != "mastered":
                wrong_rounds += 1
            misconception = str(turn.get("diagnosed_misconception", "") or "")
            if misconception and misconception != "UNKNOWN":
                misconception_counts[misconception] = (
                    misconception_counts.get(misconception, 0) + 1
                )
                examples.append({
                    "round": turn.get("round"),
                    "misconception": misconception,
                    "question": str(turn.get("question", "")),
                    "answer": str(turn.get("answer", "")),
                    "feedback": str(turn.get("feedback", "")),
                })
        report.update({
            "started_at": session.learning_started_at,
            "finished_at": finished_at,
            "mastery_plan": mastery_plan,
            "common_mistakes": {
                "misconception_counts": misconception_counts,
                "wrong_answer_rounds": wrong_rounds,
                "sql_failure_count": session.sql_failure_count,
                "examples": examples[-3:],
            },
        })
        if self._learning_records is None:
            return
        record = build_learning_record(
            account=session.account,
            profile_id=str(session.runtime.options.profile_id),
            knowledge_point=str(report.get("knowledge_point") or "本轮知识点"),
            started_at=session.learning_started_at,
            finished_at=finished_at,
            mastery_plan=mastery_plan,
            follow_up_turns=list(session.follow_up_turns),
            unresolved_misconceptions=sorted(session.unresolved_misconceptions),
            sql_failure_count=session.sql_failure_count,
            query_count=session.query_count,
            follow_up_rounds=session.follow_up_submission_count,
            achievement=str(report.get("achievement", "")),
            outcome=outcome,
            training_report=report,
        )
        try:
            self._learning_records.append(record)
        except OSError:
            logging.exception("学习记录落盘失败（不影响训练主流程）")

    @staticmethod
    def _effective_blind_spots(session: _InteractiveSession) -> list[str]:
        if session.diagnosis is None:
            return []
        content = _payload_content(session.diagnosis)
        plan = content.get("knowledge_point_plan")
        blind_spots = (
            [
                str(item["knowledge_point"]).strip()
                for item in plan
                if isinstance(item, Mapping)
                and item.get("mastery_status")
                in {"needs_training", "pending_training"}
                and isinstance(item.get("knowledge_point"), str)
                and str(item["knowledge_point"]).strip()
            ]
            if isinstance(plan, list)
            else [
                item.strip()
                for item in content.get("blind_spots", [])
                if isinstance(item, str) and item.strip()
            ]
        )
        deferred = set(session.deferred_knowledge_points)
        unique: list[str] = []
        seen: set[str] = set()
        for item in blind_spots:
            if item not in deferred and item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    @staticmethod
    def _current_knowledge_point(session: _InteractiveSession) -> str | None:
        for product in (
            session.learning_task,
            session.active_task,
            session.lecture,
        ):
            value = _payload_content(product or {}).get("knowledge_point")
            if isinstance(value, str) and value.strip():
                return value.strip()
        if session.evidence_bundle is not None:
            return session.evidence_bundle.knowledge_point
        return None

    @staticmethod
    def _remaining_blind_spots(session: _InteractiveSession) -> list[str]:
        blind_spots = InteractiveSessionManager._effective_blind_spots(session)
        if not blind_spots:
            return []
        current = InteractiveSessionManager._current_knowledge_point(session)
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
                student_profile=session.runtime.profile,
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
            draft = session.runtime.task.generate_assessment(
                template_id,
                student_profile=session.runtime.profile,
            )
            return (
                session.evidence_bundle.bind(draft)
                if session.evidence_bundle is not None
                else draft
            )
        except ValueError as exc:
            raise InteractiveSessionError(
                "暂时无法为你生成分阶测验，请稍后重试。"
            ) from exc

    @staticmethod
    def _active_follow_up_task(
        session: _InteractiveSession,
    ) -> Mapping[str, Any] | None:
        artifact = session.artifact
        if isinstance(artifact, Mapping):
            content = _payload_content(artifact)
            if (
                content.get("event") == "follow_up_question_ready"
                and str(content.get("question") or "").strip()
                == session.follow_up_question.strip()
            ):
                return artifact
        return session.learning_task or session.active_task

    @staticmethod
    def _correction_evidence_summary(
        evidence: tuple[Mapping[str, Any], ...],
    ) -> list[dict[str, Any]]:
        points = list(_expected_points(evidence))
        return [
            {
                "ref": str(item.get("ref") or "").strip(),
                "expected_points": points,
            }
            for item in evidence
            if str(item.get("ref") or "").strip()
        ]

    @staticmethod
    def _missing_evidence_fields(
        answer: str,
        evidence: tuple[Mapping[str, Any], ...],
    ) -> list[str]:
        normalized = answer.casefold()
        rows = _expected_rows(evidence)
        fields: list[str] = []
        for field_name in dict.fromkeys(
            key for row in rows for key in row
        ):
            if field_name.casefold() in normalized:
                continue
            values = {
                str(row.get(field_name)).strip().casefold()
                for row in rows
                if row.get(field_name) is not None
            }
            if values and any(value and value in normalized for value in values):
                continue
            fields.append(field_name)
        return fields or ["判断依据"]

    @classmethod
    def _open_correction_ticket(
        cls,
        session: _InteractiveSession,
        *,
        misconception_id: str,
        answer: str,
        evidence: tuple[Mapping[str, Any], ...],
        source_round: int,
    ) -> None:
        session.correction_tickets.append(
            {
                "source_round": source_round,
                "learner_answer_digest": sha256(
                    normalize_learner_input(answer).encode("utf-8")
                ).hexdigest(),
                "missing_evidence_fields": cls._missing_evidence_fields(
                    answer,
                    evidence,
                ),
                "misconception_id": misconception_id,
                "knowledge_point": (
                    InteractiveSessionManager._current_knowledge_point(session)
                    or "当前知识点"
                ),
                "correction_evidence": cls._correction_evidence_summary(
                    evidence
                ),
                "resolved": False,
                "status": "open",
            }
        )

    @classmethod
    def _resolve_correction_tickets(
        cls,
        session: _InteractiveSession,
        *,
        misconception_id: str,
        evidence: tuple[Mapping[str, Any], ...],
        resolved_round: int,
    ) -> bool:
        resolved = False
        resolution_evidence = cls._correction_evidence_summary(evidence)
        for ticket in session.correction_tickets:
            if (
                not bool(ticket.get("resolved"))
                and ticket.get("misconception_id") == misconception_id
            ):
                ticket["resolved"] = True
                ticket["status"] = "resolved"
                ticket["resolved_round"] = resolved_round
                ticket["resolution_evidence"] = resolution_evidence
                resolved = True
        return resolved

    @staticmethod
    def _next_correction_requirements(
        session: _InteractiveSession,
        *,
        unresolved: set[str],
    ) -> tuple[str | None, tuple[str, ...]]:
        """Return the oldest open correction and its missing evidence fields."""

        for ticket in session.correction_tickets:
            target = ticket.get("misconception_id")
            if bool(ticket.get("resolved")) or target not in unresolved:
                continue
            raw_fields = ticket.get("missing_evidence_fields")
            fields = (
                tuple(
                    dict.fromkeys(
                        field.strip()
                        for field in raw_fields
                        if isinstance(field, str) and field.strip()
                    )
                )
                if isinstance(raw_fields, list)
                else ()
            )
            return str(target), fields
        if unresolved:
            return sorted(unresolved)[0], ()
        return None, ()

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
        if _is_vacuous_follow_up_answer(answer_text):
            raise InteractiveSessionError(
                "请用自己的话回答，不能只答“是/否”。"
            )
        if not session.follow_up_question:
            raise InteractiveSessionError("当前理解核对内容不完整，请稍后重试。")
        if (
            _follow_up_match_key(answer_text)
            == _follow_up_match_key(session.follow_up_question)
        ):
            raise InteractiveSessionError(
                "你提交的是题目本身。请引用查询结果中的字段和值作答。"
            )
        if _is_obviously_unrelated_follow_up_answer(answer_text):
            raise InteractiveSessionError(
                "这段回答与当前问题无关。请引用查询结果中的字段和值作答。"
            )

        session.outcome = None
        runtime = session.runtime
        if runtime.engine.state not in {State.S7_STUDENT, State.S8_PROBE}:
            raise InteractiveSessionError("当前步骤不能提交这段判断。")
        submitted_round = session.follow_up_round
        if not 1 <= submitted_round <= MAX_FOLLOW_UP_ROUNDS:
            raise InteractiveSessionError("本轮理解核对已经结束。")
        current_task = self._active_follow_up_task(session)
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
        unresolved_elsewhere = set(session.unresolved_misconceptions)
        if session.follow_up_target:
            unresolved_elsewhere.discard(session.follow_up_target)
        correction_target, historical_missing_fields = (
            self._next_correction_requirements(
                session,
                unresolved=unresolved_elsewhere,
            )
        )
        required_next_targets = tuple(
            [
                *([correction_target] if correction_target is not None else []),
                *sorted(unresolved_elsewhere - {correction_target}),
            ]
        )
        current_evidence = tuple(
            item
            for item in current_task.get("evidence", [])
            if isinstance(item, Mapping)
        )
        required_evidence_fields = (
            historical_missing_fields
            if historical_missing_fields
            else tuple(
                self._missing_evidence_fields(answer_text, current_evidence)
            )
        )
        correction_gate_open = not unresolved_elsewhere
        completion_allowed = submitted_round >= 2 and correction_gate_open
        generation_round = min(
            submitted_round + 1,
            MAX_FOLLOW_UP_ROUNDS,
        )
        probed_snapshot = tuple(sorted(session.probed_misconceptions))
        covered_snapshot = tuple(sorted(session.covered_relation_points))
        session.cross_phase_questions = list(
            dict.fromkeys(
                [
                    *session.cross_phase_questions,
                    session.follow_up_question,
                ]
            )
        )
        previous_questions = tuple(
            dict.fromkeys(
                [
                    *(
                        str(turn.get("question", "")).strip()
                        for turn in session.follow_up_turns
                    ),
                    session.follow_up_question,
                    *session.cross_phase_questions,
                ]
            )
        )
        approved_route_support_points: tuple[str, ...] = ()
        # 生成可能在校验早期抛 RelationIntegrityError（如覆盖点非法），此时
        # 作答尚未被评审——except 分支须以 generated 是否存在区分能否记录。
        generated = None
        try:
            try:
                generated = session.follow_up_agent.generate(
                    student_answer=answer_text,
                    current_task=current_task,
                    task_agent=runtime.task,
                    current_question=session.follow_up_question,
                    round_index=generation_round,
                    max_rounds=MAX_FOLLOW_UP_ROUNDS,
                    probed_misconceptions=probed_snapshot,
                    covered_relation_points=covered_snapshot,
                    routing_policy=self._routing_policy,
                    completion_allowed=completion_allowed,
                    terminal_round=terminal_round,
                    previous_questions=previous_questions,
                    previous_answers=tuple(
                        str(turn.get("answer", "")).strip()
                        for turn in session.follow_up_turns
                        if str(turn.get("answer", "")).strip()
                    ),
                    required_next_targets=required_next_targets,
                    required_evidence_fields=required_evidence_fields,
                    learner_context=(
                        _learner_context(session)
                        if self._persona_routing
                        else None
                    ),
                    lecture_digest=(
                        _lecture_digest(session)
                        if self._persona_routing
                        else ""
                    ),
                    data_digest=(
                        _data_digest(session)
                        if self._persona_routing
                        else ""
                    ),
                    current_layer=session.follow_up_layer,
                )
            except FollowUpGenerationError:
                generated = session.follow_up_agent.deterministic_fallback(
                    current_task=current_task,
                    round_index=generation_round,
                    max_rounds=MAX_FOLLOW_UP_ROUNDS,
                    student_answer=answer_text,
                    current_question=session.follow_up_question,
                    completion_allowed=completion_allowed,
                    terminal_round=terminal_round,
                    previous_questions=previous_questions,
                    previous_answers=tuple(
                        str(turn.get("answer", "")).strip()
                        for turn in session.follow_up_turns
                        if str(turn.get("answer", "")).strip()
                    ),
                    task_agent=runtime.task,
                    required_target=(
                        required_next_targets[0]
                        if required_next_targets
                        else None
                    ),
                    required_evidence_fields=required_evidence_fields,
                    current_layer=session.follow_up_layer,
                )
            reviewed_product: dict[str, Any] | None = None
            if generated.product is not None:
                generated, reviewed_product = self._review_follow_up_product(
                    session,
                    answer_text=answer_text,
                    current_task=current_task,
                    round_index=generation_round,
                    completion_allowed=completion_allowed,
                    terminal_round=terminal_round,
                    initial=generated,
                    probed_snapshot=probed_snapshot,
                    covered_snapshot=covered_snapshot,
                    required_next_targets=required_next_targets,
                    required_evidence_fields=required_evidence_fields,
                    learner_context=(
                        _learner_context(session)
                        if self._persona_routing
                        else None
                    ),
                    lecture_digest=(
                        _lecture_digest(session)
                        if self._persona_routing
                        else ""
                    ),
                    data_digest=(
                        _data_digest(session)
                        if self._persona_routing
                        else ""
                    ),
                    current_layer=session.follow_up_layer,
                )
            # 闭环六：确定性层推进——答对进深层（封顶第 3 层）、答错停留。
            # 依据是审阅与守卫修正后的最终 assessment，LLM 不参与层的裁决。
            session.follow_up_layer = resolve_follow_up_layer(
                session.follow_up_layer,
                assessment=generated.assessment,
            )
            relation_index = default_relation_index(
                tuple(runtime.task.misconception_ids)
            )
            recomputed_target, approved_route_support_points = (
                deterministic_follow_up_route(
                    assessment=generated.assessment,
                    diagnosed_misconception=(
                        generated.diagnosed_misconception
                    ),
                    completion_allowed=completion_allowed,
                    terminal_round=terminal_round,
                    probed_misconceptions=probed_snapshot,
                    covered_relation_points=covered_snapshot,
                    responsibility_scope=_responsibility_scope(
                        _payload_content(current_task)
                    ),
                    relation_index=relation_index,
                    allowed_targets=runtime.task.misconception_ids,
                    allowed_support_points=default_relation_support_points(
                        tuple(runtime.task.misconception_ids)
                    ),
                    routing_policy=self._routing_policy,
                    required_targets=required_next_targets,
                )
            )
            if (
                recomputed_target != generated.next_target_misconception
                or approved_route_support_points
                != generated.route_support_points
            ):
                raise RelationIntegrityError(
                    "approved follow-up route does not match backend recomputation"
                )
        except ReviewFlowTerminal as terminal:
            del terminal
            # The submitted round was assessed before the review failure, so
            # its question/answer record must still land in the turn history
            # — otherwise the next round's record panel skips this round.
            session.follow_up_turns.append({
                "round": submitted_round,
                "question": session.follow_up_question,
                "answer": answer_text.strip(),
                "feedback": self._follow_up_feedback(
                        generated.assessment,
                        question=session.follow_up_question,
                        answer=answer_text,
                    ),
                "assessment": generated.assessment,
                "diagnosed_misconception": generated.diagnosed_misconception,
                "target_misconception": session.follow_up_target,
            })
            session.follow_up_layer = resolve_follow_up_layer(
                session.follow_up_layer,
                assessment=generated.assessment,
            )
            try:
                fallback_turn = session.follow_up_agent.deterministic_fallback(
                    current_task=current_task,
                    round_index=min(submitted_round + 1, MAX_FOLLOW_UP_ROUNDS),
                    max_rounds=MAX_FOLLOW_UP_ROUNDS,
                    student_answer=answer_text,
                    current_question=session.follow_up_question,
                    completion_allowed=submitted_round >= 2,
                    terminal_round=False,
                    previous_questions=previous_questions,
                    previous_answers=tuple(
                        str(turn.get("answer", "")).strip()
                        for turn in session.follow_up_turns
                        if str(turn.get("answer", "")).strip()
                    ),
                    required_target=None,
                    required_evidence_fields=(),
                    current_layer=session.follow_up_layer,
                )
                if fallback_turn.product is not None:
                    fallback_question = str(
                        _payload_content(fallback_turn.product).get("question", "")
                    ).strip()
                    if fallback_question and not contains_engineering_text(fallback_question):
                        session.follow_up_round = min(
                            submitted_round + 1, MAX_FOLLOW_UP_ROUNDS
                        )
                        session.follow_up_question = fallback_question
                        if isinstance(session.interaction, dict):
                            # Keep the displayed prompt and turn history in
                            # sync with the recorded state — a stale
                            # interaction would re-show the previous question
                            # and drop this round's record.
                            session.interaction = {
                                **session.interaction,
                                "prompt": fallback_question,
                                "round": session.follow_up_round,
                                "turns": [
                                    dict(turn) for turn in session.follow_up_turns
                                ],
                            }
                        session.awaiting = "follow_up"
                        session.processed_turn_ids.add(client_turn_id)
                        return self.get_state(session_id)
            except Exception:
                pass
            self._retain_follow_up_after_quality_interruption(
                session,
                client_turn_id=client_turn_id,
            )
            return self.get_state(session_id)
        except ReviewFlowInterrupted as interrupted:
            del interrupted
            session.follow_up_turns.append({
                "round": submitted_round,
                "question": session.follow_up_question,
                "answer": answer_text.strip(),
                "feedback": self._follow_up_feedback(
                        generated.assessment,
                        question=session.follow_up_question,
                        answer=answer_text,
                    ),
                "assessment": generated.assessment,
                "diagnosed_misconception": generated.diagnosed_misconception,
                "target_misconception": session.follow_up_target,
            })
            session.follow_up_layer = resolve_follow_up_layer(
                session.follow_up_layer,
                assessment=generated.assessment,
            )
            try:
                fallback_turn = session.follow_up_agent.deterministic_fallback(
                    current_task=current_task,
                    round_index=min(submitted_round + 1, MAX_FOLLOW_UP_ROUNDS),
                    max_rounds=MAX_FOLLOW_UP_ROUNDS,
                    student_answer=answer_text,
                    current_question=session.follow_up_question,
                    completion_allowed=submitted_round >= 2,
                    terminal_round=False,
                    previous_questions=previous_questions,
                    previous_answers=tuple(
                        str(turn.get("answer", "")).strip()
                        for turn in session.follow_up_turns
                        if str(turn.get("answer", "")).strip()
                    ),
                    required_target=None,
                    required_evidence_fields=(),
                    current_layer=session.follow_up_layer,
                )
                if fallback_turn.product is not None:
                    fallback_question = str(
                        _payload_content(fallback_turn.product).get("question", "")
                    ).strip()
                    if fallback_question and not contains_engineering_text(fallback_question):
                        session.follow_up_round = min(
                            submitted_round + 1, MAX_FOLLOW_UP_ROUNDS
                        )
                        session.follow_up_question = fallback_question
                        if isinstance(session.interaction, dict):
                            session.interaction = {
                                **session.interaction,
                                "prompt": fallback_question,
                                "round": session.follow_up_round,
                                "turns": [
                                    dict(turn) for turn in session.follow_up_turns
                                ],
                            }
                        session.awaiting = "follow_up"
                        session.processed_turn_ids.add(client_turn_id)
                        return self.get_state(session_id)
            except Exception:
                pass
            self._retain_follow_up_after_quality_interruption(
                session,
                client_turn_id=client_turn_id,
            )
            return self.get_state(session_id)
        except RelationIntegrityError:
            # 本轮作答已完成评审（generated 已产出），仅路由一致性校验失败——
            # 与 ReviewFlowTerminal/Interrupted 分支同口径：作答记录必须落进
            # 历史并给出评价（0818 实录："不知道"落此分支时无记录无评价，
            # 学员看到原题重出却不知发生了什么）。先记录，再尝试确定性
            # 兜底追问，失败才保留上一题。生成期早期失败（generated 未产出，
            # 作答未被评审）则不记录，维持原保留行为。
            if generated is not None:
                session.follow_up_turns.append({
                    "round": submitted_round,
                    "question": session.follow_up_question,
                    "answer": answer_text.strip(),
                    "feedback": self._follow_up_feedback(
                        generated.assessment,
                        question=session.follow_up_question,
                        answer=answer_text,
                    ),
                    "assessment": generated.assessment,
                    "diagnosed_misconception": generated.diagnosed_misconception,
                    "target_misconception": session.follow_up_target,
                })
                session.follow_up_layer = resolve_follow_up_layer(
                    session.follow_up_layer,
                    assessment=generated.assessment,
                )
                try:
                    fallback_turn = session.follow_up_agent.deterministic_fallback(
                        current_task=current_task,
                        round_index=min(submitted_round + 1, MAX_FOLLOW_UP_ROUNDS),
                        max_rounds=MAX_FOLLOW_UP_ROUNDS,
                        student_answer=answer_text,
                        current_question=session.follow_up_question,
                        completion_allowed=submitted_round >= 2,
                        terminal_round=False,
                        previous_questions=previous_questions,
                        previous_answers=tuple(
                            str(turn.get("answer", "")).strip()
                            for turn in session.follow_up_turns
                            if str(turn.get("answer", "")).strip()
                        ),
                        required_target=None,
                        required_evidence_fields=(),
                        current_layer=session.follow_up_layer,
                    )
                    if fallback_turn.product is not None:
                        fallback_question = str(
                            _payload_content(fallback_turn.product).get("question", "")
                        ).strip()
                        if fallback_question and not contains_engineering_text(fallback_question):
                            session.follow_up_round = min(
                                submitted_round + 1, MAX_FOLLOW_UP_ROUNDS
                            )
                            session.follow_up_question = fallback_question
                            if isinstance(session.interaction, dict):
                                session.interaction = {
                                    **session.interaction,
                                    "prompt": fallback_question,
                                    "round": session.follow_up_round,
                                    "turns": [
                                        dict(turn) for turn in session.follow_up_turns
                                    ],
                                }
                            session.awaiting = "follow_up"
                            session.processed_turn_ids.add(client_turn_id)
                            return self.get_state(session_id)
                except Exception:
                    pass
            self._retain_follow_up_after_quality_interruption(
                session,
                client_turn_id=client_turn_id,
            )
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
        reviewed_evidence = tuple(
            item
            for item in current_task.get("evidence", [])
            if isinstance(item, Mapping)
        )
        base_feedback = self._follow_up_feedback(
            generated.assessment,
            question=session.follow_up_question,
            answer=text.strip(),
            evidence=reviewed_evidence,
        )
        turn_record = {
            "round": submitted_round,
            "question": session.follow_up_question,
            "answer": text.strip(),
            "feedback": base_feedback,
            "assessment": generated.assessment,
            "diagnosed_misconception": generated.diagnosed_misconception,
            "target_misconception": session.follow_up_target,
        }
        session.follow_up_submission_count += 1
        resolved_historical_ticket = False
        if generated.assessment == "mastered":
            resolved_target = session.follow_up_target
            if resolved_target:
                session.unresolved_misconceptions.discard(resolved_target)
                resolved_historical_ticket = self._resolve_correction_tickets(
                    session,
                    misconception_id=resolved_target,
                    evidence=reviewed_evidence,
                    resolved_round=submitted_round,
                )
            if resolved_target == UNKNOWN_MISCONCEPTION:
                session.unresolved_misconceptions.discard(UNKNOWN_MISCONCEPTION)
        else:
            diagnosed = generated.diagnosed_misconception
            if diagnosed and diagnosed != UNKNOWN_MISCONCEPTION:
                session.unresolved_misconceptions.discard(
                    UNKNOWN_MISCONCEPTION
                )
            unresolved = (
                diagnosed
                if diagnosed and diagnosed != UNKNOWN_MISCONCEPTION
                else session.follow_up_target or UNKNOWN_MISCONCEPTION
            )
            session.unresolved_misconceptions.add(unresolved)
            self._open_correction_ticket(
                session,
                misconception_id=unresolved,
                answer=text.strip(),
                evidence=reviewed_evidence,
                source_round=submitted_round,
            )

        if generated.assessment == "mastered" and session.unresolved_misconceptions:
            turn_record["feedback"] = (
                f"{base_feedback} 本轮判断正确，但此前还有 "
                f"{len(session.unresolved_misconceptions)} 项问题需要逐项纠正，"
                "因此暂不升档。"
            )
        elif resolved_historical_ticket:
            turn_record["feedback"] = (
                f"{base_feedback} 本轮已完成对上一处错误的纠正。"
            )

        if (
            generated.assessment == "mastered"
            and submitted_round >= 2
            and not session.unresolved_misconceptions
        ):
            session.follow_up_turns.append(turn_record)
            answer = self._finish_mastered_follow_up(
                session,
                feedback=str(turn_record["feedback"]),
            )
            if answer is None:
                session.processed_turn_ids.add(client_turn_id)
                return self.get_state(session_id)
            session.artifact = answer
            session.processed_turn_ids.add(client_turn_id)
            return self.get_state(session_id)

        if terminal_round:
            session.follow_up_turns.append(turn_record)
            answer = self._finish_unmastered_follow_up(
                session,
                feedback=str(turn_record["feedback"]),
            )
            if answer is None:
                session.processed_turn_ids.add(client_turn_id)
                return self.get_state(session_id)
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
        next_target = generated.next_target_misconception
        next_turns = [*session.follow_up_turns, turn_record]
        next_probed = set(session.probed_misconceptions)
        if next_target not in {None, UNKNOWN_MISCONCEPTION}:
            next_probed.add(next_target)
        next_covered = set(session.covered_relation_points)
        next_covered.update(approved_route_support_points)
        next_processed = set(session.processed_turn_ids)
        next_processed.add(client_turn_id)
        next_round = submitted_round + 1
        next_interaction = {
            "kind": "free_text_follow_up",
            **_follow_up_task_context(session.learning_task or session.active_task),
            "prompt": question,
            "round": next_round,
            "max_rounds": MAX_FOLLOW_UP_ROUNDS,
            "follow_up_layer": session.follow_up_layer,
            "layer_name": FOLLOW_UP_LAYER_NAMES.get(
                session.follow_up_layer, "口径记忆"
            ),
            "turns": [dict(turn) for turn in next_turns],
            "feedback": turn_record["feedback"],
            "next_step_reason": (
                "当前回答已有正确依据，但此前暴露的问题尚未全部纠正；"
                "下一轮将优先核对仍未结清的问题。"
                if generated.assessment == "mastered"
                and session.unresolved_misconceptions
                else "当前回答已有正确依据；为避免一次偶然作答被误判为掌握，"
                "还需要完成下一轮不同角度的核对。"
                if generated.assessment == "mastered"
                else "当前回答的证据或解释还不完整，因此继续留在本知识点，"
                "用下一问补齐判断依据。"
            ),
            "correction_required": bool(session.unresolved_misconceptions),
            "unresolved_misconceptions": sorted(
                session.unresolved_misconceptions
            ),
        }

        session.follow_up_round = next_round
        session.follow_up_question = question
        session.follow_up_turns = next_turns
        session.artifact = reviewed_product
        session.interaction = next_interaction
        session.awaiting = "follow_up"
        session.probed_misconceptions = next_probed
        session.covered_relation_points = next_covered
        session.follow_up_target = next_target
        session.processed_turn_ids = next_processed
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
        probed_snapshot: tuple[str, ...],
        covered_snapshot: tuple[str, ...],
        required_next_targets: tuple[str, ...] = (),
        required_evidence_fields: tuple[str, ...] = (),
        learner_context: Mapping[str, Any] | None = None,
        lecture_digest: str = "",
        data_digest: str = "",
        current_layer: int = 1,
    ) -> tuple[FollowUpTurn, dict[str, Any]]:
        runtime = session.runtime
        candidate = initial
        first = True
        policy = self._follow_up_review_policy
        rebuttal_attempts = 0
        last_feedback = _GENERIC_FOLLOW_UP_REVIEW_FEEDBACK
        active_product: Mapping[str, Any] | None = None

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
                    probed_misconceptions=probed_snapshot,
                    covered_relation_points=covered_snapshot,
                    routing_policy=self._routing_policy,
                    previous_questions=tuple(
                        dict.fromkeys(
                            [
                                *(
                                    str(turn.get("question", "")).strip()
                                    for turn in session.follow_up_turns
                                ),
                                session.follow_up_question,
                            ]
                        )
                    ),
                    review_feedback=last_feedback,
                    completion_allowed=completion_allowed,
                    terminal_round=terminal_round,
                    required_next_targets=required_next_targets,
                    required_evidence_fields=required_evidence_fields,
                    learner_context=learner_context,
                    lecture_digest=lecture_digest,
                    data_digest=data_digest,
                    current_layer=current_layer,
                )
                if (
                    (
                        candidate.assessment,
                        candidate.diagnosed_misconception,
                        candidate.next_target_misconception,
                    )
                    != (
                        initial.assessment,
                        initial.diagnosed_misconception,
                        initial.next_target_misconception,
                    )
                ):
                    raise FollowUpGenerationError(
                        "regeneration changed the learner assessment"
                    )
                initial_content = _payload_content(initial.product or {})
                candidate_content = _payload_content(candidate.product or {})
                for field in (
                    "responsibility_scope",
                    "difficulty",
                    "evidence_refs",
                ):
                    if candidate_content.get(field) != initial_content.get(field):
                        raise FollowUpGenerationError(
                            "regeneration changed the learner evidence boundary"
                        )
            if candidate.product is None:
                raise FollowUpGenerationError(
                    "review regeneration produced no question"
                )
            return candidate.product

        def observe(event: str, details: Mapping[str, Any]) -> None:
            nonlocal last_feedback
            if event == "regeneration_started":
                last_feedback = _GENERIC_FOLLOW_UP_REVIEW_FEEDBACK
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

        def audit_with_feedback_capture(
            draft: Mapping[str, Any],
        ) -> dict[str, Any]:
            nonlocal active_product, last_feedback
            audited = runtime.audit(draft)
            if (
                audited.get("role") == "probe"
                and _payload_content(audited).get("event")
                == "follow_up_question_ready"
            ):
                active_product = audited
            elif (
                audited.get("role") == "re_verdict"
                and active_product is not None
            ):
                mapped = _feedback_for_re_verdict(audited, active_product)
                if mapped:
                    last_feedback = (
                        mapped
                        if policy.feedback_mode == "mapped"
                        else _GENERIC_FOLLOW_UP_REVIEW_FEEDBACK
                    )
            return audited

        def generate_selective_rebuttal(
            product: Mapping[str, Any],
            verdict: Mapping[str, Any],
        ) -> dict[str, Any]:
            nonlocal rebuttal_attempts
            if rebuttal_attempts < policy.rebuttal_budget:
                rebuttal_attempts += 1
                return runtime.rebuttal.generate(product, verdict)
            return runtime.rebuttal.deterministic_concede(product, verdict)

        product = audit_and_review(
            produce,
            audit=audit_with_feedback_capture,
            review=lambda value: runtime.review.review(
                value,
                learning_report=session.diagnosis,
                student_profile=runtime.profile,
                learned_knowledge_points=runtime.learned_knowledge_points,
                activity_observer=observe,
                learning_contract=session.learning_contract,
            ),
            generate_rebuttal=generate_selective_rebuttal,
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
                    "diagnosed_misconception": (
                        generated.diagnosed_misconception
                    ),
                    "next_target_misconception": (
                        generated.next_target_misconception
                    ),
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
        difficulty_action: str | None = None,
    ) -> dict[str, Any]:
        target = session.follow_up_target or UNKNOWN_MISCONCEPTION
        probe: dict[str, Any] = {
            "wrong_attempts": 1,
            "target_misconception": target,
        }
        # T17 转换消息的 difficulty_action 从该元数据读取（engine 侧），
        # 使 trace 里的动作与实际补学决策（step_down/refresh/deferred）一致。
        if difficulty_action is not None:
            probe["difficulty_action"] = difficulty_action
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
            "probe": probe,
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
                generated.diagnosed_misconception == UNKNOWN_MISCONCEPTION
                and not session.generic_fallback_used
            ):
                runtime.transition(
                    self._unknown_probe_draft(session),
                    "T18",
                )
                session.generic_fallback_used = True
        elif runtime.engine.state is not State.S8_PROBE:
            raise InteractiveSessionError("当前理解核对进度无法继续。")

    @staticmethod
    def _follow_up_feedback(
        assessment: str,
        *,
        question: str = "",
        answer: str = "",
        evidence: tuple[Mapping[str, Any], ...] = (),
    ) -> str:
        if assessment == "mastered":
            confirmation = reviewed_answer_confirmation(
                question=question,
                answer=answer,
                evidence=evidence,
            )
            if confirmation:
                return f"回答有效：{confirmation}"
            # 无确定性核验结论时不做“已引用对应数据”的事实宣称，只说明
            # 本轮评审结果，避免与学员实际作答内容不符。
            return "回答有效：已通过本轮理解核对，进入下一步训练。"
        if _is_dont_know_follow_up_answer(answer):
            feedback = (
                "不会也没关系。跟着下一问的提示，先在表里找到对应的字段和值，"
                "再用自己的话说一遍即可。"
            )
        elif _is_meaningless_short_answer(question, answer):
            feedback = (
                "这个回答还看不懂你想表达什么。请用完整的句子说明你的判断，"
                "引用查询结果中的字段名和数值。"
            )
        elif assessment == "needs_support":
            feedback = "回答部分有效：方向基本相关，但关键数值、比较对象或因果依据仍不完整。"
        else:
            feedback = "暂时无法确认掌握：当前回答还不足以和查询证据建立稳定对应，请明确引用结果中的字段和值。"
        correction = reviewed_answer_correction(
            question=question,
            answer=answer,
            evidence=evidence,
        ) or reviewed_plan_actual_correction(
            question=question,
            answer=answer,
            evidence=evidence,
        ) or reviewed_row_value_correction(
            question=question,
            answer=answer,
            evidence=evidence,
        )
        return f"{correction} {feedback}" if correction else feedback

    def _finish_mastered_follow_up(
        self,
        session: _InteractiveSession,
        *,
        feedback: str,
    ) -> dict[str, Any] | None:
        runtime = session.runtime
        try:
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
        except DemoSessionError:
            self._finish_system_error(session)
            return None
        session.pending_learning_action = "step_up"
        session.completed_correction = session.follow_up_had_support
        session.awaiting = "advance"
        if (
            session.follow_up_target == "M-01"
            and session.completed_correction
        ):
            session.interaction = {
                **self._collision_interaction(session),
                "feedback": feedback,
                "next_step_reason": (
                    "已完成至少两轮理解核对，回答通过证据审核并达到当前知识点要求，"
                    "因此进入纠正展示和下一步训练。"
                ),
            }
        else:
            session.interaction = {
                "kind": "next_learning_step",
                "message": "你的判断已经能够用数据说明，正在为你安排下一步训练。",
                "feedback": feedback,
                "next_step_reason": (
                    "已完成至少两轮理解核对，回答通过证据审核并达到当前知识点要求，"
                    "因此进入下一步训练。"
                ),
            }
        return answer

    def _finish_unmastered_follow_up(
        self,
        session: _InteractiveSession,
        *,
        feedback: str,
    ) -> dict[str, Any] | None:
        runtime = session.runtime
        try:
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
            preview = self._preview_t17_remediation(session)
            answer = runtime.transition(
                self._probe_outcome_draft(
                    session,
                    answer_result="wrong",
                    difficulty_action=str(preview["action"]),
                ),
                "T17",
            )
        except DemoSessionError:
            self._finish_system_error(session)
            return None
        self._apply_t17_remediation(session, feedback=feedback, preview=preview)
        return answer

    @staticmethod
    def _preview_t17_remediation(session: _InteractiveSession) -> dict[str, Any]:
        """Purely compute the T17 remediation decision without mutating state.

        The engine stamps ``difficulty_action`` onto the T17 transition message
        from the probe outcome draft, so the action must be known *before* the
        transition fires.  This preview applies exactly the same rule as
        ``_apply_t17_remediation`` (same attempt count, same difficulty table)
        and the apply step reuses the preview so the two can never diverge.
        """
        current_point = InteractiveSessionManager._current_knowledge_point(
            session
        ) or "当前知识点"
        attempt = session.remediation_attempts.get(current_point, 0) + 1
        old_difficulty = (
            session.learning_contract.difficulty
            if session.learning_contract
            else "basic"
        )
        lower_difficulty = {
            "advanced": "applied",
            "applied": "basic",
            "basic": "basic",
        }[old_difficulty]
        action = (
            "deferred"
            if attempt >= 2
            else ("step_down" if lower_difficulty != old_difficulty else "refresh")
        )
        return {
            "knowledge_point": current_point,
            "attempt": attempt,
            "from_difficulty": old_difficulty,
            "to_difficulty": lower_difficulty,
            "action": action,
        }

    def _apply_t17_remediation(
        self,
        session: _InteractiveSession,
        *,
        feedback: str,
        reason: str = "follow_up",
        preview: Mapping[str, Any] | None = None,
    ) -> None:
        """Apply T17 teaching remediation without changing the state graph.

        The first T17 creates a new immutable contract revision and lowers one
        difficulty band where possible.  A second T17 defers the knowledge
        point so the existing S2 resource branch can continue with the next
        point.  This caps repetition while preserving T17 and all 21 published
        transitions.

        ``preview`` is the pre-transition decision computed by
        ``_preview_t17_remediation`` (used to stamp the T17 trace message);
        passing it in keeps the stamped action and the applied action from
        the exact same computation.
        """
        decision = dict(preview) if preview is not None else (
            self._preview_t17_remediation(session)
        )
        current_point = str(decision["knowledge_point"])
        attempt = int(decision["attempt"])
        session.remediation_attempts[current_point] = attempt
        old_contract = session.learning_contract
        old_difficulty = str(decision["from_difficulty"])
        lower_difficulty = str(decision["to_difficulty"])
        previous_chunks = {
            chunk.chunk_id for chunk in session.evidence_chunks
        }

        deferred = attempt >= 2
        if deferred and current_point not in session.deferred_knowledge_points:
            session.deferred_knowledge_points.append(current_point)
        remaining = self._effective_blind_spots(session)
        target_points = tuple(remaining) or (
            old_contract.target_knowledge_points if old_contract else (current_point,)
        )
        if old_contract is not None:
            revised_contract = replace(
                old_contract,
                difficulty=lower_difficulty,
                target_knowledge_points=target_points,
                revision=old_contract.revision + 1,
            )
            session.runtime.audit(
                revised_contract.control_draft(session.runtime.options.trace_id)
            )
            session.learning_contract = revised_contract

        session.remediation_excluded_chunk_ids = previous_chunks
        remediation_action = str(decision["action"])
        session.remediation_context = {
            "knowledge_point": current_point,
            "attempt": attempt,
            "action": remediation_action,
            "from_difficulty": old_difficulty,
            "to_difficulty": lower_difficulty,
            "exposed_misconceptions": sorted(
                session.unresolved_misconceptions
            ),
        }
        for ticket in session.correction_tickets:
            if (
                ticket.get("status") == "open"
                and ticket.get("knowledge_point") == current_point
            ):
                ticket["status"] = "deferred" if deferred else "remediation"
        # 触发路径不同，表述必须如实：理解核对走四次轮次上限；数据实操
        # 走连续五次未通过安全校验，二者不应共用“四次理解核对”的说法。
        old_label = _difficulty_label(old_difficulty)
        lower_label = _difficulty_label(lower_difficulty)
        if reason == "sql":
            lead = "连续五次数据实操未通过安全校验"
            step_reason = (
                "本轮数据实操连续五次未通过；为避免反复受阻，"
                "先降低难度或更换内容，而不是继续原题。"
            )
        else:
            lead = "四次理解核对未达成掌握目标"
            step_reason = (
                "本轮已达到四次核对上限，但历史错误尚未纠正；"
                "因此先降低难度或更换内容，而不是直接升档。"
            )
        session.interaction = {
            "kind": "learning_notice",
            "message": (
                "这个知识点已转入后续补学清单，先继续学习下一个知识点。"
                if deferred
                else (
                    f"{lead}。\n已从{old_label}档调整为{lower_label}档，"
                    "并更换证据与讲解角度后重新练习。"
                    if lower_difficulty != old_difficulty
                    else f"{lead}，当前已是基础档。\n"
                    "系统将更换证据与讲解角度后再练习一次。"
                )
            ),
            "feedback": feedback,
            "next_step_reason": (
                "同一知识点已完成一次补学仍未掌握；为防止重复循环，"
                "本轮将其标记为延后学习，并在训练报告中保留。"
                if deferred
                else step_reason
            ),
        }
        session.active_task = None
        session.learning_task = None
        session.lecture = None
        session.evidence_bundle = None
        session.evidence_chunks = ()
        session.resource_bundle = None
        session.sql_result = None
        session.artifact = None
        session.outcome = None
        session.prefetched_task = None
        session.prefetched_task_generator = None
        session.prefetched_assessment = None
        session.prefetched_assessment_generator = None
        session.task_phase = "initial"
        session.pending_learning_action = None
        session.completed_correction = False
        session.follow_up_turns.clear()
        # 重练是全新的追问周期：跨档累积器一并清空。
        session.cross_phase_questions.clear()
        session.follow_up_target = None
        session.unresolved_misconceptions.clear()
        session.probed_misconceptions.clear()
        session.covered_relation_points.clear()
        session.sql_failure_count = 0
        session.sql_support = None
        session.awaiting = "advance"

    @staticmethod
    def _scaffold_analysis(task_content: Mapping[str, Any]) -> str:
        """脚手架直通的解析文案：由题面与查询授权确定性生成（不走 LLM）。"""

        question = str(task_content.get("question", "本题"))
        authority = task_content.get("query_authority")
        authority = authority if isinstance(authority, Mapping) else {}
        output_fields = [
            str(item)
            for item in authority.get("output_columns", [])
            if isinstance(item, str)
        ]
        filter_fields = [
            str(item)
            for item in authority.get("filter_columns", [])
            if isinstance(item, str)
        ]
        group_fields = [
            str(item)
            for item in authority.get("group_by_columns", [])
            if isinstance(item, str)
        ]
        parts = [f"题目要求：{question}。"]
        if output_fields:
            parts.append(f"查询需输出 {'、'.join(output_fields)}；")
        if filter_fields:
            parts.append(f"用 {'、'.join(filter_fields)} 限定对象与时间范围；")
        if group_fields:
            parts.append(f"按 {'、'.join(group_fields)} 分组汇总；")
        parts.append("对照上方查询结果逐列核对口径后，再回答提问。")
        # 逐句换行：前端 pre-line 展示，"查询需输出…"等要点各自成行
        return "\n".join(parts)

    def submit_sql(
        self,
        session_id: str,
        sql: str,
        *,
        _proxy: bool = False,
        _scaffold: bool = False,
    ) -> dict[str, Any]:
        """学员提交只读查询；_proxy=True 为闭环五系统代执行（同一代码路径）。

        代执行仅由 data_present 画像任务下发流程内部调用：成功路径的
        sql_result 与提交事件标注 sql_source=system_proxy（与学员提交可
        区分），白名单/契约列/query_authority/超时/行数门禁完全一致。
        """
        session = self._get_session(session_id)
        # data_present 延时代执行：任务下发时 awaiting="advance"（学员先读
        # 讲义），S7+advance 分支以 _proxy=True 调用本方法代执行标准查询。
        # 代执行仅由内部流程发起，放行 advance 态；学员手动提交仍须 awaiting="sql"。
        allowed_awaiting = {"sql", "advance"} if _proxy else {"sql"}
        if session.awaiting not in allowed_awaiting or session.active_task is None:
            raise InteractiveSessionError("当前步骤不能提交数据查询。")
        task_content = _payload_content(session.active_task)
        question = str(task_content.get("question", "数据实操"))
        family = str(task_content.get("family", ""))
        query_authority = task_content.get("query_authority")
        query_authority = (
            dict(query_authority)
            if isinstance(query_authority, Mapping)
            else None
        )
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
            if query_authority is not None:
                try:
                    authority_issue = query_authority_issue(
                        str(sql),
                        query_authority_from_mapping(query_authority),
                    )
                except ValueError as exc:
                    authority_issue = str(exc)
                if authority_issue is not None:
                    return self._record_template_authority_rejection(
                        session,
                        question=question,
                        family=family,
                        sql=str(sql),
                        query_authority=query_authority,
                        reason=authority_issue,
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
                        "sql_source": "system_proxy" if _proxy else "student",
                        "columns": list(result.columns),
                        "rows": normalized_rows,
                        "row_count": len(normalized_rows),
                        "query_elapsed_ms": result.elapsed_ms,
                        **(
                            {"query_authority": query_authority}
                            if query_authority is not None
                            else {}
                        ),
                        # 0819：五连错脚手架直通——结果携带标答与解析供前端展示
                        # （仅 _scaffold 路径；画像三 data_present 的 _proxy 不带，
                        # 该画像本就不写 SQL，无需标答）。
                        **(
                            {
                                "scaffold": {
                                    "standard_sql": str(sql),
                                    "analysis": self._scaffold_analysis(
                                        task_content
                                    ),
                                }
                            }
                            if _scaffold
                            else {}
                        ),
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
            session.sql_failure_count = 0
            session.sql_support = None
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

    def _record_template_authority_rejection(
        self,
        session: _InteractiveSession,
        *,
        question: str,
        family: str,
        sql: str,
        query_authority: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Return an immutable learner SQL draft for correction, not regeneration.

        The ordinary Review loop can regenerate model-authored resources.  A
        learner submission is immutable, so repeatedly reviewing the same SQL
        cannot repair a missing task filter.  The existing T21 verification
        failure route returns control to S7 without changing the state graph or
        weakening any Review rule.
        """

        failure = self._record_verification_failure(
            session,
            {
                "trace_id": session.runtime.options.trace_id,
                "agent": "verification",
                "role": "produce",
                "payload": {
                    "type": "sql_result",
                    "content": {
                        "event": "template_authority_rejected",
                        "question": question,
                        "family": family,
                        "generated_sql": sql,
                        "sql_source": "student",
                        "query_authority": dict(query_authority),
                        "student_message": (
                            "查询结构与本题目标尚未完全对应，请核对题目指定的"
                            "对象、月份、筛选条件和分组维度后重试。"
                        ),
                    },
                },
                "evidence": [
                    {
                        "kind": "review_rule",
                        "ref": "QUERY_AUTHORITY",
                        "quote": reason,
                    }
                ],
                "claims": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        session.artifact = failure
        self._register_sql_failure(
            session,
            student_message=str(
                _payload_content(failure).get("student_message")
                or "查询结构与本题目标尚未完全对应，请修改后重试。"
            ),
        )
        return self.get_state(session.session_id)

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
        self._register_sql_failure(
            session,
            student_message=str(
                _payload_content(failure).get("student_message")
                or "查询未通过校验，请修改后重试。"
            ),
        )
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
        # Empty results are attributable to the learner's query conditions and
        # can therefore advance the teaching-support ladder.  Timeouts and
        # execution failures represent external availability problems; they
        # must never lower a learner's assessed difficulty.
        if event == "query_empty":
            self._register_sql_failure(
                session,
                student_message=student_message,
            )
        return self.get_state(session.session_id)

    def _register_sql_failure(
        self,
        session: _InteractiveSession,
        *,
        student_message: str,
    ) -> None:
        session.sql_failure_count += 1
        attempt = session.sql_failure_count
        authority = _payload_content(session.active_task or {}).get(
            "query_authority"
        )
        authority = authority if isinstance(authority, Mapping) else {}
        output_fields = [
            str(item)
            for item in authority.get("output_columns", [])
            if isinstance(item, str)
        ]
        filter_fields = [
            str(item)
            for item in authority.get("filter_columns", [])
            if isinstance(item, str)
        ]
        group_fields = [
            str(item)
            for item in authority.get("group_by_columns", [])
            if isinstance(item, str)
        ]
        if attempt <= 2:
            level = "self_correction"
            hint = student_message
        elif attempt == 3:
            level = "structured_hint"
            hint = (
                "先缩小排查范围：核对输出字段、筛选条件和分组维度。"
                f"本题输出字段为 {', '.join(output_fields) or '题目要求的指标'}；"
                f"筛选字段为 {', '.join(filter_fields) or '题目中的对象与时间'}；"
                f"分组字段为 {', '.join(group_fields) or '无需额外分组'}。"
            )
        elif attempt == 4:
            level = "partial_template"
            select_shape = ", ".join(
                f"<{field}的字段或计算>" for field in output_fields
            ) or "<任务要求的字段或聚合>"
            filter_shape = " AND ".join(
                f"<{field}条件>" for field in filter_fields
            ) or "<对象与时间条件>"
            group_shape = (
                f"\nGROUP BY {', '.join(f'<{field}>' for field in group_fields)}"
                if group_fields
                else ""
            )
            hint = (
                "可按这个框架补全：\n"
                f"SELECT {select_shape}\n"
                "FROM <任务授权表>\n"
                f"WHERE {filter_shape}{group_shape}。"
            )
        else:
            level = "scaffold_direct"
            hint = (
                "已连续五次未通过，系统将代为执行标准查询，"
                "带你直接核对数据结论。"
            )
        session.sql_support = {
            "attempt": attempt,
            "level": level,
            "hint": hint,
            "will_step_down": False,
        }
        if attempt < 5:
            return
        # 0819 bug6（用户既定方案）：连续五次失误不降档——脚手架直通：
        # 系统代执行标准查询（白名单/只读/超时门禁照走），学员直接进入
        # 数据核对。此前 T17 降档补学会触发整段微课重生成（60~90s LLM），
        # 前端长时间停在"正在进行查询"，被卡死无法继续。仅当代执行不可用
        # （模板缺失）才回退旧的降档补学路径。
        template_id = str(
            _payload_content(session.active_task or {}).get("template_id") or ""
        )
        standard_sql = ""
        if template_id.strip():
            try:
                entry = session.runtime.task._catalog.templates.get(template_id)
                if entry is not None:
                    standard_sql = str(entry.standard_sql)
            except Exception:
                standard_sql = ""
        if isinstance(standard_sql, str) and standard_sql.strip():
            return self.submit_sql(
                session.session_id,
                standard_sql,
                _scaffold=True,
                _proxy=True,
            )
        runtime = session.runtime
        try:
            if runtime.engine.state is State.S7_STUDENT:
                runtime.transition(
                    self._student_answer_draft(
                        session,
                        answer_result="wrong",
                    ),
                    "T15",
                )
            if runtime.engine.state is not State.S8_PROBE:
                raise InteractiveSessionError("当前实操补学进度无法继续。")
            sql_preview = self._preview_t17_remediation(session)
            runtime.transition(
                self._probe_outcome_draft(
                    session,
                    answer_result="wrong",
                    difficulty_action=str(sql_preview["action"]),
                ),
                "T17",
            )
        except DemoSessionError:
            self._finish_system_error(session)
            return
        self._apply_t17_remediation(
            session, feedback=hint, reason="sql", preview=sql_preview
        )

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
        # 优化9：轮询/读状态视为学员在线信号
        session.last_active_at = datetime.now(timezone.utc)
        trace_path = session.runtime.bus.trace_path(
            session.runtime.options.trace_id
        )
        messages = _trace_messages(trace_path)
        events = session.events.after(0) if session.events is not None else []
        current_difficulty = None
        for product in (session.active_task, session.learning_task):
            value = _payload_content(product or {}).get("difficulty")
            if value in {"basic", "applied", "advanced"}:
                current_difficulty = str(value)
                break
        if current_difficulty is None and session.learning_contract is not None:
            current_difficulty = session.learning_contract.difficulty
        if current_difficulty is None:
            diagnosed = _payload_content(session.diagnosis or {}).get("difficulty")
            if diagnosed in {"basic", "applied", "advanced"}:
                current_difficulty = str(diagnosed)
        return {
            "session_id": session.session_id,
            "trace_id": session.runtime.options.trace_id,
            "trace_path": str(trace_path),
            "state": session.runtime.engine.state.value,
            "awaiting": session.awaiting,
            "outcome": session.outcome,
            "termination": session.termination,
            "mode": session.runtime.mode,
            "profile": dict(session.runtime.profile),
            "current_difficulty": current_difficulty,
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
            "training_report": session.training_report,
            "correction_status": {
                "open_count": sum(
                    1
                    for ticket in session.correction_tickets
                    if ticket.get("status") == "open"
                ),
                "resolved_count": sum(
                    1
                    for ticket in session.correction_tickets
                    if bool(ticket.get("resolved"))
                ),
                "total_count": len(session.correction_tickets),
            },
            "remediation_status": {
                "unresolved_misconceptions": sorted(
                    session.unresolved_misconceptions
                ),
                "attempts": dict(session.remediation_attempts),
                "deferred_knowledge_points": list(
                    session.deferred_knowledge_points
                ),
                "context": (
                    dict(session.remediation_context)
                    if session.remediation_context is not None
                    else None
                ),
            },
            "sql_support": (
                dict(session.sql_support)
                if session.sql_support is not None
                else None
            ),
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

    def _start_follow_up(
        self,
        session: _InteractiveSession,
        task: Mapping[str, Any],
    ) -> None:
        question = self._personalized_initial_question(session, task)
        if not question or contains_engineering_text(question):
            raise InteractiveSessionError(
                "当前理解核对内容不完整，请稍后重试。"
            )
        session.follow_up_round = 1
        session.follow_up_layer = 1
        session.follow_up_question = question
        session.cross_phase_questions = list(
            dict.fromkeys(
                [
                    *session.cross_phase_questions,
                    question,
                ]
            )
        )
        session.follow_up_turns.clear()
        session.follow_up_target = None
        session.unresolved_misconceptions.clear()
        session.probed_misconceptions.clear()
        session.covered_relation_points.clear()
        session.follow_up_had_support = False
        session.generic_fallback_used = False
        session.processed_turn_ids.clear()
        session.interaction = InteractiveSessionManager._follow_up_interaction(
            session
        )
        session.awaiting = "follow_up"

    def _personalized_initial_question(
        self,
        session: _InteractiveSession,
        task: Mapping[str, Any],
    ) -> str:
        """闭环六：第 1 轮追问个性化变式；失败/未开画像路由时回退族模板。"""

        fallback = _initial_follow_up_question(task)
        if not self._persona_routing:
            return fallback
        try:
            question = session.follow_up_agent.generate_initial(
                current_task=task,
                learner_context=_learner_context(session),
                lecture_digest=_lecture_digest(session),
                data_digest=_data_digest(session),
                previous_questions=tuple(session.cross_phase_questions),
            )
        except Exception:
            return fallback
        if not question or contains_engineering_text(question):
            return fallback
        return question

    @staticmethod
    def _follow_up_interaction(
        session: _InteractiveSession,
    ) -> dict[str, Any]:
        return {
            "kind": "free_text_follow_up",
            **_follow_up_task_context(session.learning_task or session.active_task),
            "prompt": session.follow_up_question,
            "round": session.follow_up_round,
            "max_rounds": MAX_FOLLOW_UP_ROUNDS,
            "follow_up_layer": session.follow_up_layer,
            "layer_name": FOLLOW_UP_LAYER_NAMES.get(
                session.follow_up_layer, "口径记忆"
            ),
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

    def list_learning_records(
        self,
        *,
        user_id: str | None = None,
        profile_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """0818 需求 5：学员学习记录查询（未启用记录时返回空表）。"""

        if self._learning_records is None:
            return []
        return self._learning_records.list_records(
            user_id=user_id,
            profile_id=profile_id,
        )

    def summarize_learning_records(self) -> list[dict[str, Any]]:
        """0818 需求 6：画像维度汇总（误区频次/平均档位等，供针对性引导迭代）。"""

        if self._learning_records is None:
            return []
        return self._learning_records.summarize_by_profile()


class _InteractiveRequestHandler(BaseHTTPRequestHandler):
    manager: InteractiveSessionManager
    auth_handler: AuthHttpHandler | None = None
    max_body_bytes = 1_000_000
    protocol_version = "HTTP/1.1"

    def _handle_auth(self, method: str) -> bool:
        """Dispatch /api/auth/* to the dedicated auth handler.

        Returns True when the request was handled; the caller continues
        with the session routes otherwise.
        """
        handler = self.auth_handler
        if handler is None:
            return False
        parsed = self.path.partition("?")
        path = parsed[0].rstrip("/") or "/"
        if not path.startswith("/api/auth/"):
            return False
        if method == "POST":
            parsed_body = self._read_json_body()
            body = json.dumps(parsed_body, ensure_ascii=False).encode("utf-8")
        else:
            body = b""
        status, payload = handler.handle(method, path, parsed[2], body)
        self._send_json(status, payload)
        return True

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _resolve_account(self, token: Any) -> dict[str, Any] | None:
        """token → {user_id, username, role}；未登录/无效/未启用鉴权返回 None（游客）。"""

        if not isinstance(token, str) or not token.strip():
            return None
        handler = self.auth_handler
        if handler is None:
            return None
        return handler.account_from_token(token.strip())

    def _account_from_query(self) -> dict[str, Any] | None:
        query = parse_qs(urlsplit(self.path).query)
        return self._resolve_account(query.get("token", [""])[0])

    def do_GET(self) -> None:
        try:
            if self._handle_auth("GET"):
                return
            parts = self._path_parts()
            # 0818 需求 5：学员学习记录（登录账号归属；游客仅返回 guest 标记）
            if parts == ["api", "learning-records"]:
                account = self._account_from_query()
                if account is None:
                    self._send_json(200, {"guest": True, "records": []})
                    return
                records = self.manager.list_learning_records(
                    user_id=str(account.get("user_id", "")),
                )
                self._send_json(
                    200,
                    {"guest": False, "records": records},
                )
                return
            # 0818 需求 6：画像维度汇总（仅 admin）
            if parts == ["api", "learning-records", "summary"]:
                account = self._account_from_query()
                if account is None or account.get("role") != "admin":
                    self._send_json(403, {"error": "仅管理员可查看画像学习总览。"})
                    return
                self._send_json(
                    200,
                    {"profiles": self.manager.summarize_learning_records()},
                )
                return
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
            if (
                len(parts) == 4
                and parts[:2] == ["api", "sessions"]
                and parts[3] == "diagnostic-probes"
            ):
                self._send_json(
                    200,
                    {"questions": self.manager.get_diagnostic_probes(parts[2])},
                )
                return
            self._send_json(404, {"error": "未找到请求的交互接口。"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except AuthHttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
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
            if self._handle_auth("POST"):
                return
            parts = self._path_parts()
            body = self._read_json_body()
            if parts == ["api", "sessions"]:
                profile_id = body.get("profile_id")
                if not isinstance(profile_id, str):
                    raise ValueError("请选择有效岗位画像。")
                experience_tags = body.get("experience_tags", [])
                if not isinstance(experience_tags, list) or not all(
                    isinstance(item, str) for item in experience_tags
                ):
                    raise ValueError("岗位画像经历标签格式无效。")
                # 0818 需求 5/6：携带登录 token 创建会话→学习记录归属该账号；
                # 未登录/校验失败按游客（account=None，仅当次可见）。
                account = self._resolve_account(body.get("auth_token"))
                self._send_json(
                    201,
                    self.manager.create_session(
                        profile_id,
                        experience_tags=experience_tags,
                        account=account,
                    ),
                )
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
            if (
                len(parts) == 4
                and parts[:2] == ["api", "sessions"]
                and parts[3] == "diagnostic-probes"
            ):
                answers = body.get("answers")
                if not isinstance(answers, dict):
                    raise ValueError("请完成当前补充诊断题目。")
                self._send_json(
                    200,
                    self.manager.submit_diagnostic_probes(parts[2], answers),
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
        except AuthHttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except InteractiveSessionError as exc:
            self._send_json(409, {"error": str(exc)})
        except DemoSessionError:
            self._send_json(
                409,
                {
                    "error": _REVIEW_STOP_COPY["system_error"],
                    "outcome": "system_error",
                },
            )
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
    # Auth routes are optional at startup: a missing REF_AUTH_PASSWORD only
    # disables login/registration, the training channel keeps working.
    auth_handler: AuthHttpHandler | None = None
    try:
        from orchestrator.auth_store import AuthStore

        store = AuthStore()
        store.ensure_schema()
        if not store.ensure_admin(os.environ.get("ADMIN_INITIAL_PASSWORD")):
            logging.getLogger(__name__).warning(
                "ADMIN_INITIAL_PASSWORD 未设置或过短：管理员登录不可用。"
            )
        auth_handler = AuthHttpHandler(store)
    except Exception:  # noqa: BLE001 — startup must not fail on auth config
        logging.getLogger(__name__).warning(
            "登录注册模块未启用（数据库账号或表初始化失败）。", exc_info=True
        )
        auth_handler = None
    if auth_handler is not None:
        # 优化9：在线学员会话=未结束 且 10 分钟内有过活动（轮询/推进都会刷新
        # last_active_at）。此前只按 awaiting!=done 统计，历史遗留的未完成会话
        # （学员关掉浏览器即弃）会永久计入，出现"在线学员会话为15"的虚高。
        online_window = timedelta(minutes=10)

        def _count_online_sessions() -> int:
            now = datetime.now(timezone.utc)
            return sum(
                1
                for value in manager._sessions.values()
                if value.awaiting != "done"
                and now - value.last_active_at <= online_window
            )

        auth_handler.sessions_online = _count_online_sessions
    Handler.auth_handler = auth_handler
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
    # 0818 需求 5/6：学习记录落盘目录（容器内 /app/runtime 为持久卷，重建不丢）
    parser.add_argument(
        "--records-dir",
        type=Path,
        default=root / "runtime" / "learning_records",
    )
    args = parser.parse_args(argv)
    manager = InteractiveSessionManager(
        trace_dir=args.trace_dir,
        cache_dir=args.cache_dir,
        persona_routing=True,  # 闭环二：生产走画像领域路由 v4；测试默认 v3 冻结通道
        records_dir=args.records_dir,
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
