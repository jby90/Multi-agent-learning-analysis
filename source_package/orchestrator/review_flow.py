"""Shared production control flow for review, rebuttal, and regeneration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from coordination.contracts import QualityPolicy
from coordination.disputes import plan_review_dispute


class ReviewFlowError(RuntimeError):
    """Raised when review cannot reach approval or a canonical fallback."""


class ReviewFlowTerminal(ReviewFlowError):
    """Carry a canonical terminal fallback to the interactive boundary."""

    def __init__(
        self,
        action: str,
        control_message: Mapping[str, Any],
    ) -> None:
        self.action = action
        self.control_message = dict(control_message)
        super().__init__(f"review flow terminated with {action}")


class ReviewFlowInterrupted(ReviewFlowError):
    """Carry the last canonical message when review follow-up becomes unavailable."""

    def __init__(self, last_message: Mapping[str, Any]) -> None:
        self.last_message = dict(last_message)
        super().__init__("review follow-up became unavailable")


TransitionSender = Callable[
    [Mapping[str, Any]],
    tuple[dict[str, Any], str],
]
AuditSender = Callable[[Mapping[str, Any]], dict[str, Any]]
ReviewCall = Callable[[Mapping[str, Any]], dict[str, Any]]
RebuttalCall = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    dict[str, Any],
]
ReReviewCall = Callable[
    [Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]],
    dict[str, Any],
]
ApproveWithFixRevision = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    dict[str, Any],
]
FallbackResolver = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    dict[str, Any] | None,
]
ApprovedCallback = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    None,
]
ReviewEventCallback = Callable[[str, Mapping[str, Any]], None]


def _notify(
    callback: ReviewEventCallback | None,
    event: str,
    **details: Any,
) -> None:
    if callback is not None:
        callback(event, details)


def _review_decision(
    verdict_message: Mapping[str, Any],
    product: Mapping[str, Any],
) -> str:
    product_msg_id = product.get("msg_id")
    payload = verdict_message.get("payload")
    content = payload.get("content") if isinstance(payload, Mapping) else None
    reviewed_msg_id = (
        content.get("reviewed_msg_id") if isinstance(content, Mapping) else None
    )
    verdict = verdict_message.get("verdict")
    decision = verdict.get("decision") if isinstance(verdict, Mapping) else None
    if (
        not isinstance(product_msg_id, str)
        or not product_msg_id.strip()
        or reviewed_msg_id != product_msg_id
    ):
        raise ReviewFlowError("review association does not match the audited product")
    if decision not in {"approve", "approve_with_fix", "reject"}:
        raise ReviewFlowError("review decision is not canonical")
    return str(decision)


def audit_and_review(
    producer: Callable[[], dict[str, Any]],
    *,
    audit: AuditSender,
    review: ReviewCall,
    generate_rebuttal: RebuttalCall,
    re_review: ReReviewCall,
    revise_approve_with_fix: ApproveWithFixRevision | None = None,
    max_cycles: int = 4,
    terminal_action: str = "refuse",
    on_event: ReviewEventCallback | None = None,
    quality_policy: QualityPolicy | None = None,
) -> dict[str, Any]:
    """Review an in-state product without inventing state-machine transitions.

    Interactive follow-up questions are produced while the learner remains in
    S7/S8.  They still use the same Review/Rebuttal/Re-review contract and the
    same bounded regeneration policy as state-transitioned products, but every
    message is appended through the audit path.
    """

    if max_cycles < 1:
        raise ValueError("max_cycles must be positive")
    if terminal_action not in {"human_review", "refuse"}:
        raise ValueError("terminal_action must be human_review or refuse")
    last_message: dict[str, Any] | None = None
    pending_revision: dict[str, Any] | None = None
    for cycle in range(1, max_cycles + 1):
        try:
            _notify(on_event, "producer_started", cycle=cycle)
            product = audit(
                pending_revision if pending_revision is not None else producer()
            )
            pending_revision = None
            _notify(on_event, "product_ready", cycle=cycle)
            _notify(on_event, "review_started", cycle=cycle)
            original = audit(review(product))
            last_message = original
            decision = _review_decision(original, product)
            dispute_plan = plan_review_dispute(original, policy=quality_policy)
            _notify(
                on_event,
                "review_completed",
                cycle=cycle,
                decision=decision,
                **dispute_plan.as_event_details(),
            )
            if decision == "approve":
                return product
            if decision == "approve_with_fix":
                if revise_approve_with_fix is None:
                    return product
                _notify(on_event, "revision_started", cycle=cycle)
                pending_revision = revise_approve_with_fix(product, original)
                _notify(on_event, "revision_completed", cycle=cycle)
                continue

            if dispute_plan.route == "local_regeneration":
                _notify(
                    on_event,
                    "regeneration_started",
                    cycle=cycle,
                    **dispute_plan.as_event_details(),
                )
                continue

            _notify(
                on_event,
                "debate_started",
                cycle=cycle,
                **dispute_plan.as_event_details(),
            )
            rebuttal = audit(generate_rebuttal(product, original))
            last_message = rebuttal
            reconsidered = audit(re_review(product, original, rebuttal))
            last_message = reconsidered
            re_decision = _review_decision(reconsidered, product)
            _notify(
                on_event,
                "debate_completed",
                cycle=cycle,
                decision=re_decision,
                **dispute_plan.as_event_details(),
            )
            if re_decision in {"approve", "approve_with_fix"}:
                return product
        except (ReviewFlowError, ReviewFlowTerminal):
            raise
        except Exception as exc:
            if last_message is None:
                raise ReviewFlowError("review audit could not start safely") from exc
            raise ReviewFlowInterrupted(last_message) from exc

    if last_message is None:
        raise ReviewFlowError("review audit did not produce a canonical message")
    raise ReviewFlowTerminal(terminal_action, last_message)


def produce_and_review(
    producer: Callable[[], dict[str, Any]],
    *,
    produced_transition: str,
    approved_transition: str,
    send_transition: TransitionSender,
    audit: AuditSender,
    review: ReviewCall,
    generate_rebuttal: RebuttalCall,
    re_review: ReReviewCall,
    resolve_fallback: FallbackResolver,
    on_approved: ApprovedCallback | None = None,
    max_cycles: int = 4,
    on_event: ReviewEventCallback | None = None,
    quality_policy: QualityPolicy | None = None,
) -> dict[str, Any]:
    """Produce until Review approves, the rebuttal wins, or the engine falls back."""

    if max_cycles < 1:
        raise ValueError("max_cycles must be positive")
    for cycle in range(1, max_cycles + 1):
        _notify(on_event, "producer_started", cycle=cycle)
        product, actual_transition = send_transition(producer())
        _notify(on_event, "product_ready", cycle=cycle)
        if actual_transition != produced_transition:
            raise ReviewFlowError(
                f"expected {produced_transition}, got {actual_transition}"
            )
        _notify(on_event, "review_started", cycle=cycle)
        original, transition_id = send_transition(review(product))
        dispute_plan = plan_review_dispute(original, policy=quality_policy)
        _notify(
            on_event,
            "review_completed",
            cycle=cycle,
            transition=transition_id,
            **dispute_plan.as_event_details(),
        )
        if transition_id == approved_transition:
            if on_approved is not None:
                on_approved(product, original)
            return product
        if transition_id == "T08":
            fallback = resolve_fallback(product, original)
            if fallback is None:
                raise ReviewFlowError("T08 did not yield a canonical fallback")
            return fallback
        if transition_id != "T05":
            raise ReviewFlowError(
                f"review reject expected T05, got {transition_id}"
            )

        if dispute_plan.route == "local_regeneration":
            _notify(
                on_event,
                "regeneration_started",
                cycle=cycle,
                **dispute_plan.as_event_details(),
            )
            last_message = original
            try:
                concession = audit(generate_rebuttal(product, original))
                last_message = concession
                re_message, re_transition = send_transition(
                    re_review(product, original, concession)
                )
                _notify(
                    on_event,
                    "regeneration_completed",
                    cycle=cycle,
                    transition=re_transition,
                    **dispute_plan.as_event_details(),
                )
            except ReviewFlowTerminal:
                raise
            except Exception as exc:
                raise ReviewFlowInterrupted(last_message) from exc
            if re_transition == "T08":
                fallback = resolve_fallback(product, re_message)
                if fallback is None:
                    raise ReviewFlowError("T08 did not yield a canonical fallback")
                return fallback
            if re_transition != "T07":
                raise ReviewFlowError(
                    f"hard rejection regeneration expected T07, got {re_transition}"
                )
            continue

        _notify(
            on_event,
            "debate_started",
            cycle=cycle,
            **dispute_plan.as_event_details(),
        )
        last_message = original
        try:
            rebuttal = audit(generate_rebuttal(product, original))
            last_message = rebuttal
            re_message, re_transition = send_transition(
                re_review(product, original, rebuttal)
            )
            _notify(
                on_event,
                "debate_completed",
                cycle=cycle,
                transition=re_transition,
                **dispute_plan.as_event_details(),
            )
        except ReviewFlowTerminal:
            raise
        except Exception as exc:
            raise ReviewFlowInterrupted(last_message) from exc
        if re_transition == "T06":
            if on_approved is not None:
                on_approved(product, re_message)
            return product
        if re_transition == "T08":
            fallback = resolve_fallback(product, re_message)
            if fallback is None:
                raise ReviewFlowError("T08 did not yield a canonical fallback")
            return fallback
        if re_transition != "T07":
            raise ReviewFlowError(
                f"re-review reject expected T07, got {re_transition}"
            )

    raise ReviewFlowError("review regeneration cycle exceeded safety bound")
