"""Run one complete deterministic orchestrator conversation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from orchestrator.agents_stub import (
    build_stubs,
    path_update_draft,
    profile_loaded_draft,
    student_sql_draft,
)
from orchestrator.bus import MessageBus
from orchestrator.engine import OrchestratorEngine


def _send_transition(
    engine: OrchestratorEngine,
    draft: Mapping[str, Any],
    transition_id: str,
) -> dict[str, Any]:
    result = engine.send(draft)
    if (
        not result.transitioned
        or result.transition is None
        or result.transition.transition_id != transition_id
    ):
        raise RuntimeError(
            f"expected {transition_id}, got "
            f"{result.transition.transition_id if result.transition else result.reason}"
        )
    return result.bus_result.message


def _read_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _replay_states(messages: list[dict[str, Any]]) -> list[str]:
    states = [messages[0]["payload"]["content"]["state"]]
    states.extend(
        message["payload"]["content"]["to_state"]
        for message in messages
        if message.get("role") == "system"
        and "transition_id" in message.get("payload", {}).get("content", {})
        and not message.get("rejected_by_bus", False)
    )
    return states


def main(
    trace_dir: Path | None = None,
    trace_id: str | None = None,
) -> int:
    resolved_trace_dir = trace_dir or Path(__file__).resolve().parents[1] / "traces"
    resolved_trace_id = trace_id or f"demo-{uuid4().hex[:12]}"
    bus = MessageBus(resolved_trace_dir)
    engine = OrchestratorEngine(bus, resolved_trace_id, "cs_student")
    stubs = build_stubs(resolved_trace_id)

    _send_transition(engine, profile_loaded_draft(resolved_trace_id), "T01")
    _send_transition(engine, stubs.diagnosis.profile_assessment(), "T02")
    lecture = _send_transition(engine, stubs.knowledge.lecture(), "T03")
    _send_transition(
        engine,
        stubs.review.verdict("approve", "lecture_note", lecture["msg_id"]),
        "T04",
    )
    task = _send_transition(engine, stubs.task.task("quiz_set"), "T09")
    _send_transition(
        engine,
        stubs.review.verdict("approve", "quiz_set", task["msg_id"]),
        "T10",
    )
    _send_transition(engine, student_sql_draft(resolved_trace_id), "T11")
    sql_result = _send_transition(engine, stubs.verification.sql_result(), "T12")
    _send_transition(
        engine,
        stubs.review.verdict("approve", "sql_result", sql_result["msg_id"]),
        "T13",
    )
    _send_transition(
        engine,
        path_update_draft(
            resolved_trace_id,
            has_next=False,
            learning_goal_achieved=True,
        ),
        "T20",
    )

    messages = _read_trace(bus.trace_path(resolved_trace_id))
    states = _replay_states(messages)
    if states != [state.value for state in engine.state_history]:
        raise RuntimeError("trace replay diverged from engine state history")

    print(f"Trace ID: {resolved_trace_id}")
    print(f"State sequence: {' -> '.join(states)}")
    print(f"Message count: {len(messages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
