from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from agents.follow_up_agent import FollowUpAgent
from agents.kb_loader import require_valid_chunks
from agents.knowledge_scope import CHUNK_DIRECTORY
from agents.misconception_relations import (
    MisconceptionRelation,
    RelationIntegrityError,
    RoutingPolicy,
    build_relation_index,
    default_relation_support_points,
    rank_relation_routes,
    select_relation_route,
)
from agents.task_agent import load_task_catalog
from agents.rebuttal_generator import RebuttalGenerator
from agents.review_agent import ReviewAgent
from eval.test_demo_session import ScriptedLLM
from eval.test_follow_up_agent import (
    FollowUpLLM,
    _current_task,
    _task_agent,
)
from eval.test_interactive_session import CatalogExecutor
from eval.test_p5_interactive_follow_up import FollowUpScript, _response
from orchestrator import interactive_session as interactive_session_module
from orchestrator.interactive_session import (
    InteractiveSessionError,
    InteractiveSessionManager,
)


PRODUCTION_IDS = ("M-01", "M-02", "M-03", "M-04", "M-05")
PRODUCTION_POINTS = (
    "计划量与实际量口径",
    "完成率计算",
    "三道工序与传导关系",
    "跨工序归因方法",
    "异常识别",
)
T02_SCOPE = ("完成率计算", "计划量与实际量口径")


