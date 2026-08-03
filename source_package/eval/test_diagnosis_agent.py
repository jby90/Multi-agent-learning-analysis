from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

from orchestrator.agents_stub import profile_loaded_draft
from orchestrator.bus import MessageBus
from orchestrator.engine import OrchestratorEngine
from orchestrator.llm import LLMResult, TokenUsage, call_llm
from orchestrator.transitions import State


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSIS_PROMPT_PATH = ROOT / "agents" / "prompts" / "diagnosis_narrative.md"
EXPECTED_DIAGNOSIS_PROMPT = (
    '你是船厂培训教研员。根据诊断数据为学员写一段学情分析(150字内)与学习建议(最多3条)。'
    '只输出JSON{"narrative":"...","suggestions":["..."]}。规则：1.涉及数字必须与输入数据完全一致；'
    '2.建议只能针对输入中的盲区知识点，按其顺序；3.不得虚构学员未表现出的问题；'
    '4.语言风格贴合画像lecture_style。[诊断数据JSON][画像JSON]\n'
)
ALL_CORRECT = {
    "PT-1": "B",
    "PT-2": "B",
    "PT-3": "C",
    "PT-4": "B",
    "PT-5": "C",
}
ALL_WRONG = {question_id: "A" for question_id in ALL_CORRECT}
MIXED = {
    "PT-1": "B",
    "PT-2": "A",
    "PT-3": "C",
    "PT-4": "A",
    "PT-5": "C",
}


class SpyLLM:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        return LLMResult(
            data=dict(self.data),
            model="qwen3-32b",
            latency_ms=41,
            token_usage=TokenUsage(80, 20, 100),
            attempts=1,
        )


EXPECTED_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "profile_id": "planner_new",
        "answer_set": "all_correct",
        "answers": ALL_CORRECT,
        "score": {"correct": 5, "total": 5, "rate": 1.0},
        "difficulty": "applied",
        "blind_spots": [
            "三道工序与传导关系",
            "计划量与实际量口径",
            "传导时滞分析",
        ],
        "hit_misconceptions": [],
    },
    {
        "profile_id": "planner_new",
        "answer_set": "all_wrong",
        "answers": ALL_WRONG,
        "score": {"correct": 0, "total": 5, "rate": 0.0},
        "difficulty": "basic",
        "blind_spots": [
            "三道工序与传导关系",
            "计划量与实际量口径",
            "传导时滞分析",
            "完成率计算",
            "异常识别标准",
            "月度聚合方法",
        ],
        "hit_misconceptions": ["M-01", "M-04", "M-02", "M-03"],
    },
    {
        "profile_id": "planner_new",
        "answer_set": "mixed",
        "answers": MIXED,
        "score": {"correct": 3, "total": 5, "rate": 0.6},
        "difficulty": "basic",
        "blind_spots": [
            "三道工序与传导关系",
            "计划量与实际量口径",
            "传导时滞分析",
            "完成率计算",
            "异常识别标准",
        ],
        "hit_misconceptions": ["M-01", "M-04", "M-03"],
    },
    {
        "profile_id": "craft_engineer",
        "answer_set": "all_correct",
        "answers": ALL_CORRECT,
        "score": {"correct": 5, "total": 5, "rate": 1.0},
        "difficulty": "advanced",
        "blind_spots": ["完成率计算", "月度聚合方法", "跨工序归因方法"],
        "hit_misconceptions": [],
    },
    {
        "profile_id": "craft_engineer",
        "answer_set": "all_wrong",
        "answers": ALL_WRONG,
        "score": {"correct": 0, "total": 5, "rate": 0.0},
        "difficulty": "basic",
        "blind_spots": [
            "完成率计算",
            "月度聚合方法",
            "跨工序归因方法",
            "计划量与实际量口径",
            "传导时滞分析",
            "异常识别标准",
        ],
        "hit_misconceptions": ["M-01", "M-04", "M-02", "M-03"],
    },
    {
        "profile_id": "craft_engineer",
        "answer_set": "mixed",
        "answers": MIXED,
        "score": {"correct": 3, "total": 5, "rate": 0.6},
        "difficulty": "applied",
        "blind_spots": [
            "完成率计算",
            "月度聚合方法",
            "跨工序归因方法",
            "异常识别标准",
        ],
        "hit_misconceptions": ["M-01", "M-04", "M-03"],
    },
    {
        "profile_id": "line_leader",
        "answer_set": "all_correct",
        "answers": ALL_CORRECT,
        "score": {"correct": 5, "total": 5, "rate": 1.0},
        "difficulty": "applied",
        "blind_spots": [
            "计划量与实际量口径",
            "完成率计算",
            "异常识别标准",
            "责任单元定位",
        ],
        "hit_misconceptions": [],
    },
    {
        "profile_id": "line_leader",
        "answer_set": "all_wrong",
        "answers": ALL_WRONG,
        "score": {"correct": 0, "total": 5, "rate": 0.0},
        "difficulty": "basic",
        "blind_spots": [
            "计划量与实际量口径",
            "完成率计算",
            "异常识别标准",
            "责任单元定位",
            "传导时滞分析",
            "月度聚合方法",
        ],
        "hit_misconceptions": ["M-01", "M-04", "M-02", "M-03"],
    },
    {
        "profile_id": "line_leader",
        "answer_set": "mixed",
        "answers": MIXED,
        "score": {"correct": 3, "total": 5, "rate": 0.6},
        "difficulty": "basic",
        "blind_spots": [
            "计划量与实际量口径",
            "完成率计算",
            "异常识别标准",
            "责任单元定位",
        ],
        "hit_misconceptions": ["M-01", "M-04", "M-03"],
    },
)


