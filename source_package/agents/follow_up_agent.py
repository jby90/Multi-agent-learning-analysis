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

from agents.misconception_relations import (
    RoutingPolicy,
    default_relation_index,
    default_relation_support_points,
    rank_relation_routes,
    select_relation_route,
)
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
NO_NEXT_TARGET = "NO_NEXT_TARGET"
_DEFAULT_FALLBACK_QUESTIONS = (
    "为回答“{standard_stem}”，查询结果中最关键的字段和值是什么？",
    "请依据查询结果说明“{standard_stem}”可以得到什么结论？",
    "围绕“{standard_stem}”，当前结果能够确认什么、还不能确认什么？",
)
_FAMILY_FALLBACK_QUESTIONS = {
    "Q1": (
        "查询结果中的实际完成量是多少，它代表计划目标还是实际已完成数量？",
        "实际完成量与计划量分别表示什么，两者为什么不能混用？",
        "请引用实际完成量字段和值说明当前完成情况？",
    ),
    "Q2": (
        "查询结果中的计划量与实际完成量分别是多少，哪一个表示已经完成的数量？",
        "计划量和实际完成量在业务含义上有什么区别？",
        "请引用计划量和实际完成量的字段与数值说明判断？",
    ),
    "Q3": (
        "该工序完成率是多少，换算为百分比后是否达到计划目标？",
        "当前完成率说明实际完成量与计划量是什么关系？",
        "请引用完成率字段和值完整说明当前完成情况？",
    ),
    "Q4": (
        "月度结果中哪个月的完成率变化最明显，你依据的月份和值是什么？",
        "相邻月份的完成率如何变化，这属于单期变化还是持续趋势？",
        "请引用月份和完成率说明当前序列能确认什么？",
    ),
    "Q5": (
        "船号对比中哪艘船的完成率最低，你依据的船号和值是什么？",
        "其他船号的完成率分别是多少，它们与最低值有何差异？",
        "请引用船号和完成率说明当前比较结论？",
    ),
    "Q6": (
        "三道工序中哪一道完成率最低，你依据的工序和值是什么？",
        "另外两道工序的完成率分别是多少？",
        "按完成率从低到高，三道工序应怎样排序？",
    ),
    "Q7": (
        "责任单元对比中哪个单元完成率最低，你依据的单元和值是什么？",
        "其他责任单元的完成率分别是多少？",
        "请引用责任单元和完成率说明当前比较结论？",
    ),
}
_FAMILY_ANSWER_REQUIREMENTS = {
    "Q1": {
        "operation": "identify_and_interpret",
        "required_fields": ["actual_qty"],
        "required_reasoning": ["actual_completed_quantity"],
    },
    "Q2": {
        "operation": "distinguish",
        "required_fields": ["plan_qty", "actual_qty"],
        "required_reasoning": ["plan_and_actual_are_not_interchangeable"],
    },
    "Q3": {
        "operation": "interpret_completion_rate",
        "required_fields": ["complete_rate"],
        "required_reasoning": ["relationship_to_plan_target"],
    },
    "Q4": {
        "operation": "compare_over_time",
        "required_fields": ["month_label", "complete_rate"],
        "required_reasoning": ["change_or_trend"],
    },
    "Q5": {
        "operation": "compare_entities",
        "required_fields": ["ship_no", "complete_rate"],
        "required_reasoning": ["comparison"],
    },
    "Q6": {
        "operation": "compare_entities",
        "required_fields": ["process_code", "complete_rate"],
        "required_reasoning": ["comparison"],
    },
    "Q7": {
        "operation": "compare_entities",
        "required_fields": ["workshop_code", "complete_rate"],
        "required_reasoning": ["comparison"],
    },
}
_TEMPLATE_FALLBACK_QUESTIONS = {
    "T-03": (
        "查询结果中AZTP和ZZTP的完成率分别是多少？",
        "按完成率从低到高，三道工序应怎样排序？",
        "请引用当前查询结果中的字段和值说明你的判断？",
    ),
    "T-10": (
        "查询结果中的月偏差率是多少？",
        "该偏差率为正值还是负值？",
        "只依据该偏差率，当前属于欠产还是超产？",
    ),
    "T-07-B": (
        "查询结果中YCL在2025-05、ZZTP在2025-06和AZTP在2025-07的完成率分别是多少？",
        "三道工序的完成率低点分别出现在哪个月？",
        "只依据当前月度表，四态候选应归为哪一种状态？",
    ),
}
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
_UNRESOLVED_REFERENCE_PATTERNS = (
    re.compile(r"题目中的问题"),
    re.compile(r"你会怎样回答"),
    re.compile(r"根据(?:上述|以上)内容(?:回答|判断)"),
    re.compile(r"(?:该|这个|上述)数据说明了什么"),
)


