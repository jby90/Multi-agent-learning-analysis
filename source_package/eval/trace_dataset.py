"""Immutable attempt ledger and self-contained P7 trace dataset utilities."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from eval.case_matrix import EvaluationCase, load_case_matrix
from orchestrator.outcomes import Outcome


ATTEMPT_FIELDS = frozenset(
    {
        "case_id",
        "attempt",
        "trace_id",
        "status",
        "trace_path",
        "cache_path",
        "base_commit",
        "started_at",
        "finished_at",
        "terminal_state",
        "transition_sequence",
        "message_count",
        "error_type",
        "error_message",
        "outcome",
    }
)
LEGACY_ATTEMPT_FIELDS = ATTEMPT_FIELDS - {"outcome"}
OUTCOME_VALUES = frozenset(item.value for item in Outcome)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    case_id: str
    attempt: int
    trace_id: str
    status: str
    trace_path: str
    cache_path: str
    base_commit: str
    started_at: str
    finished_at: str
    terminal_state: str | None
    transition_sequence: tuple[str, ...]
    message_count: int
    error_type: str | None
    error_message: str | None
    outcome: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["transition_sequence"] = list(self.transition_sequence)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "AttemptRecord":
        if not isinstance(value, Mapping):
            raise ValueError("attempt record fields do not match schema")
        fields = set(value)
        if fields == LEGACY_ATTEMPT_FIELDS:
            value = dict(value)
            value["outcome"] = (
                Outcome.COMPLETED.value
                if value.get("status") == "succeeded"
                else Outcome.SYSTEM_ERROR.value
            )
        elif fields != ATTEMPT_FIELDS:
            raise ValueError("attempt record fields do not match schema")
        if value["status"] not in {"succeeded", "failed"}:
            raise ValueError("attempt status must be succeeded or failed")
        if value["outcome"] not in OUTCOME_VALUES:
            raise ValueError("attempt outcome is not recognized")
        if (value["status"] == "succeeded") != (
            value["outcome"] == Outcome.COMPLETED.value
        ):
            raise ValueError("attempt status and outcome do not agree")
        if not isinstance(value["attempt"], int) or value["attempt"] < 1:
            raise ValueError("attempt must be a positive integer")
        transitions = value["transition_sequence"]
        if not isinstance(transitions, list) or any(
            not isinstance(item, str) or not item for item in transitions
        ):
            raise ValueError("transition_sequence must be a string list")
        for field in (
            "case_id",
            "trace_id",
            "trace_path",
            "cache_path",
            "base_commit",
            "started_at",
            "finished_at",
        ):
            if not isinstance(value[field], str) or not value[field]:
                raise ValueError(f"attempt {field} must be a non-empty string")
        if not isinstance(value["message_count"], int) or value["message_count"] < 0:
            raise ValueError("message_count must be a non-negative integer")
        return cls(
            case_id=value["case_id"],
            attempt=value["attempt"],
            trace_id=value["trace_id"],
            status=value["status"],
            trace_path=value["trace_path"],
            cache_path=value["cache_path"],
            base_commit=value["base_commit"],
            started_at=value["started_at"],
            finished_at=value["finished_at"],
            terminal_state=value["terminal_state"],
            transition_sequence=tuple(transitions),
            message_count=value["message_count"],
            error_type=value["error_type"],
            error_message=value["error_message"],
            outcome=value["outcome"],
        )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_lines(path: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read JSONL {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSON at {path}:{line_number}: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row at {path}:{line_number} must be an object")
        rows.append(value)
    return tuple(rows)


def load_attempt_records(path: Path) -> tuple[AttemptRecord, ...]:
    path = Path(path)
    if not path.exists():
        return ()
    return tuple(AttemptRecord.from_dict(row) for row in _read_json_lines(path))


def write_attempt_record(path: Path, record: AttemptRecord) -> None:
    path = Path(path)
    existing = load_attempt_records(path)
    key = (record.case_id, record.attempt)
    if any((item.case_id, item.attempt) == key for item in existing):
        raise FileExistsError(
            f"attempt already recorded: {record.case_id} attempt {record.attempt}"
        )
    if any(item.trace_id == record.trace_id for item in existing):
        raise FileExistsError(f"trace_id already recorded: {record.trace_id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as target:
        target.write(canonical_json(record.as_dict()) + "\n")


def _resolve_repository_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"repository path must be safe and relative: {relative}")
    root = Path(root).resolve()
    path = root.joinpath(*pure.parts).resolve()
    if root not in path.parents:
        raise ValueError(f"repository path escapes root: {relative}")
    return path


def _trace_transitions(messages: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    transitions: list[str] = []
    for message in messages:
        if message.get("role") != "system" or message.get("rejected_by_bus"):
            continue
        payload = message.get("payload")
        content = payload.get("content") if isinstance(payload, Mapping) else None
        transition = content.get("transition_id") if isinstance(content, Mapping) else None
        if isinstance(transition, str):
            transitions.append(transition)
    return tuple(transitions)


def _validated_messages(record: AttemptRecord, trace_path: Path) -> tuple[dict[str, Any], ...]:
    messages = _read_json_lines(trace_path)
    if not messages:
        raise ValueError(f"trace is empty: {record.trace_id}")
    for index, message in enumerate(messages, start=1):
        if message.get("trace_id") != record.trace_id:
            raise ValueError(
                f"trace_id mismatch in {record.trace_id} at message {index}"
            )
        if message.get("step") != index:
            raise ValueError(f"non-contiguous step in {record.trace_id} at {index}")
        if message.get("msg_id") != f"{record.trace_id}-{index:03d}":
            raise ValueError(f"msg_id mismatch in {record.trace_id} at {index}")
    if record.message_count != len(messages):
        raise ValueError(f"message_count mismatch for {record.trace_id}")
    transitions = _trace_transitions(messages)
    if record.transition_sequence != transitions:
        raise ValueError(f"transition sequence mismatch for {record.trace_id}")
    if record.status == "succeeded":
        if record.terminal_state != "S10_DONE" or not transitions or transitions[-1] != "T20":
            raise ValueError(f"successful trace is not terminal S10: {record.trace_id}")
    elif record.terminal_state == "S10_DONE" or (transitions and transitions[-1] == "T20"):
        raise ValueError(f"failed trace cannot claim terminal S10: {record.trace_id}")
    return messages


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    temporary.replace(path)


def build_dataset(
    cases: Sequence[EvaluationCase],
    records: Sequence[AttemptRecord],
    repository_root: Path,
    output: Path,
    *,
    allow_failed: bool = False,
) -> None:
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("dataset cases contain duplicate IDs")
    rows: list[dict[str, Any]] = []
    for case in cases:
        attempts = [item for item in records if item.case_id == case.case_id]
        successes = [
            item
            for item in attempts
            if item.status == "succeeded"
        ]
        if allow_failed:
            if len(attempts) != 1:
                raise ValueError(
                    f"dataset with failed traces requires exactly one attempt for "
                    f"{case.case_id}; found {len(attempts)}"
                )
            record = attempts[0]
        elif len(successes) != 1:
            raise ValueError(
                f"dataset requires one successful attempt for {case.case_id}; "
                f"found {len(successes)}"
            )
        else:
            record = successes[0]
        trace_path = _resolve_repository_path(repository_root, record.trace_path)
        messages = _validated_messages(record, trace_path)
        rows.append(
            {
                "case": case.as_dict(),
                "attempt": record.attempt,
                "trace_id": record.trace_id,
                "trace_path": record.trace_path,
                "trace_sha256": sha256_file(trace_path),
                "base_commit": record.base_commit,
                "terminal_state": record.terminal_state,
                "transition_sequence": list(record.transition_sequence),
                "message_count": len(messages),
                "messages": list(messages),
            }
        )
    _atomic_write(output, "".join(canonical_json(row) + "\n" for row in rows))


def load_dataset(path: Path) -> tuple[dict[str, Any], ...]:
    rows = _read_json_lines(Path(path))
    required = {
        "case",
        "attempt",
        "trace_id",
        "trace_path",
        "trace_sha256",
        "base_commit",
        "terminal_state",
        "transition_sequence",
        "message_count",
        "messages",
    }
    for index, row in enumerate(rows, start=1):
        if set(row) != required:
            raise ValueError(f"dataset row {index} fields do not match schema")
        if not isinstance(row["messages"], list) or not row["messages"]:
            raise ValueError(f"dataset row {index} messages must be non-empty")
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建P7自包含trace数据集")
    parser.add_argument("command", choices=("build",))
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--allow-failed", action="store_true")
    args = parser.parse_args(argv)
    build_dataset(
        load_case_matrix(args.matrix),
        load_attempt_records(args.ledger),
        args.root,
        args.output,
        allow_failed=args.allow_failed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
