"""Deterministic pretest diagnosis from the approved P4/P5 assets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Callable

from agents.diagnostic_router import DiagnosticRouter, ProbeResult
from orchestrator.llm import LLMResult


PROFILE_ORDER = ("planner_new", "craft_engineer", "line_leader")
DIFFICULTIES = ("basic", "applied", "advanced")
PROFILE_DIR = Path(__file__).with_name("profiles")
PRETEST_PATH = Path(__file__).resolve().parents[1] / "eval" / "cases" / "pretest.json"
MODEL = "qwen3-32b"
TEMPERATURE = 0.3
PROMPT_PATH = Path(__file__).with_name("prompts") / "diagnosis_narrative.md"
DIAGNOSIS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["narrative", "suggestions"],
    "properties": {
        "narrative": {"type": "string", "maxLength": 150},
        "suggestions": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "minLength": 1},
        },
    },
    "additionalProperties": False,
}
_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "title",
        "background",
        "strengths",
        "gaps_prior",
        "difficulty_start",
        "lecture_style",
        # 闭环一：画像学习领域清单（展示层/关注点过滤用；路由本闭环不读它）
        "knowledge_scope",
        # 闭环五：实操模式（sql=学员书写查询；data_present=系统代执行并呈现）
        "practice_mode",
    }
)
QUESTION_FIELDS = frozenset(
    {"question_id", "knowledge_point", "misconceptions", "stem", "options", "answer"}
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON asset {path}: {exc}") from exc


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return list(value)


def load_profiles(directory: Path = PROFILE_DIR) -> dict[str, dict[str, Any]]:
    """Load the three approved profiles in their contractual order."""

    resolved = Path(directory)
    profiles: dict[str, dict[str, Any]] = {}
    for profile_id in PROFILE_ORDER:
        path = resolved / f"{profile_id}.json"
        raw = _read_json(path)
        if not isinstance(raw, dict) or set(raw) != PROFILE_FIELDS:
            raise ValueError(f"{path}: profile fields do not match the approved schema")
        if raw.get("profile_id") != profile_id:
            raise ValueError(f"{path}: profile_id must be {profile_id}")
        for field in ("profile_id", "title", "background", "lecture_style"):
            _non_empty_string(raw[field], field)
        raw["strengths"] = _string_list(raw["strengths"], "strengths")
        raw["gaps_prior"] = _string_list(raw["gaps_prior"], "gaps_prior")
        raw["knowledge_scope"] = _string_list(raw["knowledge_scope"], "knowledge_scope")
        if raw["practice_mode"] not in {"sql", "data_present"}:
            raise ValueError("practice_mode must be sql|data_present")
        if raw["difficulty_start"] not in DIFFICULTIES:
            raise ValueError("difficulty_start must be basic|applied|advanced")
        profiles[profile_id] = raw
    return profiles


def load_pretest(path: Path = PRETEST_PATH) -> tuple[dict[str, Any], ...]:
    """Load the five approved single-choice questions in question-id order."""

    raw = _read_json(Path(path))
    if not isinstance(raw, list) or len(raw) != 5:
        raise ValueError("pretest must contain exactly five questions")
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict) or set(item) != QUESTION_FIELDS:
            raise ValueError(f"pretest[{index - 1}] fields do not match the approved schema")
        expected_id = f"PT-{index}"
        if item.get("question_id") != expected_id:
            raise ValueError(f"pretest question order must be {expected_id}")
        _non_empty_string(item["knowledge_point"], "knowledge_point")
        _non_empty_string(item["stem"], "stem")
        item["misconceptions"] = _string_list(
            item["misconceptions"], "misconceptions"
        )
        options = item["options"]
        if not isinstance(options, dict) or list(options) != ["A", "B", "C", "D"]:
            raise ValueError(f"{expected_id}.options must contain ordered A/B/C/D")
        for option_id, text in options.items():
            _non_empty_string(text, f"{expected_id}.options.{option_id}")
        if item["answer"] not in options:
            raise ValueError(f"{expected_id}.answer must reference an option")
        questions.append(item)
    return tuple(questions)


def _append_once(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _step_up(difficulty: str) -> str:
    index = DIFFICULTIES.index(difficulty)
    return DIFFICULTIES[min(index + 1, len(DIFFICULTIES) - 1)]


def _numbers_in(value: Any) -> set[Decimal]:
    numbers: set[Decimal] = set()
    if isinstance(value, bool) or value is None:
        return numbers
    if isinstance(value, (int, float, Decimal)):
        numbers.add(Decimal(str(value)))
        return numbers
    if isinstance(value, str):
        numbers.update(Decimal(match.group(0)) for match in _NUMBER_RE.finditer(value))
        return numbers
    if isinstance(value, Mapping):
        for item in value.values():
            numbers.update(_numbers_in(item))
        return numbers
    if isinstance(value, Sequence):
        for item in value:
            numbers.update(_numbers_in(item))
    return numbers


def _narrative_numbers_are_grounded(
    narrative: str, diagnosis_data: Mapping[str, Any]
) -> bool:
    """Require every Arabic number in the narrative to occur in diagnosis data."""

    return _numbers_in(narrative).issubset(_numbers_in(diagnosis_data))


class DiagnosisAgent:
    """Score one approved pretest and optionally add an LLM narrative layer."""

    def __init__(
        self,
        trace_id: str,
        profiles: Mapping[str, Mapping[str, Any]] | None = None,
        pretest: Sequence[Mapping[str, Any]] | None = None,
        llm_call: Callable[..., LLMResult] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        self._trace_id = trace_id
        loaded_profiles = load_profiles() if profiles is None else profiles
        loaded_pretest = load_pretest() if pretest is None else pretest
        self._profiles = {key: dict(value) for key, value in loaded_profiles.items()}
        self._pretest = tuple(dict(question) for question in loaded_pretest)
        self._llm_call = llm_call
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self._router = DiagnosticRouter()

    def assess(
        self,
        profile_id: str,
        answers: Mapping[str, str],
        probe_results: Sequence[ProbeResult | Mapping[str, Any]] = (),
        *,
        experience_tags: Sequence[str] = (),
    ) -> dict[str, Any]:
        if profile_id not in self._profiles:
            raise ValueError(f"unsupported profile_id: {profile_id}")
        if not isinstance(answers, Mapping):
            raise ValueError("answers must be a mapping")
        expected_ids = [question["question_id"] for question in self._pretest]
        if set(answers) != set(expected_ids):
            raise ValueError("answers must contain exactly the five approved question IDs")
        for question in self._pretest:
            question_id = question["question_id"]
            if answers[question_id] not in question["options"]:
                raise ValueError(f"{question_id} answer must be an approved option")

        profile = self._profiles[profile_id]
        blind_spots = list(profile["gaps_prior"])
        hit_misconceptions: list[str] = []
        correct = 0
        for question in self._pretest:
            question_id = question["question_id"]
            if answers[question_id] == question["answer"]:
                correct += 1
                continue
            _append_once(blind_spots, str(question["knowledge_point"]))
            for misconception in question["misconceptions"]:
                _append_once(hit_misconceptions, str(misconception))

        total = len(self._pretest)
        rate = round(correct / total, 4)
        if rate < 0.4:
            difficulty = "basic"
        elif rate <= 0.8:
            difficulty = str(profile["difficulty_start"])
        else:
            difficulty = _step_up(str(profile["difficulty_start"]))

        route = self._router.route(
            profile_id,
            answers,
            probe_results,
            experience_tags=experience_tags,
        )
        knowledge_point_plan = route["knowledge_point_plan"]
        selected_difficulty = route["selected_difficulty"] or difficulty
        blind_spots = [
            str(item["knowledge_point"])
            for item in knowledge_point_plan
            if item["mastery_status"] in {"needs_training", "pending_training"}
        ]

        content: dict[str, Any] = {
            "event": "diagnosis_ready",
            "profile_id": profile_id,
            "blind_spots": blind_spots,
            "hit_misconceptions": hit_misconceptions,
            # Preserve the legacy aggregate field for forced/internal module
            # regression.  Production contracts consume ``selected_difficulty``
            # explicitly, so probe-routed tasks still use the point-level value.
            "difficulty": difficulty,
            "pretest_baseline_difficulty": difficulty,
            "knowledge_point_plan": knowledge_point_plan,
            "selected_plan_item_id": route["selected_plan_item_id"],
            "selected_knowledge_point": route["selected_knowledge_point"],
            "selected_difficulty": selected_difficulty,
            "route_evidence": route["route_evidence"],
            "diagnostic_probe_count": route["probe_count"],
            "router_version": route["router_version"],
            "experience_tags": route["experience_tags"],
            "recommended_probe_knowledge_point": route[
                "recommended_probe_knowledge_point"
            ],
            "probe_recommendation_evidence": route[
                "probe_recommendation_evidence"
            ],
            "pretest_score": {
                "correct": correct,
                "total": total,
                "rate": rate,
            },
        }
        metadata = self._add_narrative(content, profile)
        draft = {
            "trace_id": self._trace_id,
            "agent": "diagnosis",
            "role": "produce",
            "payload": {
                "type": "profile_assessment",
                "content": content,
            },
            "evidence": [],
            "claims": [],
            "student_profile_ref": profile_id,
            "timestamp": self._clock().isoformat(),
        }
        draft.update(metadata)
        return draft

    def _add_narrative(
        self,
        diagnosis_data: dict[str, Any],
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        started = perf_counter()
        if self._llm_call is None:
            diagnosis_data.update(
                {
                    "diagnosis_narrative": "",
                    "suggestions": [],
                    "narrative_fallback": True,
                    "narrative_fallback_reason": "llm_disabled",
                    "llm_latency_ms": 0,
                }
            )
            return {}

        narrative_basis = {
            key: diagnosis_data[key]
            for key in (
                "profile_id",
                "blind_spots",
                "hit_misconceptions",
                "difficulty",
                "pretest_score",
            )
        }
        user_message = (
            "[诊断数据JSON]"
            + json.dumps(
                {
                    "diagnosis_data": narrative_basis,
                    "narrative_number_whitelist": [
                        format(number, "f")
                        for number in sorted(_numbers_in(narrative_basis))
                    ],
                    "numeric_copy_rule": (
                        "叙述若使用阿拉伯数字，只能逐字复制"
                        "narrative_number_whitelist；禁止换算、添加百分号或组合新数字"
                    ),
                    "safe_score_phrase": (
                        f"答对{diagnosis_data['pretest_score']['correct']}/"
                        f"{diagnosis_data['pretest_score']['total']}题，正确率"
                        f"{diagnosis_data['pretest_score']['rate']}"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "[画像JSON]"
            + json.dumps(
                dict(profile),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        try:
            result = self._llm_call(
                model=MODEL,
                system=self._system_prompt,
                user=user_message,
                json_schema=DIAGNOSIS_OUTPUT_SCHEMA,
                temperature=TEMPERATURE,
            )
        except Exception:
            elapsed_ms = round(max(0.0, perf_counter() - started) * 1000)
            diagnosis_data.update(
                {
                    "diagnosis_narrative": "",
                    "suggestions": [],
                    "narrative_fallback": True,
                    "narrative_fallback_reason": "llm_failure",
                    "llm_latency_ms": elapsed_ms,
                }
            )
            return {
                "model": MODEL,
                "latency_ms": elapsed_ms,
                "token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }

        narrative = result.data.get("narrative")
        suggestions = result.data.get("suggestions")
        structurally_valid = (
            isinstance(narrative, str)
            and len(narrative) <= 150
            and isinstance(suggestions, list)
            and len(suggestions) <= 3
            and all(isinstance(item, str) and item for item in suggestions)
        )
        numbers_grounded = structurally_valid and _narrative_numbers_are_grounded(
            narrative, narrative_basis
        )
        fallback = not numbers_grounded
        diagnosis_data.update(
            {
                "diagnosis_narrative": "" if fallback else narrative,
                "suggestions": [] if fallback else list(suggestions),
                "narrative_fallback": fallback,
                "narrative_fallback_reason": (
                    "numeric_mismatch" if structurally_valid else "invalid_output"
                )
                if fallback
                else None,
                "llm_latency_ms": result.latency_ms,
            }
        )
        elapsed_ms = round(max(0.0, perf_counter() - started) * 1000)
        return {
            "model": result.model,
            "latency_ms": max(elapsed_ms, result.latency_ms),
            "token_usage": result.token_usage.as_dict(),
        }
