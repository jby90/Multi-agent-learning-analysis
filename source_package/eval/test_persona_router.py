"""闭环二单测：画像领域路由 v4（三级优先级/拓扑/拉升/钳制/抬前置/点级档位）。

蓝图依据：《个性化培训完整蓝图》3.3/3.4/3.6（2026-08-17 定稿）。
"""

from __future__ import annotations

import pytest

from agents.persona_router import (
    PersonaDiagnosticRouter,
    apply_calibration,
    calibration_probe_for,
    load_persona_pretest,
)


DEPENDENCIES = {
    "三道工序与传导关系": [],
    "计划量与实际量口径": [],
    "完成率计算": ["计划量与实际量口径"],
    "偏差率与风险等级": ["计划量与实际量口径", "完成率计算"],
    "月度聚合方法": ["计划量与实际量口径", "完成率计算"],
    "异常识别标准": ["完成率计算", "偏差率与风险等级"],
    "传导时滞分析": ["三道工序与传导关系"],
    "异常衰减规律": ["三道工序与传导关系", "传导时滞分析", "异常识别标准"],
    "责任单元定位": ["完成率计算", "异常识别标准"],
    "跨工序归因方法": ["三道工序与传导关系", "传导时滞分析", "责任单元定位"],
}

# 知识块分档前置（实测结构的简化投影：B 档普遍依赖 KB-001-B；chunk 前缀≠知识点名）
def _chunk(chunk_id: str, point: str, prereqs: list[str]) -> dict:
    return {"chunk_id": chunk_id, "knowledge_point": point, "prerequisites": prereqs}


CHUNK_RECORDS = [
    _chunk("KB-001", "三道工序与传导关系", []),
    _chunk("KB-001-A", "三道工序与传导关系", ["KB-001"]),
    _chunk("KB-001-B", "三道工序与传导关系", ["KB-001", "KB-001-A"]),
    _chunk("KB-002", "计划量与实际量口径", []),
    _chunk("KB-003", "完成率计算", ["KB-002"]),
    _chunk("KB-003-A", "完成率计算", ["KB-002", "KB-003"]),
    _chunk("KB-007", "传导时滞分析", ["KB-001"]),
    _chunk("KB-007-A", "传导时滞分析", ["KB-007", "KB-001-A"]),
    _chunk("KB-007-B", "传导时滞分析", ["KB-007", "KB-007-A", "KB-001-B"]),
    _chunk("KB-008", "异常衰减规律", ["KB-007"]),
    _chunk("KB-008-B", "异常衰减规律", ["KB-008", "KB-001-B"]),
    _chunk("KB-009", "责任单元定位", ["KB-003"]),
    _chunk("KB-009-B", "责任单元定位", ["KB-009", "KB-001-B"]),
    _chunk("KB-010", "跨工序归因方法", ["KB-009", "KB-007", "KB-008"]),
    _chunk("KB-010-A", "跨工序归因方法", ["KB-010", "KB-007-A", "KB-008-A", "KB-009-A"]),
    _chunk("KB-010-B", "跨工序归因方法", ["KB-010", "KB-010-A", "KB-001-B"]),
]

PROFILES = {
    "planner_new": {
        "knowledge_scope": [
            "三道工序与传导关系", "计划量与实际量口径", "传导时滞分析",
            "异常衰减规律", "责任单元定位", "跨工序归因方法",
        ],
    },
    "craft_engineer": {
        "knowledge_scope": [
            "计划量与实际量口径", "完成率计算", "偏差率与风险等级",
            "月度聚合方法", "异常识别标准", "责任单元定位", "跨工序归因方法",
        ],
    },
    "line_leader": {
        "knowledge_scope": [
            "计划量与实际量口径", "完成率计算", "偏差率与风险等级",
            "异常识别标准", "责任单元定位",
        ],
    },
}


def make_router() -> PersonaDiagnosticRouter:
    return PersonaDiagnosticRouter(PROFILES)


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


def plan_points(result: dict) -> list[str]:
    return [item["knowledge_point"] for item in result["knowledge_point_plan"]]


class TestPretestLoading:
    def test_each_persona_gets_domain_sized_topological_pretest(self):
        for profile_id, size in (("planner_new", 6), ("craft_engineer", 7), ("line_leader", 5)):
            pretest = load_persona_pretest(profile_id)
            points = [str(q["knowledge_point"]) for q in pretest.questions]
            assert len(points) == size
            # 题序 = 依赖拓扑序（域内前置先于依赖点）
            seen: set[str] = set()
            for point in points:
                for prereq in DEPENDENCIES[point]:
                    if prereq in points:
                        assert prereq in seen, f"{profile_id}: {prereq} 应在 {point} 之前"
                seen.add(point)

    def test_answers_must_match_exactly_this_personas_ids(self):
        router = make_router()
        answers = answers_for("planner_new", wrong=set())
        with pytest.raises(ValueError):
            router.build_plan("craft_engineer", answers, dependencies=DEPENDENCIES)


