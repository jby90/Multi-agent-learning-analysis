from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from orchestrator.demo_cache import DemoCacheError, DemoLLMCache
from orchestrator.llm import LLMResult, TokenUsage


REQUEST = {
    "model": "qwen3-32b",
    "system": "只输出JSON",
    "user": "审核这条结论",
    "json_schema": {
        "type": "object",
        "required": ["decision"],
        "properties": {"decision": {"enum": ["approve", "reject"]}},
    },
    "temperature": 0.1,
}


def fake_result(*, decision: str = "approve") -> LLMResult:
    return LLMResult(
        data={"decision": decision},
        model="qwen3-32b",
        token_usage=TokenUsage(11, 3, 14, cached_tokens=2),
        attempts=1,
        latency_ms=25,
    )


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_live_records_one_versioned_jsonl_row(tmp_path: Path) -> None:
    live_calls: list[dict[str, Any]] = []

    def live_call(**request: Any) -> LLMResult:
        live_calls.append(request)
        return fake_result()

    cache = DemoLLMCache("demo-cache", tmp_path, "live", live_call)

    assert cache(**REQUEST) == fake_result()

    assert live_calls == [REQUEST]
    assert cache.calls == 1
    assert cache.cache_hits == 0
    rows = read_rows(tmp_path / "demo-cache.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert (row["trace_id"], row["step"], row["schema_version"]) == (
        "demo-cache",
        1,
        1,
    )
    assert row["request"]["model"] == "qwen3-32b"
    assert len(row["request"]["sha256"]) == 64
    assert row["result"] == {
        "data": {"decision": "approve"},
        "model": "qwen3-32b",
        "latency_ms": 25,
        "token_usage": {
            "prompt_tokens": 11,
            "completion_tokens": 3,
            "total_tokens": 14,
            "cached_tokens": 2,
        },
        "attempts": 1,
    }
    cache.assert_exhausted()


def test_live_refuses_to_append_to_an_existing_cache(tmp_path: Path) -> None:
    path = tmp_path / "demo-cache.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(DemoCacheError, match="already exists"):
        DemoLLMCache("demo-cache", tmp_path, "live", lambda **_: fake_result())


def test_cached_rebuilds_result_without_live_call(tmp_path: Path) -> None:
    DemoLLMCache("demo-cache", tmp_path, "live", lambda **_: fake_result())(
        **REQUEST
    )
    network_calls = 0

    def forbidden_network(**_: Any) -> LLMResult:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("network called")

    cached = DemoLLMCache("demo-cache", tmp_path, "cached", forbidden_network)

    assert cached(**REQUEST) == fake_result()
    assert network_calls == 0
    assert cached.calls == 1
    assert cached.cache_hits == 1
    cached.assert_exhausted()


def test_cached_request_fingerprint_mismatch_fails_closed(tmp_path: Path) -> None:
    DemoLLMCache("demo-cache", tmp_path, "live", lambda **_: fake_result())(
        **REQUEST
    )
    cached = DemoLLMCache(
        "demo-cache", tmp_path, "cached", lambda **_: fake_result()
    )

    with pytest.raises(DemoCacheError, match="fingerprint mismatch.*step 1"):
        cached(**{**REQUEST, "user": "另一条请求"})


def test_cached_fingerprint_ignores_only_volatile_audit_fields(
    tmp_path: Path,
) -> None:
    live_request = {
        **REQUEST,
        "user": json.dumps(
            {
                "product": {
                    "text": "计划量与实际量应分开。",
                    "timestamp": "2026-07-16T02:00:00+00:00",
                    "latency_ms": 1200,
                    "content": {
                        "query_elapsed_ms": 8,
                        "generation_llm_latency_ms": 1100,
                    },
                }
            },
            ensure_ascii=False,
        ),
    }
    DemoLLMCache("demo-cache", tmp_path, "live", lambda **_: fake_result())(
        **live_request
    )
    cached_request = {
        **live_request,
        "user": json.dumps(
            {
                "product": {
                    "text": "计划量与实际量应分开。",
                    "timestamp": "2026-07-16T03:00:00+00:00",
                    "latency_ms": 2,
                    "content": {
                        "query_elapsed_ms": 1,
                        "generation_llm_latency_ms": 2,
                    },
                }
            },
            ensure_ascii=False,
        ),
    }
    cached = DemoLLMCache(
        "demo-cache", tmp_path, "cached", lambda **_: fake_result()
    )

    assert cached(**cached_request) == fake_result()
    cached.assert_exhausted()


def test_cached_requires_existing_non_empty_file(tmp_path: Path) -> None:
    with pytest.raises(DemoCacheError, match="does not exist"):
        DemoLLMCache("missing", tmp_path, "cached", lambda **_: fake_result())

    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(DemoCacheError, match="is empty"):
        DemoLLMCache("empty", tmp_path, "cached", lambda **_: fake_result())