class FollowUpGenerationError(ValueError):
    """Raised when a generated follow-up cannot pass deterministic gates."""


@dataclass(frozen=True, slots=True)
class FollowUpTurn:
    assessment: str
    diagnosed_misconception: str
    next_target_misconception: str | None
    route_support_points: tuple[str, ...]
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
            "diagnosed_misconception",
            "next_target_misconception",
            "question",
        ],
        "properties": {
            "assessment": {
                "type": "string",
                "enum": ["mastered", "needs_support", "unknown"],
            },
            "diagnosed_misconception": {
                "type": "string",
                "enum": [*allowed_targets, UNKNOWN_MISCONCEPTION],
            },
            "next_target_misconception": {
                "type": "string",
                "enum": [
                    *allowed_targets,
                    UNKNOWN_MISCONCEPTION,
                    NO_NEXT_TARGET,
                ],
            },
            "question": {
                "type": "string",
                "minLength": 0 if completion_allowed else 1,
                "maxLength": MAX_QUESTION_LENGTH,
            },
        },
        "additionalProperties": False,
    }


def _responsibility_scope(content: Mapping[str, Any]) -> tuple[str, ...]:
    declared = content.get("responsibility_scope")
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        scope = tuple(
            dict.fromkeys(
                item.strip()
                for item in declared
                if isinstance(item, str) and item.strip()
            )
        )
        if scope:
            return scope
    knowledge_point = content.get("knowledge_point")
    if isinstance(knowledge_point, str) and knowledge_point.strip():
        return (knowledge_point.strip(),)
    return ()


