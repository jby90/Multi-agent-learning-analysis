from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from eval.v4_cases import load_formal_cases, load_gold_standard
from eval.v4_formal_runner import (
    V4FormalCaseRunner,
    V4LearnerActor,
    _target_scenario_completed,
    validate_v4_gold,
)


def _completed_state(session_id: str, point: str, difficulty: str) -> dict:
    return {
        "session_id": session_id,
        "trace_id": f"trace-{session_id}",
        "awaiting": "done",
        "outcome": "completed",
        "learning_contract": {"target_knowledge_points": [point]},
        "training_report": {
            "knowledge_point": point,
            "final_difficulty": difficulty,
        },
        "messages": [
            {
                "step": 1,
                "payload": {
                    "content": {
                        "event": "path_updated",
                        "difficulty_action": "step_up",
                    }
                },
            }
        ],
    }


class _TwoUnitManager:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.first = {
            "session_id": "session-1",
            "trace_id": "trace-session-1",
            "awaiting": "pretest",
            "outcome": None,
            "messages": [],
        }

    def create_session(self, profile_id, *, experience_tags=()):
        self.calls.append(("create_session", profile_id, tuple(experience_tags)))
        return dict(self.first)

    def submit_pretest(self, session_id, answers):
        self.calls.append(("submit_pretest", session_id, dict(answers)))
        return _completed_state(session_id, "前置知识点", "applied")

    def continue_learning(self, session_id):
        self.calls.append(("continue_learning", session_id))
        return _completed_state("session-2", "目标知识点", "applied")


def test_v4_gold_matches_all_current_production_task_contracts():
    cases = [asdict(item) for item in load_formal_cases()]
    assert validate_v4_gold(cases, load_gold_standard()) == []


def test_v4_runner_follows_continue_learning_until_target_without_injection(
    tmp_path: Path,
):
    manager = _TwoUnitManager()
    gold = {
        "case_id": "E2E-X",
        "target_knowledge_point": "目标知识点",
        "expected_target_plan_position": 2,
        "expected_initial_difficulty": "basic",
        "expected_final_difficulty": "applied",
        "initial_standard_sql": "SELECT 1",
        "initial_expected_points": ["值为1"],
        "final_expected_points": ["值为1"],
    }
    result = V4FormalCaseRunner(
        manager,
        actor=V4LearnerActor(gold),
        run_id="V4-TEST",
        seed_id="seed_A",
        output_dir=tmp_path,
        code_version="test",
    ).run_case(
        {
            "case_id": "E2E-X",
            "route_mode": "production",
            "profile_id": "planner_new",
            "experience_tags": [],
            "pretest_answers": {"PT-1": "A"},
            "diagnostic_probe_answers": [],
            "learner_script_id": "S-STEP-UP-B",
        }
    )

    assert result["status"] == "completed_target_scenario"
    assert result["encountered_knowledge_points"] == ["前置知识点", "目标知识点"]
    assert result["session_ids"] == ["session-1", "session-2"]
    assert manager.calls[-1] == ("continue_learning", "session-1")
    assert "knowledge_point" not in repr(manager.calls)
    assert "template_id" not in repr(manager.calls)


def test_target_completion_requires_declared_final_difficulty():
    state = _completed_state("session-1", "目标知识点", "basic")
    gold = {
        "target_knowledge_point": "目标知识点",
        "expected_final_difficulty": "applied",
    }
    assert not _target_scenario_completed(state, gold, "S-STEP-UP-B")


