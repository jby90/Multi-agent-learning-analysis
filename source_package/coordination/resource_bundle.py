"""Content-addressed bundle for independently generated learning resources."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Literal, Mapping


ResourceBranchId = Literal["knowledge", "practice", "assessment"]
ResourceBranchStatus = Literal["ready", "unavailable"]
_BRANCH_ORDER: tuple[ResourceBranchId, ...] = (
    "knowledge",
    "practice",
    "assessment",
)


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _payload(product: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    payload = product.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("resource product.payload must be a mapping")
    payload_type = _required_string(payload.get("type"), "resource payload.type")
    content = payload.get("content")
    if not isinstance(content, Mapping):
        raise ValueError("resource payload.content must be a mapping")
    return payload_type, content


def _draft_id(product: Mapping[str, Any]) -> str:
    message_id = product.get("msg_id")
    if isinstance(message_id, str) and message_id.strip():
        return message_id.strip()
    canonical = json.dumps(
        product,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"draft-{sha256(canonical).hexdigest()[:20]}"


@dataclass(frozen=True, slots=True)
class ResourceBranch:
    branch_id: ResourceBranchId
    status: ResourceBranchStatus
    required: bool
    draft_id: str | None = None
    payload_type: str | None = None
    difficulty: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "status": self.status,
            "required": self.required,
            **({"draft_id": self.draft_id} if self.draft_id else {}),
            **({"payload_type": self.payload_type} if self.payload_type else {}),
            **({"difficulty": self.difficulty} if self.difficulty else {}),
        }


@dataclass(frozen=True, slots=True)
class ResourceBundle:
    bundle_id: str
    contract_id: str
    evidence_bundle_id: str
    branches: tuple[ResourceBranch, ...]

    @classmethod
    def build(
        cls,
        *,
        contract_id: str,
        evidence_bundle_id: str,
        products: Mapping[ResourceBranchId, Mapping[str, Any] | None],
    ) -> "ResourceBundle":
        resolved_contract = _required_string(contract_id, "contract_id")
        resolved_evidence = _required_string(
            evidence_bundle_id,
            "evidence_bundle_id",
        )
        if set(products) != set(_BRANCH_ORDER):
            raise ValueError("resource bundle must declare all three branches")
        expected_types = {
            "knowledge": frozenset({"lecture_note"}),
            "practice": frozenset({"quiz_set", "practice_guide"}),
            "assessment": frozenset({"quiz_set"}),
        }
        branches: list[ResourceBranch] = []
        for branch_id in _BRANCH_ORDER:
            product = products[branch_id]
            required = branch_id == "knowledge"
            if product is None:
                if required:
                    raise ValueError("knowledge resource branch is required")
                branches.append(ResourceBranch(branch_id, "unavailable", required))
                continue
            payload_type, content = _payload(product)
            if payload_type not in expected_types[branch_id]:
                raise ValueError(
                    f"{branch_id} resource has invalid payload type {payload_type}"
                )
            if content.get("evidence_bundle_ref") != resolved_evidence:
                raise ValueError(
                    f"{branch_id} resource is not bound to the shared evidence bundle"
                )
            difficulty = content.get("difficulty")
            branches.append(
                ResourceBranch(
                    branch_id=branch_id,
                    status="ready",
                    required=required,
                    draft_id=_draft_id(product),
                    payload_type=payload_type,
                    difficulty=(
                        difficulty.strip()
                        if isinstance(difficulty, str) and difficulty.strip()
                        else None
                    ),
                )
            )
        identity = {
            "contract_id": resolved_contract,
            "evidence_bundle_id": resolved_evidence,
            "branches": [branch.as_dict() for branch in branches],
        }
        digest = sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            bundle_id=f"rb-{digest[:24]}",
            contract_id=resolved_contract,
            evidence_bundle_id=resolved_evidence,
            branches=tuple(branches),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "contract_id": self.contract_id,
            "evidence_bundle_id": self.evidence_bundle_id,
            "branches": [branch.as_dict() for branch in self.branches],
        }