def _review_verdict(
    product: Mapping[str, Any],
    decision: str,
    *,
    role: str = "verdict",
    rule_id: str = "R-02",
) -> dict[str, Any]:
    reason = "固定桩审核理由，不得进入学员界面。"
    return {
        "trace_id": product["trace_id"],
        "agent": "review",
        "role": role,
        "payload": {
            "type": "review_verdict",
            "content": {
                "event": "review_complete",
                "reviewed_payload_type": product["payload"]["type"],
                "reviewed_msg_id": product["msg_id"],
            },
        },
        "evidence": (
            []
            if decision == "approve"
            else [{"kind": "review_rule", "ref": rule_id, "quote": reason}]
        ),
        "claims": [],
        "verdict": {
            "decision": decision,
            "rule_hits": (
                []
                if decision == "approve"
                else [
                    {
                        "rule_id": rule_id,
                        "reason": reason,
                        "evidence_ref": product["msg_id"],
                    }
                ]
            ),
            "difficulty_action": "none",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _production_index() -> Any:
    return build_relation_index(
        require_valid_chunks(CHUNK_DIRECTORY),
        PRODUCTION_IDS,
    )


def _route(
    source: str,
    *,
    policy: str,
    probed: tuple[str, ...],
    covered: tuple[str, ...],
) -> Any:
    return select_relation_route(
        source,
        responsibility_scope=T02_SCOPE,
        probed=probed,
        covered_relation_points=covered,
        relation_index=_production_index(),
        allowed_ids=PRODUCTION_IDS,
        allowed_support_points=PRODUCTION_POINTS,
        policy=RoutingPolicy(policy),
    )


def test_real_reachable_history_creates_the_expected_first_strategy_divergence() -> None:
    b0_first = _route(
        "M-01",
        policy="total_support",
        probed=("M-01",),
        covered=(),
    )
    b1_first = _route(
        "M-01",
        policy="marginal_support",
        probed=("M-01",),
        covered=(),
    )
    assert b0_first == b1_first
    assert b1_first is not None
    assert b1_first.target == "M-04"
    assert b1_first.route_support_points == (
        "计划量与实际量口径",
        "完成率计算",
    )

    approved_covered = b1_first.route_support_points
    b0_divergence = _route(
        "M-04",
        policy="total_support",
        probed=("M-01", "M-04"),
        covered=approved_covered,
    )
    b1_divergence = _route(
        "M-04",
        policy="marginal_support",
        probed=("M-01", "M-04"),
        covered=approved_covered,
    )

    assert b0_divergence is not None
    assert b0_divergence.target == "M-05"
    assert b0_divergence.route_support_points == ("完成率计算",)
    assert b0_divergence.marginal_support_points == ()
    assert b1_divergence is None


def test_marginal_routes_use_gain_then_existing_support_then_domain_order() -> None:
    index = {
        "M-01": (
            MisconceptionRelation("M-05", 1, 5, ("完成率计算",)),
            MisconceptionRelation(
                "M-04",
                2,
                4,
                ("计划量与实际量口径", "完成率计算"),
            ),
        ),
        "M-02": (),
        "M-03": (),
        "M-04": (),
        "M-05": (),
    }
    routes = rank_relation_routes(
        "M-01",
        responsibility_scope=T02_SCOPE,
        probed=("M-01",),
        covered_relation_points=("计划量与实际量口径",),
        relation_index=index,
        allowed_ids=PRODUCTION_IDS,
        allowed_support_points=PRODUCTION_POINTS,
        policy=RoutingPolicy("marginal_support"),
    )

    assert [route.target for route in routes] == ["M-04", "M-05"]
    assert [route.marginal_gain for route in routes] == [1, 1]
    assert routes == rank_relation_routes(
        "M-01",
        responsibility_scope=T02_SCOPE,
        probed=("M-01",),
        covered_relation_points=("计划量与实际量口径",),
        relation_index=index,
        allowed_ids=PRODUCTION_IDS,
        allowed_support_points=PRODUCTION_POINTS,
        policy=RoutingPolicy("marginal_support"),
    )


def test_default_support_vocabulary_is_independent_and_domain_scoped() -> None:
    assert set(default_relation_support_points(PRODUCTION_IDS)) == {
        "计划量与实际量口径",
        "完成率计算",
        "三道工序与传导关系",
        "跨工序归因方法",
    }
    assert default_relation_support_points(("M-FS01",)) == ()


@pytest.mark.parametrize(
    ("relation_index", "covered"),
    (
        (
            {
                "M-01": (
                    MisconceptionRelation("M-X", 1, 1, ("完成率计算",)),
                ),
                "M-02": (),
                "M-03": (),
                "M-04": (),
                "M-05": (),
            },
            (),
        ),
        (
            {
                "M-01": (
                    MisconceptionRelation("M-04", 1, 1, ("伪造支持点",)),
                ),
                "M-02": (),
                "M-03": (),
                "M-04": (),
                "M-05": (),
            },
            (),
        ),
        (_production_index(), ("跨域覆盖点",)),
    ),
)
def test_relation_integrity_errors_are_not_silently_treated_as_zero_gain(
    relation_index: Any,
    covered: tuple[str, ...],
) -> None:
    with pytest.raises(RelationIntegrityError):
        select_relation_route(
            "M-01",
            responsibility_scope=T02_SCOPE,
            probed=("M-01",),
            covered_relation_points=covered,
            relation_index=relation_index,
            allowed_ids=PRODUCTION_IDS,
            allowed_support_points=PRODUCTION_POINTS,
            policy=RoutingPolicy("marginal_support"),
        )


def test_single_node_domain_is_a_legal_empty_relation_not_an_error() -> None:
    route = select_relation_route(
        "M-FS01",
        responsibility_scope=("首件进度口径",),
        probed=("M-FS01",),
        covered_relation_points=(),
        relation_index={"M-FS01": ()},
        allowed_ids=("M-FS01",),
        allowed_support_points=("首件进度口径",),
        policy=RoutingPolicy("marginal_support"),
    )
    assert route is None


def test_follow_up_agent_b0_and_b1_share_inputs_but_diverge_only_on_zero_gain() -> None:
    task_agent = _task_agent()
    current_task = _current_task(task_agent, "T-02")
    covered = ("计划量与实际量口径", "完成率计算")
    common = {
        "student_answer": "我仍然无法区分两种完成率口径。",
        "current_task": current_task,
        "task_agent": task_agent,
        "round_index": 4,
        "max_rounds": 4,
        "probed_misconceptions": ("M-01", "M-04"),
        "covered_relation_points": covered,
    }
    b0_llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-04",
            "next_target_misconception": "M-05",
            "question": "面对同一组进度数据，你会先核对哪个完成率口径？",
        }
    )
    b1_llm = FollowUpLLM(
        {
            "assessment": "needs_support",
            "diagnosed_misconception": "M-04",
            "next_target_misconception": "M-04",
            "question": "面对同一组进度数据，你会先核对哪个完成率口径？",
        }
    )

    b0 = FollowUpAgent("trace-production-progress", llm_call=b0_llm).generate(
        **common,
        routing_policy=RoutingPolicy("total_support"),
    )
    b1 = FollowUpAgent("trace-production-progress", llm_call=b1_llm).generate(
        **common,
        routing_policy=RoutingPolicy("marginal_support"),
    )

    assert b0.next_target_misconception == "M-05"
    assert b0.route_support_points == ("完成率计算",)
    assert b1.next_target_misconception == "M-04"
    assert b1.route_support_points == ()
    assert b0_llm.calls[0]["json_schema"] == b1_llm.calls[0]["json_schema"]


