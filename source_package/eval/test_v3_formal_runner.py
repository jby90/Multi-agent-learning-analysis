from __future__ import annotations

from pathlib import Path

from eval.v3_formal_runner import FormalCaseRunner, GoldLearnerActor
from orchestrator.interactive_session import _is_vacuous_follow_up_answer


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
                }
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
        "learner_script_id": "S-KEEP-B",
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
        "learner_script_id": "S-KEEP-B",
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
