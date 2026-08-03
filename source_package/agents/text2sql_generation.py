"""Text2SQL generation with optional task-template family authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter
from typing import Any

from agents.domain_config import DomainConfig, active_domain_config, load_domain_config
from agents.prompts.build_verification_prompt import (
    PromptCatalog,
    build_prompt_catalog,
)
from agents.query_authority import query_authority_from_mapping
from orchestrator.llm import LLMCallError, LLMResult, TokenUsage, call_llm


ROUTER_MODEL = "qwen3-32b"
GENERATOR_MODEL = "qwen3-235b-a22b"
ROUTER_TEMPERATURE = 0.0
GENERATOR_TEMPERATURE = 0.1
ROUTER_MAX_ATTEMPTS = 1
GENERATOR_MAX_ATTEMPTS = 3


@lru_cache(maxsize=8)
def _cached_prompt_catalog(package_path: str, package_sha256: str) -> PromptCatalog:
    domain = load_domain_config(package_path)
    if domain.package_sha256 != package_sha256:
        raise ValueError("domain package changed while prompt catalog was being loaded")
    return build_prompt_catalog(domain_config=domain)


def default_prompt_catalog(domain_config: DomainConfig | None = None) -> PromptCatalog:
    domain = domain_config or active_domain_config()
    return _cached_prompt_catalog(str(domain.package_path), domain.package_sha256)


@dataclass(frozen=True, slots=True)
class Text2SQLGenerationResult:
    data: dict[str, Any]
    model: str
    latency_ms: int
    token_usage: TokenUsage
    routing_model: str
    routing_predicted_family: str | None
    routing_final_family: str
    routing_fallback: bool
    routing_family_mismatch: bool
    routing_fallback_reason: str
    routing_prompt_profile: str
    routing_few_shot_ids: tuple[str, ...]
    routing_latency_ms: int
    generation_latency_ms: int
    routing_token_usage: TokenUsage
    generation_token_usage: TokenUsage
    routing_model_disagreement: bool = False

    def routing_content(self) -> dict[str, Any]:
        return {
            "raw_llm_family": str(self.data["family"]),
            "routing_predicted_family": self.routing_predicted_family,
            "routing_final_family": self.routing_final_family,
            "routing_fallback": self.routing_fallback,
            "routing_family_mismatch": self.routing_family_mismatch,
            "routing_model_disagreement": self.routing_model_disagreement,
            "routing_fallback_reason": self.routing_fallback_reason,
            "routing_prompt_profile": self.routing_prompt_profile,
            "routing_few_shot_ids": list(self.routing_few_shot_ids),
            "routing_model": self.routing_model,
            "generation_model": self.model,
            "routing_llm_latency_ms": self.routing_latency_ms,
            "generation_llm_latency_ms": self.generation_latency_ms,
            "routing_prompt_tokens": self.routing_token_usage.prompt_tokens,
            "routing_completion_tokens": (
                self.routing_token_usage.completion_tokens
            ),
            "routing_total_tokens": self.routing_token_usage.total_tokens,
            "routing_cached_tokens": self.routing_token_usage.cached_tokens,
            "generation_prompt_tokens": (
                self.generation_token_usage.prompt_tokens
            ),
            "generation_completion_tokens": (
                self.generation_token_usage.completion_tokens
            ),
            "generation_total_tokens": self.generation_token_usage.total_tokens,
            "generation_cached_tokens": self.generation_token_usage.cached_tokens,
        }


class Text2SQLGenerationCoordinator:
    """Route examples, while an attached task template remains authoritative."""

    def __init__(
        self,
        *,
        llm_call: Callable[..., LLMResult] = call_llm,
        prompt_catalog: PromptCatalog | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._llm_call = llm_call
        self._prompt_catalog = prompt_catalog or default_prompt_catalog()
        self._clock = clock

    def generate(
        self,
        question: str,
        *,
        query_authority: Mapping[str, Any] | None = None,
    ) -> Text2SQLGenerationResult:
        authority = (
            query_authority_from_mapping(query_authority)
            if query_authority is not None
            else None
        )
        generation_question = (
            authority.standard_stem if authority is not None else question
        )
        route_started = self._clock()
        try:
            routing = self._llm_call(
                model=ROUTER_MODEL,
                system=self._prompt_catalog.router_prompt,
                user=generation_question,
                json_schema=self._prompt_catalog.router_output_schema,
                temperature=ROUTER_TEMPERATURE,
                max_attempts=ROUTER_MAX_ATTEMPTS,
            )
        except LLMCallError:
            routing_model = ROUTER_MODEL
            predicted_family = None
            routing_fallback = True
            fallback_reason = "router_error"
            prompt_profile = (
                authority.family
                if authority is not None
                else self._prompt_catalog.full_profile
            )
            routing_latency_ms = round(
                max(0.0, self._clock() - route_started) * 1000
            )
            routing_token_usage = TokenUsage(0, 0, 0)
        else:
            routing_model = routing.model
            predicted_family = str(routing.data["family"])
            routing_fallback = False
            fallback_reason = "none"
            prompt_profile = (
                authority.family
                if authority is not None
                else (
                    self._prompt_catalog.out_of_scope_profile
                    if predicted_family == "OUT_OF_SCOPE"
                    else predicted_family
                )
            )
            routing_latency_ms = routing.latency_ms
            routing_token_usage = routing.token_usage

        few_shot_ids = self._prompt_catalog.ids_for(prompt_profile)
        generation = self._llm_call(
            model=GENERATOR_MODEL,
            system=self._prompt_catalog.render_generation(prompt_profile),
            user=generation_question,
            json_schema=self._prompt_catalog.text2sql_output_schema,
            temperature=GENERATOR_TEMPERATURE,
            max_attempts=GENERATOR_MAX_ATTEMPTS,
        )
        raw_family = str(generation.data["family"])
        final_family = authority.family if authority is not None else raw_family
        model_disagreement = (
            predicted_family is not None and predicted_family != raw_family
        )
        # A router/generator disagreement is diagnostic evidence, not an
        # effective routing error: without template authority the validated
        # generator result is the final route.  Authority disagreement remains
        # a hard mismatch and is still rejected by downstream contracts.
        family_mismatch = raw_family != final_family
        return Text2SQLGenerationResult(
            data=generation.data,
            model=generation.model,
            latency_ms=routing_latency_ms + generation.latency_ms,
            token_usage=routing_token_usage + generation.token_usage,
            routing_model=routing_model,
            routing_predicted_family=predicted_family,
            routing_final_family=final_family,
            routing_fallback=routing_fallback,
            routing_family_mismatch=family_mismatch,
            routing_fallback_reason=fallback_reason,
            routing_prompt_profile=prompt_profile,
            routing_few_shot_ids=few_shot_ids,
            routing_latency_ms=routing_latency_ms,
            generation_latency_ms=generation.latency_ms,
            routing_token_usage=routing_token_usage,
            generation_token_usage=generation.token_usage,
            routing_model_disagreement=model_disagreement,
        )