@pytest.mark.parametrize("mode", ("", "offline", "LIVE", " cached "))
def test_mode_is_exactly_live_or_cached(tmp_path: Path, mode: str) -> None:
    with pytest.raises(DemoCacheError, match="live or cached"):
        DemoLLMCache("demo-cache", tmp_path, mode, lambda **_: fake_result())


@pytest.mark.parametrize(
    "trace_id",
    ("../escape", "has space", "含中文", "", "CON", "a" * 129),
)
def test_trace_id_must_be_safe(tmp_path: Path, trace_id: str) -> None:
    with pytest.raises(DemoCacheError, match="trace_id"):
        DemoLLMCache(trace_id, tmp_path, "live", lambda **_: fake_result())


def test_cached_rejects_non_contiguous_steps(tmp_path: Path) -> None:
    cache = DemoLLMCache("demo-cache", tmp_path, "live", lambda **_: fake_result())
    cache(**REQUEST)
    cache(**{**REQUEST, "user": "第二条"})
    path = tmp_path / "demo-cache.jsonl"
    rows = read_rows(path)
    rows[1]["step"] = 3
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DemoCacheError, match="steps must be contiguous"):
        DemoLLMCache("demo-cache", tmp_path, "cached", lambda **_: fake_result())


def test_cached_rejects_inconsistent_token_total(tmp_path: Path) -> None:
    DemoLLMCache("demo-cache", tmp_path, "live", lambda **_: fake_result())(
        **REQUEST
    )
    path = tmp_path / "demo-cache.jsonl"
    row = read_rows(path)[0]
    row["result"]["token_usage"]["total_tokens"] = 99
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(DemoCacheError, match="total_tokens"):
        DemoLLMCache("demo-cache", tmp_path, "cached", lambda **_: fake_result())


def test_cached_detects_unconsumed_trailing_records(tmp_path: Path) -> None:
    cache = DemoLLMCache("demo-cache", tmp_path, "live", lambda **_: fake_result())
    cache(**REQUEST)
    cache(**{**REQUEST, "user": "第二条"})
    cached = DemoLLMCache(
        "demo-cache", tmp_path, "cached", lambda **_: fake_result()
    )

    cached(**REQUEST)

    with pytest.raises(DemoCacheError, match="1 unconsumed"):
        cached.assert_exhausted()


def test_live_failure_does_not_write_a_cache_record(tmp_path: Path) -> None:
    def fail(**_: Any) -> LLMResult:
        raise RuntimeError("provider unavailable")

    cache = DemoLLMCache("demo-cache", tmp_path, "live", fail)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        cache(**REQUEST)

    assert not (tmp_path / "demo-cache.jsonl").exists()


def test_parallel_scope_records_ordered_rows_and_replays_unordered(
    tmp_path: Path,
) -> None:
    rendezvous = Barrier(2)

    def live_call(**request: Any) -> LLMResult:
        rendezvous.wait(timeout=1)
        decision = "reject" if request["user"] == "并行难度审核" else "approve"
        return fake_result(decision=decision)

    live = DemoLLMCache("parallel-cache", tmp_path, "live", live_call)
    fact_request = {**REQUEST, "user": "并行事实审核"}
    difficulty_request = {**REQUEST, "user": "并行难度审核"}
    with live.parallel_scope():
        with ThreadPoolExecutor(max_workers=2) as pool:
            fact_future = pool.submit(live, **fact_request)
            difficulty_future = pool.submit(live, **difficulty_request)
            assert fact_future.result().data["decision"] == "approve"
            assert difficulty_future.result().data["decision"] == "reject"

    rows = read_rows(tmp_path / "parallel-cache.jsonl")
    assert [row["step"] for row in rows] == [1, 2]

    cached = DemoLLMCache(
        "parallel-cache",
        tmp_path,
        "cached",
        lambda **_: pytest.fail("parallel replay called the network"),
    )
    with cached.parallel_scope():
        with ThreadPoolExecutor(max_workers=2) as pool:
            difficulty_future = pool.submit(cached, **difficulty_request)
            fact_future = pool.submit(cached, **fact_request)
            assert difficulty_future.result().data["decision"] == "reject"
            assert fact_future.result().data["decision"] == "approve"
    assert cached.calls == 2
    assert cached.cache_hits == 2
    cached.assert_exhausted()


def test_parallel_scope_rejects_non_contiguous_cache_consumption(
    tmp_path: Path,
) -> None:
    live = DemoLLMCache(
        "parallel-cache",
        tmp_path,
        "live",
        lambda **_: fake_result(),
    )
    requests = [{**REQUEST, "user": f"请求-{index}"} for index in range(3)]
    for request in requests:
        live(**request)

    cached = DemoLLMCache(
        "parallel-cache",
        tmp_path,
        "cached",
        lambda **_: pytest.fail("parallel replay called the network"),
    )
    with pytest.raises(DemoCacheError, match="contiguous segment"):
        with cached.parallel_scope():
            cached(**requests[0])
            cached(**requests[2])
