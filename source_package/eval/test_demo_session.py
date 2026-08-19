from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from agents.sandbox import QueryResult
from agents.task_agent import load_task_catalog
from agents.validate_message import validate_message
from agents.review_agent import ReviewAgent
from orchestrator.demo_answers import PRETEST_ANSWERS
import orchestrator.demo_session as demo_session_module
from orchestrator.demo_session import (
    MAX_LECTURE_GENERATION_ATTEMPTS,
    DemoOptions,
    run_demo_session,
)
from orchestrator.llm import LLMResult, TokenUsage


EXPECTED_NORMAL_STATES = (
    "S0_INIT",
    "S1_DIAGNOSIS",
    "S2_KNOWLEDGE",
    "S5_REVIEW",
    "S3_TASK",
    "S5_REVIEW",
    "S7_STUDENT",
    "S4_VERIFY",
    "S5_REVIEW",
    "S9_PATH_UPDATE",
    "S3_TASK",
    "S5_REVIEW",
    "S7_STUDENT",
    "S8_PROBE",
    "S9_PATH_UPDATE",
    "S10_DONE",
)
EXPECTED_NORMAL_TRANSITIONS = (
    "T01",
    "T02",
    "T03",
    "T04",
    "T09",
    "T10",
    "T11",
    "T12",
    "T13",
    "T19",
    "T09",
    "T10",
    "T15",
    "T16",
    "T20",
)


def test_training_summary_does_not_apply_plan_actual_copy_to_other_knowledge_points() -> None:
    profile = {"title": "转岗数字化的工艺工程师"}
    diagnosis = {
        "payload": {
            "content": {
                "pretest_score": {"correct": 6, "total": 7},
                "selected_knowledge_point": "完成率计算",
            }
        }
    }
    result = {
        "payload": {
            "content": {
                "rows": [
                    {"process_code": "YCL", "complete_rate": "0.6236"},
                    {"process_code": "ZZTP", "complete_rate": "1.0249"},
                ]
            }
        }
    }

    summary = demo_session_module._summary(
        profile,
        diagnosis,
        result,
        knowledge_point="完成率计算",
    )

    assert "完成率计算" in summary
    assert "2行真实查询结果" in summary
    assert "None" not in summary
    assert "修正了计划量与实际量混淆" not in summary


def test_training_summary_keeps_plan_actual_correction_when_both_values_exist() -> None:
    profile = {"title": "新入职生产计划员"}
    diagnosis = {
        "payload": {
            "content": {
                "pretest_score": {"correct": 3, "total": 5},
                "selected_knowledge_point": "计划量与实际量口径",
            }
        }
    }
    result = {
        "payload": {
            "content": {
                "rows": [{"plan_qty": "1855.06", "actual_qty": "1156.87"}]
            }
        }
    }

    summary = demo_session_module._summary(
        profile,
        diagnosis,
        result,
        knowledge_point="计划量与实际量口径",
    )

    assert "计划量1855.06" in summary
    assert "实际完成量1156.87" in summary
    assert "完成了计划量与实际量口径核对" in summary
def test_lecture_generation_retry_budget_is_three_attempts() -> None:
    assert MAX_LECTURE_GENERATION_ATTEMPTS == 3


def llm_result(data: dict[str, Any], model: str) -> LLMResult:
    return LLMResult(
        data=data,
        model=model,
        latency_ms=7,
        token_usage=TokenUsage(20, 5, 25),
        attempts=1,
    )


class ScriptedLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        catalog = load_task_catalog()
        self.query_by_stem = {
            entry.question_template.format_map(catalog.demo_parameters): (
                entry.family,
                entry.standard_sql,
            )
            for template_id, entry in catalog.templates.items()
            if template_id in {"T-01", "T-01-A"}
        }

    def _query_for(self, standard_stem: str) -> tuple[str, str]:
        return self.query_by_stem.get(
            standard_stem,
            self.query_by_stem["查询H26012025-05YCL的计划量与实际量"],
        )

    def __call__(self, **request: Any) -> LLMResult:
        self.calls.append(request)
        schema = request["json_schema"]
        model = str(request["model"])
        if "oneOf" in schema:
            user = json.loads(request["user"])
            chunk = user["chunks"][0]
            claim = "计划量与实际量必须分开理解。"
            return llm_result(
                {
                    "lecture_md": f"# 岗位微课\n\n{claim}",
                    "claims": [
                        {
                            "text": claim,
                            "kind": "fact",
                            "chunk_id": chunk["chunk_id"],
                            "sentence_ref": [1],
                        }
                    ],
                    "coverage": [user["knowledge_point"]],
                },
                model,
            )
        required = set(schema.get("required", []))
        if required == {"family"}:
            family, _ = self._query_for(str(request["user"]))
            return llm_result({"family": family}, model)
        if required == {"sql", "family", "explanation"}:
            family, standard_sql = self._query_for(str(request["user"]))
            return llm_result(
                {
                    "sql": standard_sql,
                    "family": family,
                    "explanation": "按任务模板权威限定的口径与分组查询。",
                },
                model,
            )
        if required == {"supported", "reason"}:
            return llm_result(
                {"supported": True, "reason": "引文直接支撑该结论。"},
                model,
            )
        if required == {
            "blind_spots_scaffolded",
            "required_skills",
            "reason",
        }:
            return llm_result(
                {
                    "blind_spots_scaffolded": True,
                    "required_skills": [],
                    "reason": "相关盲区已有铺垫。",
                },
                model,
            )
        if required == {"concede"} and schema.get("allOf"):
            user = json.loads(request["user"])
            evidence_ref = user["product"]["evidence"][0]["ref"]
            return llm_result(
                {
                    "concede": False,
                    "rebuttal": "查询结果与结论使用同一口径，原驳回不成立。",
                    "evidence_refs": [evidence_ref],
                },
                model,
            )
        raise AssertionError(f"unexpected LLM schema: {schema}")


class FirstReviewR02RejectLLM(ScriptedLLM):
    """Make the real reviewer reject its first semantic support check."""

    def __init__(self) -> None:
        super().__init__()
        self.support_checks = 0
        self.review_rejected = False

    def __call__(self, **request: Any) -> LLMResult:
        schema = request["json_schema"]
        if "oneOf" in schema:
            self.calls.append(request)
            user = json.loads(request["user"])
            chunk = user["chunks"][0]
            claim = "预处理完成后进入托盘制作，托盘制作完成后再进入安装环节。"
            return llm_result(
                {
                    "lecture_md": f"# 岗位微课\n\n{claim}",
                    "claims": [
                        {
                            "text": claim,
                            "kind": "fact",
                            "chunk_id": chunk["chunk_id"],
                            "sentence_ref": [2],
                        }
                    ],
                    "coverage": [user["knowledge_point"]],
                },
                str(request["model"]),
            )
        required = set(schema.get("required", []))
        if required == {"supported", "reason"}:
            self.support_checks += 1
            user = json.loads(request["user"])
            claim = str(user.get("claim", "")).strip().rstrip("。")
            quotes = [
                str(quote).strip().rstrip("。")
                for quote in user.get("quotes", [])
            ]
            is_declaration_check = claim in quotes
            if not is_declaration_check and not self.review_rejected:
                self.review_rejected = True
                self.calls.append(request)
                return llm_result(
                    {
                        "supported": False,
                        "reason": "现有引文不足以直接支撑该结论",
                    },
                    str(request["model"]),
                )
        return super().__call__(**request)


