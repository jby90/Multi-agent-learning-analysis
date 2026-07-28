"""Thread-safe, session-scoped Agent activity events for live observability."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Condition, RLock
from typing import Any, Mapping, Sequence


AGENT_IDS = frozenset(
    {
        "diagnosis",
        "knowledge",
        "task",
        "verification",
        "evidence_review",
        "pedagogy_review",
        "data_safety_review",
        "readability_review",
        "review",
    }
)
AGENT_STATUSES = frozenset(
    {
        "idle",
        "queued",
        "working",
        "waiting",
        "collaborating",
        "reviewing",
        "debating",
        "approved",
        "blocked",
        "done",
    }
)


class AgentEventStream:
    """Keep a bounded event history and wake SSE clients on new activity."""

    def __init__(self, trace_id: str, *, max_events: int = 500) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.trace_id = trace_id
        self.max_events = max_events
        self._events: list[dict[str, Any]] = []
        self._next_sequence = 1
        self._condition = Condition(RLock())

    def publish(
        self,
        *,
        agent: str,
        status: str,
        activity: str,
        label: str,
        stage: str,
        peers: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent not in AGENT_IDS:
            raise ValueError(f"unknown agent: {agent}")
        if status not in AGENT_STATUSES:
            raise ValueError(f"unknown agent status: {status}")
        normalized_peers = [
            peer for peer in dict.fromkeys(peers)
            if peer in AGENT_IDS and peer != agent
        ]
        with self._condition:
            sequence = self._next_sequence
            self._next_sequence += 1
            event: dict[str, Any] = {
                "sequence": sequence,
                "trace_id": self.trace_id,
                "agent": agent,
                "status": status,
                "activity": activity,
                "label": label,
                "stage": stage,
                "peers": normalized_peers,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if details:
                event["details"] = dict(details)
            self._events.append(event)
            if len(self._events) > self.max_events:
                del self._events[: len(self._events) - self.max_events]
            self._condition.notify_all()
            return dict(event)

    def after(self, sequence: int = 0) -> list[dict[str, Any]]:
        with self._condition:
            return [dict(event) for event in self._events if event["sequence"] > sequence]

    def wait_after(
        self,
        sequence: int = 0,
        *,
        timeout: float = 15.0,
    ) -> list[dict[str, Any]]:
        with self._condition:
            events = [
                dict(event)
                for event in self._events
                if event["sequence"] > sequence
            ]
            if events:
                return events
            self._condition.wait(timeout=max(timeout, 0.0))
            return [
                dict(event)
                for event in self._events
                if event["sequence"] > sequence
            ]
