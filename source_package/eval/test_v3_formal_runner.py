from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from eval.v3_cases import load_formal_cases, load_gold_standard
from eval.v3_formal_runner import (
    FormalCaseRunner,
    GoldLearnerActor,
    validate_frozen_task_gold,
)
from orchestrator.interactive_session import _is_vacuous_follow_up_answer


def test_frozen_specification_gate_reports_declared_template_sql_mismatch():
    contract = {
        "knowledge_point": "异常识别标准",
        "difficulty": "applied",
        "payload_type": "practice_guide",
        "family": "Q5",
        "question_template": "查询各船排名",
        "standard_sql": "SELECT month_label, complete_rate FROM monthly_rates",
        "expected_rows": [{"month_label": "2025-05", "complete_rate": 0.9}],
        "expected_points": ["按月比较"],
    }
    conflicts = validate_frozen_task_gold(
        [{"case_id": "E2E-X"}],
        {
            "E2E-X": {
                "目标知识点": "异常识别标准",
                "预期初始难度": "applied",
                "预期初始模板": "T-05-A",
                "初始难度": "applied",
                "初始题型": "practice_guide",
                "初始family": "Q5",
                "初始业务任务": "查询各船排名",
                "初始标准SQL": "SELECT ship_no, complete_rate FROM ship_rates",
                "初始expected_rows": [{"month_label": "2025-05", "complete_rate": 0.9}],
                "初始预期要点": "按月比较",
                "预期最终难度": "applied",
                "预期最终模板": "T-05-A",
                "最终难度": "applied",
                "最终题型": "practice_guide",
                "最终family": "Q5",
                "最终业务任务": "查询各船排名",
                "最终标准SQL": "SELECT month_label, complete_rate FROM monthly_rates",
                "最终expected_rows": [{"month_label": "2025-05", "complete_rate": 0.9}],
                "最终预期要点": "按月比较",
            }
        },
        task_contracts={
            "T-05": {**contract, "standard_sql": "SELECT ship_no, complete_rate FROM ship_rates"},
            "T-05-A": contract,
        },
    )

    assert conflicts == [
        {
            "case_id": "E2E-X",
            "stage": "初始",
            "conflict_type": "template_contract_mismatch",
            "knowledge_point": "异常识别标准",
            "declared_difficulty": "applied",
            "declared_template": "T-05-A",
            "mismatched_fields": ["standard_sql"],
            "sql_matching_templates": ["T-05"],
        }
    ]


def test_v3_2_frozen_specification_matches_all_initial_and_final_templates():
    cases = [asdict(case) for case in load_formal_cases()]
    assert validate_frozen_task_gold(cases, load_gold_standard()) == []


class FakeManager:
    def __init__(self):
        self.calls = []
        self._state = {
            "session_id": "session-1",
            "trace_id": "trace-1",
            "trace_path": "trace-1.jsonl",
            "state": "S1_DIAGNOSIS",
            "awaiting": "pretest",
            "messages": [],
            "outcome": None,
        }

    def create_session(self, profile_id, *, experience_tags=()):
        self.calls.append(("create_session", profile_id, tuple(experience_tags)))
        return dict(self._state)

    def submit_pretest(self, session_id, answers):
        self.calls.append(("submit_pretest", session_id, dict(answers)))
        self._state.update(awaiting="diagnostic_probe")
        return dict(self._state)

    def get_diagnostic_probes(self, session_id):
        return [{"probe_id": "DP-01-B", "stem": "probe"}]

    def submit_diagnostic_probes(self, session_id, answers):
        self.calls.append(("submit_diagnostic_probes", session_id, dict(answers)))
        self._state.update(
            awaiting="follow_up",
            state="S7_STUDENT",
            messages=[
                {
                    "step": 1,
                    "payload": {
                        "type": "control",
                        "content": {
                            "event": "learner_follow_up_assessed",
                            "round": 1,
                            "assessment": "mastered",
                        },
                    },
                },
                {
                    "step": 2,
                    "payload": {
                        "type": "learning_path_update",
                        "content": {"difficulty_action": "step_up"},
                    },
                },
                {
                    "step": 3,
                    "payload": {
                        "type": "practice_guide",
                        "content": {
                            "event": "product_ready",
                            "difficulty": "applied",
                        },
                    },
                },
            ],
        )
        return dict(self._state)


