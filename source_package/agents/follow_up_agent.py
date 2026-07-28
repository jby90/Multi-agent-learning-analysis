"""Evidence-bounded free-text follow-up generation for interactive training."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any
import unicodedata

from jsonschema import Draft202012Validator

from agents.task_agent import TaskAgent
from orchestrator.llm import LLMResult, call_llm


MODEL = "qwen3-235b-a22b"
TEMPERATURE = 0.1
PROMPT_PATH = Path(__file__).with_name("prompts") / "follow_up.md"
MIN_FOLLOW_UP_ROUNDS = 2
MAX_FOLLOW_UP_ROUNDS = 4
MAX_LEARNER_TEXT_LENGTH = 500
MAX_QUESTION_LENGTH = 180
UNKNOWN_MISCONCEPTION = "UNKNOWN"
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_ENGINEERING_PATTERNS = (
    re.compile(r"\b(?:no_matching_transition|injected_for_demo)\b", re.I),
    re.compile(
        r"\b(?:safe_rejected|external_unavailable|system_error|completed)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:msg_?id|trace_?id|session_?id|rule_?hits|template_?id|"
        r"evidence_?ref|reviewed_?msg_?id)\b",
        re.I,
    ),
    re.compile(r"\b(?:agent|llm|orchestrator)\b", re.I),
    re.compile(r"\b(?:http)\s*\d{3}\b", re.I),
    re.compile(r"/api(?:/|\b)", re.I),
    re.compile(r"\b[tmrsq]-?\d+(?:-[a-z0-9]+)*\b", re.I),
    re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", re.I),
    re.compile(r"(?:状态机|协议字段|转移名|工程实现|审核智能体)"),
)
_ANSWER_LEAK_PATTERNS = (
    re.compile(r"(?:答案|正确结论)\s*(?:是|为|：|:)"),
    re.compile(r"(?:直接记住|标准答案)"),
)


class FollowUpGenerationError(ValueError):
    """Raised when a generated follow-up cannot pass deterministic gates."""


@dataclass(frozen=True, slots=True)
class FollowUpTurn:
    assessment: str
    target_misconception: str
    product: dict[str, Any] | None
    model: str
    latency_ms: int
    token_usage: dict[str, int]


def _normalized_visible_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def contains_engineering_text(value: str) -> bool:
    normalized = _normalized_visible_text(value)
    return bool(_ZERO_WIDTH_RE.search(normalized)) or any(
        pattern.search(normalized) for pattern in _ENGINEERING_PATTERNS
    )


def normalize_learner_input(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("learner input must be a string")
    normalized = _normalized_visible_text(value)
    if (
        len(normalized) < 2
        or _ZERO_WIDTH_RE.search(normalized)
        or len(normalized) > MAX_LEARNER_TEXT_LENGTH
    ):
        raise ValueError("learner input must contain 2-500 visible characters")
    if contains_engineering_text(normalized):
        raise ValueError("learner input must use business learning language")
    return normalized


def _question_schema(
    allowed_targets: Sequence[str],
    *,
    completion_allowed: bool,
) -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "assessment",
            "target_misconception",
            "question",
        ],
        "properties": {
            "assessment": {
                "type": "string",
                "enum": ["mastered", "needs_support", "unknown"],
            },
            "target_misconception": {
                "type": "string",
                "enum": [*allowed_targets, UNKNOWN_MISCONCEPTION],
            },
            "question": {
                "type": "string",
                "minLength": 0 if completion_allowed else 1,
                "maxLength": MAX_QUESTION_LENGTH,
            },
        },
        "additionalProperties": False,
    }


def _payload_content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    content = payload.get("content") if isinstance(payload, Mapping) else None
    if not isinstance(content, Mapping):
        raise FollowUpGenerationError("current task has no payload content")
    return content


def _evidence_items(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = message.get("evidence")
    evidence = (
        [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]
        if isinstance(value, list)
        else []
    )
    if not evidence or any(
        not isinstance(item.get("ref"), str) or not str(item["ref"]).strip()
        for item in evidence
    ):
        raise FollowUpGenerationError("follow-up evidence must be non-empty")
    return evidence


def _numbers_in(value: str) -> frozenset[Decimal]:
    numbers: set[Decimal] = set()
    for token in _NUMBER_RE.findall(unicodedata.normalize("NFKC", value)):
        try:
            numbers.add(Decimal(token))
        except InvalidOperation:
            continue
    return frozenset(numbers)


def _expected_points(evidence: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    values: list[str] = []
    for item in evidence:
        quote = item.get("quote")
        if not isinstance(quote, str):
            continue
        try:
            data = json.loads(quote)
        except json.JSONDecodeError:
            continue
        points = data.get("expected_points") if isinstance(data, Mapping) else None
        if isinstance(points, list):
            values.extend(str(point) for point in points if isinstance(point, str))
    return tuple(dict.fromkeys(values))


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _validate_question(
    question: Any,
    *,
    standard_stem: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    if not isinstance(question, str):
        raise FollowUpGenerationError("question must be a string")
    visible = question.strip()
    normalized = _normalized_visible_text(visible)
    if not normalized or len(normalized) > MAX_QUESTION_LENGTH:
        raise FollowUpGenerationError("question length is invalid")
    if contains_engineering_text(normalized):
        raise FollowUpGenerationError("question contains engineering text")
    if len(re.findall(r"[?？]", normalized)) != 1 or not normalized.endswith(("?", "？")):
        raise FollowUpGenerationError("question must contain exactly one question")
    allowed_source = standard_stem + json.dumps(
        list(evidence),
        ensure_ascii=False,
        sort_keys=True,
    )
    if _numbers_in(normalized) - _numbers_in(allowed_source):
        raise FollowUpGenerationError("question contains an ungrounded number")
    question_key = _match_key(normalized)
    if any(
        point_key
        and point_key in question_key
        for point in _expected_points(evidence)
        if (point_key := _match_key(point))
    ) or any(pattern.search(normalized) for pattern in _ANSWER_LEAK_PATTERNS):
        raise FollowUpGenerationError("question leaks the answer")
    return visible


class FollowUpAgent:
    """Assess one learner answer and produce one evidence-bounded next question."""

    def __init__(
        self,
        trace_id: str,
        *,
        llm_call: Callable[..., LLMResult] = call_llm,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        if not callable(llm_call):
            raise ValueError("llm_call must be callable")
        self._trace_id = trace_id
        self._llm_call = llm_call
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def generate(
        self,
        *,
        student_answer: str,
        current_task: Mapping[str, Any],
        task_agent: TaskAgent,
        round_index: int,
        max_rounds: int = MAX_FOLLOW_UP_ROUNDS,
        review_feedback: Sequence[str] = (),
        completion_allowed: bool = False,
        terminal_round: bool = False,
    ) -> FollowUpTurn:
        answer = normalize_learner_input(student_answer)
        if (
            not isinstance(max_rounds, int)
            or isinstance(max_rounds, bool)
            or not MIN_FOLLOW_UP_ROUNDS <= max_rounds <= MAX_FOLLOW_UP_ROUNDS
        ):
            raise ValueError("max_rounds must be between 2 and 4")
        if (
            not isinstance(round_index, int)
            or isinstance(round_index, bool)
            or not MIN_FOLLOW_UP_ROUNDS <= round_index <= max_rounds
        ):
            raise ValueError("round_index must be between 2 and max_rounds")

        allowed_targets = task_agent.misconception_ids
        candidates: dict[str, dict[str, Any]] = {}
        candidate_summary: list[dict[str, Any]] = []
        for misconception in allowed_targets:
            candidate = task_agent.counter_evidence(misconception)
            candidates[misconception] = candidate
            content = _payload_content(candidate)
            evidence = _evidence_items(candidate)
            candidate_summary.append(
                {
                    "target": misconception,
                    "standard_stem": content.get("standard_stem")
                    or content.get("question"),
                    "evidence_summary": list(_expected_points(evidence)),
                }
            )

        current_content = _payload_content(current_task)
        current_evidence = _evidence_items(current_task)
        schema = _question_schema(
            allowed_targets,
            completion_allowed=completion_allowed or terminal_round,
        )
        try:
            result = self._llm_call(
                model=MODEL,
                system=self._system_prompt,
                user=json.dumps(
                    {
                        "student_answer": answer,
                        "current_task": {
                            key: current_content.get(key)
                            for key in (
                                "question",
                                "standard_stem",
                                "knowledge_point",
                                "difficulty",
                                "family",
                            )
                        },
                        "current_evidence_summary": list(
                            _expected_points(current_evidence)
                        ),
                        "candidates": candidate_summary,
                        "allowed_targets": [
                            *allowed_targets,
                            UNKNOWN_MISCONCEPTION,
                        ],
                        "round_index": round_index,
                        "max_rounds": max_rounds,
                        "review_feedback": [
                            str(item)
                            for item in review_feedback
                            if isinstance(item, str) and item.strip()
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json_schema=schema,
                temperature=TEMPERATURE,
            )
        except Exception as exc:
            raise FollowUpGenerationError(
                "follow-up generation is temporarily unavailable"
            ) from exc
        if not isinstance(result, LLMResult):
            raise FollowUpGenerationError("follow-up model returned no metadata")
        raw_target = result.data.get("target_misconception")
        if (
            isinstance(raw_target, str)
            and raw_target != UNKNOWN_MISCONCEPTION
            and raw_target not in allowed_targets
        ):
            raise FollowUpGenerationError(
                "target misconception is not in the current domain"
            )
        try:
            Draft202012Validator(schema).validate(result.data)
        except Exception as exc:
            raise FollowUpGenerationError(
                "follow-up model output is invalid"
            ) from exc

        assessment = str(result.data["assessment"])
        target = str(result.data["target_misconception"])
        if (assessment == "unknown") != (target == UNKNOWN_MISCONCEPTION):
            raise FollowUpGenerationError(
                "unknown assessment and target must be aligned"
            )
        if target == UNKNOWN_MISCONCEPTION:
            base = current_task
        else:
            try:
                base = candidates[target]
            except KeyError as exc:
                raise FollowUpGenerationError(
                    "target misconception is not in the current domain"
                ) from exc

        raw_question = result.data["question"]
        if terminal_round or (completion_allowed and assessment == "mastered"):
            if not isinstance(raw_question, str) or raw_question.strip():
                raise FollowUpGenerationError(
                    "completed follow-up must not generate another question"
                )
            return FollowUpTurn(
                assessment=assessment,
                target_misconception=target,
                product=None,
                model=result.model,
                latency_ms=result.latency_ms,
                token_usage=result.token_usage.as_dict(),
            )
        if isinstance(raw_question, str) and not raw_question.strip():
            raise FollowUpGenerationError(
                "an unfinished follow-up must generate a question"
            )

        base_content = _payload_content(base)
        evidence = _evidence_items(base)
        standard_stem = str(
            base_content.get("standard_stem")
            or base_content.get("question")
            or ""
        ).strip()
        if not standard_stem:
            raise FollowUpGenerationError("follow-up evidence has no standard stem")
        question = _validate_question(
            raw_question,
            standard_stem=standard_stem,
            evidence=evidence,
        )
        evidence_refs = [str(item["ref"]) for item in evidence]
        content: dict[str, Any] = {
            "event": "follow_up_question_ready",
            "question": question,
            "questions": [
                {
                    "id": f"follow-up-{round_index}",
                    "prompt": question,
                }
            ],
            "standard_stem": standard_stem,
            "assessment": assessment,
            "target_misconception": target,
            "follow_up_round": round_index,
            "max_follow_up_rounds": max_rounds,
            "evidence_refs": evidence_refs,
        }
        for key in (
            "knowledge_point",
            "difficulty",
            "family",
            "responsibility_scope",
        ):
            value = current_content.get(key)
            if value is not None:
                content[key] = deepcopy(value)
        draft: dict[str, Any] = {
            "trace_id": self._trace_id,
            "agent": "task",
            "role": "probe",
            "payload": {
                "type": "quiz_set",
                "content": content,
            },
            "evidence": evidence,
            "claims": [],
            "probe": {
                "wrong_attempts": max(1, round_index - 1),
                "questions": [question],
                "target_misconception": target,
            },
            "model": result.model,
            "latency_ms": result.latency_ms,
            "token_usage": result.token_usage.as_dict(),
            "timestamp": self._clock().isoformat(),
        }
        profile_ref = current_task.get("student_profile_ref")
        if isinstance(profile_ref, str) and profile_ref.strip():
            draft["student_profile_ref"] = profile_ref
        return FollowUpTurn(
            assessment=assessment,
            target_misconception=target,
            product=draft,
            model=result.model,
            latency_ms=result.latency_ms,
            token_usage=result.token_usage.as_dict(),
        )