class TestTiersAndDifficulty:
    def test_all_wrong_yields_basic_starts_and_wrong_tier_only(self):
        router = make_router()
        pretest = load_persona_pretest("line_leader")
        all_points = {str(q["knowledge_point"]) for q in pretest.questions}
        result = router.build_plan(
            "line_leader", answers_for("line_leader", wrong=all_points),
            dependencies=DEPENDENCIES,
        )
        plan = result["knowledge_point_plan"]
        assert all(item["initial_difficulty"] == "basic" for item in plan)
        assert all(item["mastery_status"] == "needs_training" for item in plan)
        assert set(result["blind_spots"]) == all_points

    def test_all_correct_yields_applied_starts_and_pending_status(self):
        router = make_router()
        result = router.build_plan(
            "line_leader", answers_for("line_leader", wrong=set()),
            dependencies=DEPENDENCIES,
        )
        plan = result["knowledge_point_plan"]
        assert all(item["initial_difficulty"] == "applied" for item in plan)
        assert all(item["mastery_status"] == "pending_training" for item in plan)
        assert result["blind_spots"] == []

    def test_wrong_tier_precedes_correct_tier(self):
        router = make_router()
        pretest = load_persona_pretest("craft_engineer")
        points = [str(q["knowledge_point"]) for q in pretest.questions]
        wrong = {points[0], points[-1]}
        result = router.build_plan(
            "craft_engineer", answers_for("craft_engineer", wrong=wrong),
            dependencies=DEPENDENCIES,
        )
        tiers = [item["tier"] for item in result["knowledge_point_plan"]]
        assert set(tiers) <= {"correct", "wrong"}
        assert set(result["blind_spots"]) == wrong
        # 跨层级联拉升至结果：归因（wrong）的域内前置闭包全部先于归因；
        # wrong 点互不倒序（口径先于归因）
        ordered = plan_points(result)
        assert ordered.index("计划量与实际量口径") < ordered.index("跨工序归因方法")
        assert ordered.index("责任单元定位") < ordered.index("跨工序归因方法")

    def test_same_tier_topological_order(self):
        router = make_router()
        pretest = load_persona_pretest("planner_new")
        points = {str(q["knowledge_point"]) for q in pretest.questions}
        # 全错 → 全部落②层，层内必须拓扑（完成率→责任单元→归因 类约束不反序）
        result = router.build_plan(
            "planner_new", answers_for("planner_new", wrong=points),
            dependencies=DEPENDENCIES,
        )
        ordered = plan_points(result)
        seen: set[str] = set()
        for point in ordered:
            for prereq in DEPENDENCIES[point]:
                if prereq in points:
                    assert prereq in seen
            seen.add(point)

    def test_cross_tier_prerequisite_pull_up(self):
        router = make_router()
        # planner：归因答错（②层），其前置责任单元/时滞/三道工序答对（③层）
        result = router.build_plan(
            "planner_new",
            answers_for("planner_new", wrong={"跨工序归因方法"}),
            dependencies=DEPENDENCIES,
        )
        ordered = plan_points(result)
        for prereq in ("三道工序与传导关系", "传导时滞分析", "责任单元定位"):
            assert ordered.index(prereq) < ordered.index("跨工序归因方法")

    def test_focus_tag_point_goes_first(self):
        router = make_router()
        result = router.build_plan(
            "craft_engineer",
            answers_for("craft_engineer", wrong={"完成率计算"}),
            dependencies=DEPENDENCIES,
            experience_tag_point="跨工序归因方法",
        )
        plan = result["knowledge_point_plan"]
        focus_item = next(item for item in plan if item["tier"] == "focus")
        assert focus_item["knowledge_point"] == "跨工序归因方法"
        # 级联拉升：归因的整条域内前置闭包（口径→完成率→偏差率→异常识别→责任单元）
        # 都在关注点之前满足；关注点先于非前置闭包的点（聚合）
        ordered = plan_points(result)
        for prereq in (
            "计划量与实际量口径", "完成率计算", "偏差率与风险等级",
            "异常识别标准", "责任单元定位",
        ):
            assert ordered.index(prereq) < ordered.index("跨工序归因方法")
        assert ordered.index("跨工序归因方法") < ordered.index("月度聚合方法")

        # 无域内前置的关注点（口径）恒为第一项
        result2 = router.build_plan(
            "craft_engineer",
            answers_for("craft_engineer", wrong={"完成率计算"}),
            dependencies=DEPENDENCIES,
            experience_tag_point="计划量与实际量口径",
        )
        assert result2["knowledge_point_plan"][0]["tier"] == "focus"

    def test_wrong_focus_point_is_not_duplicated_in_learning_plan(self):
        router = make_router()
        result = router.build_plan(
            "line_leader",
            answers_for("line_leader", wrong={"异常识别标准"}),
            dependencies=DEPENDENCIES,
            experience_tag_point="异常识别标准",
        )

        matching = [
            item for item in result["knowledge_point_plan"]
            if item["knowledge_point"] == "异常识别标准"
        ]
        assert len(matching) == 1
        assert matching[0]["tier"] == "focus"
        assert matching[0]["initial_difficulty"] == "basic"
        assert result["blind_spots"] == ["异常识别标准"]

    def test_plan_only_contains_scope_points(self):
        router = make_router()
        pretest = load_persona_pretest("craft_engineer")
        points = {str(q["knowledge_point"]) for q in pretest.questions}
        result = router.build_plan(
            "craft_engineer", answers_for("craft_engineer", wrong=points),
            dependencies=DEPENDENCIES,
        )
        scope = set(PROFILES["craft_engineer"]["knowledge_scope"])
        assert set(plan_points(result)) <= scope
        # 归因的三道工序/时滞前置被钳制并标注
        assert "三道工序与传导关系" in result["clamped_prerequisites"]
        assert "传导时滞分析" in result["clamped_prerequisites"]


