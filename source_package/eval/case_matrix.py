"""Load and validate the user-approved P7 50-case matrix."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "eval" / "cases" / "e2e_50_matrix.json"
APPROVED_MATRIX_SHA256 = (
    "9b8f45d72ec0dd6e00ac96ed4fd814df8e0f3bb0f80e3aac2e40932060b5c6c5"
)
APPROVED_SAMPLE_IDS = (
    "E2E-001",
    "E2E-010",
    "E2E-012",
    "E2E-018",
    "E2E-023",
    "E2E-030",
    "E2E-034",
    "E2E-036",
    "E2E-044",
    "E2E-050",
)

PRETEST_PATTERNS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "A5": MappingProxyType(
            {"PT-1": "B", "PT-2": "B", "PT-3": "C", "PT-4": "B", "PT-5": "C"}
        ),
        "A4": MappingProxyType(
            {"PT-1": "A", "PT-2": "B", "PT-3": "C", "PT-4": "B", "PT-5": "C"}
        ),
        "A3": MappingProxyType(
            {"PT-1": "B", "PT-2": "A", "PT-3": "C", "PT-4": "A", "PT-5": "C"}
        ),
        "A2": MappingProxyType(
            {"PT-1": "A", "PT-2": "B", "PT-3": "A", "PT-4": "A", "PT-5": "C"}
        ),
        "A1": MappingProxyType(
            {"PT-1": "A", "PT-2": "A", "PT-3": "A", "PT-4": "A", "PT-5": "C"}
        ),
        "A0": MappingProxyType(
            {"PT-1": "A", "PT-2": "A", "PT-3": "A", "PT-4": "A", "PT-5": "A"}
        ),
    }
)

CORE_POINTS = frozenset(
    {
        "三道工序与传导关系",
        "计划量与实际量口径",
        "完成率计算",
        "偏差率与风险等级",
        "月度聚合方法",
        "异常识别标准",
        "传导时滞分析",
        "异常衰减规律",
        "责任单元定位",
        "跨工序归因方法",
    }
)
PROFILE_START = MappingProxyType(
    {"planner_new": "basic", "craft_engineer": "applied", "line_leader": "basic"}
)
DIFFICULTIES = ("basic", "applied", "advanced")
APPROVED_TASK_TEMPLATE_IDS = frozenset(
    {*(f"T-{item:02d}" for item in range(1, 11)), "T-08-DECAY"}
)
PATH_BY_PATTERN = MappingProxyType(
    {
        "A5": "direct_correct",
        "A4": "direct_correct",
        "A3": "rebuttal_corrected",
        "A2": "rebuttal_corrected",
        "A1": "rebuttal_corrected",
        "A0": "second_wrong_step_down",
    }
)
EXPECTED_FIELDS = frozenset(
    {
        "case_id",
        "profile_id",
        "knowledge_point",
        "pretest_pattern",
        "answers",
        "task_template_id",
        "learning_path",
        "misconception_id",
        "manual_review",
    }
)


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    profile_id: str
    knowledge_point: str
    pretest_pattern: str
    answers: Mapping[str, str]
    task_template_id: str
    learning_path: str
    misconception_id: str | None
    manual_review: bool

    @property
    def expected_difficulty(self) -> str:
        score = int(self.pretest_pattern[1])
        if score < 2:
            return "basic"
        start = PROFILE_START[self.profile_id]
        if score <= 4:
            return start
        return DIFFICULTIES[min(DIFFICULTIES.index(start) + 1, 2)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "profile_id": self.profile_id,
            "knowledge_point": self.knowledge_point,
            "pretest_pattern": self.pretest_pattern,
            "answers": dict(self.answers),
            "task_template_id": self.task_template_id,
            "learning_path": self.learning_path,
            "misconception_id": self.misconception_id,
            "manual_review": self.manual_review,
            "expected_difficulty": self.expected_difficulty,
        }


def _canonical_hash(raw: Any) -> str:
    payload = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_string(row: Mapping[str, Any], field: str, index: int) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise ValueError(f"matrix row {index} {field} must be a non-empty string")
    return value


def _parse_case(row: Any, index: int) -> EvaluationCase:
    if not isinstance(row, Mapping) or set(row) != EXPECTED_FIELDS:
        raise ValueError(f"matrix row {index} fields do not match approved schema")
    pattern = _require_string(row, "pretest_pattern", index)
    if pattern not in PRETEST_PATTERNS:
        raise ValueError(f"matrix row {index} has unknown pretest_pattern")
    answers = row["answers"]
    if not isinstance(answers, Mapping) or dict(answers) != dict(PRETEST_PATTERNS[pattern]):
        raise ValueError(f"matrix row {index} answers do not match {pattern}")
    profile_id = _require_string(row, "profile_id", index)
    if profile_id not in PROFILE_START:
        raise ValueError(f"matrix row {index} has unknown profile_id")
    knowledge_point = _require_string(row, "knowledge_point", index)
    if knowledge_point not in CORE_POINTS:
        raise ValueError(f"matrix row {index} has unknown knowledge_point")
    task_template_id = _require_string(row, "task_template_id", index)
    if task_template_id not in APPROVED_TASK_TEMPLATE_IDS:
        raise ValueError(f"matrix row {index} has unknown task_template_id")
    learning_path = _require_string(row, "learning_path", index)
    if learning_path != PATH_BY_PATTERN[pattern]:
        raise ValueError(f"matrix row {index} path does not match {pattern}")
    misconception_id = row["misconception_id"]
    if learning_path == "direct_correct":
        if misconception_id is not None:
            raise ValueError(f"matrix row {index} direct path cannot have misconception")
    elif misconception_id not in {f"M-{item:02d}" for item in range(1, 6)}:
        raise ValueError(f"matrix row {index} probe path needs approved misconception")
    manual_review = row["manual_review"]
    if not isinstance(manual_review, bool):
        raise ValueError(f"matrix row {index} manual_review must be boolean")
    return EvaluationCase(
        case_id=_require_string(row, "case_id", index),
        profile_id=profile_id,
        knowledge_point=knowledge_point,
        pretest_pattern=pattern,
        answers=MappingProxyType(dict(answers)),
        task_template_id=task_template_id,
        learning_path=learning_path,
        misconception_id=misconception_id,
        manual_review=manual_review,
    )


def _validate_quotas(cases: tuple[EvaluationCase, ...]) -> None:
    expected_ids = [f"E2E-{index:03d}" for index in range(1, 51)]
    if [case.case_id for case in cases] != expected_ids:
        raise ValueError("case IDs must be exactly E2E-001..E2E-050 in order")
    if Counter(case.profile_id for case in cases) != {
        "planner_new": 17,
        "craft_engineer": 17,
        "line_leader": 16,
    }:
        raise ValueError("profile quota does not match approved 17/17/16")
    if Counter(case.knowledge_point for case in cases) != {
        point: 5 for point in CORE_POINTS
    }:
        raise ValueError("knowledge-point quota must be ten points times five")
    if Counter(case.pretest_pattern for case in cases) != {
        "A5": 10,
        "A4": 8,
        "A3": 8,
        "A2": 8,
        "A1": 8,
        "A0": 8,
    }:
        raise ValueError("pretest-pattern quota does not match approved matrix")
    if Counter(case.learning_path for case in cases) != {
        "direct_correct": 18,
        "rebuttal_corrected": 24,
        "second_wrong_step_down": 8,
    }:
        raise ValueError("learning-path quota does not match approved 18/24/8")
    if tuple(case.case_id for case in cases if case.manual_review) != APPROVED_SAMPLE_IDS:
        raise ValueError("manual-review sample IDs do not match approval")
    if Counter(case.expected_difficulty for case in cases) != {
        "basic": 29,
        "applied": 19,
        "advanced": 2,
    }:
        raise ValueError("expected difficulty quota does not match approved 29/19/2")
    if {case.task_template_id for case in cases} != APPROVED_TASK_TEMPLATE_IDS:
        raise ValueError("all approved task templates must be covered")


def load_case_matrix(path: Path = MATRIX_PATH) -> tuple[EvaluationCase, ...]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load case matrix: {exc}") from exc
    if not isinstance(raw, list) or len(raw) != 50:
        raise ValueError("case matrix must contain exactly 50 rows")
    cases = tuple(_parse_case(row, index) for index, row in enumerate(raw, start=1))
    _validate_quotas(cases)
    if _canonical_hash(raw) != APPROVED_MATRIX_SHA256:
        raise ValueError("case matrix content differs from user-approved matrix")
    return cases