def deterministic_follow_up_route(
    *,
    assessment: str,
    diagnosed_misconception: str,
    completion_allowed: bool,
    terminal_round: bool,
    probed_misconceptions: Sequence[str],
    covered_relation_points: Sequence[str],
    responsibility_scope: Sequence[str],
    relation_index: Mapping[str, Sequence[Any]],
    allowed_targets: Sequence[str],
    allowed_support_points: Sequence[str],
    routing_policy: RoutingPolicy,
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve one immutable next target and its backend-only route support."""

    if terminal_round or (completion_allowed and assessment == "mastered"):
        return None, ()
    if diagnosed_misconception == UNKNOWN_MISCONCEPTION:
        return UNKNOWN_MISCONCEPTION, ()
    probed = tuple(dict.fromkeys(probed_misconceptions))
    if assessment != "needs_support" or diagnosed_misconception not in probed:
        return diagnosed_misconception, ()
    route = select_relation_route(
        diagnosed_misconception,
        responsibility_scope=responsibility_scope,
        probed=probed,
        covered_relation_points=covered_relation_points,
        relation_index=relation_index,
        allowed_ids=allowed_targets,
        allowed_support_points=allowed_support_points,
        policy=routing_policy,
    )
    if route is None:
        return diagnosed_misconception, ()
    return route.target, route.route_support_points


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


def _expected_rows(
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return only reviewed scalar result rows from task evidence."""

    values: list[dict[str, Any]] = []
    for item in evidence:
        quote = item.get("quote")
        if not isinstance(quote, str):
            continue
        try:
            data = json.loads(quote)
        except json.JSONDecodeError:
            continue
        rows = data.get("expected_rows") if isinstance(data, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or any(
                isinstance(value, (Mapping, list, tuple, set))
                for value in row.values()
            ):
                continue
            values.append({str(key): value for key, value in row.items()})
    return tuple(values)


def _answer_requirements(
    content: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    family = str(content.get("family") or "").strip()
    configured = _FAMILY_ANSWER_REQUIREMENTS.get(family)
    if configured is not None:
        return deepcopy(configured)
    available_fields = tuple(
        dict.fromkeys(
            key
            for row in _expected_rows(evidence)
            for key in row
        )
    )
    return {
        "operation": "answer_current_question",
        "required_fields": list(available_fields),
        "required_reasoning": [],
    }


def _decimal_scalar(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _matches_reviewed_completion_extreme(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Confirm one narrow, fully reproducible completion-rate extrema answer.

    This is intentionally a positive-only guard for model false negatives.  It
    requires the question to state the metric and extrema direction, while the
    learner must cite one reviewed row identifier and that row's exact reviewed
    value.  The learner does not need to repeat wording already supplied by the
    question.  It never infers correctness from keywords or unreviewed output.
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    normalized_answer = unicodedata.normalize("NFKC", answer).casefold()
    if "完成率" not in normalized_question:
        return False
    if any(token in normalized_question for token in ("最低", "最小")):
        direction = "min"
    elif any(token in normalized_question for token in ("最高", "最大")):
        direction = "max"
    else:
        return False

    rows = _expected_rows(evidence)
    if len(rows) < 2:
        return False
    common_keys = set(rows[0])
    for row in rows[1:]:
        common_keys.intersection_update(row)
    metric_keys = [
        key
        for key in common_keys
        if _match_key(key) in {
            "completerate",
            "completionrate",
            _match_key("完成率"),
        }
    ]
    if len(metric_keys) != 1:
        return False
    metric_key = metric_keys[0]
    reviewed: list[tuple[Mapping[str, Any], Decimal]] = []
    for row in rows:
        value = _decimal_scalar(row.get(metric_key))
        if value is None:
            return False
        reviewed.append((row, value))
    extreme_value = (
        min(value for _, value in reviewed)
        if direction == "min"
        else max(value for _, value in reviewed)
    )
    winners = [row for row, value in reviewed if value == extreme_value]
    if len(winners) != 1:
        return False

    cited_numbers = _numbers_in(normalized_answer)
    exact_value_cited = extreme_value in cited_numbers
    if not exact_value_cited:
        percent_values = {
            Decimal(token)
            for token in re.findall(
                r"([-+]?\d+(?:\.\d+)?)\s*%",
                normalized_answer,
            )
        }
        exact_value_cited = extreme_value * Decimal("100") in percent_values
    if not exact_value_cited:
        return False

    winner = winners[0]
    identifiers = [
        unicodedata.normalize("NFKC", str(value)).casefold().strip()
        for key, value in winner.items()
        if key != metric_key
        and isinstance(value, str)
        and value.strip()
        and _decimal_scalar(value) is None
    ]
    return any(identifier in normalized_answer for identifier in identifiers)


def reviewed_answer_correction(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str | None:
    """Return one evidence-bounded correction for a contradicted extreme.

    The correction is intentionally narrower than answer assessment.  It only
    fires when the current question asks for a completion-rate minimum or
    maximum and the learner explicitly cites a reviewed losing row or value.
    Merely omitting the value remains a request for more evidence, not an
    incorrect-answer accusation.
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    normalized_answer = unicodedata.normalize("NFKC", answer).casefold()
    if "完成率" not in normalized_question:
        return None
    if any(token in normalized_question for token in ("最低", "最小")):
        direction = "min"
        direction_label = "最低值"
    elif any(token in normalized_question for token in ("最高", "最大")):
        direction = "max"
        direction_label = "最高值"
    else:
        return None

    rows = _expected_rows(evidence)
    if len(rows) < 2:
        return None
    common_keys = set(rows[0])
    for row in rows[1:]:
        common_keys.intersection_update(row)
    metric_keys = [
        key
        for key in common_keys
        if _match_key(key)
        in {"completerate", "completionrate", _match_key("完成率")}
    ]
    if len(metric_keys) != 1:
        return None
    metric_key = metric_keys[0]
    reviewed: list[tuple[Mapping[str, Any], Decimal]] = []
    for row in rows:
        value = _decimal_scalar(row.get(metric_key))
        if value is None:
            return None
        reviewed.append((row, value))
    extreme_value = (
        min(value for _, value in reviewed)
        if direction == "min"
        else max(value for _, value in reviewed)
    )
    winners = [row for row, value in reviewed if value == extreme_value]
    if len(winners) != 1:
        return None
    winner = winners[0]

    def identifiers(row: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(
            unicodedata.normalize("NFKC", str(value)).strip()
            for key, value in row.items()
            if key != metric_key
            and isinstance(value, str)
            and value.strip()
            and _decimal_scalar(value) is None
        )

    winner_identifiers = identifiers(winner)
    if not winner_identifiers:
        return None
    if (
        any(item.casefold() in normalized_answer for item in winner_identifiers)
        and _answer_cites_reviewed_value(answer, extreme_value)
    ):
        return None

    contradicted: tuple[Mapping[str, Any], Decimal] | None = None
    for row, value in reviewed:
        if row is winner:
            continue
        row_identifiers = identifiers(row)
        if any(item.casefold() in normalized_answer for item in row_identifiers):
            contradicted = (row, value)
            break
        if _answer_cites_reviewed_value(answer, value):
            contradicted = (row, value)
            break
    if contradicted is None:
        return None

    wrong_row, wrong_value = contradicted
    wrong_identifiers = identifiers(wrong_row)
    if not wrong_identifiers:
        return None
    return (
        f"需要纠正：查询结果显示 {winner_identifiers[0]} 的完成率为 "
        f"{winner[metric_key]}，是{direction_label}；你回答中的 "
        f"{wrong_identifiers[0]} 为 {wrong_row[metric_key]}，不是{direction_label}。"
    )


def _answer_cites_reviewed_value(answer: str, value: Decimal) -> bool:
    normalized = unicodedata.normalize("NFKC", answer).casefold()
    if value in _numbers_in(normalized):
        return True
    percent_values = {
        Decimal(token)
        for token in re.findall(r"([-+]?\d+(?:\.\d+)?)\s*%", normalized)
    }
    return value * Decimal("100") in percent_values


def _matches_reviewed_completion_interpretation(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Confirm a scalar completion-rate value plus its plan interpretation.

    This positive-only guard covers the common false negative where the learner
    cites the exact reviewed completion rate and states the correct direction in
    plain language.  It deliberately refuses multi-row questions and never
    derives a value from unreviewed text.
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    normalized_answer = unicodedata.normalize("NFKC", answer).casefold()
    if "完成率" not in normalized_question or not any(
        token in normalized_question
        for token in ("完成情况", "说明", "计划", "达到")
    ):
        return False
    rows = _expected_rows(evidence)
    if len(rows) != 1:
        return False
    row = rows[0]
    metric_keys = [
        key
        for key in row
        if _match_key(key) in {
            "completerate",
            "completionrate",
            _match_key("完成率"),
        }
    ]
    if len(metric_keys) != 1:
        return False
    value = _decimal_scalar(row.get(metric_keys[0]))
    if value is None or not _answer_cites_reviewed_value(normalized_answer, value):
        return False

    if value < Decimal("1"):
        return any(
            phrase in normalized_answer
            for phrase in (
                "完成率低",
                "完成率偏低",
                "未完成计划",
                "没有完成计划",
                "未达计划",
                "低于计划",
                "低于100%",
                "低于100％",
                "只完成",
                "完成不足",
                "尚未完成",
            )
        )
    if value > Decimal("1"):
        return any(
            phrase in normalized_answer
            for phrase in (
                "超过计划",
                "超出计划",
                "超额完成",
                "高于计划",
                "超过100%",
                "超过100％",
            )
        )
    return any(
        phrase in normalized_answer
        for phrase in ("完成计划", "达到计划", "正好完成", "等于100%", "等于100％")
    )


def _matches_reviewed_answer(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    return _matches_reviewed_completion_extreme(
        question=question,
        answer=answer,
        evidence=evidence,
    ) or _matches_reviewed_completion_interpretation(
        question=question,
        answer=answer,
        evidence=evidence,
    )


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
    if any(pattern.search(normalized) for pattern in _UNRESOLVED_REFERENCE_PATTERNS):
        raise FollowUpGenerationError("question contains an unresolved reference")
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

    def deterministic_fallback(
        self,
        *,
        current_task: Mapping[str, Any],
        round_index: int,
        max_rounds: int = MAX_FOLLOW_UP_ROUNDS,
        student_answer: str | None = None,
        current_question: str | None = None,
        completion_allowed: bool = False,
        terminal_round: bool = False,
        previous_questions: Sequence[str] = (),
    ) -> FollowUpTurn:
        """Build one evidence-bound probe when model output fails hard gates.

        The fallback does not infer mastery from free-form semantics.  It may
        confirm one narrow extrema answer when the identifier and exact value
        match reviewed rows; otherwise it keeps the learner in the reviewed
        follow-up loop and asks for another approved evidence item.
        """

        current_content = _payload_content(current_task)
        evidence = _evidence_items(current_task)
        reviewed_mastery = bool(
            student_answer
            and current_question
            and _matches_reviewed_answer(
                question=current_question,
                answer=student_answer,
                evidence=evidence,
            )
        )
        assessment = "mastered" if reviewed_mastery else "unknown"
        model = (
            "deterministic-reviewed-answer"
            if reviewed_mastery
            else "deterministic-evidence-fallback"
        )

        if terminal_round or (completion_allowed and reviewed_mastery):
            return FollowUpTurn(
                assessment=assessment,
                diagnosed_misconception=UNKNOWN_MISCONCEPTION,
                next_target_misconception=None,
                route_support_points=(),
                product=None,
                model=model,
                latency_ms=0,
                token_usage={
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )

        source_standard_stem = str(
            current_content.get("standard_stem")
            or current_content.get("question")
            or ""
        ).strip()
        if not source_standard_stem:
            raise FollowUpGenerationError("follow-up evidence has no standard stem")
        template_id = str(current_content.get("template_id") or "").strip()
        template_questions = _TEMPLATE_FALLBACK_QUESTIONS.get(template_id)
        family = str(current_content.get("family") or "").strip()
        family_questions = _FAMILY_FALLBACK_QUESTIONS.get(family)
        standard_stem = source_standard_stem
        fallback_questions = (
            template_questions or family_questions or _DEFAULT_FALLBACK_QUESTIONS
        )
        previous_keys = {
            _match_key(item)
            for item in previous_questions
            if isinstance(item, str) and item.strip()
        }
        start_index = min(
            max(round_index - MIN_FOLLOW_UP_ROUNDS, 0),
            len(fallback_questions) - 1,
        )
        ordered_questions = (
            *fallback_questions[start_index:],
            *fallback_questions[:start_index],
        )
        question = ""
        for template in ordered_questions:
            candidate = _validate_question(
                template.format(
                    standard_stem=source_standard_stem.rstrip("。？！?!"),
                ),
                standard_stem=source_standard_stem,
                evidence=evidence,
            )
            if _match_key(candidate) not in previous_keys:
                question = candidate
                break
        if not question:
            raise FollowUpGenerationError(
                "no unused evidence-bound fallback question remains"
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
            "target_misconception": UNKNOWN_MISCONCEPTION,
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
        product: dict[str, Any] = {
            "trace_id": self._trace_id,
            "agent": "task",
            "role": "probe",
            "payload": {"type": "quiz_set", "content": content},
            "evidence": evidence,
            "claims": [],
            "probe": {
                "wrong_attempts": max(1, round_index - 1),
                "questions": [question],
                "target_misconception": UNKNOWN_MISCONCEPTION,
            },
            "model": model,
            "latency_ms": 0,
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "timestamp": self._clock().isoformat(),
        }
        profile_ref = current_task.get("student_profile_ref")
        if isinstance(profile_ref, str) and profile_ref.strip():
            product["student_profile_ref"] = profile_ref
        return FollowUpTurn(
            assessment=assessment,
            diagnosed_misconception=UNKNOWN_MISCONCEPTION,
            next_target_misconception=UNKNOWN_MISCONCEPTION,
            route_support_points=(),
            product=product,
            model=model,
            latency_ms=0,
            token_usage={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )

    def generate(
        self,
        *,
        student_answer: str,
        current_task: Mapping[str, Any],
        task_agent: TaskAgent,
        round_index: int,
        current_question: str | None = None,
        max_rounds: int = MAX_FOLLOW_UP_ROUNDS,
        probed_misconceptions: Sequence[str] = (),
        covered_relation_points: Sequence[str] = (),
        routing_policy: RoutingPolicy | None = None,
        review_feedback: Sequence[str] = (),
        completion_allowed: bool = False,
        terminal_round: bool = False,
        previous_questions: Sequence[str] = (),
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
        active_routing_policy = routing_policy or RoutingPolicy()
        probed = tuple(
            dict.fromkeys(
                target
                for target in probed_misconceptions
                if isinstance(target, str) and target in allowed_targets
            )
        )
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
        active_question = (
            current_question.strip()
            if isinstance(current_question, str) and current_question.strip()
            else str(
                current_content.get("question")
                or current_content.get("standard_stem")
                or ""
            ).strip()
        )
        if not active_question or contains_engineering_text(active_question):
            raise FollowUpGenerationError("current follow-up question is invalid")
        responsibility_scope = _responsibility_scope(current_content)
        relation_index = default_relation_index(tuple(allowed_targets))
        allowed_route_points = default_relation_support_points(
            tuple(allowed_targets)
        )
        eligible_related_targets = {
            target: list(
                route.target
                for route in rank_relation_routes(
                    target,
                    responsibility_scope=responsibility_scope,
                    probed=probed,
                    covered_relation_points=covered_relation_points,
                    relation_index=relation_index,
                    allowed_ids=allowed_targets,
                    allowed_support_points=allowed_route_points,
                    policy=active_routing_policy,
                )
            )
            for target in allowed_targets
        }
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
                        "current_question": active_question,
                        "previous_questions": [
                            item
                            for item in previous_questions
                            if isinstance(item, str) and item.strip()
                        ],
                        "current_task": {
                            key: current_content.get(key)
                            for key in (
                                "question",
                                "standard_stem",
                                "knowledge_point",
                                "responsibility_scope",
                                "difficulty",
                                "family",
                            )
                        },
                        "current_evidence_summary": list(
                            _expected_points(current_evidence)
                        ),
                        "current_evidence_rows": list(
                            _expected_rows(current_evidence)
                        ),
                        "answer_requirements": _answer_requirements(
                            current_content,
                            current_evidence,
                        ),
                        "candidates": candidate_summary,
                        "allowed_targets": [
                            *allowed_targets,
                            UNKNOWN_MISCONCEPTION,
                        ],
                        "eligible_related_targets": eligible_related_targets,
                        "probed_misconceptions": list(probed),
                        "responsibility_scope": list(responsibility_scope),
                        "round_index": round_index,
                        "max_rounds": max_rounds,
                        "completion_allowed": completion_allowed,
                        "terminal_round": terminal_round,
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
        raw_diagnosed = result.data.get("diagnosed_misconception")
        raw_next = result.data.get("next_target_misconception")
        for raw_target in (raw_diagnosed, raw_next):
            if (
                isinstance(raw_target, str)
                and raw_target not in (UNKNOWN_MISCONCEPTION, NO_NEXT_TARGET)
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
        diagnosed = str(result.data["diagnosed_misconception"])
        if assessment != "mastered" and _matches_reviewed_answer(
            question=active_question,
            answer=answer,
            evidence=current_evidence,
        ):
            assessment = "mastered"
            diagnosed = UNKNOWN_MISCONCEPTION
        proposed_next = (
            None
            if result.data["next_target_misconception"] == NO_NEXT_TARGET
            else str(result.data["next_target_misconception"])
        )
        if completion_allowed and assessment == "mastered":
            return FollowUpTurn(
                assessment=assessment,
                diagnosed_misconception=diagnosed,
                next_target_misconception=None,
                route_support_points=(),
                product=None,
                model=result.model,
                latency_ms=result.latency_ms,
                token_usage=result.token_usage.as_dict(),
            )
        if assessment == "unknown" and diagnosed != UNKNOWN_MISCONCEPTION:
            raise FollowUpGenerationError(
                "unknown assessment and diagnosis must be aligned"
            )
        if assessment == "needs_support" and diagnosed == UNKNOWN_MISCONCEPTION:
            raise FollowUpGenerationError(
                "support assessment requires a diagnosed misconception"
            )
        expected_next, route_support_points = deterministic_follow_up_route(
            assessment=assessment,
            diagnosed_misconception=diagnosed,
            completion_allowed=completion_allowed,
            terminal_round=terminal_round,
            probed_misconceptions=probed,
            covered_relation_points=covered_relation_points,
            responsibility_scope=responsibility_scope,
            relation_index=relation_index,
            allowed_targets=allowed_targets,
            allowed_support_points=allowed_route_points,
            routing_policy=active_routing_policy,
        )
        if proposed_next != expected_next:
            raise FollowUpGenerationError(
                "next target does not match deterministic routing"
            )

        raw_question = result.data["question"]
        if expected_next is None:
            if not isinstance(raw_question, str) or raw_question.strip():
                raise FollowUpGenerationError(
                    "completed follow-up must not generate another question"
                )
            return FollowUpTurn(
                assessment=assessment,
                diagnosed_misconception=diagnosed,
                next_target_misconception=None,
                route_support_points=(),
                product=None,
                model=result.model,
                latency_ms=result.latency_ms,
                token_usage=result.token_usage.as_dict(),
            )
        if expected_next == UNKNOWN_MISCONCEPTION:
            base = current_task
        else:
            try:
                base = candidates[expected_next]
            except KeyError as exc:
                raise FollowUpGenerationError(
                    "target misconception is not in the current domain"
                ) from exc

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
        previous_keys = {
            _match_key(item)
            for item in previous_questions
            if isinstance(item, str) and item.strip()
        }
        if _match_key(question) in previous_keys:
            raise FollowUpGenerationError("follow-up question repeats history")
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
            "target_misconception": expected_next,
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
                "target_misconception": expected_next,
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
            diagnosed_misconception=diagnosed,
            next_target_misconception=expected_next,
            route_support_points=route_support_points,
            product=draft,
            model=result.model,
            latency_ms=result.latency_ms,
            token_usage=result.token_usage.as_dict(),
        )
