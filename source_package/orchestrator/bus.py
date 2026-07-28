"""Validated append-only message bus for orchestrator traces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any, Mapping

from agents.validate_message import validate_message


RESERVED_SENDER_FIELDS = frozenset({"step", "msg_id"})
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class TraceRoutingError(ValueError):
    """Raised when a trace cannot be routed safely under the trace directory."""


@dataclass(frozen=True, slots=True)
class BusResult:
    accepted: bool
    message: dict[str, Any]
    errors: tuple[str, ...]


class MessageBus:
    """Assign envelopes, validate messages, and append every attempt to trace."""

    def __init__(self, trace_dir: Path) -> None:
        self._trace_dir = Path(trace_dir)
        self._next_steps: dict[str, int] = {}
        self._closed: dict[str, str] = {}
        self._lock = Lock()

    def trace_path(self, trace_id: str) -> Path:
        self._validate_trace_id(trace_id)
        root = self._trace_dir.resolve()
        path = (root / f"{trace_id}.jsonl").resolve()
        if path.parent != root:
            raise TraceRoutingError("trace_id must stay inside the trace directory")
        return path

    def close_trace(self, trace_id: str, terminal_state: str) -> None:
        self._validate_trace_id(trace_id)
        with self._lock:
            self._closed[trace_id] = terminal_state

    def send(self, draft: Mapping[str, Any]) -> BusResult:
        trace_id = draft.get("trace_id")
        self._validate_trace_id(trace_id)
        path = self.trace_path(trace_id)

        with self._lock:
            if trace_id not in self._next_steps and path.exists():
                raise TraceRoutingError(
                    f"trace already exists and cannot be resumed: {trace_id}"
                )
            step = self._next_steps.get(trace_id, 1)
            self._next_steps[trace_id] = step + 1

            message = deepcopy(dict(draft))
            supplied_reserved = sorted(RESERVED_SENDER_FIELDS.intersection(message))
            for field in RESERVED_SENDER_FIELDS:
                message.pop(field, None)
            message["step"] = step
            message["msg_id"] = f"{trace_id}-{step:03d}"

            errors: list[str] = []
            if supplied_reserved:
                errors.append(
                    f"BUS reserved sender fields: {', '.join(supplied_reserved)}"
                )
            terminal_state = self._closed.get(trace_id)
            if terminal_state is not None:
                errors.append(f"BUS trace is closed at {terminal_state}")
            errors.extend(validate_message(message))

            if errors:
                message["rejected_by_bus"] = True
                message["bus_errors"] = errors

            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as trace_file:
                trace_file.write(json.dumps(message, ensure_ascii=False) + "\n")

            return BusResult(not errors, message, tuple(errors))

    @staticmethod
    def _validate_trace_id(trace_id: object) -> None:
        if not isinstance(trace_id, str) or _TRACE_ID_RE.fullmatch(trace_id) is None:
            raise TraceRoutingError(
                "trace_id must be a safe identifier of 1-128 ASCII letters, "
                "digits, dots, underscores, or hyphens"
            )
        device_stem = trace_id.split(".", 1)[0].upper()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise TraceRoutingError(
                f"trace_id uses a reserved Windows device name: {trace_id}"
            )
