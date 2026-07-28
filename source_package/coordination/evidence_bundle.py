"""Immutable, content-addressed evidence shared by parallel resource agents."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


EVIDENCE_SOURCES = ("knowledge", "business_data", "pedagogy")


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"evidence bundle value is not JSON-compatible: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """One immutable blackboard snapshot consumed by every resource branch."""

    contract_id: str
    knowledge_point: str
    difficulty: str
    sources: Mapping[str, Mapping[str, Any]]
    bundle_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract_id", _required_string(self.contract_id, "contract_id"))
        object.__setattr__(
            self,
            "knowledge_point",
            _required_string(self.knowledge_point, "knowledge_point"),
        )
        difficulty = _required_string(self.difficulty, "difficulty")
        if difficulty not in {"basic", "applied", "advanced"}:
            raise ValueError("difficulty must be basic|applied|advanced")
        object.__setattr__(self, "difficulty", difficulty)
        if not isinstance(self.sources, Mapping):
            raise ValueError("sources must be a mapping")
        if set(self.sources) != set(EVIDENCE_SOURCES):
            raise ValueError("sources must contain knowledge, business_data, and pedagogy")
        frozen_sources = _freeze(self.sources)
        object.__setattr__(self, "sources", frozen_sources)
        canonical = json.dumps(
            self._content_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        object.__setattr__(self, "bundle_id", f"eb-{digest[:24]}")

    def _content_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "knowledge_point": self.knowledge_point,
            "difficulty": self.difficulty,
            "sources": _thaw(self.sources),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"bundle_id": self.bundle_id, **self._content_dict()}

    def source(self, source_id: str) -> Mapping[str, Any]:
        try:
            return self.sources[source_id]
        except KeyError as exc:
            raise KeyError(f"unknown evidence source: {source_id}") from exc

    def bind(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        """Stamp a resource draft with the exact shared blackboard identity."""
        bound = deepcopy(dict(draft))
        payload = bound.get("payload")
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, dict):
            raise ValueError("resource draft must contain payload.content")
        content["evidence_bundle_ref"] = self.bundle_id
        content["evidence_source_ids"] = list(EVIDENCE_SOURCES)
        return bound

    def control_draft(self, trace_id: str) -> dict[str, Any]:
        return {
            "trace_id": _required_string(trace_id, "trace_id"),
            "agent": "system",
            "role": "system",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "type": "control",
                "content": {
                    "event": "evidence_bundle_ready",
                    "evidence_bundle": self.as_dict(),
                },
            },
            "evidence": [],
        }
