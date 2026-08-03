"""Evaluation-only batch driver for the frozen five-agent system."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import traceback
from typing import Any

from agents.diagnosis_agent import DiagnosisAgent, load_profiles
from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.knowledge_agent import KnowledgeAgent
from agents.rebuttal_generator import RebuttalGenerator
from agents.retriever import BM25Retriever
from agents.review_agent import ReviewAgent, evaluate_hard_rules
from agents.sandbox import DatabaseSettings, ReadOnlyExecutor
from agents.task_agent import TaskAgent, TaskCatalog, load_task_catalog
from agents.verification_agent import VerificationAgent
from coordination.contracts import LearningContract
from coordination.parallel import bounded_llm_executor
from eval.case_matrix import EvaluationCase, load_case_matrix
from eval.trace_dataset import (
    AttemptRecord,
    canonical_json,
    load_attempt_records,
    write_attempt_record,
)
from orchestrator.bus import MessageBus
from orchestrator.demo_cache import DemoLLMCache
from orchestrator.demo_session import DemoSessionError, evidence_projection_lecture
from orchestrator.engine import OrchestratorEngine
from orchestrator.llm import LLMResult, call_llm
from orchestrator.outcomes import Outcome, OutcomeError, verification_outcome
from orchestrator.transitions import State


ROOT = Path(__file__).resolve().parents[1]
FROZEN_BASE_COMMIT = "release-1.1.0"
MAX_LECTURE_ATTEMPTS = 3
MAX_NEW_ATTEMPTS_PER_CASE = 3
DIFFICULTY_ORDER = ("basic", "applied", "advanced")


class EvaluationRunError(RuntimeError):
    """Raised when a frozen-system case cannot complete honestly."""


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path
    trace_dir: Path
    cache_dir: Path
    attempts_dir: Path
    ledger_path: Path

    @classmethod
    def under(
        cls, root: Path, results_dir: Path | None = None
    ) -> "RunPaths":
        root = Path(root).resolve()
        if results_dir is None:
            results = root / "eval" / "results"
        else:
            candidate = Path(results_dir)
            results = (
                candidate.resolve()
                if candidate.is_absolute()
                else (root / candidate).resolve()
            )
            if results != root and root not in results.parents:
                raise ValueError("results_dir must stay inside repository root")
        return cls(
            root=root,
            trace_dir=results / "traces",
            cache_dir=results / "cache",
            attempts_dir=results / "attempts",
            ledger_path=results / "run_ledger.jsonl",
        )


def _default_executor_factory() -> ReadOnlyExecutor:
    return ReadOnlyExecutor(DatabaseSettings.from_environment())


@dataclass(frozen=True, slots=True)
class RunnerDependencies:
    llm_call: Callable[..., LLMResult] = call_llm
    executor_factory: Callable[[], Any] = _default_executor_factory
    mode: str = "live"
    base_commit: str = FROZEN_BASE_COMMIT
    verify_frozen_tree: bool = True


@dataclass(frozen=True, slots=True)
class CaseRunResult:
    case_id: str
    attempt: int
    trace_id: str
    trace_path: Path
    cache_path: Path
    state_sequence: tuple[str, ...]
    transition_sequence: tuple[str, ...]
    terminal_state: str
    message_count: int
    llm_calls: int
    cache_hits: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    value = payload.get("content") if isinstance(payload, Mapping) else None
    return value if isinstance(value, Mapping) else {}


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


def _query_id_factory(trace_id: str) -> Callable[[], str]:
    current = 0

    def next_id() -> str:
        nonlocal current
        current += 1
        return f"{trace_id}-sql-{current:02d}"

    return next_id


def _trace_messages(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return tuple(json.loads(line) for line in lines if line.strip())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationRunError(f"cannot read trace {path}: {exc}") from exc


def _transition_sequence(messages: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    transitions: list[str] = []
    for message in messages:
        if message.get("role") != "system" or message.get("rejected_by_bus"):
            continue
        transition_id = _content(message).get("transition_id")
        if isinstance(transition_id, str):
            transitions.append(transition_id)
    return tuple(transitions)


def _assert_frozen_tree(root: Path, base_commit: str) -> None:
    if not isinstance(base_commit, str) or not base_commit.strip():
        raise ValueError("base_commit must be a non-empty revision")
    command = [
        "git",
        "diff",
        "--quiet",
        base_commit,
        "--",
        "agents",
        "orchestrator",
        "frontend",
    ]
    diff = subprocess.run(command, cwd=root, check=False)
    if diff.returncode != 0:
        raise EvaluationRunError(
            "frozen agents/orchestrator/frontend differ from base "
            f"{base_commit}"
        )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "agents", "orchestrator", "frontend"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise EvaluationRunError("frozen paths contain uncommitted changes")


def _chunk_and_catalog() -> tuple[tuple[KnowledgeChunk, ...], TaskCatalog]:
    chunks = require_valid_chunks(ROOT / "agents" / "knowledge_base" / "chunks")
    return chunks, load_task_catalog()


def derive_step_down_target(knowledge_point: str) -> tuple[str, str]:
    """Choose an existing lower foundation by chunk prerequisites and task assets."""

    chunks, catalog = _chunk_and_catalog()
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    point_chunks = [
        chunk for chunk in chunks if chunk.knowledge_point == knowledge_point
    ]
    if not point_chunks:
        raise ValueError(f"unknown knowledge point: {knowledge_point}")
    source = min(
        point_chunks,
        key=lambda chunk: (
            DIFFICULTY_ORDER.index(chunk.difficulty),
            chunk.chunk_id,
        ),
    )
    candidates = [by_id[item] for item in source.prerequisites if item in by_id]
    if candidates:
        indexed = {chunk.chunk_id: index for index, chunk in enumerate(candidates)}
        target = min(
            candidates,
            key=lambda chunk: (
                DIFFICULTY_ORDER.index(chunk.difficulty),
                indexed[chunk.chunk_id],
            ),
        )
    else:
        target = source
    templates = [
        item
        for item in catalog.templates.values()
        if item.knowledge_point == target.knowledge_point
    ]
    if not templates:
        raise EvaluationRunError(
            f"no approved task template for step-down point {target.knowledge_point}"
        )
    template = min(
        templates,
        key=lambda item: (DIFFICULTY_ORDER.index(item.difficulty), item.template_id),
    )
    return target.knowledge_point, template.template_id


class _EvaluationRuntime:
    def __init__(
        self,
        case: EvaluationCase,
        trace_id: str,
        paths: RunPaths,
        dependencies: RunnerDependencies,
    ) -> None:
        self.case = case
        self.trace_id = trace_id
        self.paths = paths
        self.dependencies = dependencies
        self.cache = DemoLLMCache(
            trace_id,
            paths.cache_dir,
            dependencies.mode,
            dependencies.llm_call,
        )
        self.chunks, self.catalog = _chunk_and_catalog()
        self.knowledge_dimensions = tuple(
            dict.fromkeys(chunk.knowledge_point for chunk in self.chunks)
        )
        self.profile = load_profiles()[case.profile_id]
        self.bus = MessageBus(paths.trace_dir)
        self.engine = OrchestratorEngine(self.bus, trace_id, case.profile_id)
        # Match the approved P4/P5 complete-engine wiring: these two agents keep
        # their deterministic core path, while knowledge/verification/review use
        # the live audited cache below.
        self.diagnosis = DiagnosisAgent(trace_id)
        self.retriever = BM25Retriever(self.chunks)
        self.knowledge = KnowledgeAgent(trace_id, self.retriever, llm_call=self.cache)
        self.task = TaskAgent(trace_id, catalog=self.catalog, llm_call=self.cache)
        self.verification = VerificationAgent(
            trace_id,
            llm_call=self.cache,
            executor=dependencies.executor_factory(),
            query_id_factory=_query_id_factory(trace_id),
        )
        self.review = ReviewAgent(
            trace_id,
            llm_call=self.cache,
            knowledge_chunks=self.chunks,
            parallel_executor=bounded_llm_executor(
                self.cache,
                max_concurrency=3,
            ),
        )
        self.rebuttal = RebuttalGenerator(trace_id, llm_call=self.cache)
        self.learning_contract: LearningContract | None = None
        self.learned_knowledge_points: list[str] = []
        self._approved_review_decisions: dict[str, str] = {}

    def _remember_approved_coverage(
        self,
        product: Mapping[str, Any],
        decision: str,
    ) -> None:
        if decision != "approve":
            return
        for item in self.review.grounded_coverage(product):
            if item not in self.learned_knowledge_points:
                self.learned_knowledge_points.append(item)

    def _record_approved_decision(
        self,
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> None:
        msg_id = product.get("msg_id")
        verdict_data = verdict.get("verdict")
        decision = (
            verdict_data.get("decision")
            if isinstance(verdict_data, Mapping)
            else None
        )
        if (
            not isinstance(msg_id, str)
            or decision not in {"approve", "approve_with_fix"}
        ):
            raise EvaluationRunError("approved transition has no valid decision")
        self._approved_review_decisions[msg_id] = str(decision)

    def _prepare(self, draft: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.dependencies.mode != "cached" or "model" not in draft:
            return draft
        prepared = deepcopy(dict(draft))
        payload = prepared.get("payload")
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, dict):
            content["cached"] = True
        return prepared

    def _send_transition(self, draft: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        result = self.engine.send(self._prepare(draft))
        if not result.bus_result.accepted:
            raise EvaluationRunError(
                f"message rejected by bus: {result.bus_result.errors}"
            )
        if not result.transitioned or result.transition is None:
            raise EvaluationRunError(
                f"expected transition at {result.from_state.value}: {result.reason}"
            )
        return result.bus_result.message, result.transition.transition_id

    def transition(self, draft: Mapping[str, Any], transition_id: str) -> dict[str, Any]:
        message, actual = self._send_transition(draft)
        if actual != transition_id:
            raise EvaluationRunError(f"expected {transition_id}, got {actual}")
        return message

    def audit(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        before = self.engine.state
        result = self.engine.send(self._prepare(draft))
        if not result.bus_result.accepted:
            raise EvaluationRunError(f"audit rejected: {result.bus_result.errors}")
        if result.transitioned or self.engine.state is not before:
            actual = result.transition.transition_id if result.transition else "unknown"
            raise EvaluationRunError(f"audit unexpectedly triggered {actual}")
        return result.bus_result.message

    def _learning_summary(
        self,
        diagnosis: Mapping[str, Any],
        knowledge_point: str,
    ) -> str:
        difficulty = _content(diagnosis).get("difficulty")
        return (
            f"{self.profile['title']}当前学习{knowledge_point}，"
            f"岗前测评难度档为{difficulty}。"
        )

    def _lecture_draft(
        self,
        knowledge_point: str,
        diagnosis: Mapping[str, Any],
    ) -> dict[str, Any]:
        diagnosis_content = _content(diagnosis)
        requested_difficulty = str(diagnosis_content.get("difficulty"))
        blind_spots = diagnosis_content.get("blind_spots")
        keywords = tuple(str(item) for item in blind_spots[:3]) if isinstance(blind_spots, list) else ()
        fallback = not self.retriever.retrieve(
            knowledge_point,
            requested_difficulty,
            keywords,
        )
        generation_difficulty = None if fallback else requested_difficulty
        last_hits: tuple[dict[str, str], ...] = ()
        last_draft: dict[str, Any] | None = None
        for _ in range(MAX_LECTURE_ATTEMPTS):
            draft = self.knowledge.generate(
                knowledge_point=knowledge_point,
                student_profile=self.profile,
                learning_report_summary=self._learning_summary(
                    diagnosis, knowledge_point
                ),
                keywords=keywords,
                difficulty=generation_difficulty,
            )
            if _content(draft).get("event") == "knowledge_refused":
                self.audit(draft)
                raise EvaluationRunError(
                    f"knowledge refused: {_content(draft).get('refuse_reason')}"
                )
            if fallback:
                draft = deepcopy(draft)
                content = draft["payload"]["content"]
                content["difficulty_fallback"] = True
            last_draft = draft
            hard_hits = evaluate_hard_rules(draft)
            if hard_hits:
                last_hits = hard_hits
                continue
            semantic_hit = self.review.preflight_r04(draft)
            if semantic_hit is None:
                return draft
            last_hits = (semantic_hit,)
        if last_draft is not None:
            try:
                projected = evidence_projection_lecture(last_draft, last_hits)
            except DemoSessionError:
                projected = None
            if projected is not None:
                projected_hard_hits = evaluate_hard_rules(projected)
                projected_semantic_hit = self.review.preflight_r04(projected)
                if not projected_hard_hits and projected_semantic_hit is None:
                    return projected
        labels = ",".join(hit["rule_id"] for hit in last_hits)
        raise EvaluationRunError(
            f"lecture failed review preflight after {MAX_LECTURE_ATTEMPTS}: {labels}"
        )

    def _review_call(
        self,
        product: Mapping[str, Any],
        diagnosis: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload_type = product.get("payload", {}).get("type")
        if payload_type in {"lecture_note", "quiz_set", "practice_guide"}:
            return self.review.review(
                product,
                learning_report=diagnosis,
                student_profile=self.profile,
                learned_knowledge_points=tuple(self.learned_knowledge_points),
                learning_contract=self.learning_contract,
            )
        return self.review.review(
            product,
            learning_contract=self.learning_contract,
        )

    def _re_review_call(
        self,
        product: Mapping[str, Any],
        original: Mapping[str, Any],
        rebuttal: Mapping[str, Any],
        diagnosis: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload_type = product.get("payload", {}).get("type")
        if payload_type in {"lecture_note", "quiz_set", "practice_guide"}:
            return self.review.re_review(
                product,
                original,
                rebuttal,
                learning_report=diagnosis,
                student_profile=self.profile,
                learned_knowledge_points=tuple(self.learned_knowledge_points),
                learning_contract=self.learning_contract,
            )
        return self.review.re_review(
            product,
            original,
            rebuttal,
            learning_contract=self.learning_contract,
        )

    def produce_and_review(
        self,
        producer: Callable[[], dict[str, Any]],
        produced_transition: str,
        approved_transition: str,
        diagnosis: Mapping[str, Any],
    ) -> dict[str, Any]:
        cycles = 0
        max_cycles = (
            self.learning_contract.quality_policy.max_review_cycles
            if self.learning_contract is not None
            else 4
        )
        while cycles < max_cycles:
            cycles += 1
            product, actual_transition = self._send_transition(producer())
            if (
                produced_transition == "T12"
                and actual_transition == "T21"
            ):
                event = str(_content(product).get("event", ""))
                outcome = verification_outcome(event)
                if outcome is None:
                    raise OutcomeError(
                        Outcome.SYSTEM_ERROR,
                        event="unknown_verification_failure",
                    )
                raise OutcomeError(outcome, event=event)
            if actual_transition != produced_transition:
                raise EvaluationRunError(
                    f"expected {produced_transition}, got {actual_transition}"
                )
            verdict = self._review_call(product, diagnosis)
            original, transition_id = self._send_transition(verdict)
            if transition_id == approved_transition:
                self._record_approved_decision(product, original)
                return product
            if transition_id == "T08":
                raise EvaluationRunError("review retries exhausted into T08")
            if transition_id != "T05":
                raise EvaluationRunError(
                    f"review reject expected T05, got {transition_id}"
                )
            rebuttal = self.audit(self.rebuttal.generate(product, original))
            re_verdict = self._re_review_call(
                product,
                original,
                rebuttal,
                diagnosis,
            )
            re_message, re_transition = self._send_transition(re_verdict)
            if re_transition == "T06":
                self._record_approved_decision(product, re_message)
                return product
            if re_transition == "T08":
                raise EvaluationRunError("re-review retries exhausted into T08")
            if re_transition != "T07":
                raise EvaluationRunError(
                    f"re-review reject expected T07, got {re_transition}"
                )
        raise EvaluationRunError("review regeneration cycle exceeded safety bound")

    def load_and_diagnose(self) -> dict[str, Any]:
        self.transition(
            _system_draft(
                self.trace_id,
                "control",
                {
                    "action": "profile_loaded",
                    "profile": dict(self.profile),
                    "knowledge_dimensions": list(self.knowledge_dimensions),
                    "training_scene": "船厂数字化岗位培训",
                    "assessment_label": "岗前测评",
                },
                student_profile_ref=self.case.profile_id,
            ),
            "T01",
        )
        diagnosis = self.transition(
            self.diagnosis.assess(self.case.profile_id, self.case.answers),
            "T02",
        )
        self.learning_contract = LearningContract.from_diagnosis(
            profile=self.profile,
            diagnosis=diagnosis,
            domain_id=self.catalog.domain_id,
            domain_package_sha256=self.catalog.domain_package_sha256,
        )
        self.audit(self.learning_contract.control_draft(self.trace_id))
        return diagnosis

    def teach(
        self,
        knowledge_point: str,
        diagnosis: Mapping[str, Any],
    ) -> dict[str, Any]:
        lecture = self.produce_and_review(
            lambda: self._lecture_draft(knowledge_point, diagnosis),
            "T03",
            "T04",
            diagnosis,
        )
        lecture_msg_id = lecture.get("msg_id")
        decision = (
            self._approved_review_decisions.get(lecture_msg_id)
            if isinstance(lecture_msg_id, str)
            else None
        )
        if decision is None:
            raise EvaluationRunError("approved lecture decision was not recorded")
        self._remember_approved_coverage(lecture, decision)
        return lecture

    def task_and_review(
        self,
        template_id: str,
        diagnosis: Mapping[str, Any],
    ) -> dict[str, Any]:
        diagnostic_difficulty = _content(diagnosis).get("difficulty")
        if not isinstance(diagnostic_difficulty, str):
            raise EvaluationRunError("diagnosis has no difficulty")
        return self.produce_and_review(
            lambda: self.task.generate(
                template_id,
                diagnostic_difficulty=diagnostic_difficulty,
                student_profile=self.profile,
                learning_report_summary=self._learning_summary(
                    diagnosis,
                    self.catalog.templates[template_id].knowledge_point,
                ),
            ),
            "T09",
            "T10",
            diagnosis,
        )

    def sql_and_review(
        self,
        task: Mapping[str, Any],
        diagnosis: Mapping[str, Any],
    ) -> dict[str, Any]:
        question = _content(task).get("question")
        if not isinstance(question, str) or not question.strip():
            raise EvaluationRunError("task has no question")
        query_authority = _content(task).get("query_authority")
        if not isinstance(query_authority, Mapping):
            raise EvaluationRunError("task has no query authority")
        self.transition(
            _system_draft(
                self.trace_id,
                "control",
                {
                    "event": "student_sql_submitted",
                    "source": "student",
                    "question": question,
                },
            ),
            "T11",
        )
        return self.produce_and_review(
            lambda: self.verification.answer(
                question,
                query_authority=query_authority,
            ),
            "T12",
            "T13",
            diagnosis,
        )

    def path_update(
        self,
        *,
        has_next: bool,
        achieved: bool,
        content: Mapping[str, Any],
        transition_id: str,
    ) -> dict[str, Any]:
        return self.transition(
            _system_draft(
                self.trace_id,
                "learning_path_update",
                {
                    "event": "path_updated",
                    "has_next": has_next,
                    "learning_goal_achieved": achieved,
                    **dict(content),
                },
            ),
            transition_id,
        )

    def _student_answer(self, result: str, transition_id: str) -> None:
        content: dict[str, Any] = {
            "event": "student_answer",
            "answer_result": result,
            "source": "student",
        }
        if result == "wrong":
            content["requested_action"] = "probe"
        self.transition(_system_draft(self.trace_id, "control", content), transition_id)

    def _counter_evidence(
        self,
        diagnosis: Mapping[str, Any],
        corrected: bool,
    ) -> None:
        misconception = self.case.misconception_id
        if misconception is None:
            raise EvaluationRunError("probe path has no misconception")
        counter = self.audit(
            self.task.counter_evidence(
                misconception,
                student_profile=self.profile,
                learning_report_summary=self._learning_summary(
                    diagnosis,
                    self.case.knowledge_point,
                ),
            )
        )
        question = _content(counter).get("question")
        if not isinstance(question, str) or not question.strip():
            raise EvaluationRunError("counter-evidence task has no question")
        self.audit(
            _system_draft(
                self.trace_id,
                "control",
                {
                    "event": "student_sql_submitted",
                    "source": "student",
                    "question": question,
                },
            )
        )
        result = self.audit(self.verification.answer(question))
        verdict = self.audit(
            self.review.review(
                result,
                learning_contract=self.learning_contract,
            )
        )
        if verdict.get("verdict", {}).get("decision") not in {
            "approve",
            "approve_with_fix",
        }:
            raise EvaluationRunError("counter-evidence SQL result failed review")
        outcome = "correct" if corrected else "wrong"
        self.transition(
            _system_draft(
                self.trace_id,
                "control",
                {
                    "event": "probe_outcome",
                    "answer_result": outcome,
                    "target_misconception": misconception,
                    "source": "student",
                },
                probe={
                    "wrong_attempts": 1,
                    "target_misconception": misconception,
                },
            ),
            "T16" if corrected else "T17",
        )


def _run_flow(runtime: _EvaluationRuntime) -> None:
    case = runtime.case
    diagnosis = runtime.load_and_diagnose()
    runtime.teach(case.knowledge_point, diagnosis)
    first_task = runtime.task_and_review(case.task_template_id, diagnosis)
    runtime.sql_and_review(first_task, diagnosis)
    runtime.path_update(
        has_next=True,
        achieved=False,
        content={
            "completed_nodes": ["岗前测评", "岗位微课", "数据实操"],
            "current_node": "结论判断",
            "difficulty_action": "keep",
            "case_id": case.case_id,
        },
        transition_id="T19",
    )
    runtime.task_and_review(case.task_template_id, diagnosis)

    if case.learning_path == "direct_correct":
        runtime._student_answer("correct", "T14")
        completed_nodes = ["岗前测评", "岗位微课", "数据实操", "直接作答"]
    elif case.learning_path == "rebuttal_corrected":
        runtime._student_answer("wrong", "T15")
        runtime._counter_evidence(diagnosis, corrected=True)
        completed_nodes = ["岗前测评", "岗位微课", "数据实操", "反证追问", "修正结论"]
    elif case.learning_path == "second_wrong_step_down":
        runtime._student_answer("wrong", "T15")
        runtime._counter_evidence(diagnosis, corrected=False)
        point, template_id = derive_step_down_target(case.knowledge_point)
        runtime.teach(point, diagnosis)
        runtime.task_and_review(template_id, diagnosis)
        runtime._student_answer("correct", "T14")
        completed_nodes = [
            "岗前测评",
            "岗位微课",
            "数据实操",
            "反证追问",
            "降维讲解",
            "重新作答",
        ]
    else:
        raise EvaluationRunError(f"unknown learning path: {case.learning_path}")

    runtime.path_update(
        has_next=False,
        achieved=True,
        content={
            "completed_nodes": completed_nodes,
            "current_node": "培养目标达成",
            "difficulty_action": (
                "step_down"
                if case.learning_path == "second_wrong_step_down"
                else "keep"
            ),
            "case_id": case.case_id,
            "knowledge_point": case.knowledge_point,
            "summary": (
                f"{runtime.profile['title']}完成{case.knowledge_point}的真实评测链路，"
                f"路径为{case.learning_path}。"
            ),
        },
        transition_id="T20",
    )


def run_case(
    case: EvaluationCase,
    attempt: int,
    paths: RunPaths,
    dependencies: RunnerDependencies | None = None,
) -> CaseRunResult:
    dependencies = dependencies or RunnerDependencies()
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if dependencies.verify_frozen_tree:
        _assert_frozen_tree(paths.root, dependencies.base_commit)
    trace_id = f"p7-{case.case_id.lower()}-a{attempt:02d}"
    runtime = _EvaluationRuntime(case, trace_id, paths, dependencies)
    _run_flow(runtime)
    if runtime.engine.state is not State.S10_DONE:
        raise EvaluationRunError(
            f"case ended at {runtime.engine.state.value}, expected S10_DONE"
        )
    runtime.cache.assert_exhausted()
    trace_path = runtime.bus.trace_path(trace_id)
    messages = _trace_messages(trace_path)
    transitions = _transition_sequence(messages)
    states = tuple(item.value for item in runtime.engine.state_history)
    if len(states) != len(transitions) + 1:
        raise EvaluationRunError("trace transitions cannot reconstruct state history")
    return CaseRunResult(
        case_id=case.case_id,
        attempt=attempt,
        trace_id=trace_id,
        trace_path=trace_path,
        cache_path=runtime.cache.path,
        state_sequence=states,
        transition_sequence=transitions,
        terminal_state=runtime.engine.state.value,
        message_count=len(messages),
        llm_calls=runtime.cache.calls,
        cache_hits=runtime.cache.cache_hits,
    )


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _archive_file(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if destination.exists():
        raise FileExistsError(f"attempt archive already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)


def execute_case(
    case: EvaluationCase,
    attempt: int,
    paths: RunPaths,
    dependencies: RunnerDependencies | None = None,
) -> AttemptRecord:
    dependencies = dependencies or RunnerDependencies()
    started = _now()
    trace_id = f"p7-{case.case_id.lower()}-a{attempt:02d}"
    try:
        result = run_case(case, attempt, paths, dependencies)
    except Exception as exc:
        outcome = (
            exc.outcome
            if isinstance(exc, OutcomeError)
            else Outcome.SYSTEM_ERROR
        )
        finished = _now()
        attempt_dir = paths.attempts_dir / case.case_id / f"a{attempt:02d}"
        trace_source = paths.trace_dir / f"{trace_id}.jsonl"
        cache_source = paths.cache_dir / f"{trace_id}.jsonl"
        trace_destination = attempt_dir / f"{trace_id}.jsonl"
        cache_destination = attempt_dir / f"{trace_id}.cache.jsonl"
        _archive_file(trace_source, trace_destination)
        _archive_file(cache_source, cache_destination)
        messages = _trace_messages(trace_destination) if trace_destination.exists() else ()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        error_path = attempt_dir / "error.json"
        if error_path.exists():
            raise FileExistsError(f"attempt error record already exists: {error_path}")
        error_path.write_text(
            canonical_json(
                {
                    "case_id": case.case_id,
                    "attempt": attempt,
                    "trace_id": trace_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "outcome": outcome.value,
                    "traceback": traceback.format_exc(),
                    "started_at": started,
                    "finished_at": finished,
                }
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        record = AttemptRecord(
            case_id=case.case_id,
            attempt=attempt,
            trace_id=trace_id,
            status="failed",
            trace_path=_relative(trace_destination, paths.root),
            cache_path=_relative(cache_destination, paths.root),
            base_commit=dependencies.base_commit,
            started_at=started,
            finished_at=finished,
            terminal_state=None,
            transition_sequence=_transition_sequence(messages),
            message_count=len(messages),
            error_type=type(exc).__name__,
            error_message=str(exc),
            outcome=outcome.value,
        )
    else:
        record = AttemptRecord(
            case_id=case.case_id,
            attempt=attempt,
            trace_id=result.trace_id,
            status="succeeded",
            trace_path=_relative(result.trace_path, paths.root),
            cache_path=_relative(result.cache_path, paths.root),
            base_commit=dependencies.base_commit,
            started_at=started,
            finished_at=_now(),
            terminal_state=result.terminal_state,
            transition_sequence=result.transition_sequence,
            message_count=result.message_count,
            error_type=None,
            error_message=None,
            outcome=Outcome.COMPLETED.value,
        )
    write_attempt_record(paths.ledger_path, record)
    return record


def _selected_cases(
    cases: Sequence[EvaluationCase],
    *,
    case_id: str | None,
    start_case: str | None,
    limit: int | None,
) -> tuple[EvaluationCase, ...]:
    selected = list(cases)
    if case_id is not None:
        selected = [case for case in selected if case.case_id == case_id]
        if not selected:
            raise ValueError(f"unknown case_id: {case_id}")
    if start_case is not None:
        indexes = [index for index, case in enumerate(selected) if case.case_id == start_case]
        if not indexes:
            raise ValueError(f"unknown start_case: {start_case}")
        selected = selected[indexes[0] :]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        selected = selected[:limit]
    return tuple(selected)


def _new_attempt_numbers(
    used_attempts: Sequence[int], *, max_new_attempts: int
) -> tuple[int, ...]:
    """Allocate fresh immutable attempt IDs without capping historical retries."""

    if (
        not isinstance(max_new_attempts, int)
        or isinstance(max_new_attempts, bool)
        or max_new_attempts < 1
    ):
        raise ValueError("max_new_attempts must be a positive integer")
    start = max(used_attempts, default=0) + 1
    return tuple(range(start, start + max_new_attempts))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实调用冻结版五智能体生成P7 trace")
    parser.add_argument("--matrix", type=Path, default=ROOT / "eval" / "cases" / "e2e_50_matrix.json")
    parser.add_argument("--case-id")
    parser.add_argument("--start-case")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path("eval/results/live_50"),
        help="正式trace、cache、失败档与不可变账本目录",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_NEW_ATTEMPTS_PER_CASE,
        help="本次启动为每组新增的最大尝试数（历史失败不占本次配额）",
    )
    parser.add_argument(
        "--base-commit",
        default=FROZEN_BASE_COMMIT,
        help="已提交评审候选，用于冻结 agents/orchestrator/frontend 差异检查",
    )
    args = parser.parse_args(argv)
    if args.max_attempts < 1 or args.max_attempts > MAX_NEW_ATTEMPTS_PER_CASE:
        parser.error(f"--max-attempts must be 1..{MAX_NEW_ATTEMPTS_PER_CASE}")
    paths = RunPaths.under(ROOT, args.run_dir)
    dependencies = RunnerDependencies(base_commit=args.base_commit)
    cases = _selected_cases(
        load_case_matrix(args.matrix),
        case_id=args.case_id,
        start_case=args.start_case,
        limit=args.limit,
    )
    failures = 0
    for case in cases:
        records = load_attempt_records(paths.ledger_path)
        if args.resume and any(
            item.case_id == case.case_id and item.status == "succeeded"
            for item in records
        ):
            print(canonical_json({"case_id": case.case_id, "status": "skipped_success"}))
            continue
        used = [item.attempt for item in records if item.case_id == case.case_id]
        succeeded = False
        for next_attempt in _new_attempt_numbers(
            used, max_new_attempts=args.max_attempts
        ):
            record = execute_case(case, next_attempt, paths, dependencies)
            print(canonical_json(record.as_dict()))
            if record.status == "succeeded":
                succeeded = True
                break
        if not succeeded:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
