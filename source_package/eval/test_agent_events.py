from __future__ import annotations

from threading import Thread
from time import sleep

import pytest

from orchestrator.agent_events import AgentEventStream


def test_event_stream_orders_and_filters_session_events() -> None:
    stream = AgentEventStream("trace-1")
    first = stream.publish(
        agent="knowledge",
        status="working",
        activity="retrieval",
        label="正在检索证据",
        stage="S2_KNOWLEDGE",
    )
    second = stream.publish(
        agent="review",
        status="reviewing",
        activity="quality_gate",
        label="正在审核",
        stage="S5_REVIEW",
        peers=("knowledge", "knowledge", "system"),
    )

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert second["peers"] == ["knowledge"]
    assert stream.after(1) == [second]


def test_wait_after_wakes_when_an_agent_event_arrives() -> None:
    stream = AgentEventStream("trace-2")

    def publish_later() -> None:
        sleep(0.02)
        stream.publish(
            agent="task",
            status="queued",
            activity="task_design",
            label="任务进入队列",
            stage="S3_TASK",
        )

    worker = Thread(target=publish_later)
    worker.start()
    events = stream.wait_after(0, timeout=0.5)
    worker.join()

    assert [event["agent"] for event in events] == ["task"]


def test_event_stream_accepts_independent_quality_specialists() -> None:
    stream = AgentEventStream("trace-specialists")
    evidence = stream.publish(
        agent="evidence_review",
        status="working",
        activity="specialist_quality_review",
        label="正在独立核验事实与证据",
        stage="S5_REVIEW",
        peers=("pedagogy_review", "review"),
    )

    assert evidence["agent"] == "evidence_review"
    assert evidence["peers"] == ["pedagogy_review", "review"]


@pytest.mark.parametrize(
    ("agent", "status"),
    [("system", "working"), ("task", "thinking")],
)
def test_event_stream_rejects_unknown_public_states(agent: str, status: str) -> None:
    stream = AgentEventStream("trace-3")
    with pytest.raises(ValueError):
        stream.publish(
            agent=agent,
            status=status,
            activity="unsafe",
            label="不应发布",
            stage="S_FAIL",
        )