def test_first_diagnosis_confirms_the_current_misconception_before_routing() -> None:
    task_agent = _task_agent()
    current_task = _current_task(task_agent, "T-02")
    turn = FollowUpAgent(
        "trace-first-diagnosis",
        llm_call=FollowUpLLM(
            {
                "assessment": "needs_support",
                "diagnosed_misconception": "M-01",
                "next_target_misconception": "M-01",
                "question": "你会先核对计划量还是实际完成量？",
            }
        ),
    ).generate(
        student_answer="计划量就是已经完成的数量。",
        current_task=current_task,
        task_agent=task_agent,
        round_index=2,
        max_rounds=4,
        probed_misconceptions=(),
        covered_relation_points=(),
        routing_policy=RoutingPolicy("marginal_support"),
    )

    assert turn.next_target_misconception == "M-01"
    assert turn.route_support_points == ()


def test_second_domain_b0_b1_are_observationally_equivalent_and_empty() -> None:
    task_agent = _task_agent("first_segment")
    current_task = _current_task(task_agent, "T-FS02")
    response = {
        "assessment": "needs_support",
        "diagnosed_misconception": "M-FS01",
        "next_target_misconception": "M-FS01",
        "question": "比较当日实际数时，你会先确认哪个业务口径？",
    }
    turns = []
    for mode in ("total_support", "marginal_support"):
        turn = FollowUpAgent(
            "trace-first-segment",
            llm_call=FollowUpLLM(response),
        ).generate(
            student_answer="我把两个口径混在一起了。",
            current_task=current_task,
            task_agent=task_agent,
            round_index=3,
            max_rounds=4,
            probed_misconceptions=("M-FS01",),
            covered_relation_points=(),
            routing_policy=RoutingPolicy(mode),
        )
        turns.append(turn)

    assert turns[0].next_target_misconception == turns[1].next_target_misconception
    assert turns[0].route_support_points == turns[1].route_support_points == ()
    assert turns[0].product is not None and turns[1].product is not None
    assert turns[0].product["payload"] == turns[1].product["payload"]
    assert turns[0].product["evidence"] == turns[1].product["evidence"]
    assert turns[0].product["probe"] == turns[1].product["probe"]


def _start_interactive(
    tmp_path: Path,
    follow_up: FollowUpScript,
    policy: RoutingPolicy,
) -> tuple[InteractiveSessionManager, str]:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "traces",
        cache_dir=tmp_path / "cache",
        llm_call=ScriptedLLM(),
        follow_up_llm_call=follow_up,
        executor_factory=CatalogExecutor,
        routing_policy=policy,
    )
    session_id = manager.create_session("line_leader")["session_id"]
    manager.submit_pretest(
        session_id,
        {f"PT-{index}": "D" for index in range(1, 6)},
    )
    manager.advance(session_id)
    task = manager.advance(session_id)
    template_id = task["artifact"]["payload"]["content"]["template_id"]
    manager.submit_sql(
        session_id,
        load_task_catalog().templates[template_id].standard_sql,
    )
    manager.advance(session_id)
    return manager, session_id


def test_approved_relation_question_commits_coverage_once_and_never_exposes_it(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript(
        _response("needs_support", "你会先核对计划量还是实际完成量？"),
        _response(
            "needs_support",
            "换一个汇总层级后，你会怎样核对计划与实际口径？",
            target="M-01",
            next_target="M-04",
        ),
    )
    manager, session_id = _start_interactive(
        tmp_path,
        follow_up,
        RoutingPolicy("marginal_support"),
    )
    first = manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "b-approved-1",
    )
    assert first["awaiting"] == "follow_up"
    assert manager._get_session(session_id).covered_relation_points == set()

    second = manager.submit_follow_up(
        session_id,
        "我还是把计划量当成完成量。",
        "b-approved-2",
    )
    internal = manager._get_session(session_id)
    assert internal.covered_relation_points == {"计划量与实际量口径"}
    duplicate = manager.submit_follow_up(
        session_id,
        "这次重放不应更新任何覆盖。",
        "b-approved-2",
    )
    assert internal.covered_relation_points == {"计划量与实际量口径"}
    assert duplicate == second
    public = str(second)
    for forbidden in (
        "covered_relation_points",
        "route_support_points",
        "marginal_support",
        "RelationIntegrityError",
    ):
        assert forbidden not in public

    assert internal.learning_task is not None
    manager._start_follow_up(internal, internal.learning_task)
    assert internal.probed_misconceptions == set()
    assert internal.covered_relation_points == set()
    assert internal.processed_turn_ids == set()


