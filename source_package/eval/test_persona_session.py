"""闭环二会话级测试：interactive_session 的 v4 画像领域路由接入（成对替换）。

仅覆盖诊断环节（前测出题与提交路由）；微课/任务/追问链路由既有冻结测试与
闭环验收剧本覆盖。manager 以 persona_routing=True 构造（生产口径），
对照测试默认 v3 通道不动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents.persona_router import load_persona_pretest
from orchestrator.interactive_session import InteractiveSessionManager, _payload_content


def forbidden_llm(**_: Any) -> Any:
    raise AssertionError("v4 诊断路径不得调用 LLM（叙事走降级通道）")


class _IdleExecutor:
    """create_session 会立即构造执行器；诊断环节不应真正执行查询。"""

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("诊断环节不应执行数据库查询")


SCOPE = {
    "planner_new": {
        "三道工序与传导关系", "计划量与实际量口径", "传导时滞分析",
        "异常衰减规律", "责任单元定位", "跨工序归因方法",
    },
    "craft_engineer": {
        "计划量与实际量口径", "完成率计算", "偏差率与风险等级",
        "月度聚合方法", "异常识别标准", "责任单元定位", "跨工序归因方法",
    },
    "line_leader": {
        "计划量与实际量口径", "完成率计算", "偏差率与风险等级",
        "异常识别标准", "责任单元定位",
    },
}


def make_manager(tmp_path: Path, *, experience_tags: tuple[str, ...] = ()) -> InteractiveSessionManager:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=forbidden_llm,
        executor_factory=_IdleExecutor,
        persona_routing=True,
    )
    return manager


def answers_for(profile_id: str, *, wrong: set[str]) -> dict[str, str]:
    pretest = load_persona_pretest(profile_id)
    answers: dict[str, str] = {}
    for question in pretest.questions:
        point = str(question["knowledge_point"])
        answers[str(question["question_id"])] = (
            next(o for o in question["options"] if o != question["answer"])
            if point in wrong
            else str(question["answer"])
        )
    return answers


def probe_gold(probe: dict) -> str:
    from agents.persona_router import PROBE_PATH, _load_json

    for item in _load_json(PROBE_PATH):
        if str(item.get("probe_id")) == str(probe["probe_id"]):
            return str(item.get("gold_answer", ""))
    raise AssertionError(f"probe {probe['probe_id']} not found")


def submit(tmp_path: Path, profile_id: str, *, wrong: set[str], tags: tuple[str, ...] = ()):
    manager = make_manager(tmp_path)
    created = manager.create_session(profile_id, experience_tags=tags)
    session_id = created["session_id"]
    state = manager.submit_pretest(session_id, answers_for(profile_id, wrong=wrong))
    content = _payload_content(manager._get_session(session_id).diagnosis)
    return manager, session_id, state, content


class TestPersonaPretestServing:
    @pytest.mark.parametrize(
        "profile_id,expected_count",
        [("planner_new", 6), ("craft_engineer", 7), ("line_leader", 5)],
    )
    def test_serves_domain_sized_pretest_per_profile(self, tmp_path, profile_id, expected_count):
        manager = make_manager(tmp_path)
        created = manager.create_session(profile_id)
        questions = manager.get_pretest(created["session_id"])
        assert len(questions) == expected_count
        points = {str(question["knowledge_point"]) for question in questions}
        assert points <= SCOPE[profile_id]

    def test_serving_rejects_v3_style_five_question_ids(self, tmp_path):
        manager = make_manager(tmp_path)
        created = manager.create_session("line_leader")
        session_id = created["session_id"]
        manager.get_pretest(session_id)
        with pytest.raises(Exception):
            manager.submit_pretest(session_id, {f"PT-{index}": "D" for index in range(1, 6)})


class TestPersonaRouting:
    def test_learning_contract_uses_selected_plan_item_not_later_blind_spot(
        self, tmp_path
    ):
        """The contract, evidence bundle and visible task must name one point.

        A line leader can miss the last profile question while the production
        plan deliberately begins with a verified prerequisite.  The historic
        contract constructor still preferred ``blind_spots[0]`` and therefore
        claimed the later blind spot even though the current task trained the
        selected first plan item.
        """

        manager, session_id, state, content = submit(
            tmp_path,
            "line_leader",
            wrong={"责任单元定位"},
        )

        assert content["selected_knowledge_point"] == "计划量与实际量口径"
        assert state["learning_contract"]["target_knowledge_points"] == [
            "计划量与实际量口径"
        ]

    def test_plan_is_domain_scoped_with_point_level_difficulty(self, tmp_path):
        manager, session_id, state, content = submit(
            tmp_path, "line_leader", wrong={"计划量与实际量口径"}
        )
        assert content["router_version"].startswith("persona-router")
        plan = content["knowledge_point_plan"]
        assert {item["knowledge_point"] for item in plan} == SCOPE["line_leader"]
        by_point = {item["knowledge_point"]: item for item in plan}
        assert by_point["计划量与实际量口径"]["initial_difficulty"] == "basic"
        assert by_point["完成率计算"]["initial_difficulty"] == "applied"
        assert content["blind_spots"] == ["计划量与实际量口径"]
        assert content["selected_knowledge_point"] == "计划量与实际量口径"
        assert content["selected_difficulty"] == "basic"
        assert state["awaiting"] == "advance"
        # v4 不读预设盲区：内容里不出现画像先验字段
        assert "gaps_prior" not in content

    def test_all_correct_yields_applied_starts_and_empty_blind_spots(self, tmp_path):
        _manager, _session_id, state, content = submit(tmp_path, "craft_engineer", wrong=set())
        assert content["blind_spots"] == []
        assert all(
            item["initial_difficulty"] == "applied"
            for item in content["knowledge_point_plan"]
        )
        assert all(
            item["mastery_status"] == "pending_training"
            for item in content["knowledge_point_plan"]
        )

    def test_cross_domain_prerequisites_clamped_and_annotated(self, tmp_path):
        _manager, _session_id, state, content = submit(
            tmp_path, "craft_engineer", wrong={"完成率计算", "跨工序归因方法"}
        )
        assert "三道工序与传导关系" in content["clamped_prerequisites"]
        assert "传导时滞分析" in content["clamped_prerequisites"]
        assert "三道工序与传导关系" not in {
            item["knowledge_point"] for item in content["knowledge_point_plan"]
        }

    def test_prerequisite_lift_entry_in_plan_for_danger_combo(self, tmp_path):
        # 蓝图 3.4 危险组合：三道工序答错（完成于应用档），时滞答对（要进阶，
        # KB-007-B 依赖 KB-001-B）→ 计划中三道工序出现标准条目+提升条目
        _manager, _session_id, state, content = submit(
            tmp_path, "planner_new", wrong={"三道工序与传导关系"}
        )
        plan = content["knowledge_point_plan"]
        entries = [item for item in plan if item["knowledge_point"] == "三道工序与传导关系"]
        assert len(entries) == 2
        standard, lift = entries
        assert standard["evidence_source"] == "persona_pretest"
        assert lift["evidence_source"] == "prerequisite_lift"
        assert lift["initial_difficulty"] == "advanced"
        ordered = [item["knowledge_point"] for item in plan]
        assert ordered.index("三道工序与传导关系") < ordered.index("传导时滞分析")
        # 盲区去重：同名条目只显示一次
        assert content["blind_spots"] == ["三道工序与传导关系"]

    def test_focus_tag_orders_point_before_non_prerequisites(self, tmp_path):
        # 闭环三：带关注点提交会先进入校准探针等待态，完成后才终化诊断
        manager = make_manager(tmp_path)
        created = manager.create_session(
            "craft_engineer", experience_tags=("cross_process_root_cause_review",)
        )
        sid = created["session_id"]
        state = manager.submit_pretest(
            sid, answers_for("craft_engineer", wrong={"完成率计算"})
        )
        assert state["awaiting"] == "diagnostic_probe"
        probe = manager.get_diagnostic_probes(sid)[0]
        state = manager.submit_diagnostic_probes(sid, {probe["probe_id"]: probe_gold(probe)})
        content = _payload_content(manager._get_session(sid).diagnosis)
        ordered = [item["knowledge_point"] for item in content["knowledge_point_plan"]]
        focus = next(
            item for item in content["knowledge_point_plan"] if item["tier"] == "focus"
        )
        assert focus["knowledge_point"] == "跨工序归因方法"
        assert ordered.index("跨工序归因方法") < ordered.index("月度聚合方法")
        assert ordered.index("责任单元定位") < ordered.index("跨工序归因方法")


class TestV3ChannelUntouched:
    def test_default_manager_still_serves_frozen_five_question_pretest(self, tmp_path):
        manager = InteractiveSessionManager(
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
            llm_call=forbidden_llm,
            executor_factory=_IdleExecutor,
        )
        created = manager.create_session("line_leader")
        questions = manager.get_pretest(created["session_id"])
        assert len(questions) == 5
        assert [q["question_id"] for q in questions] == [f"PT-{i}" for i in range(1, 6)]


class TestCalibrationProbeFlow:
    FOCUS = "异常衰减规律"
    TAG = "decay_pattern_review"

    def _to_probe_stage(self, tmp_path, wrong):
        manager = make_manager(tmp_path)
        created = manager.create_session("planner_new", experience_tags=(self.TAG,))
        sid = created["session_id"]
        state = manager.submit_pretest(sid, answers_for("planner_new", wrong=wrong))
        return manager, sid, state

    def test_focus_tag_issues_single_applied_calibration_probe(self, tmp_path):
        manager, sid, state = self._to_probe_stage(tmp_path, set())
        assert state["awaiting"] == "diagnostic_probe"
        probes = manager.get_diagnostic_probes(sid)
        assert len(probes) == 1
        assert probes[0]["difficulty"] == "applied"
        assert probes[0]["knowledge_point"] == self.FOCUS
        assert state["interaction"]["title"] == "校准探针"
        assert state["interaction"]["probe_total"] == 1

    def test_quadrant_pretest_correct_probe_correct_confirms_applied(self, tmp_path):
        manager, sid, state = self._to_probe_stage(tmp_path, set())
        probe = manager.get_diagnostic_probes(sid)[0]
        state = manager.submit_diagnostic_probes(sid, {probe["probe_id"]: probe_gold(probe)})
        content = _payload_content(manager._get_session(sid).diagnosis)
        assert state["awaiting"] == "advance"
        assert content["calibration"] == {
            "probe_id": probe["probe_id"], "knowledge_point": self.FOCUS, "is_correct": True,
        }
        focus_item = next(i for i in content["knowledge_point_plan"] if i.get("tier") == "focus")
        assert focus_item["initial_difficulty"] == "applied"
        assert "确认从应用档起步" in focus_item["route_reason"]
        assert any(
            e["evidence_source"] == "calibration_probe" and e["is_correct"]
            for e in content["route_evidence"]
        )

    def test_quadrant_pretest_correct_probe_wrong_downgrades(self, tmp_path):
        manager, sid, _state = self._to_probe_stage(tmp_path, set())
        probe = manager.get_diagnostic_probes(sid)[0]
        manager.submit_diagnostic_probes(sid, {probe["probe_id"]: "完全无关的回答"})
        content = _payload_content(manager._get_session(sid).diagnosis)
        focus_item = next(i for i in content["knowledge_point_plan"] if i.get("tier") == "focus")
        assert focus_item["initial_difficulty"] == "basic"
        assert "降回基础档起步" in focus_item["route_reason"]
        assert content["calibration"]["is_correct"] is False

    def test_quadrant_pretest_wrong_probe_correct_lifts_to_applied(self, tmp_path):
        manager, sid, _state = self._to_probe_stage(tmp_path, {self.FOCUS})
        probe = manager.get_diagnostic_probes(sid)[0]
        manager.submit_diagnostic_probes(sid, {probe["probe_id"]: probe_gold(probe)})
        content = _payload_content(manager._get_session(sid).diagnosis)
        focus_item = next(i for i in content["knowledge_point_plan"] if i.get("tier") == "focus")
        assert focus_item["initial_difficulty"] == "applied"
        assert "跳过基础重学" in focus_item["route_reason"]

    def test_quadrant_pretest_wrong_probe_wrong_keeps_basic(self, tmp_path):
        manager, sid, _state = self._to_probe_stage(tmp_path, {self.FOCUS})
        probe = manager.get_diagnostic_probes(sid)[0]
        manager.submit_diagnostic_probes(sid, {probe["probe_id"]: "错误回答"})
        content = _payload_content(manager._get_session(sid).diagnosis)
        focus_item = next(i for i in content["knowledge_point_plan"] if i.get("tier") == "focus")
        assert focus_item["initial_difficulty"] == "basic"
        assert "维持基础档起步" in focus_item["route_reason"]

    def test_default_auto_diagnosis_skips_probe_entirely(self, tmp_path):
        manager = make_manager(tmp_path)
        created = manager.create_session("planner_new")  # 无关注点
        sid = created["session_id"]
        state = manager.submit_pretest(sid, answers_for("planner_new", wrong={"计划量与实际量口径"}))
        assert state["awaiting"] == "advance"
        content = _payload_content(manager._get_session(sid).diagnosis)
        assert "calibration" not in content
        assert content["diagnostic_probe_count"] == 0

    def test_wrong_answer_keys_rejected(self, tmp_path):
        manager, sid, _state = self._to_probe_stage(tmp_path, set())
        with pytest.raises(Exception):
            manager.submit_diagnostic_probes(sid, {"DP-99-B": "任意"})


class TestLectureDeferral:
    """闭环四：前测答对点微课懒生成（谓词与延期草稿形态）。"""

    def _session_with(self, tmp_path, wrong):
        manager = make_manager(tmp_path)
        created = manager.create_session("planner_new")
        sid = created["session_id"]
        manager.submit_pretest(sid, answers_for("planner_new", wrong=wrong))
        session = manager._get_session(sid)
        return manager, session

    def test_correct_tier_point_deferred(self, tmp_path):
        manager, session = self._session_with(tmp_path, wrong={"计划量与实际量口径"})
        # 口径答错（wrong 档）→ 不延期；三道工序答对（correct 档）→ 延期
        assert manager._lecture_deferral_applies(session, "三道工序与传导关系") is True
        assert manager._lecture_deferral_applies(session, "计划量与实际量口径") is False

    def test_v3_channel_never_defers(self, tmp_path):
        manager = InteractiveSessionManager(
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
            llm_call=forbidden_llm,
            executor_factory=_IdleExecutor,
        )
        created = manager.create_session("planner_new")
        sid = created["session_id"]
        # v3 走冻结五题：全部答错口径即可（PT-1:D 在多数题为错项）
        manager.submit_pretest(sid, {"PT-1": "D", "PT-2": "D", "PT-3": "D", "PT-4": "D", "PT-5": "D"})
        session = manager._get_session(sid)
        assert manager._lecture_deferral_applies(session, "三道工序与传导关系") is False

    def test_remediation_round_disables_deferral(self, tmp_path):
        manager, session = self._session_with(tmp_path, wrong={"计划量与实际量口径"})
        session.remediation_context = {
            "knowledge_point": "三道工序与传导关系",
            "action": "step_down",
        }
        assert manager._lecture_deferral_applies(session, "三道工序与传导关系") is False
        # 其他点的判定不受影响
        assert manager._lecture_deferral_applies(session, "传导时滞分析") is True

    def test_prerequisite_lift_entry_not_deferred(self, tmp_path):
        # 危险组合：三道工序答错 → 标准（wrong）条目 + 提升条目，均不延期
        manager, session = self._session_with(tmp_path, wrong={"三道工序与传导关系"})
        assert manager._lecture_deferral_applies(session, "三道工序与传导关系") is False

    def test_deferred_draft_envelope_and_content(self, tmp_path):
        manager, session = self._session_with(tmp_path, wrong=set())
        draft = manager._deferred_lecture_draft(
            session, "三道工序与传导关系", "applied"
        )
        assert draft["agent"] == "knowledge"
        assert draft["role"] == "produce"
        assert draft["payload"]["type"] == "lecture_note"
        content = draft["payload"]["content"]
        assert content["event"] == "product_ready"
        assert content["lecture_md"] == ""
        assert content["lecture_deferred"] is True
        assert content["deferred_reason"] == "pretest_verified"
        assert "直入实操" in content["deferred_notice"]
        assert content["knowledge_point"] == "三道工序与传导关系"
        assert content["difficulty"] == "applied"


class TestStepUpCompletionLine:
    """闭环二遗留核对项（蓝图 3.6）：降阶爬升完成线锚定前测初始档+1。"""

    def _mk_session(self, tmp_path, *, initial, current, kp="三道工序与传导关系"):
        manager = make_manager(tmp_path)
        created = manager.create_session("planner_new")
        sid = created["session_id"]
        session = manager._get_session(sid)
        from orchestrator.interactive_session import _payload_content

        def msg(difficulty, extra=None):
            return {
                "trace_id": session.runtime.options.trace_id,
                "agent": "task",
                "role": "produce",
                "payload": {
                    "type": "practice_guide",
                    "content": {
                        "event": "product_ready",
                        "knowledge_point": kp,
                        "difficulty": difficulty,
                        **(extra or {}),
                    },
                },
            }

        session.diagnosis = {
            "payload": {
                "type": "profile_assessment",
                "content": {
                    "knowledge_point_plan": [
                        {"knowledge_point": kp, "initial_difficulty": initial}
                    ],
                    "difficulty": initial,
                },
            }
        }
        session.learning_task = msg(current)
        return manager, session

    def test_applied_start_downgraded_to_basic_not_reached(self, tmp_path):
        # 前测对（applied 起步）降档后当前在 basic → 未达线（需 advanced）
        _manager, session = self._mk_session(tmp_path, initial="applied", current="basic")
        assert not _manager_step_up_reached(session)

    def test_applied_start_back_to_applied_not_reached(self, tmp_path):
        # 爬回 applied 仍未达线（完成线是 advanced）
        _manager, session = self._mk_session(tmp_path, initial="applied", current="applied")
        assert not _manager_step_up_reached(session)

    def test_applied_start_reached_at_advanced(self, tmp_path):
        _manager, session = self._mk_session(tmp_path, initial="applied", current="advanced")
        assert _manager_step_up_reached(session)

    def test_basic_start_reached_at_applied(self, tmp_path):
        # 前测错（basic 起步）完成线 applied：basic 未达、applied 达线
        _manager, session = self._mk_session(tmp_path, initial="basic", current="basic")
        assert not _manager_step_up_reached(session)
        _m2, s2 = self._mk_session(tmp_path, initial="basic", current="applied")
        assert _manager_step_up_reached(s2)

    def test_advanced_start_reached_immediately(self, tmp_path):
        _manager, session = self._mk_session(tmp_path, initial="advanced", current="advanced")
        assert _manager_step_up_reached(session)

    def test_v3_channel_always_reached(self, tmp_path):
        # v3 冻结通道不启用该校验（行为不变）
        manager = InteractiveSessionManager(
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
            llm_call=forbidden_llm,
            executor_factory=_IdleExecutor,
        )
        created = manager.create_session("planner_new")
        session = manager._get_session(created["session_id"])
        assert manager._reached_initial_step_up(session) is True


def _manager_step_up_reached(session) -> bool:
    from orchestrator.interactive_session import InteractiveSessionManager

    fake = InteractiveSessionManager.__new__(InteractiveSessionManager)
    fake._persona_routing = True
    return InteractiveSessionManager._reached_initial_step_up(fake, session)
