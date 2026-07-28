"""Deterministic, balanced and judgment-free teacher blind-review material."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from agents.diagnosis_agent import load_profiles
from eval.case_matrix import EvaluationCase, load_case_matrix
from eval.trace_dataset import canonical_json, load_dataset


PROFILE_ORDER = ("planner_new", "craft_engineer", "line_leader")
PROFILE_FIELDS = ("title", "background", "strengths", "gaps_prior", "lecture_style")
RESOURCE_TYPES = frozenset({"lecture_note", "quiz_set", "practice_guide"})
RESOURCE_FIELDS = {
    "lecture_note": ("lecture_md",),
    "quiz_set": ("question", "questions", "contextualized_stem", "guide_intro"),
    "practice_guide": (
        "question",
        "guide_md",
        "contextualized_stem",
        "guide_intro",
    ),
}
SOURCE_ID_RE = re.compile(r"(?i)\b(?:E2E-\d{3}|p7-e2e-[a-z0-9-]+)\b")
BLIND_HEADING_RE = re.compile(r"(?m)^## (BR-\d{3})[ \t]*$")
RATING_RE = re.compile(
    r"(?m)^结论（适配/不适配）：[ \t]*(适配|不适配)?[ \t]*$"
)
SOURCE_DIGEST_RE = re.compile(r"盲评材料 SHA-256：`([0-9a-f]{64})`")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _case_value(case: EvaluationCase | Mapping[str, Any], field: str) -> Any:
    if isinstance(case, EvaluationCase):
        return getattr(case, field)
    return case.get(field)


def _systematic_indices(population: int, sample: int) -> tuple[int, ...]:
    if sample > population:
        raise ValueError(
            f"blind sample requests {sample} items from a population of {population}"
        )
    if sample == population:
        return tuple(range(population))
    # Midpoints of equal-width intervals: stable, spans the whole approved matrix,
    # and cannot be tuned against downstream scores.
    return tuple(((2 * index + 1) * population) // (2 * sample) for index in range(sample))


def _final_resource(row: Mapping[str, Any]) -> tuple[str, Mapping[str, Any], str]:
    candidates: list[tuple[str, Mapping[str, Any], str]] = []
    messages = row.get("messages")
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "produce":
            continue
        payload = _mapping(message.get("payload"))
        payload_type = payload.get("type")
        content = _mapping(payload.get("content"))
        if payload_type not in RESOURCE_TYPES or content.get("event") == "knowledge_refused":
            continue
        visible_fields = RESOURCE_FIELDS[str(payload_type)]
        if not any(
            field in content and content[field] not in (None, "", [])
            for field in visible_fields
        ):
            continue
        msg_id = message.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            raise ValueError("blind-review resource has no msg_id for private audit key")
        candidates.append((str(payload_type), content, msg_id))
    if not candidates:
        raise ValueError(f"{row.get('trace_id', '<unknown>')} has no final learning resource")
    return candidates[-1]


def _has_teacher_visible_resource(row: Mapping[str, Any]) -> bool:
    try:
        _final_resource(row)
    except ValueError as exc:
        if "has no final learning resource" in str(exc):
            return False
        raise
    return True


def _redact_source_ids(value: Any) -> Any:
    if isinstance(value, str):
        return SOURCE_ID_RE.sub("[来源编号已隐去]", value)
    if isinstance(value, list):
        return [_redact_source_ids(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _redact_source_ids(item) for key, item in value.items()}
    return value


def _resource_body(payload_type: str, content: Mapping[str, Any]) -> dict[str, Any]:
    if payload_type == "practice_guide" and content.get("guide_md") not in (None, "", []):
        fields = ("guide_md", "guide_intro")
    elif payload_type == "quiz_set" and content.get("questions") not in (None, "", []):
        fields = ("questions", "guide_intro")
    else:
        fields = RESOURCE_FIELDS[payload_type]
    body: dict[str, Any] = {}
    seen_values: set[str] = set()
    for field in fields:
        if field not in content or content[field] in (None, "", []):
            continue
        value = _redact_source_ids(content[field])
        fingerprint = canonical_json(value)
        if fingerprint in seen_values:
            continue
        seen_values.add(fingerprint)
        body[field] = value
    if not body:
        raise ValueError(f"{payload_type} has no teacher-visible content")
    return body


def _selection(
    dataset: Sequence[Mapping[str, Any]],
    matrix: Sequence[EvaluationCase | Mapping[str, Any]],
    *,
    size: int,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("blind sample size must be a positive integer")
    if size % len(PROFILE_ORDER):
        raise ValueError("blind sample size must be divisible by three")
    by_case: dict[str, Mapping[str, Any]] = {}
    for row in dataset:
        case_id = _mapping(row.get("case")).get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("dataset row has no case_id")
        if case_id in by_case:
            raise ValueError(f"duplicate dataset case_id: {case_id}")
        by_case[case_id] = row
    eligible_case_ids = {
        case_id
        for case_id, row in by_case.items()
        if _has_teacher_visible_resource(row)
    }

    per_profile = size // len(PROFILE_ORDER)
    selected: list[tuple[str, Mapping[str, Any]]] = []
    for profile_id in PROFILE_ORDER:
        case_ids = [
            str(_case_value(case, "case_id"))
            for case in matrix
            if _case_value(case, "profile_id") == profile_id
            and str(_case_value(case, "case_id")) in eligible_case_ids
        ]
        indices = _systematic_indices(len(case_ids), per_profile)
        for index in indices:
            case_id = case_ids[index]
            if case_id not in by_case:
                raise ValueError(f"blind sample case missing from dataset: {case_id}")
            selected.append((case_id, by_case[case_id]))
    return tuple(selected)


def build_blind_sample(
    dataset: Sequence[Mapping[str, Any]],
    matrix: Sequence[EvaluationCase | Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    size: int = 30,
) -> tuple[dict[str, Any], ...]:
    """Return teacher-facing material with no source IDs or system judgments."""

    material: list[dict[str, Any]] = []
    for serial, (case_id, row) in enumerate(
        _selection(dataset, matrix, size=size), start=1
    ):
        del case_id  # source identity is intentionally absent from the public material
        profile_id = _mapping(row.get("case")).get("profile_id")
        profile = _mapping(profiles.get(str(profile_id)))
        if any(field not in profile for field in PROFILE_FIELDS):
            raise ValueError(f"profile fields are incomplete for {profile_id}")
        payload_type, content, _ = _final_resource(row)
        material.append(
            {
                "blind_id": f"BR-{serial:03d}",
                "learner_profile": {
                    field: profile[field] for field in PROFILE_FIELDS
                },
                "resource": {
                    "type": payload_type,
                    "content": _resource_body(payload_type, content),
                },
            }
        )
    return tuple(material)


def build_blind_key(
    dataset: Sequence[Mapping[str, Any]],
    matrix: Sequence[EvaluationCase | Mapping[str, Any]],
    *,
    size: int = 30,
) -> tuple[dict[str, str], ...]:
    """Build the private source mapping used only for reproducibility audits."""

    rows: list[dict[str, str]] = []
    for serial, (case_id, row) in enumerate(
        _selection(dataset, matrix, size=size), start=1
    ):
        _, _, msg_id = _final_resource(row)
        rows.append(
            {
                "blind_id": f"BR-{serial:03d}",
                "case_id": case_id,
                "trace_id": str(row.get("trace_id", "")),
                "msg_id": msg_id,
            }
        )
    return tuple(rows)


def _display(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return "；".join(value)
        return json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_blind_form(
    sample: Sequence[Mapping[str, Any]], *, source_sha256: str
) -> str:
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise ValueError("source_sha256 must be a 64-character digest")
    lines = [
        "# P7 教师盲评表",
        "",
        "说明：请仅依据给出的学员画像与学习产物，判断产物是否适配。材料按三个画像等额系统抽样；不含模型、难度裁决、评审结论和来源ID。",
        "",
        f"盲评材料 SHA-256：`{source_sha256}`",
        "",
    ]
    for row in sample:
        blind_id = str(row["blind_id"])
        profile = _mapping(row["learner_profile"])
        resource = _mapping(row["resource"])
        content = _mapping(resource.get("content"))
        lines.extend(
            [
                f"## {blind_id}",
                "",
                f"- 学员：{profile.get('title', '')}",
                f"- 背景：{profile.get('background', '')}",
                f"- 优势：{_display(profile.get('strengths', []))}",
                f"- 待补：{_display(profile.get('gaps_prior', []))}",
                f"- 偏好：{profile.get('lecture_style', '')}",
                f"- 产物类型：{resource.get('type', '')}",
                "",
                "### 学习产物",
                "",
            ]
        )
        for field, value in content.items():
            lines.extend([f"{field}：", "", _display(value), ""])
        lines.extend(
            [
                "结论（适配/不适配）：",
                "",
                "理由：",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_blind_form(
    text: str,
    *,
    expected_ids: Sequence[str],
    source_sha256: str,
) -> dict[str, str | None]:
    """Parse filled ratings while binding them to one exact blind material file."""

    digest = SOURCE_DIGEST_RE.search(text)
    if digest is None or digest.group(1) != source_sha256:
        raise ValueError("blind form source digest does not match current material")
    parts = BLIND_HEADING_RE.split(text)
    sections = {parts[index]: parts[index + 1] for index in range(1, len(parts), 2)}
    if tuple(sections) != tuple(expected_ids):
        raise ValueError("blind form IDs do not match current material")
    ratings: dict[str, str | None] = {}
    for blind_id in expected_ids:
        matches = RATING_RE.findall(sections[blind_id])
        if len(matches) != 1:
            raise ValueError(f"blind form {blind_id} must contain exactly one rating slot")
        ratings[blind_id] = matches[0] or None
    return ratings


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
        newline="",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--material", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--form", type=Path, required=True)
    parser.add_argument("--size", type=int, default=30)
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    matrix = load_case_matrix()
    profiles = load_profiles()
    sample = build_blind_sample(dataset, matrix, profiles, size=args.size)
    key = build_blind_key(dataset, matrix, size=args.size)
    _write_jsonl(args.material, sample)
    _write_jsonl(args.key, key)
    digest = hashlib.sha256(args.material.read_bytes()).hexdigest()
    args.form.parent.mkdir(parents=True, exist_ok=True)
    args.form.write_text(
        render_blind_form(sample, source_sha256=digest),
        encoding="utf-8",
        newline="",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
