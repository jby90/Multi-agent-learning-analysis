from __future__ import annotations

from pathlib import Path

from eval.v3_formal_runner import FormalCaseRunner, GoldLearnerActor


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

    def create_session(self, profile_id):
        self.calls.append(("create_session", profile_id))
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


def test_formal_runner_never_injects_knowledge_point_or_template(tmp_path: Path):
    case = {
        "case_id": "E2E-001",
        "route_mode": "production",
        "profile_id": "planner_new",
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