def test_final_commit_preparation_failure_preserves_all_six_session_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    follow_up = FollowUpScript(
        _response("needs_support", "你会先核对计划量还是实际完成量？"),
        _response(
            "needs_support",
            "换一个汇总层级后，你会怎样核对计划与实际口径？",
            target="M-01",
            next_target="M-04",
        ),
    )
    manager, session_id = _start_interactive(
        tmp_path,
        follow_up,
        RoutingPolicy("marginal_support"),
    )
    manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "b-prepare-1",
    )
    internal = manager._get_session(session_id)
    before = {
        "question": internal.follow_up_question,
        "artifact": deepcopy(internal.artifact),
        "interaction": deepcopy(internal.interaction),
        "probed": set(internal.probed_misconceptions),
        "covered": set(internal.covered_relation_points),
        "processed": set(internal.processed_turn_ids),
    }
    monkeypatch.setattr(
        interactive_session_module,
        "contains_engineering_text",
        lambda _: True,
    )

    with pytest.raises(InteractiveSessionError, match="内容暂时无法继续生成"):
        manager.submit_follow_up(
            session_id,
            "我还是把计划量当成完成量。",
            "b-prepare-2",
        )

    assert internal.follow_up_question == before["question"]
    assert internal.artifact == before["artifact"]
    assert internal.interaction == before["interaction"]
    assert internal.probed_misconceptions == before["probed"]
    assert internal.covered_relation_points == before["covered"]
    assert internal.processed_turn_ids == before["processed"]


def test_invalid_coverage_retains_the_last_approved_probe_idempotently(
    tmp_path: Path,
) -> None:
    follow_up = FollowUpScript(
        _response("needs_support", "你会先核对计划量还是实际完成量？"),
    )
    manager, session_id = _start_interactive(
        tmp_path,
        follow_up,
        RoutingPolicy("marginal_support"),
    )
    manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "b-error-1",
    )
    internal = manager._get_session(session_id)
    internal.covered_relation_points.add("跨域覆盖点")

    failed = manager.submit_follow_up(
        session_id,
        "我还是把计划量当成完成量。",
        "b-error-2",
    )
    replay = manager.submit_follow_up(
        session_id,
        "重放不得再次生成。",
        "b-error-2",
    )

    assert failed["awaiting"] == "follow_up"
    assert failed["outcome"] is None
    assert failed["interaction"]["retry_required"] is True
    assert replay == failed
    assert internal.covered_relation_points == set()
    assert len(follow_up.calls) == 1


def test_rejected_relation_drafts_never_commit_probe_or_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    follow_up = FollowUpScript(
        _response("needs_support", "你会先核对计划量还是实际完成量？"),
        *(
            _response(
                "needs_support",
                f"{label}版未批准关系追问？",
                target="M-01",
                next_target="M-04",
            )
            for label in ("第一", "第二", "第三", "第四")
        ),
    )
    original_review = ReviewAgent.review
    follow_up_reviews = 0

    def reject_relation_candidates(
        self: ReviewAgent,
        product: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal follow_up_reviews
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_review(self, product, **kwargs)
        follow_up_reviews += 1
        if follow_up_reviews == 1:
            return _review_verdict(product, "approve")
        return _review_verdict(product, "reject", rule_id="R-04")

    monkeypatch.setattr(ReviewAgent, "review", reject_relation_candidates)
    manager, session_id = _start_interactive(
        tmp_path,
        follow_up,
        RoutingPolicy("marginal_support"),
    )
    manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "b-reject-1",
    )
    failed = manager.submit_follow_up(
        session_id,
        "我还是把计划量当作完成量。",
        "b-reject-2",
    )
    internal = manager._get_session(session_id)

    assert failed["awaiting"] == "follow_up"
    assert failed["outcome"] is None
    assert failed["interaction"]["retry_required"] is True
    assert internal.probed_misconceptions == {"M-01"}
    assert internal.covered_relation_points == set()
    assert "M-04" not in internal.probed_misconceptions


