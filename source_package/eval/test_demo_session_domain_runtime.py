from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agents.domain_config import load_domain_config
from agents.task_agent import TaskAgent, load_task_catalog
from orchestrator import demo_session as demo_session_module
from orchestrator.agents_stub import (
    build_stubs,
    profile_loaded_draft,
    student_answer_draft,
)
from orchestrator.demo_session import DemoOptions, DemoSessionError
from orchestrator.llm import LLMResult


class NoopExecutor:
    pass


def forbidden_llm(**_: Any) -> LLMResult:
    raise AssertionError("domain runtime tests must not call an LLM")


def domain_task_agent(trace_id: str, domain_id: str) -> TaskAgent:
    return TaskAgent(
        trace_id,
        catalog=load_task_catalog(domain_config=load_domain_config(domain_id)),
    )


def runtime(
    tmp_path: Path,
    domain_id: str,
    *,
    trace_id: str,
) -> demo_session_module._DemoRuntime:
    return demo_session_module._DemoRuntime(
        DemoOptions(
            profile_id="planner_new",
            trace_id=trace_id,
            trace_dir=tmp_path / "traces",
            cache_dir=tmp_path / "cache",
        ),
        "live",
        forbidden_llm,
        NoopExecutor,
        task_agent=domain_task_agent(trace_id, domain_id),
    )


def drive_to_probe(
    current: demo_session_module._DemoRuntime,
) -> Any:
    stubs = build_stubs(current.options.trace_id)
    current.transition(profile_loaded_draft(current.options.trace_id), "T01")
    current.transition(stubs.diagnosis.profile_assessment(), "T02")
    lecture = current.transition(stubs.knowledge.lecture(), "T03")
    current.transition(
        stubs.review.verdict("approve", "lecture_note", lecture["msg_id"]),
        "T04",
    )
    task = current.transition(stubs.task.task(), "T09")
    current.transition(
        stubs.review.verdict("approve", "quiz_set", task["msg_id"]),
        "T10",
    )
    current.transition(
        student_answer_draft(current.options.trace_id, "wrong"),
        "T15",
    )
    return stubs


@pytest.mark.parametrize(
    ("domain_id", "known_misconception", "foreign_misconception"),
    (
        ("production_progress", "M-01", "M-FS01"),
        ("first_segment", "M-FS01", "M-01"),
    ),
)
def test_demo_runtime_binds_engine_misconceptions_to_its_task_domain(
    tmp_path: Path,
    domain_id: str,
    known_misconception: str,
    foreign_misconception: str,
) -> None:
    current = runtime(
        tmp_path,
        domain_id,
        trace_id=f"demo-domain-{domain_id}",
    )
    stubs = drive_to_probe(current)

    known = current.engine.send(stubs.diagnosis.probe(known_misconception))

    assert known.bus_result.accepted
    assert not known.transitioned
    assert current.engine.state.value == "S8_PROBE"

    unknown = current.engine.send(stubs.diagnosis.probe(foreign_misconception))

    assert unknown.transitioned
    assert unknown.transition is not None
    assert unknown.transition.transition_id == "T18"
    assert current.engine.state.value == "S7_STUDENT"


def test_demo_runtime_fails_closed_when_task_domain_assets_cannot_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def missing_task_agent(_: str) -> TaskAgent:
        nonlocal calls
        calls += 1
        raise ValueError("cannot read domain asset task_manifest.json")

    monkeypatch.setattr(demo_session_module, "TaskAgent", missing_task_agent)

    with pytest.raises(DemoSessionError, match="task domain"):
        demo_session_module._DemoRuntime(
            DemoOptions(
                profile_id="planner_new",
                trace_id="demo-domain-assets-missing",
                trace_dir=tmp_path / "traces",
                cache_dir=tmp_path / "cache",
            ),
            "live",
            forbidden_llm,
            NoopExecutor,
        )

    assert calls == 1
    assert not (tmp_path / "traces" / "demo-domain-assets-missing.jsonl").exists()


@pytest.mark.parametrize(
    ("domain_id", "template_id", "expected_action"),
    (
        ("production_progress", "T-01", "step_up"),
        ("first_segment", "T-FS02", "keep"),
    ),
)
def test_demo_runtime_reports_honest_completion_when_domain_has_no_higher_tier(
    tmp_path: Path,
    domain_id: str,
    template_id: str,
    expected_action: str,
) -> None:
    current = runtime(
        tmp_path,
        domain_id,
        trace_id=f"demo-completion-{domain_id}",
    )

    assert current.completion_difficulty_action(template_id) == expected_action
