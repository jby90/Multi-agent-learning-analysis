from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.task_agent import load_task_catalog
from eval.test_demo_session import ScriptedLLM
from eval.test_interactive_session import (
    ALL_CORRECT,
    CatalogExecutor,
    RecordingExecutor,
    forbidden_llm,
)
from orchestrator.interactive_session import InteractiveSessionManager


SOURCE_ROOT = Path(__file__).resolve().parents[1]
TAG_CONFIG = SOURCE_ROOT / "config" / "diagnostic_experience_tags_v3.json"
ROUTE_CASES = tuple(
    (item["tag_id"], item["knowledge_point"])
    for item in json.loads(TAG_CONFIG.read_text(encoding="utf-8"))["tags"]
)


@pytest.mark.parametrize(("experience_tag", "expected_point"), ROUTE_CASES)
def test_every_knowledge_point_is_reachable_from_the_real_session_entry(
    tmp_path: Path,
    experience_tag: str,
    expected_point: str,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / experience_tag / "traces",
        cache_dir=tmp_path / experience_tag / "cache",
        llm_call=forbidden_llm,
        executor_factory=RecordingExecutor,
    )
    created = manager.create_session(
        "planner_new",
        experience_tags=(experience_tag,),
    )
    session_id = created["session_id"]

    pending = manager.submit_pretest(session_id, ALL_CORRECT)
    probes = manager.get_diagnostic_probes(session_id)

    assert pending["awaiting"] == "diagnostic_probe"
    assert probes[0]["knowledge_point"] == expected_point
    assert pending["interaction"]["provisional_route"]["knowledge_point"] == expected_point
    assert pending["interaction"]["provisional_route"]["evidence_source"] == (
        "profile_experience"
    )

    routed = manager.submit_diagnostic_probes(
        session_id,
        {probes[0]["probe_id"]: "不知道"},
    )
    content = routed["artifact"]["payload"]["content"]

    assert routed["awaiting"] == "advance"
    assert content["selected_knowledge_point"] == expected_point
    assert content["knowledge_point_plan"][0]["evidence_source"] == "diagnostic_probe"


def test_ui_route_catalog_covers_exactly_the_ten_core_points() -> None:
    points = [point for _, point in ROUTE_CASES]

    assert len(points) == 10
    assert len(set(points)) == 10


@pytest.mark.parametrize(("experience_tag", "expected_point"), ROUTE_CASES)
def test_every_ui_route_reaches_reviewed_resources_and_safe_sql_execution(
    tmp_path: Path,
    experience_tag: str,
    expected_point: str,
) -> None:
    manager = InteractiveSessionManager(
        trace_dir=tmp_path / "functional" / experience_tag / "traces",
        cache_dir=tmp_path / "functional" / experience_tag / "cache",
        llm_call=ScriptedLLM(),
        executor_factory=CatalogExecutor,
    )
    session_id = manager.create_session(
        "planner_new",
        experience_tags=(experience_tag,),
    )["session_id"]
    manager.submit_pretest(session_id, ALL_CORRECT)
    probe = manager.get_diagnostic_probes(session_id)[0]
    manager.submit_diagnostic_probes(
        session_id,
        {probe["probe_id"]: "不知道"},
    )

    lecture = manager.advance(session_id)
    task = manager.advance(session_id)
    task_content = task["artifact"]["payload"]["content"]
    template = load_task_catalog().templates[task_content["template_id"]]
    result = manager.submit_sql(session_id, template.standard_sql)

    assert lecture["state"] == "S3_TASK"
    assert lecture["artifact"]["payload"]["content"]["knowledge_point"] == expected_point
    assert task["state"] == "S7_STUDENT"
    assert task["awaiting"] == "sql"
    assert task_content["knowledge_point"] == expected_point
    assert result["state"] != "S_FAIL"
    assert any(
        message["payload"]["type"] == "sql_result"
        for message in result["messages"]
    )
