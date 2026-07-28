"""Immutable, content-addressed artifacts for concurrent agent branches."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ArtifactEnvelope:
    """Store canonical JSON so no worker can mutate another worker's input."""

    artifact_id: str
    contract_id: str
    trace_id: str
    producer: str
    artifact_type: str
    content_sha256: str
    evidence_refs: tuple[str, ...]
    _message_json: str = field(repr=False)

    @classmethod
    def from_message(
        cls,
        message: Mapping[str, Any],
        *,
        contract_id: str,
    ) -> "ArtifactEnvelope":
        if not isinstance(message, Mapping):
            raise ValueError("artifact message must be a mapping")
        normalized = dict(message)
        try:
            canonical = json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact message must be JSON serializable") from exc
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        payload = normalized.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("artifact message must contain payload")
        evidence = normalized.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("artifact evidence must be a list")
        refs: list[str] = []
        for item in evidence:
            if not isinstance(item, Mapping):
                raise ValueError("artifact evidence entries must be mappings")
            ref = _required_string(item.get("ref"), "evidence.ref")
            if ref not in refs:
                refs.append(ref)
        msg_id = normalized.get("msg_id")
        artifact_id = (
            msg_id.strip()
            if isinstance(msg_id, str) and msg_id.strip()
            else f"artifact-{digest[:24]}"
        )
        return cls(
            artifact_id=artifact_id,
            contract_id=_required_string(contract_id, "contract_id"),
            trace_id=_required_string(normalized.get("trace_id"), "trace_id"),
            producer=_required_string(normalized.get("agent"), "agent"),
            artifact_type=_required_string(payload.get("type"), "payload.type"),
            content_sha256=digest,
            evidence_refs=tuple(refs),
            _message_json=canonical,
        )

    def materialize(self) -> dict[str, Any]:
        """Return an independent copy for one worker."""

        value = json.loads(self._message_json)
        if not isinstance(value, dict):  # Defensive: construction only accepts objects.
            raise RuntimeError("stored artifact is not an object")
        return value

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "contract_id": self.contract_id,
            "trace_id": self.trace_id,
            "producer": self.producer,
            "artifact_type": self.artifact_type,
            "content_sha256": self.content_sha256,
            "evidence_refs": list(self.evidence_refs),
        }
