"""Frozen v4 persona-route inputs and independently stored gold standard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from agents.persona_router import load_persona_pretest


ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "eval" / "cases" / "v4" / "formal_50_inputs_v4.json"
GOLD_PATH = ROOT / "eval" / "gold" / "v4" / "formal_50_gold_v4.json"
FORBIDDEN_RUNTIME_KEYS = {
    "knowledge_point",
    "template_id",
    "target_knowledge_point",
    "expected_knowledge_point",
    "expected_template_id",
    "目标知识点",
    "预期初始模板",
    "预期最终模板",
}
SCRIPT_IDS = {
    "S-STEP-UP-B",
    "S-STEP-UP-A",
    "S-REFRESH",
    "S-REBUTTAL",
    "S-DOWNSTEP",
}


@dataclass(frozen=True, slots=True)
class V4ProbeAnswer:
    probe_id: str
    answer: str


@dataclass(frozen=True, slots=True)
class V4FormalCase:
    case_id: str
    route_mode: str
    profile_id: str
    experience_tags: tuple[str, ...]
    pretest_answers: Mapping[str, str]
    diagnostic_probe_answers: tuple[V4ProbeAnswer, ...]
    learner_script_id: str


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid frozen v4 asset {path}: {exc}") from exc


def load_formal_cases(path: Path = INPUT_PATH) -> tuple[V4FormalCase, ...]:
    raw = _load_json(Path(path))
    if not isinstance(raw, list) or len(raw) != 50:
        raise ValueError("v4 formal input must contain exactly 50 cases")
    cases: list[V4FormalCase] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"v4 formal case {index} must be an object")
        forbidden = FORBIDDEN_RUNTIME_KEYS & set(item)
        if forbidden:
            raise ValueError(
                f"v4 formal case {index} contains forbidden route injection: "
                f"{sorted(forbidden)}"
            )
        expected_id = f"E2E-{index:03d}"
        if item.get("case_id") != expected_id:
            raise ValueError(f"v4 formal case order must contain {expected_id}")
        if item.get("route_mode") != "production":
            raise ValueError(f"{expected_id} must use production routing")
        profile_id = str(item.get("profile_id") or "")
        expected_pretest_ids = set(load_persona_pretest(profile_id).expected_ids())
        answers = item.get("pretest_answers")
        if not isinstance(answers, dict) or set(answers) != expected_pretest_ids:
            raise ValueError(
                f"{expected_id} must contain exactly the {profile_id} persona pretest"
            )
        tags = item.get("experience_tags", [])
        if not isinstance(tags, list) or not all(
            isinstance(value, str) and value.strip() for value in tags
        ):
            raise ValueError(f"{expected_id} experience_tags must be a string list")
        probes_raw = item.get("diagnostic_probe_answers", [])
        if not isinstance(probes_raw, list) or len(probes_raw) > 1:
            raise ValueError(
                f"{expected_id} may contain at most one persona calibration probe"
            )
        probes = tuple(
            V4ProbeAnswer(str(probe["probe_id"]), str(probe["answer"]))
            for probe in probes_raw
        )
        script_id = str(item.get("learner_script_id") or "")
        if script_id not in SCRIPT_IDS:
            raise ValueError(f"{expected_id} has unsupported learner script {script_id}")
        cases.append(
            V4FormalCase(
                case_id=expected_id,
                route_mode="production",
                profile_id=profile_id,
                experience_tags=tuple(dict.fromkeys(value.strip() for value in tags)),
                pretest_answers=dict(answers),
                diagnostic_probe_answers=probes,
                learner_script_id=script_id,
            )
        )
    return tuple(cases)


def load_gold_standard(path: Path = GOLD_PATH) -> dict[str, dict[str, Any]]:
    raw = _load_json(Path(path))
    if not isinstance(raw, list) or len(raw) != 50:
        raise ValueError("v4 gold standard must contain exactly 50 cases")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            raise ValueError("every v4 gold row must have case_id")
        case_id = str(item["case_id"])
        if case_id in result:
            raise ValueError(f"duplicate v4 gold case: {case_id}")
        result[case_id] = dict(item)
    return result
