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
# 闭环六：三层递进（口径记忆 → 机理理解 → 归因应用）。层只决定下一问的认知深度与
# 措辞，绝不参与掌握判定/完成/路由裁决——那些仍由确定性证据判定决定。
FOLLOW_UP_LAYER_NAMES = {1: "口径记忆", 2: "机理理解", 3: "归因应用"}
MIN_FOLLOW_UP_LAYER = 1
MAX_FOLLOW_UP_LAYER = 3
MAX_LECTURE_DIGEST_LENGTH = 1200
MAX_DATA_DIGEST_LENGTH = 900
# 闭环六：画像风格映射（对齐 profiles 的 lecture_style 口径）。只影响措辞与侧重，
# 不改变证据边界与评分合同。
_PERSONA_QUESTION_STYLES = {
    "planner_new": (
        "面向新入职生产计划员：优先问工艺语义与口径含义——这个数在生产上意味着什么、"
        "口径为什么这样定，用词贴近排产与计划场景"
    ),
    "craft_engineer": (
        "面向转岗数字化的工艺工程师：优先问方法选择与理由——为什么用这个口径或指标判断、"
        "换一种算法会怎样，鼓励对比与论证"
    ),
    "line_leader": (
        "面向一线班组长：步骤化短问，一次只问一个检查点，用词口语化贴近车间现场"
    ),
}


def resolve_follow_up_layer(
    current_layer: int,
    *,
    assessment: str,
) -> int:
    """Deterministically pick the cognitive layer for the NEXT question.

    答对进深层（封顶归因应用）、答错或未知停留原层。由后端在确定性守卫
    修正后的 assessment 上调用，LLM 不参与层的裁决。
    """

    if not isinstance(current_layer, int) or isinstance(current_layer, bool):
        raise ValueError("current_layer must be an integer")
    layer = min(max(current_layer, MIN_FOLLOW_UP_LAYER), MAX_FOLLOW_UP_LAYER)
    if assessment == "mastered":
        layer = min(layer + 1, MAX_FOLLOW_UP_LAYER)
    return layer


def _layer_name(layer: int) -> str:
    return FOLLOW_UP_LAYER_NAMES.get(layer, FOLLOW_UP_LAYER_NAMES[MIN_FOLLOW_UP_LAYER])


