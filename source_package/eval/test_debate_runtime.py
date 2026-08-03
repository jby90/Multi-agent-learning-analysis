from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agents.review_agent import ReviewAgent
from agents.validate_message import validate_message
from agents.verification_agent import _render_claims
from orchestrator import runtime
from orchestrator.agents_stub import (
    build_stubs,
    profile_loaded_draft,
    student_sql_draft,
)
from orchestrator.bus import MessageBus
from orchestrator.engine import OrchestratorEngine
from orchestrator.llm import LLMResult, TokenUsage
from orchestrator.transitions import State


TIMESTAMP = "2026-07-15T13:00:00+08:00"


class SpyLLM:
    def __init__(self, outputs: list[LLMResult]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("unexpected LLM call")
        return self.outputs.pop(0)


def _result(data: dict[str, Any], model: str) -> LLMResult:
    return LLMResult(
        data=data,
        model=model,
        latency_ms=17,
        token_usage=TokenUsage(20, 5, 25),
        attempts=1,
    )


def _rebuttal_module() -> Any:
    try:
        return importlib.import_module("agents.rebuttal_generator")
    except ModuleNotFoundError:
        pytest.fail("agents.rebuttal_generator is not implemented")


def _assert_transition(result: Any, transition_id: str) -> dict[str, Any]:
    assert result.bus_result.accepted, result.bus_result.errors
    assert result.transitioned, result.reason
    assert result.transition is not None
    assert result.transition.transition_id == transition_id
    return result.bus_result.message


def _runtime(
    tmp_path: Path, trace_id: str
) -> tuple[OrchestratorEngine, MessageBus, Any]:
    bus = MessageBus(tmp_path)
    engine = OrchestratorEngine(bus, trace_id, "planner_new")
    return engine, bus, build_stubs(trace_id)


def _drive_to_s2(engine: OrchestratorEngine, stubs: Any) -> None:
    _assert_transition(engine.send(profile_loaded_draft(engine.trace_id)), "T01")
    _assert_transition(engine.send(stubs.diagnosis.profile_assessment()), "T02")


def _drive_to_sql_review(engine: OrchestratorEngine, stubs: Any) -> dict[str, Any]:
    _drive_to_s4(engine, stubs)
    return _assert_transition(engine.send(stubs.verification.sql_result()), "T12")


def _drive_to_s4(engine: OrchestratorEngine, stubs: Any) -> None:
    _drive_to_s2(engine, stubs)
    lecture = _assert_transition(engine.send(stubs.knowledge.lecture()), "T03")
    _assert_transition(
        engine.send(
            stubs.review.verdict("approve", "lecture_note", lecture["msg_id"])
        ),
        "T04",
    )
    task = _assert_transition(engine.send(stubs.task.task("quiz_set")), "T09")
    _assert_transition(
        engine.send(stubs.review.verdict("approve", "quiz_set", task["msg_id"])),
        "T10",
    )
    _assert_transition(engine.send(student_sql_draft(engine.trace_id)), "T11")


def _lecture_draft(trace_id: str) -> dict[str, Any]:
    claim = "完成率等于实际量除以计划量。"
    draft = {
        "trace_id": trace_id,
        "agent": "knowledge",
        "role": "produce",
        "payload": {
            "type": "lecture_note",
            "content": {
                "event": "product_ready",
                "lecture_md": claim,
                "difficulty": "basic",
            },
        },
        "evidence": [
            {
                "kind": "kb_chunk",
                "ref": "KB-003",
                "quote": "**完成率 = 实际量 ÷ 计划量**。",
                "supports_claim": claim,
            }
        ],
        "claims": [{"text": claim, "kind": "fact"}],
        "timestamp": TIMESTAMP,
    }
    return draft


def _manual_reject(
    trace_id: str,
    product: dict[str, Any],
    rule_id: str,
    *,
    reason: str,
    evidence_ref: str,
) -> dict[str, Any]:
    draft = {
        "trace_id": trace_id,
        "agent": "review",
        "role": "verdict",
        "payload": {
            "type": "review_verdict",
            "content": {
                "event": "review_complete",
                "reviewed_payload_type": product["payload"]["type"],
                "reviewed_msg_id": product["msg_id"],
            },
        },
        "evidence": [{"kind": "review_rule", "ref": rule_id, "quote": reason}],
        "claims": [],
        "verdict": {
            "decision": "reject",
            "rule_hits": [
                {
                    "rule_id": rule_id,
                    "reason": reason,
                    "evidence_ref": evidence_ref,
                }
            ],
            "difficulty_action": "none",
        },
        "timestamp": TIMESTAMP,
    }
    return draft


def _trace_messages(bus: MessageBus, trace_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in bus.trace_path(trace_id).read_text(encoding="utf-8").splitlines()
    ]


def test_hard_rule_concede_is_deterministic_and_t07_returns_to_producer(
    tmp_path: Path,
) -> None:
    engine, bus, stubs = _runtime(tmp_path, "trace-hard-concede")
    product = _drive_to_sql_review(engine, stubs)
    original = _assert_transition(
        engine.send(
            _manual_reject(
                engine.trace_id,
                product,
                "R-01",
                reason="问题口径与SQL口径不一致",
                evidence_ref=product["msg_id"],
            )
        ),
        "T05",
    )
    generation_llm = SpyLLM([])
    generator = _rebuttal_module().RebuttalGenerator(
        engine.trace_id, llm_call=generation_llm
    )

    rebuttal_result = engine.send(generator.generate(product, original))

    assert rebuttal_result.bus_result.accepted
    assert not rebuttal_result.transitioned
    rebuttal = rebuttal_result.bus_result.message
    assert rebuttal["payload"]["content"]["concede"] is True
    assert generation_llm.calls == []
    for field in ("model", "token_usage", "latency_ms"):
        assert field not in rebuttal

    review_llm = SpyLLM([])
    re_verdict = ReviewAgent(engine.trace_id, llm_call=review_llm).re_review(
        product, original, rebuttal
    )
    final = engine.send(re_verdict)

    _assert_transition(final, "T07")
    assert final.bus_result.message["verdict"]["decision"] == "reject"
    assert review_llm.calls == []
    assert engine.state is State.S4_VERIFY
    assert engine.state_history == (
        State.S0_INIT,
        State.S1_DIAGNOSIS,
        State.S2_KNOWLEDGE,
        State.S5_REVIEW,
        State.S3_TASK,
        State.S5_REVIEW,
        State.S7_STUDENT,
        State.S4_VERIFY,
        State.S5_REVIEW,
        State.S6_DEBATE,
        State.S4_VERIFY,
    )
    messages = _trace_messages(bus, engine.trace_id)
    assert sum(message.get("role") == "rebuttal" for message in messages) == 1
    assert all(validate_message(message) == [] for message in messages)


def test_soft_defense_uses_235b_then_32b_and_t06_reaches_downstream(
    tmp_path: Path,
) -> None:
    engine, bus, stubs = _runtime(tmp_path, "trace-soft-defense")
    _drive_to_s2(engine, stubs)
    product = _assert_transition(engine.send(_lecture_draft(engine.trace_id)), "T03")
    reason = "引文相关但不足以推出结论"
    original = _assert_transition(
        engine.send(
            _manual_reject(
                engine.trace_id,
                product,
                "R-02",
                reason=reason,
                evidence_ref="KB-003",
            )
        ),
        "T05",
    )
    generation_llm = SpyLLM(
        [
            _result(
                {
                    "concede": False,
                    "rebuttal": "KB-003直接给出完成率定义，足以支撑结论。",
                    "evidence_refs": ["KB-003", "KB-999"],
                },
                "qwen3-235b-a22b",
            )
        ]
    )
    generator = _rebuttal_module().RebuttalGenerator(
        engine.trace_id, llm_call=generation_llm
    )

    rebuttal_result = engine.send(generator.generate(product, original))

    assert rebuttal_result.bus_result.accepted
    assert not rebuttal_result.transitioned
    rebuttal = rebuttal_result.bus_result.message
    assert rebuttal["agent"] == "knowledge"
    assert rebuttal["payload"]["content"]["concede"] is False
    assert rebuttal["payload"]["content"]["evidence_refs"] == ["KB-003"]
    assert [item["ref"] for item in rebuttal["evidence"]] == ["KB-003"]
    assert generation_llm.calls[0]["model"] == "qwen3-235b-a22b"
    assert generation_llm.calls[0]["temperature"] == 0.7
    assert reason in generation_llm.calls[0]["system"]
    assert json.loads(generation_llm.calls[0]["user"])["product"] == product

    review_llm = SpyLLM(
        [_result({"supported": True, "reason": "引文直接支撑"}, "qwen3-32b")]
    )
    re_verdict = ReviewAgent(engine.trace_id, llm_call=review_llm).re_review(
        product, original, rebuttal
    )
    final = engine.send(re_verdict)

    _assert_transition(final, "T06")
    assert final.bus_result.message["verdict"]["decision"] == "approve"
    assert review_llm.calls[0]["model"] == "qwen3-32b"
    assert "rebuttal" not in json.loads(review_llm.calls[0]["user"])
    assert engine.state is State.S3_TASK
    assert engine.state_history == (
        State.S0_INIT,
        State.S1_DIAGNOSIS,
        State.S2_KNOWLEDGE,
        State.S5_REVIEW,
        State.S6_DEBATE,
        State.S3_TASK,
    )
    messages = _trace_messages(bus, engine.trace_id)
    assert sum(message.get("role") == "rebuttal" for message in messages) == 1
    assert all(validate_message(message) == [] for message in messages)


def test_defense_whitelist_is_exactly_r02_r03() -> None:
    assert _rebuttal_module().DEFENSIBLE_RULES == frozenset({"R-02", "R-03"})


def test_rebuttal_prompt_requires_string_evidence_refs() -> None:
    prompt = _rebuttal_module()._system_prompt(
        ({"reason": "引文相关但不足以推出结论"},)
    )

    assert "evidence_refs必须是字符串数组" in prompt
    assert '["KB-003","sql-live-debate"]' in prompt
    assert "禁止输出对象" in prompt


def _soft_product_and_reject() -> tuple[dict[str, Any], dict[str, Any]]:
    trace_id = "trace-invalid-defense"
    product = _lecture_draft(trace_id)
    product.update({"msg_id": f"{trace_id}-001", "step": 1})
    original = _manual_reject(
        trace_id,
        product,
        "R-02",
        reason="引文相关但不足以推出结论",
        evidence_ref="KB-003",
    )
    original.update({"msg_id": f"{trace_id}-002", "step": 2})
    assert validate_message(product) == []
    assert validate_message(original) == []
    return product, original


def test_probe_product_can_generate_an_evidence_bounded_rebuttal() -> None:
    product, original = _soft_product_and_reject()
    product["role"] = "probe"
    llm = SpyLLM(
        [
            _result(
                {
                    "concede": False,
                    "rebuttal": "KB-003直接支撑当前追问。",
                    "evidence_refs": ["KB-003"],
                },
                "qwen3-235b-a22b",
            )
        ]
    )

    rebuttal = _rebuttal_module().RebuttalGenerator(
        product["trace_id"],
        llm_call=llm,
    ).generate(product, original)
    rebuttal.update({"msg_id": f"{product['trace_id']}-003", "step": 3})

    assert rebuttal["role"] == "rebuttal"
    assert rebuttal["payload"]["content"]["evidence_refs"] == ["KB-003"]
    assert [item["ref"] for item in rebuttal["evidence"]] == ["KB-003"]
    assert validate_message(rebuttal) == []


def test_probe_product_can_complete_re_review_without_relaxing_evidence() -> None:
    product, original = _soft_product_and_reject()
    product["role"] = "probe"
    rebuttal = _rebuttal_envelope(
        product,
        original,
        rebuttal="KB-003直接支撑当前追问。",
        evidence_refs=["KB-003"],
    )
    llm = SpyLLM(
        [_result({"supported": True, "reason": "引文直接支撑"}, "qwen3-32b")]
    )

    re_verdict = ReviewAgent(
        product["trace_id"],
        llm_call=llm,
    ).re_review(product, original, rebuttal)
    re_verdict.update({"msg_id": f"{product['trace_id']}-004", "step": 4})

    assert re_verdict["role"] == "re_verdict"
    assert re_verdict["verdict"]["decision"] == "approve"
    assert validate_message(re_verdict) == []


def test_generator_normalizes_minimal_concession_to_complete_rebuttal_message() -> None:
    product, original = _soft_product_and_reject()
    llm = SpyLLM([_result({"concede": True}, "qwen3-235b-a22b")])

    rebuttal = _rebuttal_module().RebuttalGenerator(
        product["trace_id"], llm_call=llm
    ).generate(product, original)
    rebuttal.update({"msg_id": f"{product['trace_id']}-003", "step": 3})

    assert rebuttal["payload"]["content"]["concede"] is True
    assert rebuttal["payload"]["content"]["rebuttal"] == ""
    assert rebuttal["payload"]["content"]["evidence_refs"] == []
    assert rebuttal["evidence"] == []
    assert validate_message(rebuttal) == []


def _rebuttal_envelope(
    product: dict[str, Any],
    original: dict[str, Any],
    *,
    rebuttal: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    selected = [
        dict(item)
        for item in product["evidence"]
        if item.get("ref") in evidence_refs
    ]
    return {
        "msg_id": f"{product['trace_id']}-003",
        "trace_id": product["trace_id"],
        "step": 3,
        "agent": product["agent"],
        "role": "rebuttal",
        "payload": {
            "type": "rebuttal_case",
            "content": {
                "event": "rebuttal_ready",
                "product_msg_id": product["msg_id"],
                "verdict_msg_id": original["msg_id"],
                "concede": False,
                "rebuttal": rebuttal,
                "evidence_refs": evidence_refs,
            },
        },
        "evidence": selected,
        "claims": [],
        "retry": {"in_reply_to": product["msg_id"]},
        "timestamp": TIMESTAMP,
    }


@pytest.mark.parametrize(
    ("rebuttal", "evidence_refs"),
    [
        ("", ["KB-003"]),
        ("KB-003直接支撑结论。", []),
        ("伪造引用不能构成辩护。", ["KB-999"]),
    ],
    ids=["empty-text", "empty-refs", "unknown-refs"],
)
def test_generator_rejects_non_conceding_defense_without_bounded_evidence(
    rebuttal: str, evidence_refs: list[str]
) -> None:
    product, original = _soft_product_and_reject()
    llm = SpyLLM(
        [
            _result(
                {
                    "concede": False,
                    "rebuttal": rebuttal,
                    "evidence_refs": evidence_refs,
                },
                "qwen3-235b-a22b",
            )
        ]
    )
    generator = _rebuttal_module().RebuttalGenerator(
        product["trace_id"], llm_call=llm
    )

    with pytest.raises(ValueError, match="non-conceding rebuttal"):
        generator.generate(product, original)

    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    ("rebuttal_text", "evidence_refs"),
    [
        ("", ["KB-003"]),
        ("KB-003直接支撑结论。", []),
        ("伪造引用不能构成辩护。", ["KB-999"]),
    ],
    ids=["empty-text", "empty-refs", "unknown-refs"],
)
def test_re_review_rejects_empty_or_unbounded_defense_before_calling_llm(
    rebuttal_text: str, evidence_refs: list[str]
) -> None:
    product, original = _soft_product_and_reject()
    rebuttal = _rebuttal_envelope(
        product,
        original,
        rebuttal=rebuttal_text,
        evidence_refs=evidence_refs,
    )
    assert validate_message(rebuttal) == []
    llm = SpyLLM([])

    with pytest.raises(ValueError, match="non-conceding rebuttal"):
        ReviewAgent(product["trace_id"], llm_call=llm).re_review(
            product, original, rebuttal
        )

    assert llm.calls == []


def test_generator_rejects_cross_wired_verdict_before_calling_llm() -> None:
    product, original = _soft_product_and_reject()
    original["payload"]["content"]["reviewed_msg_id"] = "other-product-001"
    llm = SpyLLM(
        [
            _result(
                {
                    "concede": False,
                    "rebuttal": "KB-003直接支撑结论。",
                    "evidence_refs": ["KB-003"],
                },
                "qwen3-235b-a22b",
            )
        ]
    )

    with pytest.raises(ValueError, match="verdict association"):
        _rebuttal_module().RebuttalGenerator(
            product["trace_id"], llm_call=llm
        ).generate(product, original)

    assert llm.calls == []


def test_re_review_rejects_cross_wired_rebuttal_before_calling_llm() -> None:
    product, original = _soft_product_and_reject()
    rebuttal = _rebuttal_envelope(
        product,
        original,
        rebuttal="KB-003直接支撑结论。",
        evidence_refs=["KB-003"],
    )
    rebuttal["payload"]["content"]["verdict_msg_id"] = "other-verdict-001"
    llm = SpyLLM([])

    with pytest.raises(ValueError, match="rebuttal association"):
        ReviewAgent(product["trace_id"], llm_call=llm).re_review(
            product, original, rebuttal
        )

    assert llm.calls == []


def _live_sql_fact_draft(trace_id: str) -> dict[str, Any]:
    question = "H2601五月预处理完成率"
    sql = (
        "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
        "FROM fact_production_progress WHERE ship_no='H2601' "
        "AND process_code='YCL' AND period_date>='2025-05-01' "
        "AND period_date<'2025-06-01'"
    )
    rows = [{"complete_rate": "0.6236"}]
    data_claim = _render_claims("Q3", question, rows)[0]
    fact_claim = "完成率等于实际量除以计划量。"
    query_ref = "sql-live-debate"
    query_quote = json.dumps(
        {
            "generated_sql": sql,
            "executed_sql": f"{sql} LIMIT 200",
            "status": "completed",
            "row_count": 1,
            "rows": rows,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    draft = {
        "trace_id": trace_id,
        "agent": "verification",
        "role": "produce",
        "payload": {
            "type": "sql_result",
            "content": {
                "event": "query_completed",
                "question": question,
                "family": "Q3",
                "query_id": query_ref,
                "generated_sql": sql,
                "executed_sql": f"{sql} LIMIT 200",
                "columns": ["complete_rate"],
                "rows": rows,
                "row_count": 1,
            },
        },
        "evidence": [
            {
                "kind": "sql_query",
                "ref": query_ref,
                "quote": query_quote,
                "supports_claim": data_claim,
            },
            {
                "kind": "kb_chunk",
                "ref": "KB-003",
                "quote": "**完成率 = 实际量 ÷ 计划量**。",
                "supports_claim": fact_claim,
            },
        ],
        "claims": [
            {"text": data_claim, "kind": "data_conclusion"},
            {"text": fact_claim, "kind": "fact"},
        ],
        "timestamp": TIMESTAMP,
    }
    return draft


@pytest.mark.live
def test_live_manual_false_r02_runs_real_235b_32b_and_persists_trace() -> None:
    trace_id = f"trace-live-debate-{uuid4().hex[:12]}"
    trace_dir = Path(__file__).resolve().parents[1] / "traces"
    engine, bus, stubs = _runtime(trace_dir, trace_id)
    _drive_to_s4(engine, stubs)
    product = _assert_transition(engine.send(_live_sql_fact_draft(trace_id)), "T12")
    original = _assert_transition(
        engine.send(
            _manual_reject(
                trace_id,
                product,
                "R-02",
                reason="人工故障注入：误判定义引文不能支撑同义结论",
                evidence_ref="KB-003",
            )
        ),
        "T05",
    )

    generator = runtime.build_rebuttal_generator(trace_id)
    rebuttal_result = engine.send(generator.generate(product, original))

    assert rebuttal_result.bus_result.accepted, rebuttal_result.bus_result.errors
    assert not rebuttal_result.transitioned
    rebuttal = rebuttal_result.bus_result.message
    assert rebuttal["payload"]["content"]["concede"] is False, rebuttal
    assert rebuttal["model"] == "qwen3-235b-a22b"

    reviewer = runtime.build_review_agent(trace_id)
    re_verdict = reviewer.re_review(product, original, rebuttal)
    final = engine.send(re_verdict)

    _assert_transition(final, "T06")
    assert final.bus_result.message["verdict"]["decision"] == "approve"
    assert final.bus_result.message["model"] == "qwen3-32b"
    assert engine.state is State.S9_PATH_UPDATE
    messages = _trace_messages(bus, trace_id)
    assert sum(message.get("role") == "rebuttal" for message in messages) == 1
    assert all(validate_message(message) == [] for message in messages)
    trace_path = bus.trace_path(trace_id).resolve()
    assert trace_path.exists()
    print(f"LIVE_DEBATE_TRACE={trace_path}")
    print(
        "LIVE_DEBATE="
        + json.dumps(
            {
                "trace_id": trace_id,
                "state_history": [state.value for state in engine.state_history],
                "rebuttal": rebuttal["payload"]["content"],
                "rebuttal_token_usage": rebuttal["token_usage"],
                "re_verdict": final.bus_result.message["verdict"],
                "re_review_token_usage": final.bus_result.message["token_usage"],
                "message_count": len(messages),
            },
            ensure_ascii=False,
        )
    )