class RetryingReviewManager(FakeManager):
    """Keep the learner on one turn while bounded quality review retries."""

    def __init__(self, retries: int = 55):
        super().__init__()
        self.retries = retries
        self.follow_up_calls = 0

    def submit_diagnostic_probes(self, session_id, answers):
        self.calls.append(("submit_diagnostic_probes", session_id, dict(answers)))
        self._state.update(awaiting="follow_up", state="S7_STUDENT", messages=[])
        return dict(self._state)

    def submit_follow_up(self, session_id, answer, client_turn_id):
        self.follow_up_calls += 1
        self.calls.append(("submit_follow_up", session_id, answer, client_turn_id))
        if self.follow_up_calls <= self.retries:
            return dict(self._state)
        assessments = [
            {
                "step": 1,
                "payload": {
                    "content": {
                        "event": "learner_follow_up_assessed",
                        "round": 1,
                        "assessment": "unknown",
                    }
                },
            }
        ]
        if self.follow_up_calls > self.retries + 1:
            assessments.append(
                {
                    "step": 2,
                    "payload": {
                        "content": {
                            "event": "learner_follow_up_assessed",
                            "round": 2,
                            "assessment": "mastered",
                        }
                    },
                }
            )
            assessments.append(
                {
                    "step": 3,
                    "payload": {
                        "type": "learning_path_update",
                        "content": {"difficulty_action": "step_up"},
                    },
                }
            )
        self._state["messages"] = assessments
        return dict(self._state)


def test_formal_runner_never_injects_knowledge_point_or_template(tmp_path: Path):
    case = {
        "case_id": "E2E-001",
        "route_mode": "production",
        "profile_id": "planner_new",
        "experience_tags": ["process_flow_coordination"],
        "pretest_answers": {f"PT-{index}": "A" for index in range(1, 6)},
        "diagnostic_probe_answers": [{"probe_id": "DP-01-B", "answer": "wrong"}],
        "learner_script_id": "S-STEP-UP-B",
    }
    gold = {
        "case_id": "E2E-001",
        "标准SQL": "SELECT 1 AS value",
        "预期要点": "value=1",
    }
    manager = FakeManager()
    runner = FormalCaseRunner(
        manager,
        actor=GoldLearnerActor(gold),
        run_id="RUN-A",
        seed_id="seed_A",
        output_dir=tmp_path,
        code_version="abc123",
    )
    result = runner.run_case(case)
    assert result["status"] == "completed_scenario"
    serialized_calls = repr(manager.calls)
    assert "knowledge_point" not in serialized_calls
    assert "template_id" not in serialized_calls
    assert result["route_mode"] == "production"


def test_formal_runner_rejects_forced_case_before_session_creation(tmp_path: Path):
    case = {
        "case_id": "E2E-X",
        "route_mode": "forced",
        "profile_id": "planner_new",
        "pretest_answers": {},
        "diagnostic_probe_answers": [],
        "learner_script_id": "S-STEP-UP-B",
    }
    manager = FakeManager()
    runner = FormalCaseRunner(
        manager,
        actor=GoldLearnerActor({"case_id": "E2E-X", "标准SQL": "SELECT 1"}),
        run_id="RUN-A",
        seed_id="seed_A",
        output_dir=tmp_path,
        code_version="abc123",
    )
    try:
        runner.run_case(case)
    except ValueError as exc:
        assert "production" in str(exc)
    else:
        raise AssertionError("forced case must be rejected")
    assert manager.calls == []