def _diagnosis_module() -> Any:
    try:
        return importlib.import_module("agents.diagnosis_agent")
    except ModuleNotFoundError:
        pytest.fail("agents.diagnosis_agent is not implemented")


def test_diagnosis_prompt_matches_instruction_card_verbatim() -> None:
    assert DIAGNOSIS_PROMPT_PATH.read_text(encoding="utf-8") == (
        EXPECTED_DIAGNOSIS_PROMPT
    )


@pytest.mark.parametrize(
    "case",
    EXPECTED_MATRIX,
    ids=lambda case: f"{case['profile_id']}-{case['answer_set']}",
)
def test_user_approved_nine_case_diagnosis_matrix(case: dict[str, Any]) -> None:
    module = _diagnosis_module()
    message = module.DiagnosisAgent(f"trace-{case['profile_id']}").assess(
        case["profile_id"], case["answers"]
    )
    content = message["payload"]["content"]

    assert content["profile_id"] == case["profile_id"]
    assert content["pretest_score"] == case["score"]
    assert content["difficulty"] == case["difficulty"]
    assert content["blind_spots"] == case["blind_spots"]
    assert content["hit_misconceptions"] == case["hit_misconceptions"]


@pytest.mark.parametrize(
    ("correct_ids", "expected_rate"),
    [
        ({"PT-1", "PT-2"}, 0.4),
        ({"PT-1", "PT-2", "PT-3", "PT-4"}, 0.8),
    ],
)
def test_closed_rate_boundaries_use_profile_start_difficulty(
    correct_ids: set[str], expected_rate: float
) -> None:
    module = _diagnosis_module()
    answers = {
        question_id: answer if question_id in correct_ids else "A"
        for question_id, answer in ALL_CORRECT.items()
    }

    message = module.DiagnosisAgent("trace-boundary").assess(
        "craft_engineer", answers
    )

    assert message["payload"]["content"]["pretest_score"]["rate"] == expected_rate
    assert message["payload"]["content"]["difficulty"] == "applied"


def test_assets_are_the_approved_three_profiles_and_five_questions() -> None:
    module = _diagnosis_module()

    profiles = module.load_profiles()
    pretest = module.load_pretest()

    assert list(profiles) == ["planner_new", "craft_engineer", "line_leader"]
    assert profiles["craft_engineer"]["difficulty_start"] == "applied"
    assert [item["question_id"] for item in pretest] == [
        "PT-1",
        "PT-2",
        "PT-3",
        "PT-4",
        "PT-5",
    ]
    assert [item["answer"] for item in pretest] == ["B", "B", "C", "B", "C"]


def test_diagnosis_is_protocol_valid_and_t02_advances_without_llm_metadata(
    tmp_path: Path,
) -> None:
    module = _diagnosis_module()
    trace_id = "trace-real-diagnosis"
    engine = OrchestratorEngine(MessageBus(tmp_path), trace_id, "planner_new")
    t01 = engine.send(profile_loaded_draft(trace_id))

    result = engine.send(module.DiagnosisAgent(trace_id).assess("planner_new", MIXED))

    assert t01.transitioned and t01.transition is not None
    assert t01.transition.transition_id == "T01"
    assert result.bus_result.accepted, result.bus_result.errors
    assert result.transitioned and result.transition is not None
    assert result.transition.transition_id == "T02"
    assert engine.state is State.S2_KNOWLEDGE
    for field in ("model", "token_usage", "latency_ms"):
        assert field not in result.bus_result.message


@pytest.mark.parametrize(
    "profile_id,answers,match",
    [
        ("unknown", ALL_CORRECT, "profile_id"),
        ("planner_new", {"PT-1": "B"}, "exactly"),
        ("planner_new", {**ALL_CORRECT, "PT-6": "A"}, "exactly"),
        ("planner_new", {**ALL_CORRECT, "PT-1": "Z"}, "option"),
    ],
)
def test_invalid_assessment_inputs_fail_before_scoring(
    profile_id: str, answers: dict[str, str], match: str
) -> None:
    module = _diagnosis_module()

    with pytest.raises(ValueError, match=match):
            module.DiagnosisAgent("trace-invalid").assess(profile_id, answers)