def test_interrupted_selective_rebuttal_commits_no_relation_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    follow_up = FollowUpScript(
        _response("needs_support", "你会先核对计划量还是实际完成量？"),
        _response(
            "needs_support",
            "换一个汇总层级后，你会怎样核对计划与实际口径？",
            target="M-01",
            next_target="M-04",
        ),
    )
    original_review = ReviewAgent.review
    original_generate = RebuttalGenerator.generate
    follow_up_reviews = 0

    def interrupt_after_soft_rejection(
        self: ReviewAgent,
        product: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal follow_up_reviews
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_review(self, product, **kwargs)
        follow_up_reviews += 1
        return _review_verdict(
            product,
            "approve" if follow_up_reviews == 1 else "reject",
            rule_id="R-02",
        )

    def unavailable_rebuttal(
        self: RebuttalGenerator,
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        if product["payload"]["content"].get("event") == "follow_up_question_ready":
            raise RuntimeError("fixed interruption stub")
        return original_generate(self, product, verdict)

    monkeypatch.setattr(ReviewAgent, "review", interrupt_after_soft_rejection)
    monkeypatch.setattr(RebuttalGenerator, "generate", unavailable_rebuttal)
    manager, session_id = _start_interactive(
        tmp_path,
        follow_up,
        RoutingPolicy("marginal_support"),
    )
    manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "b-interrupted-1",
    )
    interrupted = manager.submit_follow_up(
        session_id,
        "我还是把计划量当成完成量。",
        "b-interrupted-2",
    )
    replay = manager.submit_follow_up(
        session_id,
        "重放不得再次生成。",
        "b-interrupted-2",
    )
    internal = manager._get_session(session_id)

    assert interrupted["awaiting"] == "follow_up"
    assert interrupted["outcome"] is None
    assert interrupted["interaction"]["retry_required"] is True
    assert replay == interrupted
    assert internal.probed_misconceptions == {"M-01"}
    assert internal.covered_relation_points == set()
    assert len(follow_up.calls) == 2


def test_a_retries_share_one_b_snapshot_and_only_final_approve_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    follow_up = FollowUpScript(
        _response("needs_support", "你会先核对计划量还是实际完成量？"),
        *(
            _response(
                "needs_support",
                f"{label}版审核约束关系追问？",
                target="M-01",
                next_target="M-04",
            )
            for label in ("第一", "第二", "第三")
        ),
    )
    original_review = ReviewAgent.review
    original_re_review = ReviewAgent.re_review
    original_generate = RebuttalGenerator.generate
    follow_up_reviews = 0
    model_attempts = 0
    covered_snapshots: list[set[str]] = []
    manager_ref: InteractiveSessionManager | None = None
    session_ref = ""

    def review_two_soft_rejections(
        self: ReviewAgent,
        product: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal follow_up_reviews
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_review(self, product, **kwargs)
        follow_up_reviews += 1
        if manager_ref is not None and session_ref:
            covered_snapshots.append(
                set(manager_ref._get_session(session_ref).covered_relation_points)
            )
        if follow_up_reviews in {2, 3}:
            return _review_verdict(product, "reject", rule_id="R-02")
        return _review_verdict(product, "approve")

    def reject_re_review(
        self: ReviewAgent,
        product: Mapping[str, Any],
        original: Mapping[str, Any],
        rebuttal: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        del self, original, rebuttal, kwargs
        return _review_verdict(
            product,
            "reject",
            role="re_verdict",
            rule_id="R-02",
        )

    def one_model_attempt(
        self: RebuttalGenerator,
        product: Mapping[str, Any],
        verdict: Mapping[str, Any],
    ) -> dict[str, Any]:
        nonlocal model_attempts
        if product["payload"]["content"].get("event") != "follow_up_question_ready":
            return original_generate(self, product, verdict)
        model_attempts += 1
        return self.deterministic_concede(product, verdict)

    monkeypatch.setattr(ReviewAgent, "review", review_two_soft_rejections)
    monkeypatch.setattr(ReviewAgent, "re_review", reject_re_review)
    monkeypatch.setattr(RebuttalGenerator, "generate", one_model_attempt)
    manager, session_id = _start_interactive(
        tmp_path,
        follow_up,
        RoutingPolicy("marginal_support"),
    )
    manager_ref = manager
    session_ref = session_id
    manager.submit_follow_up(
        session_id,
        "计划量就是已经完成的数量。",
        "ab-combined-1",
    )
    approved = manager.submit_follow_up(
        session_id,
        "我还是把计划量当作完成量。",
        "ab-combined-2",
    )
    internal = manager._get_session(session_id)

    assert approved["awaiting"] == "follow_up"
    assert model_attempts == 1
    assert covered_snapshots[-3:] == [set(), set(), set()]
    assert internal.covered_relation_points == {"计划量与实际量口径"}
    assert [message["role"] for message in approved["messages"]].count(
        "re_verdict"
    ) == 2