def test_wrong_formal_learner_answer_is_evidence_bearing_and_accepted_by_input_gate():
    actor = GoldLearnerActor(
        {
            "case_id": "E2E-004",
            "标准SQL": "SELECT process_code, complete_rate FROM result",
            "预期要点": "YCL完成率最低",
        }
    )
    state = {
        "messages": [
            {
                "step": 1,
                "payload": {
                    "content": {
                        "event": "query_completed",
                        "rows": [
                            {"process_code": "YCL", "complete_rate": 0.6236},
                            {"process_code": "ZZTP", "complete_rate": 1.0249},
                        ],
                    }
                },
            }
        ]
    }

    answer = actor.follow_up_answer(state, "S-REBUTTAL")

    assert not _is_vacuous_follow_up_answer(answer)
    assert "工序" in answer
    assert "完成率" in answer
    assert "9999" in answer


def test_formal_learner_answer_respects_the_production_input_limit():
    point = (
        "数据可见范围内，YCL 2025-05=0.6236是最早的时序候选，"
        "ZZTP 2025-06=0.7545、AZTP 2025-07=0.8501构成后续候选节点；"
        "只到候选链，不确认传导；须补同一工作包、物量依赖路径、下游齐套暴露、"
        "源头与落点执行单元、共同因素及下游本地异常证据，才能收窄候选。"
    )
    actor = GoldLearnerActor(
        {"case_id": "E2E-047", "标准SQL": "SELECT 1", "预期要点": point}
    )
    rows = [
        {
            "process_code": process,
            "month_label": f"2025-{month:02d}",
            "complete_rate": f"{rate:.4f}",
        }
        for process, month, rate in (
            (process, month, 0.6 + month / 100)
            for process in ("AZTP", "YCL", "ZZTP")
            for month in range(4, 8)
        )
    ]
    state = {
        "messages": [
            {
                "payload": {
                    "content": {"event": "query_completed", "rows": rows}
                }
            }
        ]
    }

    answer = actor.follow_up_answer(state, "S-STEP-UP-A")

    assert len(answer) <= 500
    assert "YCL 2025-05=0.6236" in answer


def test_formal_runner_tolerates_bounded_quality_review_retries(tmp_path: Path):
    manager = RetryingReviewManager(retries=55)
    runner = FormalCaseRunner(
        manager,
        actor=GoldLearnerActor(
            {
                "case_id": "E2E-004",
                "标准SQL": "SELECT 1 AS value",
                "预期要点": "value=1",
            }
        ),
        run_id="RUN-RETRY",
        seed_id="seed_A",
        output_dir=tmp_path,
        code_version="abc123",
    )
    case = {
        "case_id": "E2E-004",
        "route_mode": "production",
        "profile_id": "planner_new",
        "experience_tags": ["process_flow_coordination"],
        "pretest_answers": {f"PT-{index}": "A" for index in range(1, 6)},
        "diagnostic_probe_answers": [{"probe_id": "DP-01-B", "answer": "wrong"}],
        "learner_script_id": "S-REBUTTAL",
    }

    result = runner.run_case(case)

    assert result["status"] == "completed_scenario"
    assert manager.follow_up_calls == 57


def test_formal_learner_solves_the_observed_runtime_task_not_a_mismatched_gold_sql():
    actor = GoldLearnerActor(
        {
            "case_id": "E2E-008",
            "标准SQL": "SELECT SUM(plan_qty) AS plan_qty, SUM(actual_qty) AS actual_qty",
            "预期要点": "计划量1855.06，实际量1156.87",
        }
    )
    state = {
        "messages": [
            {
                "step": 12,
                "payload": {
                    "content": {
                        "event": "product_ready",
                        "template_id": "T-01-A",
                        "question": "查询2025-05各船YCL完成率",
                    }
                },
            }
        ]
    }

    sql = actor.sql_for_state(state)

    assert "ship_no" in sql
    assert "complete_rate" in sql
    assert "GROUP BY ship_no" in sql
    assert "plan_qty, SUM(actual_qty)" not in sql
