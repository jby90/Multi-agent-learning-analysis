"""Immutable learning contract used by every branch in one learning run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


DIFFICULTIES = ("basic", "applied", "advanced")
DEFAULT_EVIDENCE_KINDS = (
    "kb_chunk",
    "sql_query",
    "quiz_answer_key",
    "review_rule",
)
DEFAULT_RESOURCE_REQUIREMENTS = (
    "lecture_note",
    "practice_guide",
    "quiz_set",
    "sql_result",
    "learning_path_update",
)
_DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _unique_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a string sequence")
    result: list[str] = []
    for item in value:
        text = _required_string(item, field_name)
        if text not in result:
            result.append(text)
    return tuple(result)


def _message_content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("diagnosis must contain a payload object")
    content = payload.get("content")
    if not isinstance(content, Mapping):
        raise ValueError("diagnosis must contain payload.content")
    return content


@dataclass(frozen=True, slots=True)
class LearnerProfileSnapshot:
    """Only stable learner attributes that generation and review may consume."""

    profile_id: str
    title: str
    background: str
    strengths: tuple[str, ...]
    gaps_prior: tuple[str, ...]
    lecture_style: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_id",
            _required_string(self.profile_id, "profile.profile_id"),
        )
        object.__setattr__(self, "title", _required_string(self.title, "profile.title"))
        object.__setattr__(
            self,
            "background",
            _required_string(self.background, "profile.background"),
        )
        object.__setattr__(
            self,
            "strengths",
            _unique_strings(self.strengths, "profile.strengths"),
        )
        object.__setattr__(
            self,
            "gaps_prior",
            _unique_strings(self.gaps_prior, "profile.gaps_prior"),
        )
        object.__setattr__(
            self,
            "lecture_style",
            _required_string(self.lecture_style, "profile.lecture_style"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LearnerProfileSnapshot":
        if not isinstance(value, Mapping):
            raise ValueError("learner profile must be a mapping")
        return cls(
            profile_id=_required_string(value.get("profile_id"), "profile.profile_id"),
            title=_required_string(value.get("title"), "profile.title"),
            background=_required_string(value.get("background"), "profile.background"),
            strengths=_unique_strings(value.get("strengths"), "profile.strengths"),
            gaps_prior=_unique_strings(value.get("gaps_prior"), "profile.gaps_prior"),
            lecture_style=_required_string(
                value.get("lecture_style"), "profile.lecture_style"
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "title": self.title,
            "background": self.background,
            "strengths": list(self.strengths),
            "gaps_prior": list(self.gaps_prior),
            "lecture_style": self.lecture_style,
        }


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Deterministic quality boundary shared by producers and reviewers."""

    hard_veto_rules: tuple[str, ...] = ("R-01", "R-04", "R-05", "R-06")
    debatable_rules: tuple[str, ...] = ("R-02", "R-03")
    max_review_cycles: int = 4
    fail_closed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hard_veto_rules",
            _unique_strings(self.hard_veto_rules, "quality_policy.hard_veto_rules"),
        )
        object.__setattr__(
            self,
            "debatable_rules",
            _unique_strings(self.debatable_rules, "quality_policy.debatable_rules"),
        )
        if not self.hard_veto_rules:
            raise ValueError("quality policy must contain hard veto rules")
        if set(self.hard_veto_rules) & set(self.debatable_rules):
            raise ValueError("hard veto and debatable rules must be disjoint")
        if (
            isinstance(self.max_review_cycles, bool)
            or not isinstance(self.max_review_cycles, int)
            or self.max_review_cycles < 1
        ):
            raise ValueError("max_review_cycles must be positive")
        if not isinstance(self.fail_closed, bool):
            raise ValueError("fail_closed must be boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "hard_veto_rules": list(self.hard_veto_rules),
            "debatable_rules": list(self.debatable_rules),
            "max_review_cycles": self.max_review_cycles,
            "fail_closed": self.fail_closed,
        }


