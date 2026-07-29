"""Generate evidence-bounded defenses for reviewable R-02/R-03 rejections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from time import perf_counter
from typing import Any, Callable

from jsonschema import Draft202012Validator

from orchestrator.llm import LLMResult, call_llm


DEFENSIBLE_RULES = frozenset({"R-02", "R-03"})
GENERATOR_MODEL = "qwen3-235b-a22b"
REBUTTAL_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["concede"],
    "properties": {
        "concede": {"type": "boolean"},
        "rebuttal": {"type": "string"},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
    },
    "allOf": [
        {
            "if": {
                "properties": {"concede": {"const": False}},
                "required": ["concede"],
            },
            "then": {
                "required": ["rebuttal", "evidence_refs"],
            },
        }
    ],
    "additionalProperties": False,
}
_OUTPUT_VALIDATOR = Draft202012Validator(REBUTTAL_OUTPUT_SCHEMA)


@dataclass(frozen=True, slots=True)
class _RejectionContext:
    product_msg_id: str
    verdict_msg_id: str
    producer_agent: str
    hits: tuple[Mapping[str, Any], ...]
    rule_ids: frozenset[str]


def _items(message: Mapping[str, Any], field: str) -> tuple[Mapping[str, Any], ...]:
    value = message.get(field)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _rule_hits(verdict_message: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    verdict = verdict_message.get("verdict")
    if not isinstance(verdict, Mapping):
        return ()
    value = verdict.get("rule_hits")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _required_string(message: Mapping[str, Any], field: str) -> str:
    value = message.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _payload(message: Mapping[str, Any]) -> Mapping[str, Any]:
    value = message.get("payload")
    return value if isinstance(value, Mapping) else {}


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _payload(message).get("content")
    return value if isinstance(value, Mapping) else {}


def _system_prompt(hits: tuple[Mapping[str, Any], ...]) -> str:
    reasons = "；".join(str(hit.get("reason", "")) for hit in hits)
    return (
        f"你的产出被审核驳回，理由：{reasons}。若你认为驳回有误，提交辩护："
        "引用具体证据（切片句锚点或SQL结果）反驳；evidence_refs必须是字符串数组，"
        "只能填写product.evidence[*].ref的原值，例如"
        '["KB-003","sql-live-debate"]，禁止输出对象；若驳回正确，输出'
        '{"concede":true}直接重生成。只输出JSON'
        '{"concede":bool,"rebuttal":"...","evidence_refs":["KB-003"]}'
    )


def _validate_rejection_context(
    product: Mapping[str, Any],
    verdict: Mapping[str, Any],
    *,
    trace_id: str,
) -> _RejectionContext:
    """Validate the full product/verdict association before any rebuttal path."""

    if not isinstance(product, Mapping) or not isinstance(verdict, Mapping):
        raise ValueError("product and verdict must be mappings")
    product_msg_id = _required_string(product, "msg_id")
    verdict_msg_id = _required_string(verdict, "msg_id")
    producer_agent = _required_string(product, "agent")
    product_trace_id = _required_string(product, "trace_id")
    verdict_trace_id = _required_string(verdict, "trace_id")
    product_type = _payload(product).get("type")
    verdict_content = _content(verdict)
    verdict_data = verdict.get("verdict")
    if (
        product_trace_id != trace_id
        or verdict_trace_id != trace_id
        or product.get("role") not in {"produce", "probe"}
        or not isinstance(product_type, str)
        or verdict.get("agent") != "review"
        or verdict.get("role") != "verdict"
        or _payload(verdict).get("type") != "review_verdict"
        or verdict_content.get("reviewed_msg_id") != product_msg_id
        or verdict_content.get("reviewed_payload_type") != product_type
        or not isinstance(verdict_data, Mapping)
        or verdict_data.get("decision") != "reject"
    ):
        raise ValueError("verdict association is invalid")
    hits = _rule_hits(verdict)
    rule_ids = frozenset(
        str(hit.get("rule_id"))
        for hit in hits
        if hit.get("rule_id") is not None
    )
    return _RejectionContext(
        product_msg_id=product_msg_id,
        verdict_msg_id=verdict_msg_id,
        producer_agent=producer_agent,
        hits=hits,
        rule_ids=rule_ids,
    )


class RebuttalGenerator:
    """Call 235B only when every original hit is an approved soft rule."""

    def __init__(
        self,
        trace_id: str,
        llm_call: Callable[..., Any] = call_llm,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        self._trace_id = trace_id
        self._llm_call = llm_call
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def generate(
        self, product: Mapping[str, Any], verdict: Mapping[str, Any]
    ) -> dict[str, Any]:
        context = _validate_rejection_context(
            product,
            verdict,
            trace_id=self._trace_id,
        )
        defensible = bool(context.hits) and context.rule_ids.issubset(
            DEFENSIBLE_RULES
        )
        if not defensible:
            return self._draft(
                product=product,
                product_msg_id=context.product_msg_id,
                verdict_msg_id=context.verdict_msg_id,
                producer_agent=context.producer_agent,
                concede=True,
                rebuttal="",
                evidence_refs=(),
                evidence=(),
            )

        started = perf_counter()
        result = self._llm_call(
            model=GENERATOR_MODEL,
            system=_system_prompt(context.hits),
            user=json.dumps(
                {"product": dict(product), "verdict": dict(verdict)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            json_schema=REBUTTAL_OUTPUT_SCHEMA,
            temperature=0.7,
        )
        if not isinstance(result, LLMResult):
            raise ValueError("rebuttal llm_call must return LLMResult")
        _OUTPUT_VALIDATOR.validate(result.data)
        concede = bool(result.data["concede"])
        submitted_refs = [] if concede else result.data["evidence_refs"]
        available = {
            str(item["ref"]): dict(item)
            for item in _items(product, "evidence")
            if isinstance(item.get("ref"), str)
        }
        approved_refs: list[str] = []
        for ref in submitted_refs:
            if ref in available and ref not in approved_refs:
                approved_refs.append(ref)
        evidence = tuple(available[ref] for ref in approved_refs)
        rebuttal_text = "" if concede else str(result.data["rebuttal"]).strip()
        if not concede and (not rebuttal_text or not approved_refs):
            raise ValueError(
                "non-conceding rebuttal must cite bounded product evidence"
            )
        elapsed_ms = round(max(0.0, perf_counter() - started) * 1000)
        return self._draft(
            product=product,
            product_msg_id=context.product_msg_id,
            verdict_msg_id=context.verdict_msg_id,
            producer_agent=context.producer_agent,
            concede=concede,
            rebuttal=rebuttal_text,
            evidence_refs=tuple(approved_refs),
            evidence=evidence,
            result=result,
            latency_ms=max(elapsed_ms, result.latency_ms),
        )

    def deterministic_concede(
        self,
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Concede without a model call after the same association validation."""

        context = _validate_rejection_context(
            product,
            verdict,
            trace_id=self._trace_id,
        )
        if not context.hits:
            raise ValueError("reject verdict must contain rule_hits")
        return self._draft(
            product=product,
            product_msg_id=context.product_msg_id,
            verdict_msg_id=context.verdict_msg_id,
            producer_agent=context.producer_agent,
            concede=True,
            rebuttal="",
            evidence_refs=(),
            evidence=(),
        )

    def _draft(
        self,
        *,
        product: Mapping[str, Any],
        product_msg_id: str,
        verdict_msg_id: str,
        producer_agent: str,
        concede: bool,
        rebuttal: str,
        evidence_refs: tuple[str, ...],
        evidence: tuple[Mapping[str, Any], ...],
        result: LLMResult | None = None,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        del product
        content: dict[str, Any] = {
            "event": "rebuttal_ready",
            "product_msg_id": product_msg_id,
            "verdict_msg_id": verdict_msg_id,
            "concede": concede,
            "rebuttal": rebuttal,
            "evidence_refs": list(evidence_refs),
        }
        draft: dict[str, Any] = {
            "trace_id": self._trace_id,
            "agent": producer_agent,
            "role": "rebuttal",
            "payload": {"type": "rebuttal_case", "content": content},
            "evidence": [dict(item) for item in evidence],
            "claims": [],
            "retry": {"in_reply_to": product_msg_id},
            "timestamp": self._clock().isoformat(),
        }
        if result is not None:
            content["llm_latency_ms"] = result.latency_ms
            draft.update(
                {
                    "model": GENERATOR_MODEL,
                    "latency_ms": latency_ms if latency_ms is not None else result.latency_ms,
                    "token_usage": result.token_usage.as_dict(),
                }
            )
        return draft