def test_numeric_out_of_set_discards_narrative_and_audits_fallback() -> None:
    module = _diagnosis_module()
    llm = SpyLLM(
        {
            "narrative": "岗前测评答对3/5题，但还出现99项新问题。",
            "suggestions": ["先练完成率计算"],
        }
    )

    message = module.DiagnosisAgent(
        "trace-numeric-fallback", llm_call=llm
    ).assess("planner_new", MIXED)
    content = message["payload"]["content"]

    assert content["pretest_score"] == {"correct": 3, "total": 5, "rate": 0.6}
    assert content["diagnosis_narrative"] == ""
    assert content["suggestions"] == []
    assert content["narrative_fallback"] is True
    assert message["model"] == "qwen3-32b"
    assert message["token_usage"] == {
        "prompt_tokens": 80,
        "completion_tokens": 20,
        "total_tokens": 100,
    }


def test_grounded_narrative_is_added_without_changing_rule_diagnosis(
    tmp_path: Path,
) -> None:
    module = _diagnosis_module()
    llm = SpyLLM(
        {
            "narrative": "岗前测评答对3/5题，正确率为0.6。",
            "suggestions": ["先练三道工序与传导关系", "再练计划量与实际量口径"],
        }
    )

    message = module.DiagnosisAgent(
        "trace-grounded-narrative", llm_call=llm
    ).assess("planner_new", MIXED)
    content = message["payload"]["content"]

    assert content["blind_spots"][:2] == [
        "三道工序与传导关系",
        "计划量与实际量口径",
    ]
    assert content["diagnosis_narrative"] == "岗前测评答对3/5题，正确率为0.6。"
    assert content["suggestions"] == [
        "先练三道工序与传导关系",
        "再练计划量与实际量口径",
    ]
    assert content["narrative_fallback"] is False
    assert llm.calls[0]["model"] == "qwen3-32b"
    assert llm.calls[0]["temperature"] == 0.3
    assert llm.calls[0]["system"] == EXPECTED_DIAGNOSIS_PROMPT
    assert "重讲工序与口径、少讲SQL" in llm.calls[0]["user"]
    assert MessageBus(tmp_path).send(message).accepted


def test_diagnosis_prompt_names_the_verbatim_numeric_whitelist() -> None:
    module = _diagnosis_module()
    llm = SpyLLM(
        {
            "narrative": "岗前测评答对3/5题，正确率为0.6。",
            "suggestions": [],
        }
    )

    module.DiagnosisAgent("trace-numeric-prompt", llm_call=llm).assess(
        "planner_new", MIXED
    )

    assert '"narrative_number_whitelist":["0.6","1","3","4","5"]' in (
        llm.calls[0]["user"]
    )
    assert (
        '"numeric_copy_rule":"叙述若使用阿拉伯数字，只能逐字复制narrative_number_whitelist；'
        '禁止换算、添加百分号或组合新数字"'
    ) in llm.calls[0]["user"]
    assert '"safe_score_phrase":"答对3/5题，正确率0.6"' in llm.calls[0]["user"]


def test_diagnosis_llm_failure_uses_empty_audited_fallback() -> None:
    module = _diagnosis_module()

    def fail(**_: Any) -> LLMResult:
        raise RuntimeError("provider unavailable")

    message = module.DiagnosisAgent(
        "trace-diagnosis-llm-failure", llm_call=fail
    ).assess("planner_new", MIXED)
    content = message["payload"]["content"]

    assert content["diagnosis_narrative"] == ""
    assert content["suggestions"] == []
    assert content["narrative_fallback"] is True
    assert content["narrative_fallback_reason"] == "llm_failure"
    assert message["model"] == "qwen3-32b"
    assert message["token_usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_diagnosis_constructor_exposes_optional_llm_dependency() -> None:
    module = _diagnosis_module()
    signature = inspect.signature(module.DiagnosisAgent)

    assert "llm_call" in signature.parameters
    assert signature.parameters["llm_call"].default is None


@pytest.mark.live
def test_live_p4_5_three_profile_narratives_are_grounded_and_distinct() -> None:
    module = _diagnosis_module()
    narratives: dict[str, str] = {}

    for profile_id in module.PROFILE_ORDER:
        message = module.DiagnosisAgent(
            f"trace-live-p4-5-diagnosis-{profile_id}", llm_call=call_llm
        ).assess(profile_id, MIXED)
        content = message["payload"]["content"]
        narrative = content["diagnosis_narrative"]
        diagnosis_data = {
            key: content[key]
            for key in (
                "event",
                "profile_id",
                "blind_spots",
                "hit_misconceptions",
                "difficulty",
                "pretest_score",
            )
        }

        assert content["narrative_fallback"] is False, (profile_id, content)
        assert module._narrative_numbers_are_grounded(narrative, diagnosis_data)
        assert narrative
        narratives[profile_id] = narrative

    assert len(set(narratives.values())) == 3
    print("P4.5 diagnosis live narratives:", narratives)