class TestPrerequisiteLift:
    def test_lift_inserted_when_dependent_needs_advanced_prerequisite(self):
        router = make_router()
        # 蓝图 3.4 实测危险组合：三道工序答错（完成于应用档），时滞答对（要进阶，
        # KB-007-B 依赖 KB-001-B）→ 时滞条目前须插入"三道工序@应用档重学"提升条目
        result = router.build_plan(
            "planner_new",
            answers_for("planner_new", wrong={"三道工序与传导关系"}),
            dependencies=DEPENDENCIES,
            chunk_records=CHUNK_RECORDS,
        )
        plan = result["knowledge_point_plan"]
        lifts = [item for item in plan if item["tier"] == "prerequisite_lift"]
        assert len(lifts) == 1
        lift = lifts[0]
        assert lift["knowledge_point"] == "三道工序与传导关系"
        # 直接从所需档位（进阶）起步，不重过已验证的应用档
        assert lift["initial_difficulty"] == "advanced"
        # 提升条目位于首个需要它的依赖点（时滞）之前
        ordered = plan_points(result)
        assert ordered.index("三道工序与传导关系") < ordered.index("传导时滞分析")
        lift_index = next(
            i for i, item in enumerate(plan) if item["tier"] == "prerequisite_lift"
        )
        assert lift_index < next(
            i for i, item in enumerate(plan) if item["knowledge_point"] == "传导时滞分析"
        )

    def test_no_lift_when_prerequisite_already_reaches_required_level(self):
        router = make_router()
        # 三道工序答对 → 应用起步 → 完成于进阶档，无需抬前置
        result = router.build_plan(
            "planner_new",
            answers_for("planner_new", wrong={"传导时滞分析"}),
            dependencies=DEPENDENCIES,
            chunk_records=CHUNK_RECORDS,
        )
        assert not [
            item for item in result["knowledge_point_plan"]
            if item["tier"] == "prerequisite_lift"
        ]

    def test_lift_not_duplicated_for_multiple_dependents(self):
        router = make_router()
        # 多个依赖点都需要 KB-001-B：只插一条提升条目
        result = router.build_plan(
            "planner_new",
            answers_for("planner_new", wrong={"三道工序与传导关系", "传导时滞分析"}),
            dependencies=DEPENDENCIES,
            chunk_records=CHUNK_RECORDS,
        )
        lifts = [
            item for item in result["knowledge_point_plan"]
            if item["tier"] == "prerequisite_lift"
        ]
        assert len(lifts) <= 1

    def test_leader_domain_never_needs_lift(self):
        router = make_router()
        pretest = load_persona_pretest("line_leader")
        points = {str(q["knowledge_point"]) for q in pretest.questions}
        result = router.build_plan(
            "line_leader", answers_for("line_leader", wrong=points),
            dependencies=DEPENDENCIES,
            chunk_records=CHUNK_RECORDS,
        )
        assert not [
            item for item in result["knowledge_point_plan"]
            if item["tier"] == "prerequisite_lift"
        ]


