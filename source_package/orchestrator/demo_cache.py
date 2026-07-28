"""Session-scoped JSONL cache for auditable demo LLM calls."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from contextlib import contextmanager
from threading import RLock
from typing import Any, Callable, Mapping

from orchestrator.llm import LLMResult, TokenUsage


SCHEMA_VERSION = 1
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class DemoCacheError(RuntimeError):
    """Raised when a demo cache is unsafe, incomplete, or inconsistent."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DemoCacheError(f"LLM request is not JSON serializable: {exc}") from exc


def _without_volatile_audit_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_volatile_audit_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _without_volatile_audit_fields(item)
        for key, item in value.items()
        if key not in {"timestamp", "latency_ms", "query_elapsed_ms", "cached"}
        and not key.endswith("_latency_ms")
    }


def _fingerprint_request(request: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(request)
    user = normalized.get("user")
    if isinstance(user, str):
        try:
            parsed_user = json.loads(user)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed_user, (dict, list)):
                normalized["user"] = _without_volatile_audit_fields(parsed_user)
    return normalized


def request_fingerprint(request: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 over the complete LLM request."""

    normalized = _fingerprint_request(request)
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def _plain_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise DemoCacheError(f"{field} must be an integer >= {minimum}")
    return value


def _validate_usage(usage: TokenUsage) -> None:
    prompt = _plain_int(usage.prompt_tokens, "prompt_tokens")
    completion = _plain_int(usage.completion_tokens, "completion_tokens")
    total = _plain_int(usage.total_tokens, "total_tokens")
    cached = _plain_int(usage.cached_tokens, "cached_tokens")
    if total != prompt + completion:
        raise DemoCacheError("total_tokens must equal prompt_tokens + completion_tokens")
    if cached > prompt:
        raise DemoCacheError("cached_tokens cannot exceed prompt_tokens")


def _validate_result(result: Any) -> LLMResult:
    if not isinstance(result, LLMResult):
        raise DemoCacheError("live LLM call must return LLMResult")
    if not isinstance(result.data, dict):
        raise DemoCacheError("LLMResult.data must be an object")
    if not isinstance(result.model, str) or not result.model.strip():
        raise DemoCacheError("LLMResult.model must be a non-empty string")
    _plain_int(result.latency_ms, "latency_ms")
    _plain_int(result.attempts, "attempts", minimum=1)
    if not isinstance(result.token_usage, TokenUsage):
        raise DemoCacheError("LLMResult.token_usage must be TokenUsage")
    _validate_usage(result.token_usage)
    _canonical_json(result.data)
    return result


def _serialize_result(result: LLMResult) -> dict[str, Any]:
    usage = result.token_usage
    return {
        "data": result.data,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "token_usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": usage.cached_tokens,
        },
        "attempts": result.attempts,
    }


def _deserialize_result(value: Any, row_number: int) -> LLMResult:
    if not isinstance(value, dict) or set(value) != {
        "data",
        "model",
        "latency_ms",
        "token_usage",
        "attempts",
    }:
        raise DemoCacheError(f"cache row {row_number} has an invalid result object")
    usage_value = value["token_usage"]
    if not isinstance(usage_value, dict) or set(usage_value) != {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
    }:
        raise DemoCacheError(f"cache row {row_number} has invalid token_usage")
    usage = TokenUsage(
        prompt_tokens=_plain_int(
            usage_value["prompt_tokens"], "prompt_tokens"
        ),
        completion_tokens=_plain_int(
            usage_value["completion_tokens"], "completion_tokens"
        ),
        total_tokens=_plain_int(usage_value["total_tokens"], "total_tokens"),
        cached_tokens=_plain_int(
            usage_value["cached_tokens"], "cached_tokens"
        ),
    )
    result = LLMResult(
        data=value["data"],
        model=value["model"],
        latency_ms=value["latency_ms"],
        token_usage=usage,
        attempts=value["attempts"],
    )
    return _validate_result(result)


class DemoLLMCache:
    """Record or replay every LLM call for one trace in invocation order."""

    def __init__(
        self,
        trace_id: str,
        cache_dir: Path,
        mode: str,
        live_call: Callable[..., LLMResult],
    ) -> None:
        self._validate_trace_id(trace_id)
        if mode not in {"live", "cached"}:
            raise DemoCacheError("mode must be exactly live or cached")
        if not callable(live_call):
            raise DemoCacheError("live_call must be callable")
        root = Path(cache_dir).resolve()
        path = (root / f"{trace_id}.jsonl").resolve()
        if path.parent != root:
            raise DemoCacheError("trace_id must stay inside the cache directory")

        self._trace_id = trace_id
        self._mode = mode
        self._live_call = live_call
        self._path = path
        self._calls = 0
        self._cache_hits = 0
        self._rows: list[dict[str, Any]] = []
        self._lock = RLock()
        self._pending_rows: dict[int, dict[str, Any]] = {}
        self._next_write_step = 1
        self._parallel_active = False
        self._parallel_start = 0
        self._parallel_claimed: set[int] = set()

        if mode == "live":
            if path.exists():
                raise DemoCacheError(f"cache already exists: {path}")
            root.mkdir(parents=True, exist_ok=True)
        else:
            if not path.exists():
                raise DemoCacheError(f"cache does not exist: {path}")
            self._rows = self._load_rows(path)
            if not self._rows:
                raise DemoCacheError(f"cache is empty: {path}")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    @property
    def cache_hits(self) -> int:
        with self._lock:
            return self._cache_hits

    @contextmanager
    def parallel_scope(self) -> Any:
        """Allow one bounded group to execute concurrently and replay unordered.

        Requests inside the scope must still consume one contiguous cache segment.
        This keeps everything outside the explicitly parallel group strictly ordered.
        """

        with self._lock:
            if self._parallel_active:
                raise DemoCacheError("parallel cache scopes cannot be nested")
            self._parallel_active = True
            self._parallel_start = self._calls
            self._parallel_claimed = set()
        try:
            yield self
        except BaseException:
            with self._lock:
                self._reset_parallel_scope()
            raise
        else:
            with self._lock:
                if self._mode == "cached":
                    count = len(self._parallel_claimed)
                    expected = set(
                        range(self._parallel_start, self._parallel_start + count)
                    )
                    if self._parallel_claimed != expected:
                        self._reset_parallel_scope()
                        raise DemoCacheError(
                            "parallel cache requests must consume one contiguous segment"
                        )
                    self._calls += count
                    self._cache_hits += count
                self._reset_parallel_scope()

    def _reset_parallel_scope(self) -> None:
        self._parallel_active = False
        self._parallel_start = 0
        self._parallel_claimed = set()

    def __call__(self, **request: Any) -> LLMResult:
        fingerprint = request_fingerprint(request)
        if self._mode == "live":
            with self._lock:
                self._calls += 1
                step = self._calls
            result = _validate_result(self._live_call(**request))
            self._append(step, request, fingerprint, result)
            return result

        with self._lock:
            if self._parallel_active:
                for row_index in range(self._parallel_start, len(self._rows)):
                    if row_index in self._parallel_claimed:
                        continue
                    row = self._rows[row_index]
                    if row["request"]["sha256"] == fingerprint:
                        self._parallel_claimed.add(row_index)
                        return _deserialize_result(row["result"], row_index + 1)
                raise DemoCacheError(
                    "request fingerprint mismatch in parallel cache segment "
                    f"for trace {self._trace_id}"
                )

            self._calls += 1
            step = self._calls
            if step > len(self._rows):
                raise DemoCacheError(
                    f"cache has no record for trace {self._trace_id} step {step}"
                )
            row = self._rows[step - 1]
            cached_fingerprint = row["request"]["sha256"]
            if cached_fingerprint != fingerprint:
                raise DemoCacheError(
                    f"request fingerprint mismatch for trace {self._trace_id} step {step}"
                )
            self._cache_hits += 1
            return _deserialize_result(row["result"], step)

    def assert_exhausted(self) -> None:
        """Fail if cached mode did not consume every expected LLM response."""

        if self._mode != "cached":
            return
        with self._lock:
            remaining = len(self._rows) - self._calls
        if remaining:
            raise DemoCacheError(
                f"cache for trace {self._trace_id} has {remaining} unconsumed records"
            )

    def _append(
        self,
        step: int,
        request: Mapping[str, Any],
        fingerprint: str,
        result: LLMResult,
    ) -> None:
        model = request.get("model")
        if not isinstance(model, str) or not model.strip():
            raise DemoCacheError("LLM request.model must be a non-empty string")
        row = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": self._trace_id,
            "step": step,
            "request": {"model": model, "sha256": fingerprint},
            "result": _serialize_result(result),
        }
        with self._lock:
            self._pending_rows[step] = row
            ready: list[dict[str, Any]] = []
            while self._next_write_step in self._pending_rows:
                ready.append(self._pending_rows.pop(self._next_write_step))
                self._next_write_step += 1
            if ready:
                with self._path.open("a", encoding="utf-8", newline="") as cache_file:
                    for ready_row in ready:
                        cache_file.write(
                            json.dumps(ready_row, ensure_ascii=False) + "\n"
                        )

    def _load_rows(self, path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise DemoCacheError(f"cannot read cache {path}: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DemoCacheError(
                    f"cache row {line_number} is invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "trace_id",
                "step",
                "request",
                "result",
            }:
                raise DemoCacheError(
                    f"cache row {line_number} has invalid top-level fields"
                )
            if value["schema_version"] != SCHEMA_VERSION:
                raise DemoCacheError(
                    f"cache row {line_number} has unsupported schema_version"
                )
            if value["trace_id"] != self._trace_id:
                raise DemoCacheError(
                    f"cache row {line_number} trace_id does not match"
                )
            request_value = value["request"]
            if not isinstance(request_value, dict) or set(request_value) != {
                "model",
                "sha256",
            }:
                raise DemoCacheError(
                    f"cache row {line_number} has an invalid request object"
                )
            if (
                not isinstance(request_value["model"], str)
                or not request_value["model"].strip()
                or not isinstance(request_value["sha256"], str)
                or _SHA256_RE.fullmatch(request_value["sha256"]) is None
            ):
                raise DemoCacheError(
                    f"cache row {line_number} has invalid request metadata"
                )
            _deserialize_result(value["result"], line_number)
            rows.append(value)
        expected_steps = list(range(1, len(rows) + 1))
        actual_steps = [row["step"] for row in rows]
        if actual_steps != expected_steps:
            raise DemoCacheError("cache steps must be contiguous from 1")
        return rows

    @staticmethod
    def _validate_trace_id(trace_id: object) -> None:
        if not isinstance(trace_id, str) or _TRACE_ID_RE.fullmatch(trace_id) is None:
            raise DemoCacheError("trace_id must be a safe ASCII identifier")
        if trace_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise DemoCacheError("trace_id uses a reserved Windows device name")