def test_rebuttal_actor_repeats_wrong_claim_until_a_real_nonmastered_assessment():
    gold = {
        "case_id": "E2E-X",
        "target_knowledge_point": "目标知识点",
        "initial_standard_sql": "SELECT 1",
        "initial_expected_points": ["值为1"],
        "final_expected_points": ["值为1"],
        "expected_final_difficulty": "advanced",
    }
    actor = V4LearnerActor(gold)
    state = {
        "learning_contract": {"target_knowledge_points": ["目标知识点"]},
        "current_difficulty": "applied",
        "messages": [],
    }

    first = actor.follow_up_answer_for_state(state, "S-REBUTTAL")
    # A review rejection leaves the session at follow_up without creating a
    # learner assessment.  Re-entering the actor must therefore keep the
    # intended wrong claim instead of silently switching to the correct turn.
    second = actor.follow_up_answer_for_state(state, "S-REBUTTAL")

    assert "9999%" in first
    assert "9999%" in second

    state["messages"].append(
        {
            "payload": {
                "content": {
                    "event": "learner_follow_up_assessed",
                    "assessment": "unknown",
                }
            }
        }
    )
    recovered = actor.follow_up_answer_for_state(state, "S-REBUTTAL")
    assert "9999%" not in recovered
    assert "值为1" in recovered


def test_actor_answers_monthly_trend_from_visible_rows_and_current_question():
    gold = {
        "case_id": "E2E-X",
        "target_knowledge_point": "月度聚合方法",
        "initial_standard_sql": "SELECT 1",
        "initial_expected_points": ["3行月序列"],
        "final_expected_points": ["3行月序列"],
        "expected_final_difficulty": "advanced",
    }
    actor = V4LearnerActor(gold)
    state = {
        "learning_contract": {"target_knowledge_points": ["月度聚合方法"]},
        "current_difficulty": "basic",
        "messages": [
            {
                "payload": {
                    "content": {
                        "event": "query_completed",
                        "rows": [
                            {"month_label": "2025-03", "complete_rate": 0.9534},
                            {"month_label": "2025-05", "complete_rate": 0.6236},
                            {"month_label": "2025-04", "complete_rate": 0.9061},
                        ],
                    }
                }
            },
            {
                "payload": {
                    "content": {
                        "event": "follow_up_question_ready",
                        "question": "相邻月份的完成率如何变化，这属于单期变化还是持续趋势？",
                    }
                }
            },
            {
                "payload": {
                    "content": {
                        "event": "path_updated",
                        "difficulty_action": "step_down",
                    }
                }
            },
        ],
    }

    answer = actor.follow_up_answer_for_state(state, "S-DOWNSTEP")

    assert "2025-03" in answer and "0.9534" in answer
    assert "2025-04" in answer and "0.9061" in answer
    assert "2025-05" in answer and "0.6236" in answer
    assert "连续下降" in answer
    assert "不是单期孤立波动" in answer


def test_actor_names_the_largest_visible_month_to_month_change():
    gold = {
        "case_id": "E2E-X",
        "target_knowledge_point": "月度聚合方法",
        "initial_standard_sql": "SELECT 1",
        "initial_expected_points": ["3行月序列"],
        "final_expected_points": ["3行月序列"],
        "expected_final_difficulty": "applied",
    }
    actor = V4LearnerActor(gold)
    state = {
        "learning_contract": {"target_knowledge_points": ["月度聚合方法"]},
        "messages": [
            {
                "payload": {
                    "content": {
                        "event": "query_completed",
                        "rows": [
                            {"month_label": "2025-03", "complete_rate": 0.9534},
                            {"month_label": "2025-04", "complete_rate": 0.9061},
                            {"month_label": "2025-05", "complete_rate": 0.6236},
                        ],
                    }
                }
            },
            {
                "payload": {
                    "content": {
                        "event": "follow_up_question_ready",
                        "question": "月度结果中哪个月的完成率变化最明显，你依据的月份和值是什么？",
                    }
                }
            },
            {
                "payload": {
                    "content": {
                        "event": "path_updated",
                        "difficulty_action": "refresh",
                    }
                }
            },
        ],
    }

    answer = actor.follow_up_answer_for_state(state, "S-REFRESH")

    assert "2025-05相对2025-04" in answer
    assert "0.9061" in answer and "0.6236" in answer
    assert "变化最明显" in answer