@dataclass(frozen=True, slots=True)
class LearningContract:
    """Content-addressed snapshot that prevents context drift across branches."""

    learner: LearnerProfileSnapshot
    domain_id: str
    domain_package_sha256: str
    target_knowledge_points: tuple[str, ...]
    misconceptions: tuple[str, ...]
    difficulty: str
    allowed_evidence_kinds: tuple[str, ...] = DEFAULT_EVIDENCE_KINDS
    resource_requirements: tuple[str, ...] = DEFAULT_RESOURCE_REQUIREMENTS
    quality_policy: QualityPolicy = field(default_factory=QualityPolicy)
    revision: int = 1
    contract_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.learner, LearnerProfileSnapshot):
            raise ValueError("learner must be a LearnerProfileSnapshot")
        if not isinstance(self.quality_policy, QualityPolicy):
            raise ValueError("quality_policy must be a QualityPolicy")
        object.__setattr__(
            self,
            "target_knowledge_points",
            _unique_strings(self.target_knowledge_points, "target_knowledge_points"),
        )
        object.__setattr__(
            self,
            "misconceptions",
            _unique_strings(self.misconceptions, "misconceptions"),
        )
        object.__setattr__(
            self,
            "allowed_evidence_kinds",
            _unique_strings(self.allowed_evidence_kinds, "allowed_evidence_kinds"),
        )
        object.__setattr__(
            self,
            "resource_requirements",
            _unique_strings(self.resource_requirements, "resource_requirements"),
        )
        if not _DOMAIN_ID_RE.fullmatch(self.domain_id):
            raise ValueError("domain_id must match [a-z][a-z0-9_-]*")
        if not _SHA256_RE.fullmatch(self.domain_package_sha256):
            raise ValueError("domain_package_sha256 must be a lowercase SHA-256")
        if not self.target_knowledge_points:
            raise ValueError("target_knowledge_points must not be empty")
        if self.difficulty not in DIFFICULTIES:
            raise ValueError("difficulty must be basic, applied, or advanced")
        if not self.allowed_evidence_kinds:
            raise ValueError("allowed_evidence_kinds must not be empty")
        if not self.resource_requirements:
            raise ValueError("resource_requirements must not be empty")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("revision must be positive")
        canonical = json.dumps(
            self._content_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        object.__setattr__(self, "contract_id", f"lc-{digest[:24]}")

    @classmethod
    def from_diagnosis(
        cls,
        *,
        profile: Mapping[str, Any],
        diagnosis: Mapping[str, Any],
        domain_id: str,
        domain_package_sha256: str,
        allowed_evidence_kinds: Sequence[str] = DEFAULT_EVIDENCE_KINDS,
        resource_requirements: Sequence[str] = DEFAULT_RESOURCE_REQUIREMENTS,
        quality_policy: QualityPolicy | None = None,
        use_selected_route: bool = False,
    ) -> "LearningContract":
        content = _message_content(diagnosis)
        learner = LearnerProfileSnapshot.from_mapping(profile)
        diagnosis_profile = content.get("profile_id")
        if diagnosis_profile != learner.profile_id:
            raise ValueError("diagnosis profile does not match learner profile")
        return cls(
            learner=learner,
            domain_id=_required_string(domain_id, "domain_id"),
            domain_package_sha256=_required_string(
                domain_package_sha256, "domain_package_sha256"
            ),
            target_knowledge_points=_unique_strings(
                # v4 画像路由：前测全对时 blind_spots 为空（培养清单仍非空），
                # 目标点回退为培养清单；v3 盲区恒非空，行为不变。
                content.get("blind_spots")
                or [
                    item["knowledge_point"]
                    for item in content.get("knowledge_point_plan", [])
                    if isinstance(item, Mapping) and item.get("knowledge_point")
                ],
                "diagnosis.blind_spots",
            ),
            misconceptions=_unique_strings(
                content.get("hit_misconceptions"),
                "diagnosis.hit_misconceptions",
            ),
            difficulty=_required_string(
                (
                    content.get("selected_difficulty")
                    if use_selected_route
                    else content.get("difficulty")
                )
                or content.get("difficulty")
                or content.get("selected_difficulty"),
                (
                    "diagnosis.selected_difficulty"
                    if use_selected_route
                    else "diagnosis.difficulty"
                ),
            ),
            allowed_evidence_kinds=_unique_strings(
                tuple(allowed_evidence_kinds), "allowed_evidence_kinds"
            ),
            resource_requirements=_unique_strings(
                tuple(resource_requirements), "resource_requirements"
            ),
            quality_policy=quality_policy or QualityPolicy(),
        )

    def _content_dict(self) -> dict[str, Any]:
        return {
            "learner": self.learner.as_dict(),
            "domain_id": self.domain_id,
            "domain_package_sha256": self.domain_package_sha256,
            "target_knowledge_points": list(self.target_knowledge_points),
            "misconceptions": list(self.misconceptions),
            "difficulty": self.difficulty,
            "allowed_evidence_kinds": list(self.allowed_evidence_kinds),
            "resource_requirements": list(self.resource_requirements),
            "quality_policy": self.quality_policy.as_dict(),
            "revision": self.revision,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"contract_id": self.contract_id, **self._content_dict()}

    def control_draft(self, trace_id: str) -> dict[str, Any]:
        return {
            "trace_id": _required_string(trace_id, "trace_id"),
            "agent": "system",
            "role": "system",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "type": "control",
                "content": {
                    "event": "learning_contract_ready",
                    "learning_contract": self.as_dict(),
                },
            },
            "evidence": [],
        }

    def validate_context(
        self,
        *,
        profile: Mapping[str, Any] | None,
        diagnosis: Mapping[str, Any] | None,
    ) -> None:
        if profile is not None and profile.get("profile_id") != self.learner.profile_id:
            raise ValueError("review profile does not match learning contract")
        if diagnosis is None:
            return
        content = _message_content(diagnosis)
        payload = diagnosis.get("payload")
        payload_type = payload.get("type") if isinstance(payload, Mapping) else None
        profile_id = content.get("profile_id")
        if profile_id is not None and profile_id != self.learner.profile_id:
            raise ValueError("review context does not match learning contract")
        # Only the initial diagnosis defines the contract's starting difficulty.
        # Later learning-path updates intentionally carry a changing difficulty.
        if (
            payload_type == "profile_assessment"
            and self.revision == 1
            and self.difficulty
            not in {
                content.get("difficulty"),
                content.get("selected_difficulty"),
            }
        ):
            raise ValueError("review difficulty does not match learning contract")
