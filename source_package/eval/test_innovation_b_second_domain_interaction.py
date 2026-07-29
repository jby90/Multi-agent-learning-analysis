from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from agents.follow_up_agent import FollowUpAgent
from agents.misconception_relations import RoutingPolicy
from eval.test_demo_session_domain_runtime import (
    NoopExecutor,
    drive_to_probe,
    runtime as build_domain_runtime,
)
from eval.test_p5_interactive_follow_up import FollowUpScript, _response
from orchestrator.interactive_session import (
    InteractiveSessionManager,
    _InteractiveSession,
)


class _ApproveEveryFollowUp:
    def review(
        self,
        product: Mapping[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "trace_id": product["trace_id"],
            "agent": "review",
            "role": "verdict",
            "payload": {
                "type": "review_verdict",
                "content": {
                    "event": "review_complete",
                    "reviewed_payload_type": product["payload"]["type"],
                    "reviewed_msg_id": product["msg_id"],
                },
            },
            "evidence": [],
            "claims": [],
            "verdict": {
                "decision": "approve",
                "rule_hits": [],
                "difficulty_action": "none",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def re_review(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("approved second-domain product must not re-review")


def _second_domain_session(
    tmp_path: Path,
    mode: str,
) -> tuple[InteractiveSessionManager, str, FollowUpScript, int]:
    trace_id = "innovation-b-first-segment"
    current = build_domain_runtime(
        tmp_path,
        "first_segment",
        trace_id=trace_id,
    )
    drive_to_probe(current)
    current.review = _ApproveEveryFollowUp()  # type: ignore[assignment]
    task = current.task.generate("T-FS02")
    question = str(task["payload"]["content"]["question"])
    follow_up = FollowUpScript(
        _response(
            "needs_support",
            "Which reporting basis should be confirmed before comparing the daily actual value?",
            target="M-FS01",
        ),
        _response(
            "mastered",
            "",
            target="M-FS01",
        ),
    )
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "unused-traces",
        cache_dir=tmp_path / "unused-cache",
        executor_factory=NoopExecutor,
        routing_policy=RoutingPolicy(mode),
    )
    session_id = "first-segment"
    session = _InteractiveSession(
        session_id=session_id,
        runtime=current,
        executor=NoopExecutor(),
        follow_up_agent=FollowUpAgent(trace_id, llm_call=follow_up),
        awaiting="follow_up",
        artifact=task,
        active_task=task,
        learning_task=task,
        interaction={
            "kind": "free_text_follow_up",
            "prompt": question,
            "round": 1,
            "max_rounds": 4,
            "turns": [],
        },
        follow_up_round=1,
        follow_up_question=question,
    )
    with manager._lock:
        manager._sessions[session_id] = session
    before = len(manager.get_state(session_id)["messages"])
    return manager, session_id, follow_up, before


def _public_follow_up_projection(
    state: Mapping[str, Any],
    *,
    after: int,
) -> dict[str, Any]:
    projected_messages = []
    for message in state["messages"][after:]:
        payload = message.get("payload")
        content = payload.get("content") if isinstance(payload, Mapping) else {}
        projected_messages.append(
            {
                "role": message.get("role"),
                "payload_type": (
                    payload.get("type") if isinstance(payload, Mapping) else None
                ),
                "event": content.get("event") if isinstance(content, Mapping) else None,
                "assessment": (
                    content.get("assessment")
                    if isinstance(content, Mapping)
                    else None
                ),
                "diagnosed": (
                    content.get("diagnosed_misconception")
                    if isinstance(content, Mapping)
                    else None
                ),
                "next_target": (
                    content.get("next_target_misconception")
                    if isinstance(content, Mapping)
                    else None
                ),
            }
        )
    return {
        "state": state["state"],
        "awaiting": state["awaiting"],
        "outcome": state["outcome"],
        "interaction": state["interaction"],
        "messages": projected_messages,
    }


def test_second_domain_complete_fixed_stub_interaction_is_b0_b1_equivalent(
    tmp_path: Path,
) -> None:
    results = {}
    for mode in ("total_support", "marginal_support"):
        manager, session_id, follow_up, before = _second_domain_session(
            tmp_path / mode,
            mode,
        )
        first = manager.submit_follow_up(
            session_id,
            "I mixed the daily actual value with the completion-rate definition.",
            "second-domain-turn-1",
        )
        assert first["awaiting"] == "follow_up"
        final = manager.submit_follow_up(
            session_id,
            "I will confirm the reporting basis before judging actual completion.",
            "second-domain-turn-2",
        )
        internal = manager._get_session(session_id)
        assert final["state"] == "S9_PATH_UPDATE"
        assert final["awaiting"] == "advance"
        assert internal.covered_relation_points == set()
        assert len(follow_up.calls) == 2
        new_messages = final["messages"][before:]
        public_text = json.dumps(new_messages, ensure_ascii=False)
        for forbidden in (
            "covered_relation_points",
            "route_support_points",
            "marginal_support",
            "M-01",
            "M-04",
            "M-05",
        ):
            assert forbidden not in public_text
        results[mode] = _public_follow_up_projection(final, after=before)

    assert results["total_support"] == results["marginal_support"]
