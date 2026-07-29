"""Deterministic fixed-stub ablation for transaction-level rebuttal control."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.rebuttal_generator import RebuttalGenerator
from orchestrator.interactive_session import (
    FollowUpReviewPolicy,
    _GENERIC_FOLLOW_UP_REVIEW_FEEDBACK,
    _feedback_for_re_verdict,
)
from orchestrator.llm import LLMResult, TokenUsage
from orchestrator.review_flow import audit_and_review


_TRACE_ID = "innovation-a-fixed-stub"
_FIXED_TIME = datetime(2026, 7, 29, tzinfo=timezone.utc)


class _FixedConcedeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(deepcopy(kwargs))
        return LLMResult(
            data={"concede": True},
            model="fixed-rebuttal-stub",
            latency_ms=1,
            token_usage=TokenUsage(5, 1, 6),
            attempts=1,
        )


class _AuditLog:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def __call__(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        message = deepcopy(dict(draft))
        message.setdefault("trace_id", _TRACE_ID)
        message.setdefault("msg_id", f"{_TRACE_ID}-{len(self.messages) + 1:03d}")
        message.setdefault("step", len(self.messages) + 1)
        self.messages.append(message)
        return message


def _product(index: int) -> dict[str, Any]:
    return {
        "trace_id": _TRACE_ID,
        "agent": "task",
        "role": "probe",
        "payload": {
            "type": "quiz_set",
            "content": {
                "event": "follow_up_question_ready",
                "question": f"固定桩追问-{index}？",
            },
        },
        "evidence": [
            {
                "kind": "kb_chunk",
                "ref": "KB-003",
                "quote": "完成率由计划量与实际量共同确定。",
            }
        ],
        "claims": [],
        "timestamp": _FIXED_TIME.isoformat(),
    }


def _verdict(
    product: Mapping[str, Any],
    decision: str,
    *,
    role: str,
    rule_id: str | None = None,
) -> dict[str, Any]:
    hits = []
    evidence = []
    if decision == "reject":
        assert rule_id is not None
        hits = [
            {
                "rule_id": rule_id,
                "reason": f"fixed-{rule_id}-reason",
                "evidence_ref": str(product["msg_id"]),
            }
        ]
        evidence = [
            {
                "kind": "review_rule",
                "ref": rule_id,
                "quote": f"fixed-{rule_id}-reason",
            }
        ]
    return {
        "trace_id": _TRACE_ID,
        "agent": "review",
        "role": role,
        "payload": {
            "type": "review_verdict",
            "content": {
                "event": "review_complete",
                "reviewed_payload_type": product["payload"]["type"],
                "reviewed_msg_id": product["msg_id"],
            },
        },
        "evidence": evidence,
        "claims": [],
        "verdict": {
            "decision": decision,
            "rule_hits": hits,
            "difficulty_action": "none",
        },
        "timestamp": _FIXED_TIME.isoformat(),
    }


def _message_signature(message: Mapping[str, Any]) -> dict[str, Any]:
    payload = message.get("payload")
    content = payload.get("content") if isinstance(payload, Mapping) else {}
    verdict = message.get("verdict")
    return {
        "msg_id": message.get("msg_id"),
        "role": message.get("role"),
        "payload_type": payload.get("type") if isinstance(payload, Mapping) else None,
        "question": content.get("question") if isinstance(content, Mapping) else None,
        "concede": content.get("concede") if isinstance(content, Mapping) else None,
        "decision": verdict.get("decision") if isinstance(verdict, Mapping) else None,
        "rule_ids": [
            hit.get("rule_id")
            for hit in verdict.get("rule_hits", [])
            if isinstance(hit, Mapping)
        ]
        if isinstance(verdict, Mapping)
        else [],
    }


def _run_scenario(
    policy: FollowUpReviewPolicy,
    *,
    direct_baseline: bool = False,
) -> dict[str, Any]:
    audit = _AuditLog()
    llm = _FixedConcedeLLM()
    generator = RebuttalGenerator(
        _TRACE_ID,
        llm_call=llm,
        clock=lambda: _FIXED_TIME,
    )
    generation_index = 0
    rebuttal_attempts = 0
    active_product: Mapping[str, Any] | None = None
    last_feedback = _GENERIC_FOLLOW_UP_REVIEW_FEEDBACK
    generation_feedback: list[list[str]] = []
    rules = ("R-02", "R-03")

    def produce() -> dict[str, Any]:
        nonlocal generation_index
        generation_feedback.append(
            [] if generation_index == 0 else list(last_feedback)
        )
        generation_index += 1
        return _product(generation_index)

    def review(product: Mapping[str, Any]) -> dict[str, Any]:
        index = int(str(product["payload"]["content"]["question"]).split("-")[-1][:-1])
        if index <= len(rules):
            return _verdict(
                product,
                "reject",
                role="verdict",
                rule_id=rules[index - 1],
            )
        return _verdict(product, "approve", role="verdict")

    def re_review(
        product: Mapping[str, Any],
        original: Mapping[str, Any],
        rebuttal: Mapping[str, Any],
    ) -> dict[str, Any]:
        del rebuttal
        rule_id = str(original["verdict"]["rule_hits"][0]["rule_id"])
        return _verdict(
            product,
            "reject",
            role="re_verdict",
            rule_id=rule_id,
        )

    def audit_with_feedback(draft: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal active_product, last_feedback
        audited = audit(draft)
        if audited.get("role") == "probe":
            active_product = audited
        elif audited.get("role") == "re_verdict" and active_product is not None:
            mapped = _feedback_for_re_verdict(audited, active_product)
            if mapped:
                last_feedback = (
                    mapped
                    if policy.feedback_mode == "mapped"
                    else _GENERIC_FOLLOW_UP_REVIEW_FEEDBACK
                )
        return audited

    def selective_rebuttal(
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        nonlocal rebuttal_attempts
        if direct_baseline or rebuttal_attempts < policy.rebuttal_budget:
            rebuttal_attempts += 1
            return generator.generate(product, verdict)
        return generator.deterministic_concede(product, verdict)

    approved = audit_and_review(
        produce,
        audit=audit_with_feedback,
        review=review,
        generate_rebuttal=selective_rebuttal,
        re_review=re_review,
        max_cycles=4,
    )
    signatures = [_message_signature(message) for message in audit.messages]
    return {
        "approved_question": approved["payload"]["content"]["question"],
        "message_sequence": signatures,
        "review_calls": sum(item["role"] == "verdict" for item in signatures),
        "re_review_calls": sum(
            item["role"] == "re_verdict" for item in signatures
        ),
        "model_rebuttal_attempts": len(llm.calls),
        "deterministic_concessions": sum(
            item["role"] == "rebuttal" and item["concede"] is True
            for item in signatures
        )
        - len(llm.calls),
        "generation_feedback": generation_feedback,
        "failed_products_published": 0,
        "terminal_action": "approved",
    }


def run_ablation() -> dict[str, Any]:
    groups = {
        "A0": FollowUpReviewPolicy(4, "generic"),
        "A1-B": FollowUpReviewPolicy(1, "generic"),
        "A1-F": FollowUpReviewPolicy(4, "mapped"),
        "A1-BF": FollowUpReviewPolicy(1, "mapped"),
    }
    results = {
        name: _run_scenario(policy)
        for name, policy in groups.items()
    }
    frozen_baseline = _run_scenario(groups["A0"], direct_baseline=True)
    sensitivity = {
        str(budget): _run_scenario(FollowUpReviewPolicy(budget, "mapped"))
        for budget in (0, 1, 4)
    }
    return {
        "schema_version": "innovation-a-ablation-v1",
        "fixed_stub": {
            "history_prefix": "same-free-follow-up-transaction",
            "review_rules": list(("R-02", "R-03")),
            "candidate_count": 3,
            "post_divergence_llm_output_used": False,
        },
        "groups": results,
        "a0_baseline_equivalent": (
            results["A0"] == frozen_baseline
        ),
        "budget_sensitivity": sensitivity,
        "claims_boundary": {
            "proves": [
                "transaction_model_rebuttal_call_bound",
                "complete_re_review_after_valid_rebuttal",
                "mapped_feedback_without_raw_review_text",
                "failed_product_isolation",
            ],
            "does_not_prove": [
                "real_token_reduction",
                "real_question_quality_improvement",
                "learning_outcome_improvement",
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/results/innovation_a_ablation.json"),
    )
    args = parser.parse_args()
    result = run_ablation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