class TestContractShape:
    def test_selected_point_is_first_plan_item(self):
        router = make_router()
        result = router.build_plan(
            "line_leader", answers_for("line_leader", wrong={"计划量与实际量口径"}),
            dependencies=DEPENDENCIES,
        )
        plan = result["knowledge_point_plan"]
        assert result["selected_knowledge_point"] == plan[0]["knowledge_point"]
        assert result["selected_difficulty"] == plan[0]["initial_difficulty"]
        assert result["selected_plan_item_id"] == plan[0]["plan_item_id"]
        assert plan[0]["knowledge_point"] == "计划量与实际量口径"
        assert plan[0]["initial_difficulty"] == "basic"

    def test_route_evidence_covers_every_question(self):
        router = make_router()
        result = router.build_plan(
            "craft_engineer", answers_for("craft_engineer", wrong=set()),
            dependencies=DEPENDENCIES,
        )
        assert len(result["route_evidence"]) == 7
        assert all(item["evidence_source"] == "pretest" for item in result["route_evidence"])

    def test_pretest_knowledge_point_outside_scope_is_rejected(self):
        router = make_router()
        answers = dict(answers_for("line_leader", wrong=set()))
        # line_leader 的答案集拿去 planner（含域外点）应报错
        with pytest.raises(ValueError):
            router.build_plan("planner_new", answers, dependencies=DEPENDENCIES)


class TestCalibrationProbe:
    FOCUS = "异常衰减规律"  # planner 域内、有 DP-03-A 应用档探针

    def _plan(self, pretest_wrong: set[str]) -> dict:
        router = make_router()
        return router.build_plan(
            "planner_new",
            answers_for("planner_new", wrong=pretest_wrong),
            dependencies=DEPENDENCIES,
            experience_tag_point=self.FOCUS,
        )

    def test_every_scope_point_has_an_applied_probe(self):
        for profile_id, profile in PROFILES.items():
            for point in profile["knowledge_scope"]:
                probe = calibration_probe_for(point)
                assert probe is not None, f"{point} 缺应用档探针"
                assert probe["difficulty"] == "applied"

    def test_prefers_applied_calibration_type_when_available(self):
        probe = calibration_probe_for("计划量与实际量口径")
        assert probe["probe_id"] == "AP-01"
        assert probe["probe_type"] == "applied_calibration"

    def _focus_item(self, result: dict) -> dict:
        return next(
            item for item in result["knowledge_point_plan"]
            if item["tier"] == "focus"
        )

    def test_quadrant_correct_probe_confirms_applied_start(self):
        result = self._plan(set())  # 前测全对
        assert self._focus_item(result)["initial_difficulty"] == "applied"
        apply_calibration(result, self.FOCUS, "DP-03-A", True)
        item = self._focus_item(result)
        assert item["initial_difficulty"] == "applied"
        assert "确认从应用档起步" in item["route_reason"]

    def test_quadrant_wrong_probe_downgrades_to_basic(self):
        result = self._plan(set())
        apply_calibration(result, self.FOCUS, "DP-03-A", False)
        item = self._focus_item(result)
        assert item["initial_difficulty"] == "basic"
        assert "降回基础档起步" in item["route_reason"]

    def test_quadrant_pretest_wrong_probe_correct_lifts_to_applied(self):
        result = self._plan({self.FOCUS})  # 关注点前测答错
        assert self._focus_item(result)["initial_difficulty"] == "basic"
        apply_calibration(result, self.FOCUS, "DP-03-A", True)
        item = self._focus_item(result)
        assert item["initial_difficulty"] == "applied"
        assert "跳过基础重学" in item["route_reason"]

    def test_quadrant_both_wrong_keeps_basic(self):
        result = self._plan({self.FOCUS})
        apply_calibration(result, self.FOCUS, "DP-03-A", False)
        item = self._focus_item(result)
        assert item["initial_difficulty"] == "basic"
        assert "维持基础档起步" in item["route_reason"]

    def test_calibration_appends_evidence_and_summary(self):
        result = self._plan(set())
        before = len(result["route_evidence"])
        apply_calibration(result, self.FOCUS, "DP-03-A", True)
        assert len(result["route_evidence"]) == before + 1
        entry = result["route_evidence"][-1]
        assert entry["evidence_source"] == "calibration_probe"
        assert entry["is_correct"] is True
        assert result["calibration"] == {
            "probe_id": "DP-03-A",
            "knowledge_point": self.FOCUS,
            "is_correct": True,
        }
        assert "CALIB:DP-03-A:pass" in self._focus_item(result)["evidence_ids"]
