"""Unified non-streaming DashScope LLM boundary."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from time import perf_counter
import time
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from openai import OpenAI


DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 3


class LLMCallError(RuntimeError):
    """Raised when the bounded LLM call cannot return valid JSON."""


@dataclass(frozen=True, slots=True)
class WarmupResult:
    success: bool
    latency_ms: int
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int = 0

    @classmethod
    def from_response(cls, usage: Any) -> "TokenUsage":
        if usage is None:
            return cls(0, 0, 0)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached_value = getattr(prompt_details, "cached_tokens", 0)
        cached_tokens = 0 if cached_value is None else int(cached_value)
        result = cls(
            prompt_tokens=int(usage.prompt_tokens),
            completion_tokens=int(usage.completion_tokens),
            total_tokens=int(usage.total_tokens),
            cached_tokens=cached_tokens,
        )
        if (
            result.prompt_tokens < 0
            or result.completion_tokens < 0
            or result.cached_tokens < 0
        ):
            raise ValueError("provider token usage cannot be negative")
        if result.total_tokens != result.prompt_tokens + result.completion_tokens:
            raise ValueError("provider total_tokens is inconsistent")
        if result.cached_tokens > result.prompt_tokens:
            raise ValueError("provider cached_tokens exceeds prompt_tokens")
        return result

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class LLMResult:
    data: dict[str, Any]
    model: str
    latency_ms: int
    token_usage: TokenUsage
    attempts: int


def _required_api_key() -> str:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise LLMCallError("DASHSCOPE_API_KEY environment variable is required")
    return api_key


@lru_cache(maxsize=1)
def _cached_client(api_key: str) -> OpenAI:
    """Reuse the SDK HTTP connection pool for repeated production calls."""

    return OpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)


@lru_cache(maxsize=None)
def _cached_warmup(api_key: str) -> WarmupResult:
    started = perf_counter()
    try:
        _cached_client(api_key).models.list()
    except Exception as exc:
        return WarmupResult(
            success=False,
            latency_ms=round(max(0.0, perf_counter() - started) * 1000),
            error_type=type(exc).__name__,
        )
    return WarmupResult(
        success=True,
        latency_ms=round(max(0.0, perf_counter() - started) * 1000),
    )


def warm_default_client() -> WarmupResult:
    """Best-effort one-time warmup for the process-local shared client."""

    try:
        api_key = _required_api_key()
    except Exception as exc:
        return WarmupResult(False, 0, type(exc).__name__)
    return _cached_warmup(api_key)


def _default_client() -> OpenAI:
    return _cached_client(_required_api_key())


def call_llm(
    *,
    model: str,
    system: str,
    user: str,
    json_schema: Mapping[str, Any] | None = None,
    temperature: float = 0.1,
    client: Any | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> LLMResult:
    """Call a chat model and return locally validated JSON with usage metadata."""

    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("max_attempts must be a positive integer")
    if max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    resolved_client = client if client is not None else _default_client()
    model_elapsed_seconds = 0.0
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            request_started = perf_counter()
            try:
                response = resolved_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=temperature,
                    stream=False,
                    response_format={"type": "json_object"},
                    extra_body={"enable_thinking": False},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            finally:
                model_elapsed_seconds += max(
                    0.0, perf_counter() - request_started
                )
            content = response.choices[0].message.content
            if not isinstance(content, str):
                raise ValueError("LLM response content must be a JSON string")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("LLM response JSON must be an object")
            if json_schema is not None:
                Draft202012Validator(dict(json_schema)).validate(data)
            usage = TokenUsage.from_response(response.usage)
            return LLMResult(
                data=data,
                model=str(response.model or model),
                latency_ms=round(model_elapsed_seconds * 1000),
                token_usage=usage,
                attempts=attempt,
            )
        except Exception as exc:  # Provider, parse, and schema failures share a bound.
            last_error = exc
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
    suffix = "attempt" if max_attempts == 1 else "attempts"
    raise LLMCallError(
        f"LLM call failed after {max_attempts} {suffix}"
    ) from last_error