def _clip_digest(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _learner_context_payload(
    learner_context: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Normalize the persona context for the model payload.

    None/空值一律省略，保证冻结测试的载荷形状不变。
    """

    if not isinstance(learner_context, Mapping):
        return {}
    payload: dict[str, str] = {}
    profile_id = str(learner_context.get("profile_id") or "").strip()
    if not profile_id:
        return {}
    payload["profile_id"] = profile_id
    style = _PERSONA_QUESTION_STYLES.get(profile_id)
    if style:
        payload["question_style"] = style
    for key in ("title", "background", "lecture_style"):
        value = str(learner_context.get(key) or "").strip()
        if value:
            payload[key] = value
    return payload


_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
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
        "该工序完成率是多少，换算为百分比后是多少？",
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
        "请逐行列出查询结果中的船号和完成率？",
        "请引用船号和完成率说明当前比较结论？",
    ),
    "Q6": (
        "三道工序中哪一道完成率最低，你依据的工序和值是什么？",
        "请逐行列出查询结果中的工序和完成率？",
        "请引用查询结果中的一行，说明该行的工序和完成率？",
    ),
    "Q7": (
        "责任单元对比中哪个单元完成率最低，你依据的单元和值是什么？",
        "请逐行列出查询结果中的责任单元和完成率？",
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
_EVIDENCE_FIELD_QUESTION_TERMS = {
    "actual_qty": ("实际", "完成量"),
    "plan_qty": ("计划", "计划量"),
    "complete_rate": ("完成率", "百分比", "数值"),
    "month_label": ("月份", "哪个月", "月度"),
    "process_code": ("工序",),
    "ship_no": ("船号", "哪艘船"),
    "workshop_code": ("责任单元", "单元"),
}
_TEMPLATE_FALLBACK_QUESTIONS = {
    "T-02": (
        "查询结果中的完成率数值是多少？",
        "请复述查询结果给出的完成率数值？",
        "查询结果给出的完成率具体是多少？",
    ),
    "T-03": (
        "查询结果中AZTP和ZZTP的完成率分别是多少？",
        "按完成率从低到高，三道工序应怎样排序？",
        "请引用当前查询结果中的字段和值说明你的判断？",
    ),
    "T-03-A": (
        "查询结果中YCL在2025-05、ZZTP在2025-06和AZTP在2025-07的完成率分别是多少？",
        "查询结果中YCL、ZZTP、AZTP各自最低的月份和完成率分别是什么？",
        "查询结果中2025-05、2025-06、2025-07每个月完成率最低的工序和对应值分别是什么？",
    ),
    # T-03-B deliberately uses the row-bound fallback built below instead of
    # a template-level extrema question.  The advanced task still asks for
    # causal/temporal reasoning in its reviewed primary assessment; this is
    # only the fail-closed last-resort ladder.  Pinning one reviewed row per
    # question prevents R-02 from having to infer an uncontracted sorting or
    # aggregation operation when the model-generated probe is rejected.
    "T-10": (
        "查询结果中的月偏差率是多少？",
        "该偏差率为正值还是负值？",
        "只依据该偏差率，当前属于欠产还是超产？",
    ),
    "T-10-A": (
        "查询结果中WSA与WSB的高风险记录数分别是多少？",
        "两个责任单元的高风险记录数量是否相同，你依据的单元和数值是什么？",
        "这些结果能确认高风险记录数量分布，但为什么不能直接判定偏差排序或根因？",
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
    re.compile(r"(?:状态机|协议字段|转移名|工程实现|审核智能体)"),
)
_SNAKE_CASE_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", re.I)
# Learners may legitimately quote column labels shown by the read-only query
# result.  Keep the generic engineering-token gate, but exempt only the small
# set of business fields rendered in the production training tasks.
_LEARNER_VISIBLE_BUSINESS_FIELDS = frozenset({
    "ship_no",
    "process_code",
    "period_date",
    "plan_qty",
    "actual_qty",
    "complete_rate",
    "completion_rate",
    "workshop_code",
    "month_label",
})
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
    if bool(_ZERO_WIDTH_RE.search(normalized)) or any(
        pattern.search(normalized) for pattern in _ENGINEERING_PATTERNS
    ):
        return True
    return any(
        token.lower() not in _LEARNER_VISIBLE_BUSINESS_FIELDS
        for token in _SNAKE_CASE_TOKEN_RE.findall(normalized)
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
    required_targets: Sequence[str] = (),
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve one immutable next target and its backend-only route support."""

    if terminal_round or (completion_allowed and assessment == "mastered"):
        return None, ()
    required = tuple(
        dict.fromkeys(
            target for target in required_targets if target in allowed_targets
        )
    )
    if assessment == "mastered" and required:
        # A correct answer may close only the question it actually answered.
        # Any older open correction is selected before relation expansion.
        return required[0], ()
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


def _plan_actual_pair(
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[str, str] | None:
    """Return the first reviewed (plan_qty, actual_qty) textual pair."""

    for row in _expected_rows(evidence):
        if (
            _decimal_scalar(row.get("plan_qty")) is not None
            and _decimal_scalar(row.get("actual_qty")) is not None
        ):
            return (
                str(row["plan_qty"]).strip(),
                str(row["actual_qty"]).strip(),
            )
    return None


def _demonstrates_plan_actual_binding(
    answer_text: str,
    plan_text: str,
    actual_text: str,
) -> bool:
    text = _normalized_visible_text(answer_text or "")
    return (
        "计划" in text
        and "实际" in text
        and plan_text in text
        and actual_text in text
        and not (
            _answer_binds_field(text, "计划", actual_text)
            and _answer_binds_field(text, "实际", plan_text)
        )
    )


def _q2_binding_redundant(
    candidate: str,
    previous_answers: Sequence[str],
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """A binding-style question is redundant once an answer demonstrated it.

    The evidence-gap score cannot see prior answers, so a question re-asking
    the value-to-field correspondence (or the values themselves) would win
    on "covers a missing field" even though the learner already bound both
    values in an earlier round.  This flag demotes such candidates below
    every non-redundant one regardless of gap score.
    """

    pair = _plan_actual_pair(evidence)
    if pair is None:
        return False
    plan_text, actual_text = pair
    normalized = _normalized_visible_text(candidate)
    binding_style = (
        "计划" in normalized
        and "实际" in normalized
        and plan_text in normalized
        and actual_text in normalized
        and ("分别" in normalized or "对应" in normalized)
    )
    value_ask_style = (
        ("计划" in normalized and plan_text not in normalized)
        or ("实际" in normalized and actual_text not in normalized)
    ) and "多少" in normalized
    if not (binding_style or value_ask_style):
        return False
    return any(
        _demonstrates_plan_actual_binding(item, plan_text, actual_text)
        for item in previous_answers
        if isinstance(item, str)
    )


def _row_bound_fallback_questions(
    family: str,
    evidence: Sequence[Mapping[str, Any]],
    *,
    student_answer: str = "",
    previous_answers: Sequence[str] = (),
) -> tuple[str, ...]:
    """Build three probes from identifiers that literally occur in evidence.

    It avoids unresolved references (for example, "the other two processes")
    and does not invent sorting, causal or field-mapping operations that R-02
    cannot verify from the active task evidence.
    """

    if family == "Q2":
        pair = _plan_actual_pair(evidence)
        if pair is not None:
            plan_value, actual_value = pair
            binding_demonstrated = _demonstrates_plan_actual_binding(
                student_answer or "", plan_value, actual_value
            )
            binding_in_earlier_rounds = any(
                _demonstrates_plan_actual_binding(item, plan_value, actual_value)
                for item in previous_answers
                if isinstance(item, str)
            )
            if binding_demonstrated:
                # The learner just bound both values in the latest answer,
                # so the mandatory confirmation round must shift angle: ask
                # the citation decision (which value to quote for actual
                # progress) instead of re-asking the binding.
                return (
                    f"要说明实际完成进度，应引用{plan_value}还是{actual_value}，为什么？",
                    f"查询结果中的{plan_value}和{actual_value}分别对应计划量还是实际完成量？",
                    "查询结果给出的计划量数值是多少？",
                )
            if binding_in_earlier_rounds:
                # An earlier round already bound both values to both fields,
                # so value re-asks are meaningless across rounds.  Ask the
                # justification angle (why the actual value indicates
                # progress) — the single-value phrasing keeps it distinct
                # from the earlier citation decision round.
                return (
                    f"查询结果中的{plan_value}和{actual_value}分别对应计划量还是实际完成量？",
                    f"查询结果中的{actual_value}为什么能说明实际完成进度？",
                    f"要说明实际完成进度，应引用{plan_value}还是{actual_value}，为什么？",
                )
            # A learner who quoted both values without binding them to
            # fields has a mapping gap, not a value gap — the first probe
            # asks the binding directly using the reviewed row values.
            return (
                f"查询结果中的{plan_value}和{actual_value}分别对应计划量还是实际完成量？",
                "查询结果给出的计划量数值是多少？",
                "查询结果给出的实际完成量数值是多少？",
            )

    identifier_key = {
        "Q5": "ship_no",
        "Q6": "process_code",
        "Q7": "workshop_code",
    }.get(family)
    if identifier_key is None:
        return ()
    rows = _expected_rows(evidence)
    ordered_rows = rows
    distinct_identifiers = list(dict.fromkeys(
        str(row.get(identifier_key) or "").strip()
        for row in rows
        if str(row.get(identifier_key) or "").strip()
        and row.get("complete_rate") is not None
    ))
    if len(distinct_identifiers) >= 3:
        # Interleave one row per identifier first, so the ladder spreads
        # across the data instead of drilling three months of whichever
        # identifier happens to sort first.  Fewer identifiers keep the
        # original sequential order.
        grouped = [
            [
                row
                for row in rows
                if str(row.get(identifier_key) or "").strip() == pair
            ]
            for pair in distinct_identifiers
        ]
        ordered_rows = [
            row
            for group in grouped
            for row in group[:1]
        ] + [
            row
            for group in grouped
            for row in group[1:]
        ]
    questions: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in ordered_rows:
        identifier = str(row.get(identifier_key) or "").strip()
        if not identifier or row.get("complete_rate") is None:
            continue
        month = str(row.get("month_label") or "").strip()
        key = (identifier, month)
        if key in seen:
            continue
        seen.add(key)
        subject = f"{identifier}在{month}" if month else identifier
        questions.append(f"查询结果中{subject}的完成率是多少？")
        if len(questions) == 3:
            break
    return tuple(questions) if len(questions) == 3 else ()


_IDENTIFIER_COLUMN_NOUNS: dict[str, tuple[str, ...]] = {
    "process_code": ("工序",),
    "workshop_code": ("责任单元",),
    "ship_no": ("船号", "船"),
}


def _discriminator_variants(value: str) -> tuple[str, ...]:
    """Expand a row value into the surface forms a question may quote."""

    variants = {value, value.replace("-", ""), value.replace("-", "/")}
    month = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", value)
    if month:
        year, month_number = month.group(1), int(month.group(2))
        variants.add(f"{year}年{month_number}月")
        variants.add(f"{year}-{month_number:02d}")
    return tuple(variants)


def ambiguous_dimension_reference(
    question: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Reject dimension questions when reviewed rows repeat an identifier.

    When the expected rows distinguish one identifier across another
    dimension (for example the same process over three months), a question
    naming that dimension — or naming a repeated identifier value — must
    also pin a distinguishing value (a month, a ship, a rate); otherwise
    the learner cannot know which row to answer from.
    """

    rows = _expected_rows(evidence)
    if len(rows) < 2:
        return False
    per_dimension_quantifiers = ("分别", "每个", "每道", "每船", "各")
    for column, nouns in _IDENTIFIER_COLUMN_NOUNS.items():
        values = [
            str(row.get(column) or "").strip()
            for row in rows
        ]
        values = [value for value in values if value]
        if len(values) < 2 or len(set(values)) == len(values):
            continue
        repeated = {value for value in values if values.count(value) > 1}
        if not any(noun in question for noun in nouns) and not any(
            value in question for value in repeated
        ):
            continue
        if any(quantifier in question for quantifier in per_dimension_quantifiers):
            continue
        discriminators: set[str] = set()
        for row in rows:
            for key, raw in row.items():
                if key == column:
                    continue
                text = str(raw).strip()
                if text:
                    discriminators.update(_discriminator_variants(text))
        if any(variant in question for variant in discriminators):
            continue
        return True
    return False


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
        identifiers = [
            field_value
            for key, field_value in row.items()
            if key != metric_key
            and isinstance(field_value, str)
            and field_value.strip()
            and _decimal_scalar(field_value) is None
        ]
        if not identifiers:
            continue
        reviewed.append((row, value))
    if len(reviewed) < 2:
        return False
    extreme_value = (
        min(value for _, value in reviewed)
        if direction == "min"
        else max(value for _, value in reviewed)
    )
    winners = [row for row, value in reviewed if value == extreme_value]
    if len(winners) != 1:
        return False

    cited_percent_values = {
        Decimal(token)
        for token in re.findall(
            r"([-+]?\d+(?:\.\d+)?)\s*[%％]",
            normalized_answer,
        )
    }
    reviewed_percent_values = {
        value * Decimal("100") for _, value in reviewed
    }
    if cited_percent_values - reviewed_percent_values:
        # A positive deterministic override must fail closed when the same
        # answer also introduces an unreviewed percentage.  Otherwise a learner
        # can quote the right row and append a contradictory fabricated metric
        # while still being marked as mastered.
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


def _requires_reviewed_completion_extreme(
    *,
    question: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether mastery must be reproduced from reviewed extrema rows.

    The LLM may explain an answer, but it must not waive the deterministic
    evidence requirement for a completion-rate minimum/maximum question.  The
    guard is intentionally narrow so conceptual questions keep their existing
    semantic assessment path.
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    if "完成率" not in normalized_question:
        return False
    if not any(
        token in normalized_question
        for token in ("最低", "最小", "最高", "最大")
    ):
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
        if _match_key(key)
        in {
            "completerate",
            "completionrate",
            _match_key("完成率"),
        }
    ]
    if len(metric_keys) != 1:
        return False
    metric_key = metric_keys[0]
    identified_rows = [
        row
        for row in rows
        if _decimal_scalar(row.get(metric_key)) is not None
        and any(
            key != metric_key
            and isinstance(value, str)
            and value.strip()
            and _decimal_scalar(value) is None
            for key, value in row.items()
        )
    ]
    return len(identified_rows) >= 2


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


def _answer_binds_field(answer: str, field_term: str, value: str) -> bool:
    return (
        re.search(
            re.escape(field_term) + r"[^。；\n]{0,8}(?:是|为|=|：|:)?\s*"
            + re.escape(value),
            answer,
        )
        is not None
    )


def _citation_choice_context(
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[bool, bool] | None:
    """Classify an answer to the citation decision question.

    Returns (cites_correct_value, has_justification) when the active
    question is the plan/actual citation decision; otherwise None.  The
    correct citation is the reviewed actual completed value.
    """

    normalized_question = unicodedata.normalize("NFKC", question)
    if "应引用" not in normalized_question or "为什么" not in normalized_question:
        return None
    pair = _plan_actual_pair(evidence)
    if pair is None:
        return None
    plan_text, actual_text = pair
    normalized_answer = unicodedata.normalize("NFKC", answer)
    cites_actual = actual_text in normalized_answer
    cites_plan = plan_text in normalized_answer
    has_justification = any(
        marker in normalized_answer
        for marker in (
            "因为",
            "由于",
            "所以",
            "原因是",
            "表示",
            "反映",
            "说明",
            "才是",
            "才能",
            "实际完成量",
        )
    )
    return cites_actual and not cites_plan, has_justification


def _matches_reviewed_citation_justified(
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Correct citation choice WITH justification is deterministically mastered."""

    context = _citation_choice_context(question, answer, evidence)
    return context is not None and context[0] and context[1]


def citation_choice_lacks_reason(
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Correct citation choice WITHOUT justification is never mastered.

    The question explicitly asks "why"; picking the right value alone is
    half an answer, so a lenient model verdict is deterministically
    downgraded to needs_support and the next round asks for the reason.
    """

    context = _citation_choice_context(question, answer, evidence)
    return context is not None and context[0] and not context[1]


def _row_value_target(
    question: str,
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    """Find the reviewed row a row-value question asks about.

    Matches questions like "查询结果中AZTP在2025-05的完成率是多少？"
    against the identifier/month pairs in the reviewed evidence rows.
    """

    normalized_question = unicodedata.normalize("NFKC", question)
    if "完成率" not in normalized_question or "是多少" not in normalized_question:
        return None
    for row in _expected_rows(evidence):
        identifier = str(
            row.get("process_code")
            or row.get("workshop_code")
            or row.get("ship_no")
            or ""
        ).strip()
        month = str(row.get("month_label") or "").strip()
        # Only the pinned single-row form ("X在Y") — a bare identifier could
        # false-match multi-value questions like "AZTP和ZZTP的完成率分别是多少".
        if not identifier or not month or row.get("complete_rate") is None:
            continue
        if f"{identifier}在{month}" in normalized_question:
            return {
                "identifier": identifier,
                "month": month,
                "value": str(row["complete_rate"]).strip(),
            }
    return None


def _matches_reviewed_row_value(
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """A row-value read answered with the reviewed value is mastered."""

    target = _row_value_target(question, evidence)
    if target is None:
        return False
    return target["value"] in unicodedata.normalize("NFKC", answer)


def _matches_reviewed_monthly_trend(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Confirm a fully cited single-series monthly trend.

    This guard corrects model false negatives only.  It activates for a
    reviewed result containing exactly one value per month, requires every
    month/value binding in the learner answer, and then checks the stated
    direction against the ordered reviewed values.  Multi-process tables and
    answers containing an unreviewed percentage fail closed.
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    if not any(
        token in normalized_question
        for token in ("相邻月份", "持续趋势", "序列", "月度", "月份")
    ) or not any(
        token in normalized_question
        for token in ("变化", "趋势", "序列", "确认什么")
    ):
        return False
    rows = _expected_rows(evidence)
    monthly: list[tuple[str, Decimal]] = []
    for row in rows:
        month = str(row.get("month_label") or "").strip()
        rate = _decimal_scalar(row.get("complete_rate"))
        if not month or rate is None:
            return False
        monthly.append((month, rate))
    if len(monthly) < 2 or len({month for month, _ in monthly}) != len(monthly):
        return False
    monthly.sort(key=lambda item: item[0])
    normalized_answer = unicodedata.normalize("NFKC", answer).casefold()
    cited_percent_values = {
        Decimal(token)
        for token in re.findall(r"([-+]?\d+(?:\.\d+)?)\s*[%％]", normalized_answer)
    }
    reviewed_percent_values = {rate * Decimal("100") for _, rate in monthly}
    if cited_percent_values - reviewed_percent_values:
        return False
    rates = [rate for _, rate in monthly]
    if "最明显" in normalized_question or "变化最大" in normalized_question:
        deltas = [
            abs(right - left) for left, right in zip(rates, rates[1:])
        ]
        maximum = max(deltas)
        if deltas.count(maximum) != 1:
            return False
        index = deltas.index(maximum)
        earlier_month, earlier_rate = monthly[index]
        later_month = monthly[index + 1][0]
        later_rate = monthly[index + 1][1]
        return (
            earlier_month in normalized_answer
            and later_month in normalized_answer
            and str(earlier_rate) in normalized_answer
            and str(later_rate) in normalized_answer
            and any(
                phrase in normalized_answer for phrase in ("最明显", "变化最大")
            )
        )
    for month, rate in monthly:
        rate_text = str(rate)
        if re.search(
            re.escape(month) + r"[^。；\n]{0,24}" + re.escape(rate_text),
            normalized_answer,
        ) is None:
            return False
    if all(left > right for left, right in zip(rates, rates[1:])):
        return any(
            phrase in normalized_answer
            for phrase in ("连续下降", "持续下降", "逐月下降")
        ) and not any(
            phrase in normalized_answer
            for phrase in ("连续上升", "持续上升", "逐月上升")
        )
    if all(left < right for left, right in zip(rates, rates[1:])):
        return any(
            phrase in normalized_answer
            for phrase in ("连续上升", "持续上升", "逐月上升")
        ) and not any(
            phrase in normalized_answer
            for phrase in ("连续下降", "持续下降", "逐月下降")
        )
    return any(
        phrase in normalized_answer
        for phrase in ("有升有降", "阶段性波动", "不是单一持续趋势")
    )


def reviewed_row_value_correction(
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str | None:
    """Explain a value quoted from the wrong row without leaking the answer.

    When the learner cites a reviewed value that belongs to a DIFFERENT
    row than the one the question asks about, point at the row their value
    actually comes from — teaching row alignment while never revealing the
    asked row's own value.
    """

    target = _row_value_target(question, evidence)
    if target is None:
        return None
    normalized_answer = unicodedata.normalize("NFKC", answer)
    if target["value"] in normalized_answer:
        return None
    for row in _expected_rows(evidence):
        value = str(row.get("complete_rate") or "").strip()
        if not value or value not in normalized_answer:
            continue
        identifier = str(
            row.get("process_code")
            or row.get("workshop_code")
            or row.get("ship_no")
            or ""
        ).strip()
        month = str(row.get("month_label") or "").strip()
        if not identifier:
            continue
        subject = f"{identifier}在{month}" if month else identifier
        return (
            f"查询结果中的{value}对应的是{subject}；"
            f"请重新核对{target['identifier']}在{target['month']}这一行。"
        )
    return None


def _matches_reviewed_plan_actual_binding(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    """Deterministically confirm a fully bound plan/actual answer.

    The graded seed question asks both reviewed values and which one
    represents the completed quantity.  When the learner binds each value
    to the correct field — explicitly ("计划量是1855.06") or positionally
    ("计划量与实际完成量分别是1855.06和1156.87") — and states that the
    actual completed quantity represents what has been done, the answer is
    confirmed regardless of a hesitant model verdict.  Swapped bindings
    never pass.
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    normalized_answer = unicodedata.normalize("NFKC", answer).casefold()
    if "计划" not in normalized_question or "实际" not in normalized_question:
        return False
    if not any(
        token in normalized_question
        for token in ("分别是多少", "哪个", "哪一个", "表示已经完成")
    ):
        return False
    rows = _expected_rows(evidence)
    row = next(
        (
            item
            for item in rows
            if _decimal_scalar(item.get("plan_qty")) is not None
            and _decimal_scalar(item.get("actual_qty")) is not None
        ),
        None,
    )
    if row is None:
        return False
    plan_text = str(row.get("plan_qty")).strip()
    actual_text = str(row.get("actual_qty")).strip()
    explicit_binding = (
        _answer_binds_field(normalized_answer, "计划", plan_text)
        and _answer_binds_field(normalized_answer, "实际", actual_text)
    )
    positional_binding = (
        re.search(
            r"计划[^。；\n]{0,16}实际[^。；\n]{0,16}分别是\s*"
            + re.escape(plan_text)
            + r"\s*(?:和|与|、)\s*"
            + re.escape(actual_text),
            normalized_answer,
        )
        is not None
    )
    wrong_binding = (
        _answer_binds_field(normalized_answer, "计划", actual_text)
        and _answer_binds_field(normalized_answer, "实际", plan_text)
    )
    completed_statement = any(
        phrase in normalized_answer
        for phrase in ("已经完成", "已完成", "实际做了", "表示完成", "完成了多少")
    )
    return (
        explicit_binding or positional_binding
    ) and completed_statement and not wrong_binding


def reviewed_plan_actual_correction(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str | None:
    """Correct a reviewed plan/actual value swap after a learner attempt.

    This is feedback only; it never participates in mastery.  It is bounded
    to the reviewed scalar pair carried by the active task, so a model cannot
    invent either value while explaining the mistake.
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    normalized_answer = unicodedata.normalize("NFKC", answer).casefold()
    if "计划" not in normalized_question or "实际" not in normalized_question:
        return None
    pair = _plan_actual_pair(evidence)
    if pair is None:
        return None
    plan_text, actual_text = pair
    wrong_binding = (
        _answer_binds_field(normalized_answer, "计划", actual_text)
        and _answer_binds_field(normalized_answer, "实际", plan_text)
    )
    wrong_meaning = bool(
        re.search(
            r"计划(?:量)?[^。；，,\n]{0,28}(?:车间实际|实际完成|真正完成)",
            normalized_answer,
        )
        or re.search(
            r"实际(?:完成量|量)?[^。；，,\n]{0,28}(?:应该完成|应完成|计划目标|排产要求)",
            normalized_answer,
        )
    )
    if not (wrong_binding or wrong_meaning):
        return None
    return (
        f"需要纠正：查询结果中计划量为{plan_text}，"
        f"实际完成量为{actual_text}；实际完成量才表示车间真正完成的数量。"
    )


def _is_meaningless_short_answer(
    question: str,
    answer: str,
) -> bool:
    """无意义短答（如"bzd"等拼音缩写或乱码）——不是业务语言。

    ≤6字符、无数字、汉字与题目零重叠（或纯拉丁字母非业务代码）。
    """
    compact = answer.strip().rstrip("。！!？?")
    if len(compact) == 0 or len(compact) > 6:
        return False
    if any(character.isdigit() for character in compact):
        return False
    cjk_chars = [c for c in compact if "一" <= c <= "鿿"]
    if cjk_chars:
        question_chars = set(question)
        return not any(c in question_chars for c in cjk_chars)
    business = {"ycl", "zztp", "aztp", "sql"}
    if compact.lower() in business:
        return False
    return bool(re.fullmatch(r"[a-zA-Z]+", compact))


def _lacks_directional_statement(
    *,
    question: str,
    answer: str,
) -> bool:
    """题目要求方向性回答（哪个表示已完成）但答案只给了数值没有方向陈述。

    用于守卫"mastered"判定：两个数字对了只说明查了表，
    没说"哪个是已完成的"意味着口径理解未验证——降档继续核对。
    """

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    normalized_answer = unicodedata.normalize("NFKC", answer).casefold()
    directional = any(
        token in normalized_question
        for token in ("哪一个", "哪个", "哪个数", "哪一道", "表示已经完成", "意味着什么")
    )
    if not directional:
        return False
    has_completion = any(
        phrase in normalized_answer
        for phrase in (
            "已经完成", "已完成", "实际做了", "表示完成", "完成了多少",
            "最低", "最高", "最大", "最小", "最少", "最多",
            "意味着", "说明", "因为", "由于", "所以", "导致", "影响",
            "低于", "高于", "超过", "不足", "下滑", "下降",
        )
    )
    has_digits = any(character.isdigit() for character in normalized_answer)
    # 有字段/工序名绑定（如"计划量"、"完成率"、"YCL"）= 有理解方向
    has_field_binding = any(
        token in normalized_answer
        for token in (
            "计划量", "计划", "实际完成", "实际", "完成率", "口径",
            "ycl", "zztp", "aztp", "预处理", "制作", "安装",
            "上游", "下游", "传导", "工序",
        )
    )
    # 纯数字+无方向词+无字段绑定 = 缺方向；有任一即不触发
    return bool(has_digits) and not has_completion and not has_field_binding


def _matches_reviewed_answer(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> bool:
    return (
        _matches_reviewed_completion_extreme(
            question=question,
            answer=answer,
            evidence=evidence,
        )
        or _matches_reviewed_completion_interpretation(
            question=question,
            answer=answer,
            evidence=evidence,
        )
        or _matches_reviewed_plan_actual_binding(
            question=question,
            answer=answer,
            evidence=evidence,
        )
        or _matches_reviewed_citation_justified(question, answer, evidence)
        or _matches_reviewed_row_value(question, answer, evidence)
        or _matches_reviewed_monthly_trend(
            question=question,
            answer=answer,
            evidence=evidence,
        )
    )


def reviewed_answer_confirmation(
    *,
    question: str,
    answer: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str | None:
    """Explain a deterministic positive match without exposing internals.

    The confirmation is emitted only after the same strict, reviewed-evidence
    guard used for mastery succeeds.  It therefore cannot turn an unsupported
    model judgement into a learner-facing factual claim.
    """

    if not _matches_reviewed_answer(
        question=question,
        answer=answer,
        evidence=evidence,
    ):
        return None

    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    rows = _expected_rows(evidence)
    if not rows:
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

    direction: str | None = None
    if any(token in normalized_question for token in ("最低", "最小")):
        direction = "最低值"
        extreme = min
    elif any(token in normalized_question for token in ("最高", "最大")):
        direction = "最高值"
        extreme = max

    if direction is not None and len(rows) >= 2:
        reviewed = [
            (row, _decimal_scalar(row.get(metric_key)))
            for row in rows
        ]
        if all(value is not None for _, value in reviewed):
            target_value = extreme(value for _, value in reviewed if value is not None)
            winners = [row for row, value in reviewed if value == target_value]
            if len(winners) == 1:
                winner = winners[0]
                identifiers = [
                    str(value).strip()
                    for key, value in winner.items()
                    if key != metric_key
                    and isinstance(value, str)
                    and value.strip()
                    and _decimal_scalar(value) is None
                ]
                if identifiers:
                    return (
                        f"已核验：{identifiers[0]} 的完成率为 "
                        f"{winner[metric_key]}，与查询结果中的{direction}一致。"
                    )

    if len(rows) == 1:
        value = rows[0].get(metric_key)
        return f"已核验：回答引用的完成率 {value} 与当前查询结果一致。"
    return None


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _question_history_key(value: str) -> str:
    """Collapse harmless discourse markers before deterministic de-duplication."""

    key = _match_key(value)
    prefixes = (
        "请问",
        "请你",
        "请再",
        "请",
        "根据当前查询结果",
        "根据查询结果",
    )
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            prefix_key = _match_key(prefix)
            if key.startswith(prefix_key) and len(key) > len(prefix_key):
                key = key[len(prefix_key) :]
                changed = True
                break
    return key.removesuffix("呢")


_QUESTION_SUBJECT_VOCABULARY = (
    "计划量",
    "实际完成量",
    "完成率",
    "偏差率",
    "工序",
    "月份",
    "船号",
    "责任单元",
    "传导",
    "口径",
    "趋势",
    "异常",
)


def _semantic_subject_set(question: str) -> frozenset[str]:
    """Collect the business objects a question asks about."""

    normalized = _normalized_visible_text(question)
    subjects = {
        term
        for term in _QUESTION_SUBJECT_VOCABULARY
        if term in normalized
    }
    subjects.update(re.findall(r"\d+(?:\.\d+)?", normalized))
    subjects.update(re.findall(r"[A-Za-z]{2,}", normalized))
    return frozenset(subjects)


def _han_bigrams(question: str) -> frozenset[str]:
    normalized = _normalized_visible_text(question)
    return frozenset(
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
        if _HAN_RE.match(normalized[index])
        and _HAN_RE.match(normalized[index + 1])
    )


def _repeats_asked_question(
    question: str,
    previous_questions: Sequence[str],
) -> bool:
    """Reject paraphrases that re-ask the same objects for the same data.

    Two questions count as semantic repeats only when their subject sets
    are exactly equal (narrowing to one field or switching entity/month is
    a genuine new question) and their Han bigram containment is at least
    0.65 — a reworded misconception-confirmation legitimately overlaps the
    seed it follows (~0.57), while a pure paraphrase clone such as
    "计划量和实际完成量分别是多少" after "计划量与实际完成量分别是多少，
    哪一个表示已经完成的数量" overlaps ~0.74 and adds nothing.
    """

    subjects = _semantic_subject_set(question)
    if not subjects:
        return False
    bigrams = _han_bigrams(question)
    if not bigrams:
        return False
    for previous in previous_questions:
        if not isinstance(previous, str) or not previous.strip():
            continue
        if _semantic_subject_set(previous) != subjects:
            continue
        overlap = _han_bigrams(previous)
        if not overlap:
            continue
        containment = len(bigrams & overlap) / min(len(bigrams), len(overlap))
        if containment >= 0.65:
            return True
    return False


def _evidence_gap_score(question: str, fields: Sequence[str]) -> int:
    """Score whether a learner-facing question asks for declared evidence gaps."""

    normalized = _normalized_visible_text(question).casefold()
    return sum(
        1
        for field in dict.fromkeys(fields)
        if any(
            term.casefold() in normalized
            for term in _EVIDENCE_FIELD_QUESTION_TERMS.get(field, ())
        )
    )


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
    if ambiguous_dimension_reference(normalized, evidence):
        raise FollowUpGenerationError("question is ambiguous over repeated rows")
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
        previous_answers: Sequence[str] = (),
        task_agent: TaskAgent | None = None,
        required_target: str | None = None,
        required_evidence_fields: Sequence[str] = (),
        current_layer: int = MIN_FOLLOW_UP_LAYER,
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

        target_task = current_task
        if required_target is not None:
            if task_agent is None or required_target not in task_agent.misconception_ids:
                raise FollowUpGenerationError(
                    "required correction target is not in the current domain"
                )
            target_task = task_agent.counter_evidence(required_target)
        target_content = _payload_content(target_task)
        target_evidence = _evidence_items(target_task)
        source_standard_stem = str(
            target_content.get("standard_stem")
            or target_content.get("question")
            or ""
        ).strip()
        if not source_standard_stem:
            raise FollowUpGenerationError("follow-up evidence has no standard stem")
        template_id = str(target_content.get("template_id") or "").strip()
        template_questions = _TEMPLATE_FALLBACK_QUESTIONS.get(template_id)
        family = str(target_content.get("family") or "").strip()
        family_questions = _FAMILY_FALLBACK_QUESTIONS.get(family)
        row_bound_questions = _row_bound_fallback_questions(
            family,
            target_evidence,
            student_answer=student_answer,
            previous_answers=previous_answers,
        )
        standard_stem = source_standard_stem
        fallback_questions = (
            template_questions
            or row_bound_questions
            or family_questions
            or _DEFAULT_FALLBACK_QUESTIONS
        )
        previous_keys = {
            _question_history_key(item)
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
        # Preference order: (1) not a semantic repeat, (2) not redundant
        # with a binding an earlier answer already demonstrated — this
        # outranks the evidence-gap score, which cannot see prior answers,
        # (3) higher evidence-gap score, (4) rotation position.  Repeats and
        # redundant candidates stay eligible as a last resort so a small
        # ladder can never be exhausted.
        eligible_questions: list[tuple[bool, bool, int, int, str]] = []
        for template in ordered_questions:
            candidate = _validate_question(
                template.format(
                    standard_stem=source_standard_stem.rstrip("。？！?!"),
                ),
                standard_stem=source_standard_stem,
                evidence=target_evidence,
            )
            if _question_history_key(candidate) not in previous_keys:
                eligible_questions.append(
                    (
                        _repeats_asked_question(
                            candidate,
                            [
                                item
                                for item in previous_questions
                                if isinstance(item, str) and item.strip()
                            ],
                        ),
                        _q2_binding_redundant(
                            candidate,
                            [
                                item
                                for item in previous_answers
                                if isinstance(item, str)
                            ],
                            target_evidence,
                        ),
                        -_evidence_gap_score(candidate, required_evidence_fields),
                        len(eligible_questions),
                        candidate,
                    )
                )
        if eligible_questions:
            question = min(eligible_questions)[4]
        if not question:
            raise FollowUpGenerationError(
                "no unused evidence-bound fallback question remains"
            )
        evidence = target_evidence
        evidence_refs = [str(item["ref"]) for item in evidence]
        # 闭环六：回退模板题同样按确定性层盖章（掌握则下一问进深层）。
        fallback_layer = resolve_follow_up_layer(
            current_layer, assessment=assessment
        )
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
            "target_misconception": required_target or UNKNOWN_MISCONCEPTION,
            "follow_up_round": round_index,
            "max_follow_up_rounds": max_rounds,
            "follow_up_layer": fallback_layer,
            "layer_name": _layer_name(fallback_layer),
            "evidence_refs": evidence_refs,
        }
        for key in (
            "knowledge_point",
            "difficulty",
            "family",
            "template_id",
        ):
            value = target_content.get(key)
            if value is not None:
                content[key] = deepcopy(value)
        if current_content.get("responsibility_scope") is not None:
            content["responsibility_scope"] = deepcopy(
                current_content["responsibility_scope"]
            )
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
                "target_misconception": required_target or UNKNOWN_MISCONCEPTION,
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
            # A fallback that is explicitly correcting an already-open
            # misconception ticket must keep that target in both the
            # diagnosis and next-route fields.  Reporting UNKNOWN here while
            # routing to ``required_target`` makes the backend's independent
            # route recomputation disagree and safely reject the same turn
            # forever without advancing the learner round.
            diagnosed_misconception=(
                required_target or UNKNOWN_MISCONCEPTION
            ),
            next_target_misconception=(
                required_target or UNKNOWN_MISCONCEPTION
            ),
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
        previous_answers: Sequence[str] = (),
        required_next_targets: Sequence[str] = (),
        required_evidence_fields: Sequence[str] = (),
        learner_context: Mapping[str, Any] | None = None,
        lecture_digest: str = "",
        data_digest: str = "",
        current_layer: int = MIN_FOLLOW_UP_LAYER,
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
        if (
            not isinstance(current_layer, int)
            or isinstance(current_layer, bool)
            or not MIN_FOLLOW_UP_LAYER <= current_layer <= MAX_FOLLOW_UP_LAYER
        ):
            raise ValueError("current_layer must be between 1 and 3")

        allowed_targets = task_agent.misconception_ids
        required_targets = tuple(
            dict.fromkeys(
                target
                for target in required_next_targets
                if isinstance(target, str) and target in allowed_targets
            )
        )
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
        # 闭环六：个性化出题输入（缺省省略，保持冻结载荷形状）。层计划由后端
        # 确定性计算——LLM 只按与自己判定匹配的层措辞，层的最终值以后端盖章为准。
        learner_payload = _learner_context_payload(learner_context)
        clipped_lecture = _clip_digest(
            lecture_digest, limit=MAX_LECTURE_DIGEST_LENGTH
        )
        clipped_data = _clip_digest(data_digest, limit=MAX_DATA_DIGEST_LENGTH)
        layer_plan = {
            "current_layer": current_layer,
            "current_layer_name": _layer_name(current_layer),
            "on_mastered_layer": resolve_follow_up_layer(
                current_layer, assessment="mastered"
            ),
            "on_support_layer": current_layer,
        }
        personalization_payload: dict[str, Any] = {}
        if learner_payload:
            personalization_payload["learner_context"] = learner_payload
        if clipped_lecture:
            personalization_payload["lecture_digest"] = clipped_lecture
        if clipped_data:
            personalization_payload["data_digest"] = clipped_data
        personalization_payload["layer_plan"] = layer_plan
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
                        "previous_answers": [
                            item
                            for item in previous_answers
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
                        "required_next_targets": list(required_targets),
                        "required_evidence_fields": [
                            field
                            for field in dict.fromkeys(required_evidence_fields)
                            if isinstance(field, str) and field.strip()
                        ],
                        "review_feedback": [
                            str(item)
                            for item in review_feedback
                            if isinstance(item, str) and item.strip()
                        ],
                        **personalization_payload,
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
        if assessment in {"mastered", "unknown"} and citation_choice_lacks_reason(
            active_question,
            answer,
            current_evidence,
        ):
            # The citation decision explicitly asks "why" — a correct value
            # alone is half an answer.  Downgrade lenient or vague verdicts
            # so the outcome is deterministic across sessions.
            assessment = "needs_support"
            if diagnosed == UNKNOWN_MISCONCEPTION:
                diagnosed = (
                    str(allowed_targets[0])
                    if allowed_targets
                    else UNKNOWN_MISCONCEPTION
                )
        reviewed_answer_matches = _matches_reviewed_answer(
            question=active_question,
            answer=answer,
            evidence=current_evidence,
        )
        if assessment != "mastered" and reviewed_answer_matches:
            assessment = "mastered"
            diagnosed = UNKNOWN_MISCONCEPTION

        # 方向性守卫：题目问"哪个表示已完成"但答案纯数字无方向陈述——
        # 查表对了不等于理解口径，降为 unknown 继续核对方向
        if (
            assessment == "mastered"
            and not reviewed_answer_matches
            and _lacks_directional_statement(
                question=active_question,
                answer=answer,
            )
        ):
            assessment = "unknown"
            diagnosed = UNKNOWN_MISCONCEPTION
            proposed_next = UNKNOWN_MISCONCEPTION
        elif (
            assessment == "mastered"
            and _requires_reviewed_completion_extreme(
                question=active_question,
                evidence=current_evidence,
            )
            and not reviewed_answer_matches
        ):
            return self.deterministic_fallback(
                current_task=current_task,
                round_index=round_index,
                max_rounds=max_rounds,
                student_answer=answer,
                current_question=active_question,
                completion_allowed=False,
                terminal_round=terminal_round,
                previous_questions=previous_questions,
                task_agent=task_agent,
                required_target=(required_targets[0] if required_targets else None),
                required_evidence_fields=required_evidence_fields,
                current_layer=current_layer,
            )
        proposed_next = (
            None
            if result.data["next_target_misconception"] == NO_NEXT_TARGET
            else str(result.data["next_target_misconception"])
        )
        # 宽松“掌握”判定防线（在既有确定性规则之后、放行之前把关）：轮次
        # 已达上限的最后一轮，答案既没有确定性核验结论，也没有引用任何
        # 数值（如整轮只答一个词）时，不据此放行升档，按未掌握收尾——
        # 上限轮的“翻盘掌握”必须有证据支撑。学员下一轮引用具体数值即
        # 可正常通过；非上限轮不受影响。
        if (
            terminal_round
            and assessment == "mastered"
            and not reviewed_answer_matches
            and reviewed_answer_confirmation(
                question=active_question,
                answer=answer,
                evidence=current_evidence,
            )
            is None
            and not any(character.isdigit() for character in answer)
        ):
            assessment = "unknown"
            diagnosed = UNKNOWN_MISCONCEPTION
            proposed_next = UNKNOWN_MISCONCEPTION
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
            required_targets=required_targets,
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
        # 无意义短答守卫：bzd 等乱码不应判"部分有效"或"掌握"（须在题面校验后，
        # 否则问题校验测试因路由先报错而失败）
        if (
            assessment in {"mastered", "needs_support"}
            and _is_meaningless_short_answer(active_question, answer)
        ):
            assessment = "unknown"
            diagnosed = UNKNOWN_MISCONCEPTION
            proposed_next = UNKNOWN_MISCONCEPTION
        previous_keys = {
            _question_history_key(item)
            for item in previous_questions
            if isinstance(item, str) and item.strip()
        }
        if _question_history_key(question) in previous_keys:
            raise FollowUpGenerationError("follow-up question repeats history")
        if _repeats_asked_question(question, previous_questions):
            raise FollowUpGenerationError(
                "follow-up question semantically repeats history"
            )
        if _q2_binding_redundant(
            question,
            [item for item in previous_answers if isinstance(item, str)],
            evidence,
        ):
            raise FollowUpGenerationError(
                "follow-up question re-asks a demonstrated binding"
            )
        evidence_refs = [str(item["ref"]) for item in evidence]
        # 闭环六：层的最终值由后端按确定性判定盖章（答对进深层、答错停留），
        # LLM 的措辞层若与之不一致也不影响该权威值。
        resolved_layer = resolve_follow_up_layer(
            current_layer, assessment=assessment
        )
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
            "follow_up_layer": resolved_layer,
            "layer_name": _layer_name(resolved_layer),
            "evidence_refs": evidence_refs,
        }
        for key in (
            "knowledge_point",
            "difficulty",
            "family",
            "template_id",
        ):
            value = base_content.get(key)
            if value is not None:
                content[key] = deepcopy(value)
        if current_content.get("responsibility_scope") is not None:
            content["responsibility_scope"] = deepcopy(
                current_content["responsibility_scope"]
            )
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

    def generate_initial(
        self,
        *,
        current_task: Mapping[str, Any],
        learner_context: Mapping[str, Any] | None = None,
        lecture_digest: str = "",
        data_digest: str = "",
        previous_questions: Sequence[str] = (),
    ) -> str:
        """闭环六：第 1 轮追问的个性化变式（口径记忆层）。

        同一知识点在不同画像下的首问措辞与侧重不同；仅出题、不做任何
        判定。任何失败都抛 ``FollowUpGenerationError``，由调用方回退到
        既有族模板题（``_INITIAL_FOLLOW_UP_QUESTIONS``），保证不出现空题。
        """

        content = _payload_content(current_task)
        evidence = _evidence_items(current_task)
        standard_stem = str(
            content.get("standard_stem")
            or content.get("question")
            or ""
        ).strip()
        if not standard_stem:
            raise FollowUpGenerationError("follow-up evidence has no standard stem")
        learner_payload = _learner_context_payload(learner_context)
        clipped_lecture = _clip_digest(
            lecture_digest, limit=MAX_LECTURE_DIGEST_LENGTH
        )
        clipped_data = _clip_digest(data_digest, limit=MAX_DATA_DIGEST_LENGTH)
        initial_payload: dict[str, Any] = {
            "round_kind": "initial",
            "current_task": {
                key: content.get(key)
                for key in (
                    "question",
                    "standard_stem",
                    "contextualized_stem",
                    "knowledge_point",
                    "responsibility_scope",
                    "difficulty",
                    "family",
                )
            },
            "current_evidence_summary": list(_expected_points(evidence)),
            "current_evidence_rows": list(_expected_rows(evidence)),
            "answer_requirements": _answer_requirements(content, evidence),
            "previous_questions": [
                item
                for item in previous_questions
                if isinstance(item, str) and item.strip()
            ],
        }
        if learner_payload:
            initial_payload["learner_context"] = learner_payload
        if clipped_lecture:
            initial_payload["lecture_digest"] = clipped_lecture
        if clipped_data:
            initial_payload["data_digest"] = clipped_data
        initial_payload["target_layer"] = MIN_FOLLOW_UP_LAYER
        initial_payload["layer_name"] = _layer_name(MIN_FOLLOW_UP_LAYER)
        schema = {
            "type": "object",
            "required": ["question"],
            "properties": {
                "question": {"type": "string", "minLength": 1},
            },
        }

        def attempt(repair_hint: str = "") -> str:
            payload = dict(initial_payload)
            if repair_hint:
                payload["repair_hint"] = repair_hint
            try:
                result = self._llm_call(
                    model=MODEL,
                    system=self._system_prompt,
                    user=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json_schema=schema,
                    temperature=TEMPERATURE,
                )
            except Exception as exc:
                raise FollowUpGenerationError(
                    "initial follow-up generation is temporarily unavailable"
                ) from exc
            if not isinstance(result, LLMResult):
                raise FollowUpGenerationError(
                    "follow-up model returned no metadata"
                )
            # 系统提示词规定四字段输出格式，首问调用只消费 question；其余字段
            # （assessment 等）即使返回也一概忽略——首问不做任何判定。
            raw_question = result.data.get("question")
            if not isinstance(raw_question, str) or not raw_question.strip():
                raise FollowUpGenerationError(
                    "initial follow-up model output is invalid"
                )
            return _validate_question(
                raw_question,
                standard_stem=standard_stem,
                evidence=evidence,
            )

        # 首问自修复：风格化改写偶发双问句/引用失据，带校验原因重试一次；
        # 仍失败则抛错，由会话层回退族模板题（不出现空题）。
        try:
            question = attempt()
        except FollowUpGenerationError as first_error:
            question = attempt(
                "上一版问题未通过校验（"
                + str(first_error)
                + "），请改写为符合全部规则的单一问句。"
            )
        previous_keys = {
            _question_history_key(item)
            for item in previous_questions
            if isinstance(item, str) and item.strip()
        }
        if _question_history_key(question) in previous_keys:
            raise FollowUpGenerationError("initial question repeats history")
        if _repeats_asked_question(question, previous_questions):
            raise FollowUpGenerationError(
                "initial question semantically repeats history"
            )
        return question
