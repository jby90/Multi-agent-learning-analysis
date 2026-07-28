from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from orchestrator.review_flow import (
    ReviewFlowError,
    ReviewFlowTerminal,
    audit_and_review,
)


def _product(label: str) -> dict[str, Any]:
    return {
        "agent": "task",
        "role": "probe",
        "payload": {
            "type": "quiz_set",
            "content": {"question": label},
        },
        "evidence": [{"kind": "quiz_answer_key", "ref": label}],
    }


def _verdict(
    product: Mapping[str, Any],
    decision: str,
    *,
    reviewed_msg_id: str | None = None,
    role: str = "verdict",
) -> dict[str, Any]:
    return {
        "agent": "review",
        "role": role,
        "payload": {
            "type": "review_verdict",
            "content": {
                "reviewed_msg_id": (
                    reviewed_msg_id
                    if reviewed_msg_id is not None
                    else str(product["msg_id"])
                ),
            },
        },
        "evidence": [],
        "verdict": {
            "decision": decision,
            "rule_hits": (
                []
                if decision != "reject"
                else [
                    {
                        "rule_id": "R-04",
                        "reason": "问题需要重新生成",
                        "evidence_ref": str(product["msg_id"]),
                    }
                ]
            ),
            "difficulty_action": "none",
        },
    }


class AuditLog:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def __call__(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        message = dict(draft)
        message.setdefault("msg_id", f"trace-{len(self.messages) + 1:03d}")
        self.messages.append(message)
        return message


def test_audit_and_review_returns_only_a_matching_approved_product() -> None:
    audit = AuditLog()

    approved = audit_and_review(
        lambda: _product("经证据约束的问题"),
        audit=audit,
        review=lambda product: _verdict(product, "approve"),
        generate_rebuttal=lambda *_: pytest.fail("approve must not rebut"),
        re_review=lambda *_: pytest.fail("approve must not re-review"),
    )

    assert approved["payload"]["content"]["question"] == "经证据约束的问题"
    assert [message["role"] for message in audit.messages] == [
        "probe",
        "verdict",
    ]
    assert (
        audit.messages[1]["payload"]["content"]["reviewed_msg_id"]
        == approved["msg_id"]
    )


def test_audit_and_review_routes_reject_through_rebuttal_and_re_review() -> None:
    audit = AuditLog()

    def rebuttal(
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "agent": "task",
            "role": "rebuttal",
            "payload": {
                "type": "rebuttal_case",
                "content": {
                    "product_msg_id": product["msg_id"],
                    "verdict_msg_id": verdict["msg_id"],
                },
            },
            "evidence": [],
        }

    approved = audit_and_review(
        lambda: _product("需要辩护的问题"),
        audit=audit,
        review=lambda product: _verdict(product, "reject"),
        generate_rebuttal=rebuttal,
        re_review=lambda product, *_: _verdict(
            product,
            "approve",
            role="re_verdict",
        ),
    )

    assert approved["payload"]["content"]["question"] == "需要辩护的问题"
    assert [message["role"] for message in audit.messages] == [
        "probe",
        "verdict",
        "rebuttal",
        "re_verdict",
    ]


def test_audit_and_review_regenerates_without_returning_rejected_text() -> None:
    audit = AuditLog()
    labels = iter(("不得展示的首版问题", "审核通过的替代问题"))

    def review(product: Mapping[str, Any]) -> dict[str, Any]:
        decision = (
            "reject"
            if product["payload"]["content"]["question"] == "不得展示的首版问题"
            else "approve"
        )
        return _verdict(product, decision)

    approved = audit_and_review(
        lambda: _product(next(labels)),
        audit=audit,
        review=review,
        generate_rebuttal=lambda *_: {
            "agent": "task",
            "role": "rebuttal",
            "payload": {"type": "rebuttal_case", "content": {}},
            "evidence": [],
        },
        re_review=lambda product, *_: _verdict(
            product,
            "reject",
            role="re_verdict",
        ),
    )

    assert approved["payload"]["content"]["question"] == "审核通过的替代问题"
    assert [
        message["payload"]["content"].get("question")
        for message in audit.messages
        if message["role"] == "probe"
    ] == ["不得展示的首版问题", "审核通过的替代问题"]


def test_audit_and_review_rejects_a_verdict_for_another_message() -> None:
    audit = AuditLog()

    with pytest.raises(ReviewFlowError, match="association"):
        audit_and_review(
            lambda: _product("关联必须精确"),
            audit=audit,
            review=lambda product: _verdict(
                product,
                "approve",
                reviewed_msg_id="another-message",
            ),
            generate_rebuttal=lambda *_: pytest.fail("must fail before rebuttal"),
            re_review=lambda *_: pytest.fail("must fail before re-review"),
        )


def test_audit_and_review_exhaustion_fails_closed() -> None:
    audit = AuditLog()

    with pytest.raises(ReviewFlowTerminal) as raised:
        audit_and_review(
            lambda: _product("始终未通过的问题"),
            audit=audit,
            review=lambda product: _verdict(product, "reject"),
            generate_rebuttal=lambda *_: {
                "agent": "task",
                "role": "rebuttal",
                "payload": {"type": "rebuttal_case", "content": {}},
                "evidence": [],
            },
            re_review=lambda product, *_: _verdict(
                product,
                "reject",
                role="re_verdict",
            ),
            max_cycles=2,
        )

    assert raised.value.action == "refuse"
    assert raised.value.control_message["role"] == "re_verdict"
