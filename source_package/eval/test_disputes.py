from __future__ import annotations

from typing import Any

import pytest

from coordination.disputes import plan_review_dispute


def _verdict(decision: str, *rule_ids: str) -> dict[str, Any]:
    return {
        "verdict": {
            "decision": decision,
            "rule_hits": [
                {
                    "rule_id": rule_id,
                    "reason": f"{rule_id} reason",
                    "evidence_ref": f"evidence-{index}",
                }
                for index, rule_id in enumerate(rule_ids, start=1)
            ],
        }
    }


def test_soft_rules_route_only_the_required_specialists() -> None:
    r02 = plan_review_dispute(_verdict("reject", "R-02"))
    both = plan_review_dispute(_verdict("reject", "R-02", "R-03"))

    assert r02.route == "targeted_debate"
    assert r02.specialist_agents == ("evidence_review",)
    assert both.specialist_agents == ("evidence_review", "pedagogy_review")
    assert both.as_event_details()["rule_ids"] == ["R-02", "R-03"]


@pytest.mark.parametrize("rules", [("R-01",), ("R-04",), ("R-05",), ("R-02", "R-04")])
def test_hard_or_mixed_rejects_skip_debate(rules: tuple[str, ...]) -> None:
    plan = plan_review_dispute(_verdict("reject", *rules))

    assert plan.route == "local_regeneration"
    assert plan.specialist_agents == ()


def test_unknown_rule_fails_closed_to_regeneration() -> None:
    plan = plan_review_dispute(_verdict("reject", "R-99"))

    assert plan.route == "local_regeneration"
    assert plan.hard_veto_rules == ("R-99",)


def test_approved_result_routes_to_publish() -> None:
    assert plan_review_dispute(_verdict("approve")).route == "publish"


def test_reject_without_rule_hits_is_invalid() -> None:
    with pytest.raises(ValueError, match="rule_hits"):
        plan_review_dispute(_verdict("reject"))
