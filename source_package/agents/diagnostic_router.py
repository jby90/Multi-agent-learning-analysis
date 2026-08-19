"""Pure deterministic diagnosis routing for the frozen v3 evaluation protocol.

The router consumes only production inputs: one approved profile, the five
pretest answers, and at most two answers to frozen diagnostic probes.  It has
no model dependency and never accepts a knowledge point or task template as an
input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = ROOT / "config" / "diagnostic_probes_v3.json"
DEPENDENCY_PATH = ROOT / "config" / "knowledge_dependencies_v3.json"
EXPERIENCE_TAG_PATH = ROOT / "config" / "diagnostic_experience_tags_v3.json"
PRETEST_PATH = ROOT / "eval" / "cases" / "pretest.json"
PROFILE_DIR = Path(__file__).with_name("profiles")
DIFFICULTIES = ("basic", "applied", "advanced")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe_id: str
    is_correct: bool

    @classmethod
    def from_value(cls, value: ProbeResult | Mapping[str, Any]) -> ProbeResult:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("probe result must be a mapping or ProbeResult")
        probe_id = value.get("probe_id")
        is_correct = value.get("is_correct")
        if not isinstance(probe_id, str) or not probe_id.strip():
            raise ValueError("probe_id must be a non-empty string")
        if not isinstance(is_correct, bool):
            raise ValueError("probe is_correct must be boolean")
        return cls(probe_id.strip(), is_correct)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid v3 routing asset {path}: {exc}") from exc


class DiagnosticRouter:
    """Build an auditable knowledge-point plan from frozen observations."""

    def __init__(
        self,
        *,
        probe_path: Path = PROBE_PATH,
        dependency_path: Path = DEPENDENCY_PATH,
        experience_tag_path: Path = EXPERIENCE_TAG_PATH,
        pretest_path: Path = PRETEST_PATH,
        profile_dir: Path = PROFILE_DIR,
    ) -> None:
        probes = _read_json(Path(probe_path))
        dependency = _read_json(Path(dependency_path))
        experience_config = _read_json(Path(experience_tag_path))
        pretest = _read_json(Path(pretest_path))
        if not isinstance(probes, list) or not probes:
            raise ValueError("diagnostic probe library must be a non-empty list")
        if not isinstance(dependency, dict):
            raise ValueError("knowledge dependency configuration must be an object")
        if not isinstance(pretest, list) or len(pretest) != 5:
            raise ValueError("v3 router requires exactly five pretest questions")
        if not isinstance(experience_config, dict) or not isinstance(
            experience_config.get("tags"), list
        ):
            raise ValueError("diagnostic experience tag configuration is invalid")

        self._probes = {str(item["probe_id"]): dict(item) for item in probes}
        if len(self._probes) != len(probes):
            raise ValueError("diagnostic probe IDs must be unique")
        self._order = tuple(str(item) for item in dependency["knowledge_point_order"])
        self._order_index = {point: index for index, point in enumerate(self._order)}
        self._prerequisites = {
            str(point): tuple(str(item) for item in items)
            for point, items in dependency["prerequisites"].items()
        }
        if set(self._prerequisites) != set(self._order):
            raise ValueError("dependency configuration must cover all knowledge points")
        self._pretest = {str(item["question_id"]): dict(item) for item in pretest}
        self._experience_tags = {
            str(item["tag_id"]): dict(item)
            for item in experience_config["tags"]
        }
        if len(self._experience_tags) != len(experience_config["tags"]):
            raise ValueError("diagnostic experience tag IDs must be unique")
        if {
            str(item["knowledge_point"])
            for item in self._experience_tags.values()
        } != set(self._order):
            raise ValueError("experience tags must cover all core knowledge points")
        self._profiles: dict[str, dict[str, Any]] = {}
        for path in sorted(Path(profile_dir).glob("*.json")):
            profile = _read_json(path)
            self._profiles[str(profile["profile_id"])] = dict(profile)

    @property
    def core_knowledge_points(self) -> tuple[str, ...]:
        return self._order

    def route(
        self,
        profile_id: str,
        answers: Mapping[str, str],
        probe_results: Sequence[ProbeResult | Mapping[str, Any]] = (),
        *,
        experience_tags: Sequence[str] = (),
    ) -> dict[str, Any]:
        if profile_id not in self._profiles:
            raise ValueError(f"unsupported profile_id: {profile_id}")
        if not isinstance(answers, Mapping) or set(answers) != set(self._pretest):
            raise ValueError("answers must contain exactly the five approved question IDs")
        probes = tuple(ProbeResult.from_value(item) for item in probe_results)
        normalized_tags = self._normalize_experience_tags(experience_tags)
        if len(probes) > 2:
            raise ValueError("a production session accepts at most two diagnostic probes")
        if len({item.probe_id for item in probes}) != len(probes):
            raise ValueError("diagnostic probe IDs must not repeat in one session")

        mastered: set[str] = set()
        wrong: dict[str, dict[str, Any]] = {}
        evidence_log: list[dict[str, Any]] = []

        for question_id, question in self._pretest.items():
            answer = answers[question_id]
            if answer not in question["options"]:
                raise ValueError(f"{question_id} answer must be an approved option")
            point = str(question["knowledge_point"])
            is_correct = answer == question["answer"]
            evidence_log.append(
                {
                    "evidence_id": question_id,
                    "evidence_source": "pretest",
                    "knowledge_point": point,
                    "difficulty": "basic",
                    "is_correct": is_correct,
                }
            )
            if is_correct:
                mastered.add(point)
            else:
                wrong[point] = {
                    "source": "pretest",
                    "ids": [question_id],
                    "difficulty": "basic",
                    "reason": "基础前测答错，需从基础档补足该知识点。",
                }

        for result in probes:
            probe = self._probes.get(result.probe_id)
            if probe is None:
                raise ValueError(f"unknown frozen diagnostic probe: {result.probe_id}")
            point = str(probe["knowledge_point"])
            difficulty = str(probe["difficulty"])
            evidence_log.append(
                {
                    "evidence_id": result.probe_id,
                    "evidence_source": "diagnostic_probe",
                    "knowledge_point": point,
                    "difficulty": difficulty,
                    "is_correct": result.is_correct,
                }
            )
            if result.is_correct:
                if difficulty == "applied" or point not in wrong:
                    mastered.add(point)
                continue
            mastered.discard(point)
            reason = (
                "第1道小题答错，将从基础难度开始建立概念与口径。"
                if difficulty == "basic"
                else "第2道小题答错，将从应用难度开始巩固掌握。"
            )
            wrong[point] = {
                "source": "diagnostic_probe",
                "ids": [result.probe_id],
                "difficulty": difficulty,
                "reason": reason,
            }

        candidates: dict[str, dict[str, Any]] = {}
        for point, evidence in wrong.items():
            candidates[point] = self._candidate(
                point,
                priority=100,
                mastery_status="needs_training",
                evidence_source=str(evidence["source"]),
                evidence_ids=list(evidence["ids"]),
                initial_difficulty=str(evidence["difficulty"]),
                route_reason=str(evidence["reason"]),
            )

        # Required prerequisites are inserted only when no correct observation
        # establishes basic mastery for that prerequisite.
        for target in tuple(candidates):
            for prerequisite in self._prerequisites[target]:
                if prerequisite in mastered or prerequisite in candidates:
                    continue
                candidates[prerequisite] = self._candidate(
                    prerequisite,
                    priority=80,
                    mastery_status="needs_training",
                    evidence_source="knowledge_dependency",
                    evidence_ids=[f"PREREQUISITE:{target}"],
                    initial_difficulty="basic",
                    route_reason=f"学习“{target}”前必须先补足该前置知识。",
                )

        profile = self._profiles[profile_id]
        for point in profile.get("gaps_prior", []):
            point = str(point)
            if point in mastered or point in candidates or point not in self._order_index:
                continue
            candidates[point] = self._candidate(
                point,
                priority=60,
                mastery_status="needs_training",
                evidence_source="profile_prior",
                evidence_ids=[f"PROFILE:{profile_id}"],
                initial_difficulty=str(profile["difficulty_start"]),
                route_reason="岗位画像将该点标记为可能的薄弱项，且当前没有正确作答的证据。",
            )

        for point in self._order:
            if point in mastered or point in candidates:
                continue
            candidates[point] = self._candidate(
                point,
                priority=40,
                mastery_status="pending_training",
                evidence_source="curriculum_extension",
                evidence_ids=[f"CURRICULUM:{point}"],
                initial_difficulty="basic",
                route_reason="该知识点尚无掌握证据，按既定培养顺序安排在后续学习。",
            )

        ordered = sorted(
            candidates.values(),
            key=lambda item: (-int(item["priority"]), self._order_index[item["knowledge_point"]]),
        )
        for index, item in enumerate(ordered, start=1):
            item["plan_item_id"] = f"PLAN-{index:03d}"
        selected = ordered[0] if ordered else None
        recommended_point, recommendation_evidence = self.recommend_probe_point(
            profile_id,
            answers,
            normalized_tags,
            selected_knowledge_point=(
                str(selected["knowledge_point"]) if selected is not None else None
            ),
        )
        return {
            "router_version": "diagnostic-router-v3.1",
            "knowledge_point_plan": ordered,
            "selected_plan_item_id": selected["plan_item_id"] if selected else None,
            "selected_knowledge_point": selected["knowledge_point"] if selected else None,
            "selected_difficulty": selected["initial_difficulty"] if selected else None,
            "route_evidence": evidence_log,
            "probe_count": len(probes),
            "experience_tags": list(normalized_tags),
            "recommended_probe_knowledge_point": recommended_point,
            "probe_recommendation_evidence": recommendation_evidence,
        }

    def recommend_probe_point(
        self,
        profile_id: str,
        answers: Mapping[str, str],
        experience_tags: Sequence[str] = (),
        *,
        selected_knowledge_point: str | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Choose a calibration target from observable profile evidence.

        An experience tag requests calibration only.  It never creates a
        blind spot; the subsequent frozen probe remains the sole evidence for
        marking the point as needing training.
        """

        if profile_id not in self._profiles:
            raise ValueError(f"unsupported profile_id: {profile_id}")
        if not isinstance(answers, Mapping) or set(answers) != set(self._pretest):
            raise ValueError("answers must contain exactly the five approved question IDs")
        tags = self._normalize_experience_tags(experience_tags)
        if tags:
            tag = self._experience_tags[tags[0]]
            point = str(tag["knowledge_point"])
            return point, {
                "evidence_source": "profile_experience",
                "evidence_ids": [f"EXPERIENCE:{tags[0]}"],
                "route_reason": (
                    f"你选择的经历“{tag['label']}”还需要用小题确认；"
                    "该经历本身不代表知识薄弱。"
                ),
            }
        if selected_knowledge_point:
            return selected_knowledge_point, {
                "evidence_source": "diagnostic_plan",
                "evidence_ids": [f"PLAN_CANDIDATE:{selected_knowledge_point}"],
                "route_reason": "按学习计划的第一个知识点出一道小题确认起点。",
            }
        return None, None

    def _normalize_experience_tags(
        self,
        experience_tags: Sequence[str],
    ) -> tuple[str, ...]:
        if isinstance(experience_tags, str) or not isinstance(
            experience_tags, Sequence
        ):
            raise ValueError("experience_tags must be a sequence of tag IDs")
        normalized = tuple(str(item).strip() for item in experience_tags)
        if any(not item for item in normalized):
            raise ValueError("experience tag IDs must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("experience tag IDs must not repeat")
        unknown = [item for item in normalized if item not in self._experience_tags]
        if unknown:
            raise ValueError(f"unknown diagnostic experience tags: {unknown}")
        return normalized

    def _candidate(
        self,
        point: str,
        *,
        priority: int,
        mastery_status: str,
        evidence_source: str,
        evidence_ids: list[str],
        initial_difficulty: str,
        route_reason: str,
    ) -> dict[str, Any]:
        if point not in self._order_index:
            raise ValueError(f"unknown core knowledge point: {point}")
        if initial_difficulty not in DIFFICULTIES:
            raise ValueError(f"invalid initial difficulty: {initial_difficulty}")
        return {
            "knowledge_point": point,
            "mastery_status": mastery_status,
            "evidence_source": evidence_source,
            "evidence_ids": evidence_ids,
            "priority": priority,
            "initial_difficulty": initial_difficulty,
            "route_reason": route_reason,
            "prerequisites": list(self._prerequisites[point]),
        }


def load_diagnostic_probes(path: Path = PROBE_PATH) -> tuple[dict[str, Any], ...]:
    raw = _read_json(Path(path))
    if not isinstance(raw, list):
        raise ValueError("diagnostic probe library must be a list")
    return tuple(dict(item) for item in raw)


def probes_for_knowledge_point(
    knowledge_point: str,
    path: Path = PROBE_PATH,
) -> tuple[dict[str, Any], ...]:
    """Return the frozen basic-then-applied probe sequence for one point."""

    probes = [
        item
        for item in load_diagnostic_probes(path)
        if item.get("knowledge_point") == knowledge_point
    ]
    return tuple(
        sorted(
            probes,
            key=lambda item: (
                0 if item.get("difficulty") == "basic" else 1,
                str(item.get("probe_id")),
            ),
        )[:2]
    )


def probes_for_diagnosis(
    profile_id: str,
    answers: Mapping[str, str],
    experience_tags: Sequence[str] = (),
    *,
    probe_path: Path = PROBE_PATH,
) -> tuple[dict[str, Any], ...]:
    """Return system-selected probes without accepting a target point."""

    router = DiagnosticRouter(probe_path=probe_path)
    preliminary = router.route(
        profile_id,
        answers,
        (),
        experience_tags=experience_tags,
    )
    point = preliminary.get("recommended_probe_knowledge_point")
    if not isinstance(point, str) or not point.strip():
        return ()
    return probes_for_knowledge_point(point, path=probe_path)


def grade_probe_answer(probe: Mapping[str, Any], answer: str) -> bool:
    """Deterministically grade a frozen probe answer.

    Formal v3 evaluation uses the frozen gold wording for a correct response.
    The compact normalization permits punctuation/spacing differences but does
    not call an LLM or infer a label from an expected route.
    """

    if not isinstance(answer, str) or not answer.strip():
        return False

    def normalize(value: str) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff%+-]", "", value).casefold()

    submitted = normalize(answer)
    gold = normalize(str(probe.get("gold_answer", "")))
    return bool(submitted and gold and submitted == gold)
