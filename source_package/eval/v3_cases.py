"""Frozen v3 formal evaluation inputs and separately stored gold standard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "eval" / "cases" / "v3" / "formal_50_inputs_v3.json"
GOLD_PATH = ROOT / "eval" / "gold" / "v3" / "formal_50_gold_v3.json"
_FORBIDDEN_RUNTIME_KEYS = {
    "knowledge_point",
    "template_id",
    "expected_knowledge_point",
    "expected_template_id",
    "目标知识点",
    "预期初始模板",
    "预期最终模板",
}


@dataclass(frozen=True, slots=True)
class V3ProbeAnswer:
    probe_id: str
    answer: str


@dataclass(frozen=True, slots=True)
class V3FormalCase:
    case_id: str
    route_mode: str
    profile_id: str
    pretest_answers: Mapping[str, str]
    diagnostic_probe_answers: tuple[V3ProbeAnswer, ...]
    learner_script_id: str


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid frozen v3 asset {path}: {exc}") from exc


def load_formal_cases(path: Path = INPUT_PATH) -> tuple[V3FormalCase, ...]:
    raw = _load_json(Path(path))
    if not isinstance(raw, list) or len(raw) != 50:
        raise ValueError("v3 formal input must contain exactly 50 cases")
    cases: list[V3FormalCase] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"formal case {index} must be an object")
        forbidden = _FORBIDDEN_RUNTIME_KEYS & set(item)
        if forbidden:
            raise ValueError(
                f"formal case {index} contains forbidden route injection: {sorted(forbidden)}"
            )
        expected_id = f"E2E-{index:03d}"
        if item.get("case_id") != expected_id:
            raise ValueError(f"formal case order must contain {expected_id}")
        if item.get("route_mode") != "production":
            raise ValueError(f"{expected_id} must use production routing")
        answers = item.get("pretest_answers")
        if not isinstance(answers, dict) or set(answers) != {
            "PT-1", "PT-2", "PT-3", "PT-4", "PT-5"
        }:
            raise ValueError(f"{expected_id} must contain five production pretest answers")
        probes_raw = item.get("diagnostic_probe_answers")
        if not isinstance(probes_raw, list) or len(probes_raw) > 2:
            raise ValueError(f"{expected_id} may contain at most two frozen probes")
        probes = tuple(
            V3ProbeAnswer(str(probe["probe_id"]), str(probe["answer"]))
            for probe in probes_raw
        )
        cases.append(
            V3FormalCase(
                case_id=expected_id,
                route_mode="production",
                profile_id=str(item["profile_id"]),
                pretest_answers=dict(answers),
                diagnostic_probe_answers=probes,
                learner_script_id=str(item["learner_script_id"]),
            )
        )
    return tuple(cases)


def load_gold_standard(path: Path = GOLD_PATH) -> dict[str, dict[str, Any]]:
    raw = _load_json(Path(path))
    if not isinstance(raw, list) or len(raw) != 50:
        raise ValueError("v3 gold standard must contain exactly 50 cases")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            raise ValueError("every v3 gold row must have case_id")
        case_id = str(item["case_id"])
        if case_id in result:
            raise ValueError(f"duplicate v3 gold case: {case_id}")
        result[case_id] = dict(item)
    return result