class FirstLectureHardRejectLLM(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__()
        self.lecture_calls = 0

    def __call__(self, **request: Any) -> LLMResult:
        schema = request["json_schema"]
        if "oneOf" in schema and self.lecture_calls == 0:
            self.lecture_calls += 1
            self.calls.append(request)
            user = json.loads(request["user"])
            chunk = user["chunks"][0]
            claim = "计划量与实际量必须分开理解。"
            return llm_result(
                {
                    "lecture_md": f"# 岗位微课\n\nH2601船2025年的完成率为62.36%。\n\n{claim}",
                    "claims": [
                        {
                            "text": claim,
                            "kind": "fact",
                            "chunk_id": chunk["chunk_id"],
                            "sentence_ref": [1],
                        }
                    ],
                    "coverage": [user["knowledge_point"]],
                },
                str(request["model"]),
            )
        if "oneOf" in schema:
            self.lecture_calls += 1
        return super().__call__(**request)


class FirstLectureInvalidSentenceRefLLM(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__()
        self.lecture_calls = 0

    def __call__(self, **request: Any) -> LLMResult:
        schema = request["json_schema"]
        if "oneOf" in schema and self.lecture_calls == 0:
            self.lecture_calls += 1
            self.calls.append(request)
            user = json.loads(request["user"])
            chunk = user["chunks"][0]
            claim = "无效引用不应进入成品。"
            return llm_result(
                {
                    "lecture_md": f"# 岗位微课\n\n{claim}",
                    "claims": [
                        {
                            "text": claim,
                            "kind": "fact",
                            "chunk_id": chunk["chunk_id"],
                            "sentence_ref": [999],
                        }
                    ],
                    "coverage": [user["knowledge_point"]],
                },
                str(request["model"]),
            )
        if "oneOf" in schema:
            self.lecture_calls += 1
        return super().__call__(**request)


class FirstLectureSemanticR04RejectLLM(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__()
        self.lecture_calls = 0
        self.semantic_rejections = 0

    def __call__(self, **request: Any) -> LLMResult:
        schema = request["json_schema"]
        if "oneOf" in schema and self.lecture_calls == 0:
            self.lecture_calls += 1
            self.calls.append(request)
            user = json.loads(request["user"])
            chunk = user["chunks"][0]
            claim = "计划量与实际量必须分开理解。"
            return llm_result(
                {
                    "lecture_md": (
                        "# 岗位微课\n\n完成率是衡量生产任务完成情况的重要指标。"
                    ),
                    "claims": [
                        {
                            "text": claim,
                            "kind": "fact",
                            "chunk_id": chunk["chunk_id"],
                            "sentence_ref": [1],
                        }
                    ],
                    "coverage": [user["knowledge_point"]],
                },
                str(request["model"]),
            )
        required = set(schema.get("required", []))
        if required == {"supported", "reason"} and self.semantic_rejections == 0:
            self.semantic_rejections += 1
            self.calls.append(request)
            return llm_result(
                {"supported": False, "reason": "不是已申报claim的自然改写。"},
                str(request["model"]),
            )
        if "oneOf" in schema:
            self.lecture_calls += 1
        return super().__call__(**request)


class ExhaustedLecturePreflightLLM(ScriptedLLM):
    """Reject all three model rewrites before accepting the evidence projection."""

    def __init__(self) -> None:
        super().__init__()
        self.lecture_calls = 0
        self.semantic_rejections = 0

    def __call__(self, **request: Any) -> LLMResult:
        schema = request["json_schema"]
        if "oneOf" in schema:
            self.lecture_calls += 1
            self.calls.append(request)
            user = json.loads(request["user"])
            chunk = user["chunks"][0]
            claim = str(chunk["body"].splitlines()[0]).removeprefix("[S1] ")
            return llm_result(
                {
                    "lecture_md": (
                        "# 岗位微课\n\n"
                        "完成率是衡量生产任务完成情况的重要指标。"
                    ),
                    "claims": [
                        {
                            "text": claim,
                            "kind": "fact",
                            "chunk_id": chunk["chunk_id"],
                            "sentence_ref": [1],
                        }
                    ],
                    "coverage": [user["knowledge_point"]],
                },
                str(request["model"]),
            )
        required = set(schema.get("required", []))
        if (
            required == {"supported", "reason"}
            and self.semantic_rejections < MAX_LECTURE_GENERATION_ATTEMPTS
        ):
            self.semantic_rejections += 1
            self.calls.append(request)
            return llm_result(
                {"supported": False, "reason": "该改写未被声明事实直接覆盖。"},
                str(request["model"]),
            )
        return super().__call__(**request)


class RecordingExecutor:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, executed_sql: str) -> QueryResult:
        self.sql.append(executed_sql)
        if "GROUP BY ship_no" in executed_sql:
            return QueryResult(
                columns=("ship_no", "complete_rate"),
                rows=(
                    {"ship_no": "H2601", "complete_rate": Decimal("0.6236")},
                    {"ship_no": "H2604", "complete_rate": Decimal("0.8924")},
                    {"ship_no": "H2605", "complete_rate": Decimal("0.9472")},
                    {"ship_no": "H2606", "complete_rate": Decimal("0.9727")},
                    {"ship_no": "H2602", "complete_rate": Decimal("0.9776")},
                    {"ship_no": "H2603", "complete_rate": Decimal("0.9779")},
                ),
                elapsed_ms=3,
            )
        return QueryResult(
            columns=("plan_qty", "actual_qty"),
            rows=(
                {
                    "plan_qty": Decimal("1855.06"),
                    "actual_qty": Decimal("1156.87"),
                },
            ),
            elapsed_ms=3,
        )


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def transitions(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        message["payload"]["content"]["transition_id"]
        for message in messages
        if message.get("role") == "system"
        and isinstance(message.get("payload", {}).get("content"), dict)
        and "transition_id" in message["payload"]["content"]
        and not message.get("rejected_by_bus", False)
    )


def message_content(message: dict[str, Any]) -> dict[str, Any]:
    return message["payload"]["content"]


def test_complete_session_uses_real_agent_envelopes_and_exact_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    llm = ScriptedLLM()
    executor = RecordingExecutor()
    options = DemoOptions(
        profile_id="planner_new",
        trace_id="demo-planner-test",
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
    )

    result = run_demo_session(
        options,
        llm_call=llm,
        executor_factory=lambda: executor,
    )

    assert result.state_sequence == EXPECTED_NORMAL_STATES
    assert result.transition_sequence == EXPECTED_NORMAL_TRANSITIONS
    assert result.cache_hits == 0
    assert result.llm_calls == len(llm.calls) > 0
    messages = read_trace(result.trace_path)
    assert result.message_count == len(messages)
    assert transitions(messages) == EXPECTED_NORMAL_TRANSITIONS
    assert all(validate_message(message) == [] for message in messages)
    assert {message["agent"] for message in messages} >= {
        "diagnosis",
        "knowledge",
        "task",
        "verification",
        "review",
    }
    exact_difficulty_lecture = next(
        message for message in messages if message["payload"]["type"] == "lecture_note"
    )
    assert "difficulty_fallback" not in message_content(exact_difficulty_lecture)
    assert len([message for message in messages if message["agent"] == "verification"]) == 2
    assert len(executor.sql) == 2
    assert all("plan_qty" in sql and "actual_qty" in sql for sql in executor.sql)
    probe_tasks = [
        message
        for message in messages
        if message["agent"] == "task" and message["role"] == "probe"
    ]
    assert len(probe_tasks) == 1
    assert message_content(probe_tasks[0])["misconception"] == "M-01"
    first_wrong = next(
        message
        for message in messages
        if message_content(message).get("event") == "student_answer"
    )
    assert message_content(first_wrong) == {
        "event": "student_answer",
        "answer_result": "wrong",
        "requested_action": "probe",
        "source": "student",
    }
    final_update = next(
        message
        for message in reversed(messages)
        if message["payload"]["type"] == "learning_path_update"
    )
    assert message_content(final_update)["learning_goal_achieved"] is True
    assert message_content(final_update)["has_next"] is False
    assert message_content(final_update)["summary"].strip()
    assert message_content(final_update)["completed_nodes"]


def test_session_regenerates_a_lecture_that_fails_existing_hard_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    llm = FirstLectureHardRejectLLM()

    result = run_demo_session(
        DemoOptions(
            profile_id="planner_new",
            trace_id="demo-planner-regenerate-test",
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
        ),
        llm_call=llm,
        executor_factory=RecordingExecutor,
    )

    messages = read_trace(result.trace_path)
    assert result.state_sequence == EXPECTED_NORMAL_STATES
    assert llm.lecture_calls == 2
    assert all(
        "H2601船2025年的完成率为62.36%" not in json.dumps(
            message,
            ensure_ascii=False,
        )
        for message in messages
    )


def test_session_regenerates_a_lecture_with_an_invalid_sentence_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    llm = FirstLectureInvalidSentenceRefLLM()

    result = run_demo_session(
        DemoOptions(
            profile_id="planner_new",
            trace_id="demo-planner-invalid-sentence-ref-test",
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
        ),
        llm_call=llm,
        executor_factory=RecordingExecutor,
    )

    messages = read_trace(result.trace_path)
    lectures = [
        message
        for message in messages
        if message["payload"]["type"] == "lecture_note"
    ]
    assert result.state_sequence == EXPECTED_NORMAL_STATES
    assert llm.lecture_calls == 2
    assert len(lectures) == 1
    assert message_content(lectures[0])["quote_validation"]["failed"] == 0
    assert all(
        failure.get("reason") != "invalid_sentence_ref"
        for message in messages
        for failure in message_content(message)
        .get("quote_validation", {})
        .get("failures", [])
    )


def test_session_regenerates_after_r04_semantic_review_rejects_a_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    llm = FirstLectureSemanticR04RejectLLM()

    result = run_demo_session(
        DemoOptions(
            profile_id="planner_new",
            trace_id="demo-planner-semantic-regenerate-test",
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
        ),
        llm_call=llm,
        executor_factory=RecordingExecutor,
    )

    assert result.state_sequence == EXPECTED_NORMAL_STATES
    assert llm.lecture_calls == 2
    assert llm.semantic_rejections == 1


def test_session_projects_grounded_facts_after_lecture_preflight_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    llm = ExhaustedLecturePreflightLLM()

    result = run_demo_session(
        DemoOptions(
            profile_id="craft_engineer",
            trace_id="demo-lecture-evidence-projection-test",
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
        ),
        llm_call=llm,
        executor_factory=RecordingExecutor,
    )

    messages = read_trace(result.trace_path)
    lecture = next(
        message for message in messages if message["payload"]["type"] == "lecture_note"
    )
    content = message_content(lecture)
    assert result.state_sequence == EXPECTED_NORMAL_STATES
    assert llm.lecture_calls == MAX_LECTURE_GENERATION_ATTEMPTS
    assert llm.semantic_rejections == MAX_LECTURE_GENERATION_ATTEMPTS
    assert content["generated_by"] == "evidence_projection_fallback"
    assert content["fallback_reason"] == "lecture_preflight_exhausted"
    assert content["quote_validation"]["failed"] == 0
    assert "完成率是衡量生产任务完成情况的重要指标" not in content["lecture_md"]


def test_session_uses_exact_completion_rate_chunk_for_profile_difficulty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")

    result = run_demo_session(
        DemoOptions(
            profile_id="craft_engineer",
            trace_id="demo-craft-exact-difficulty-test",
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
        ),
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )

    messages = read_trace(result.trace_path)
    lecture = next(
        message for message in messages if message["payload"]["type"] == "lecture_note"
    )
    lecture_content = message_content(lecture)
    lecture_review = next(
        message
        for message in messages
        if message["payload"]["type"] == "review_verdict"
        and message_content(message).get("reviewed_payload_type") == "lecture_note"
    )
    assert result.state_sequence == EXPECTED_NORMAL_STATES
    assert "difficulty_fallback" not in lecture_content
    assert lecture_content["retrieved_chunk_ids"] == [
        "KB-003-A",
        "KB-002",
        "KB-003",
    ]
    assert lecture_review["verdict"] == {
        "decision": "approve",
        "rule_hits": [],
        "difficulty_action": "keep",
    }
    assert "r03_checked" not in message_content(lecture_review)


def test_real_review_reject_drives_t05_rebuttal_re_review_and_t06(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    llm = FirstReviewR02RejectLLM()
    options = DemoOptions(
        profile_id="planner_new",
        trace_id="demo-planner-debate-test",
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
    )
    runtime = demo_session_module._DemoRuntime(
        options,
        "live",
        llm,
        RecordingExecutor,
    )
    runtime.transition(
        demo_session_module._profile_loaded_draft(
            options.trace_id,
            runtime.profile,
            runtime.knowledge_dimensions,
        ),
        "T01",
    )
    diagnosis = runtime.transition(
        runtime.diagnosis.assess(options.profile_id, PRETEST_ANSWERS),
        "T02",
    )
    diagnosis_content = message_content(diagnosis)
    blind_spots = diagnosis_content["blind_spots"]

    def legacy_paraphrase_lecture() -> dict[str, Any]:
        draft = runtime.knowledge.generate(
            knowledge_point=str(blind_spots[0]),
            student_profile=runtime.profile,
            learning_report_summary="根据岗前测评安排岗位微课。",
            keywords=tuple(str(item) for item in blind_spots[:3]),
            difficulty=str(diagnosis_content["difficulty"]),
        )
        draft = deepcopy(draft)
        claim = "上一步完成材料准备后，下一步继续制作，再进入安装环节。"
        draft["payload"]["content"]["lecture_md"] = f"# 岗位微课\n\n{claim}"
        draft["claims"][0]["text"] = claim
        draft["evidence"][0]["supports_claim"] = claim
        return draft

    product = demo_session_module._produce_reviewed_product(
        runtime,
        legacy_paraphrase_lecture,
        diagnosis,
        "T03",
        "T04",
    )

    assert runtime.engine.state.value == "S3_TASK"
    messages = read_trace(runtime.bus.trace_path(options.trace_id))
    assert transitions(messages) == ("T01", "T02", "T03", "T05", "T06")
    original = next(
        message
        for message in messages
        if message["role"] == "verdict"
        and message.get("verdict", {}).get("decision") == "reject"
    )
    product_id = message_content(original)["reviewed_msg_id"]
    t05 = next(
        message
        for message in messages
        if message_content(message).get("transition_id") == "T05"
    )
    rebuttal = next(message for message in messages if message["role"] == "rebuttal")
    re_verdict = next(
        message for message in messages if message["role"] == "re_verdict"
    )

    assert original["model"] == "qwen3-32b"
    assert message_content(original)["r02_checks"] > 0
    assert original["token_usage"]["total_tokens"] > 0
    assert [hit["rule_id"] for hit in original["verdict"]["rule_hits"]] == ["R-02"]
    assert message_content(t05)["based_on_msg_id"] == original["msg_id"]
    assert message_content(rebuttal)["product_msg_id"] == product_id
    assert message_content(rebuttal)["verdict_msg_id"] == original["msg_id"]
    assert message_content(re_verdict)["reviewed_msg_id"] == product_id
    assert re_verdict["verdict"]["decision"] == "approve"
    assert product["msg_id"] == product_id
    assert llm.support_checks >= 2
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "injected_for_demo" not in serialized
    assert "injection_label" not in serialized
    assert "人工误驳" not in serialized


def _review_reject_fixture(
    product: dict[str, Any],
    rule_id: str = "R-01",
) -> dict[str, Any]:
    """Protocol-valid hard rejection used only to exercise T07 in tests."""

    evidence = product.get("evidence")
    evidence_ref = (
        str(evidence[0]["ref"])
        if isinstance(evidence, list)
        and evidence
        and isinstance(evidence[0], dict)
        and isinstance(evidence[0].get("ref"), str)
        else str(product["msg_id"])
    )
    payload_type = product["payload"]["type"]
    reason = (
        "现有引文不足以直接支撑该结论。"
        if rule_id == "R-02"
        else "这份内容的口径与证据需要重新核对。"
    )
    return {
        "trace_id": product["trace_id"],
        "agent": "review",
        "role": "verdict",
        "payload": {
            "type": "review_verdict",
            "content": {
                "event": "review_complete",
                "reviewed_payload_type": payload_type,
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
        "timestamp": product["timestamp"],
    }


def test_approve_with_fix_does_not_mark_scope_as_learned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def approve_with_fix(
        self: ReviewAgent,
        product: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        verdict = _review_reject_fixture(product, "R-03")
        verdict["verdict"]["decision"] = "approve_with_fix"
        verdict["verdict"]["difficulty_action"] = "step_down"
        return verdict

    monkeypatch.setattr(ReviewAgent, "review", approve_with_fix)
    options = DemoOptions(
        profile_id="planner_new",
        trace_id="demo-approve-with-fix-memory-test",
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
    )
    runtime = demo_session_module._DemoRuntime(
        options,
        "live",
        ScriptedLLM(),
        RecordingExecutor,
    )
    runtime.transition(
        demo_session_module._profile_loaded_draft(
            options.trace_id,
            runtime.profile,
            runtime.knowledge_dimensions,
        ),
        "T01",
    )
    diagnosis = runtime.transition(
        runtime.diagnosis.assess(options.profile_id, PRETEST_ANSWERS),
        "T02",
    )
    diagnosis_content = message_content(diagnosis)
    blind_spots = diagnosis_content["blind_spots"]

    product = demo_session_module._produce_reviewed_product(
        runtime,
        lambda: runtime.knowledge.generate(
            knowledge_point=str(blind_spots[0]),
            student_profile=runtime.profile,
            learning_report_summary="根据岗前测评安排岗位微课。",
            keywords=tuple(str(item) for item in blind_spots[:3]),
            difficulty=str(diagnosis_content["difficulty"]),
        ),
        diagnosis,
        "T03",
        "T04",
    )

    assert message_content(product)["responsibility_scope"]
    assert runtime.engine.state.value == "S3_TASK"
    assert runtime.learned_knowledge_points == []
    t04 = next(
        message
        for message in read_trace(runtime.bus.trace_path(options.trace_id))
        if message_content(message).get("transition_id") == "T04"
    )
    assert message_content(t04)["difficulty_action"] == "step_down"


def test_test_fixture_reject_takes_t07_then_regenerates_through_real_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    original_review = ReviewAgent.review
    injected_calls = 0

    def reject_first_product(
        self: ReviewAgent,
        product: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal injected_calls
        if injected_calls == 0:
            injected_calls += 1
            return _review_reject_fixture(product)
        return original_review(self, product, **kwargs)

    monkeypatch.setattr(ReviewAgent, "review", reject_first_product)
    result = run_demo_session(
        DemoOptions(
            profile_id="planner_new",
            trace_id="demo-planner-regeneration-test",
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
        ),
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )

    assert injected_calls == 1
    assert result.transition_sequence[:7] == (
        "T01",
        "T02",
        "T03",
        "T05",
        "T07",
        "T03",
        "T04",
    )
    messages = read_trace(result.trace_path)
    lectures = [
        message
        for message in messages
        if message["payload"]["type"] == "lecture_note"
        and message["role"] == "produce"
    ]
    assert len(lectures) == 2
    assert lectures[1]["retry"]["retry_count"] == 1
    assert lectures[1]["retry"]["in_reply_to"] == lectures[0]["msg_id"]
    assert "injected_for_demo" not in json.dumps(messages, ensure_ascii=False)


def test_production_review_flow_contains_no_demo_rejection_injection() -> None:
    production_root = Path(__file__).resolve().parents[1] / "orchestrator"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(production_root.glob("*.py"))
    )

    for marker in (
        "_manual_false_reject",
        "injected_for_demo",
        "injection_label",
        "人工误驳演示",
    ):
        assert marker not in source


def test_official_replay_traces_have_no_demo_injection_markers() -> None:
    trace_root = Path(__file__).resolve().parents[1] / "traces"
    traces = sorted(trace_root.glob("demo-*.jsonl"))

    assert traces
    for path in traces:
        contents = path.read_text(encoding="utf-8")
        for marker in (
            "injected_for_demo",
            "injection_label",
            "人工误驳",
            "人工误判",
            "人工注入",
            "故障注入",
            "用于演示辩论回路",
        ):
            assert marker not in contents, f"{path.name} contains {marker}"


def strip_volatile(value: Any) -> Any:
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: strip_volatile(item)
        for key, item in value.items()
        if key not in {"timestamp", "cached"}
        and key != "latency_ms"
        and not key.endswith("_latency_ms")
        and key != "query_elapsed_ms"
    }


def test_cached_session_replays_complete_flow_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace_id = "demo-planner-cached-test"
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("REF_DEMO_MODE", "live")
    live = run_demo_session(
        DemoOptions(
            profile_id="planner_new",
            trace_id=trace_id,
            trace_dir=tmp_path / "live-traces",
            cache_dir=cache_dir,
        ),
        llm_call=ScriptedLLM(),
        executor_factory=RecordingExecutor,
    )
    network_calls = 0

    def forbidden_network(**_: Any) -> LLMResult:
        nonlocal network_calls
        network_calls += 1
        raise AssertionError("network called")

    monkeypatch.setenv("REF_DEMO_MODE", "cached")
    cached = run_demo_session(
        DemoOptions(
            profile_id="planner_new",
            trace_id=trace_id,
            trace_dir=tmp_path / "cached-traces",
            cache_dir=cache_dir,
        ),
        llm_call=forbidden_network,
        executor_factory=RecordingExecutor,
    )

    assert network_calls == 0
    assert cached.state_sequence == live.state_sequence == EXPECTED_NORMAL_STATES
    assert cached.transition_sequence == live.transition_sequence
    assert cached.cache_hits == cached.llm_calls == live.llm_calls
    live_messages = read_trace(live.trace_path)
    cached_messages = read_trace(cached.trace_path)
    assert strip_volatile(cached_messages) == strip_volatile(live_messages)
    model_messages = [message for message in cached_messages if "model" in message]
    assert model_messages
    assert all(message_content(message).get("cached") is True for message in model_messages)
