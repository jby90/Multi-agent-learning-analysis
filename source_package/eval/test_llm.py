from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator import llm
from orchestrator.llm import LLMCallError, TokenUsage, call_llm


MODEL = "qwen3-235b-a22b"
VALID_JSON = '{"sql": null, "family": "OUT_OF_SCOPE", "explanation": "族外问题"}'
OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["sql", "family", "explanation"],
    "properties": {
        "sql": {"type": ["string", "null"]},
        "family": {"enum": ["Q1", "OUT_OF_SCOPE"]},
        "explanation": {"type": "string"},
    },
    "additionalProperties": False,
}


def fake_response(content: str, *, model: str = MODEL) -> Any:
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        ),
    )


@dataclass
class FakeCompletions:
    outcomes: list[Any]
    requests: list[dict[str, Any]]

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=FakeCompletions(outcomes, self.requests)
        )


class FakeModels:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.list_calls = 0

    def list(self) -> SimpleNamespace:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=[])


class FakeWarmClient:
    def __init__(self, error: BaseException | None = None) -> None:
        self.models = FakeModels(error)


def test_call_llm_hard_codes_dashscope_non_streaming_request() -> None:
    client = FakeClient([fake_response(VALID_JSON)])

    result = call_llm(
        model=MODEL,
        system="system prompt",
        user="天气如何",
        json_schema=OUTPUT_SCHEMA,
        client=client,
    )

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["model"] == MODEL
    assert request["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "天气如何"},
    ]
    assert request["temperature"] == 0.1
    assert request["stream"] is False
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"enable_thinking": False}
    assert request["timeout"] == 60.0
    assert result.data["family"] == "OUT_OF_SCOPE"
    assert result.model == MODEL
    assert result.token_usage == TokenUsage(10, 5, 15)
    assert result.attempts == 1
    assert result.latency_ms >= 0


def test_cached_tokens_do_not_expand_protocol_dict() -> None:
    response = fake_response(VALID_JSON)
    response.usage.prompt_tokens_details = SimpleNamespace(cached_tokens=8)

    result = call_llm(
        model=MODEL,
        system="s",
        user="u",
        client=FakeClient([response]),
    )

    assert result.token_usage.cached_tokens == 8
    assert result.token_usage.as_dict() == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_token_usage_adds_split_calls_without_expanding_protocol() -> None:
    combined = TokenUsage(10, 2, 12, cached_tokens=3) + TokenUsage(
        20, 4, 24, cached_tokens=5
    )

    assert combined == TokenUsage(30, 6, 36, cached_tokens=8)
    assert set(combined.as_dict()) == {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }


def test_call_llm_can_limit_router_to_one_attempt() -> None:
    client = FakeClient([TimeoutError(), fake_response(VALID_JSON)])

    with pytest.raises(LLMCallError, match="after 1 attempt"):
        call_llm(
            model=MODEL,
            system="s",
            user="u",
            client=client,
            max_attempts=1,
        )

    assert len(client.requests) == 1


def test_call_llm_rejects_non_positive_attempt_limit() -> None:
    client = FakeClient([fake_response(VALID_JSON)])

    with pytest.raises(ValueError, match="max_attempts"):
        call_llm(
            model=MODEL,
            system="s",
            user="u",
            client=client,
            max_attempts=0,
        )

    assert client.requests == []


def test_call_llm_retries_invalid_json_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient([fake_response("not-json"), fake_response(VALID_JSON)])
    sleeps: list[int] = []
    monkeypatch.setattr("orchestrator.llm.time.sleep", sleeps.append)

    result = call_llm(model=MODEL, system="s", user="u", client=client)

    assert result.attempts == 2
    assert sleeps == [1]


def test_call_llm_retries_json_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = '{"sql": null, "family": "UNKNOWN", "explanation": "x"}'
    client = FakeClient([fake_response(invalid), fake_response(VALID_JSON)])
    monkeypatch.setattr("orchestrator.llm.time.sleep", lambda _: None)

    result = call_llm(
        model=MODEL,
        system="s",
        user="u",
        json_schema=OUTPUT_SCHEMA,
        client=client,
    )

    assert result.attempts == 2


def test_llm_latency_accumulates_only_sdk_request_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient([fake_response("not-json"), fake_response(VALID_JSON)])
    clock_values = iter((0.0, 0.2, 1.2, 1.5))
    sleeps: list[int] = []
    monkeypatch.setattr(llm, "perf_counter", lambda: next(clock_values))
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)

    result = call_llm(model=MODEL, system="s", user="u", client=client)

    assert result.attempts == 2
    assert result.latency_ms == 500
    assert sleeps == [1]


def test_call_llm_stops_after_initial_plus_two_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient([TimeoutError(), TimeoutError(), TimeoutError()])
    sleeps: list[int] = []
    monkeypatch.setattr("orchestrator.llm.time.sleep", sleeps.append)

    with pytest.raises(LLMCallError, match="after 3 attempts"):
        call_llm(model=MODEL, system="s", user="u", client=client)

    assert len(client.requests) == 3
    assert sleeps == [1, 2]


def test_call_llm_rejects_inconsistent_provider_token_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = fake_response(VALID_JSON)
    response.usage.total_tokens = 99
    client = FakeClient([response, response, response])
    monkeypatch.setattr("orchestrator.llm.time.sleep", lambda _: None)

    with pytest.raises(LLMCallError, match="after 3 attempts"):
        call_llm(model=MODEL, system="s", user="u", client=client)


def test_call_llm_requires_environment_key_only_for_real_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(LLMCallError, match="DASHSCOPE_API_KEY"):
        call_llm(model=MODEL, system="s", user="u")


def test_default_client_reuses_one_connection_pool_per_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient([fake_response(VALID_JSON), fake_response(VALID_JSON)])
    constructor_calls: list[dict[str, str]] = []

    def fake_openai(**kwargs: str) -> FakeClient:
        constructor_calls.append(kwargs)
        return client

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "OpenAI", fake_openai)
    llm._cached_client.cache_clear()
    try:
        call_llm(model=MODEL, system="s", user="first")
        call_llm(model=MODEL, system="s", user="second")
    finally:
        llm._cached_client.cache_clear()

    assert constructor_calls == [
        {"api_key": "test-key", "base_url": llm.DASHSCOPE_BASE_URL}
    ]
    assert len(client.requests) == 2


def test_warm_default_client_lists_models_once_per_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeWarmClient()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_cached_client", lambda key: client)
    llm._cached_warmup.cache_clear()
    try:
        first = llm.warm_default_client()
        second = llm.warm_default_client()
    finally:
        llm._cached_warmup.cache_clear()

    assert first.success is True
    assert second == first
    assert client.models.list_calls == 1


def test_warmup_failure_is_returned_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeWarmClient(error=TimeoutError("network"))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_cached_client", lambda key: client)
    llm._cached_warmup.cache_clear()
    try:
        result = llm.warm_default_client()
    finally:
        llm._cached_warmup.cache_clear()

    assert result.success is False
    assert result.error_type == "TimeoutError"


def test_missing_key_warmup_is_returned_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    result = llm.warm_default_client()

    assert result.success is False
    assert result.error_type == "LLMCallError"
