"""Internal outcome categories for audited orchestration results."""

from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    COMPLETED = "completed"
    SAFE_REJECTED = "safe_rejected"
    EXTERNAL_UNAVAILABLE = "external_unavailable"
    SYSTEM_ERROR = "system_error"


SAFE_REJECTED_VERIFICATION_EVENTS = frozenset(
    {
        "refuse_out_of_scope",
        "sandbox_rejected",
        "template_authority_rejected",
        "query_empty",
    }
)
EXTERNAL_UNAVAILABLE_VERIFICATION_EVENTS = frozenset(
    {
        "query_timeout",
        "query_failed",
    }
)
VERIFICATION_FAILURE_OUTCOMES = {
    **{
        event: Outcome.SAFE_REJECTED
        for event in SAFE_REJECTED_VERIFICATION_EVENTS
    },
    **{
        event: Outcome.EXTERNAL_UNAVAILABLE
        for event in EXTERNAL_UNAVAILABLE_VERIFICATION_EVENTS
    },
}


def verification_outcome(event: object) -> Outcome | None:
    return VERIFICATION_FAILURE_OUTCOMES.get(str(event))


class OutcomeError(RuntimeError):
    """Stop one run with an explicit, non-success outcome."""

    def __init__(
        self,
        outcome: Outcome,
        *,
        event: str,
        detail: str | None = None,
    ) -> None:
        if outcome is Outcome.COMPLETED:
            raise ValueError("completed is not an error outcome")
        self.outcome = outcome
        self.event = event
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{outcome.value}: {event}{suffix}")
