from __future__ import annotations

from contextlib import contextmanager
from threading import Barrier, Lock
from time import sleep

import pytest

from coordination.parallel import (
    BranchSpec,
    ParallelStage,
    ParallelStageError,
    ParallelStageExecutor,
)


def test_parallel_stage_really_overlaps_branches_and_keeps_declared_order() -> None:
    rendezvous = Barrier(2, timeout=1.0)
    completion_order: list[str] = []
    lock = Lock()

    def branch(branch_id: str, delay: float) -> str:
        rendezvous.wait()
        sleep(delay)
        with lock:
            completion_order.append(branch_id)
        return branch_id

    stage = ParallelStage(
        stage_id="dual-review",
        branches=(
            BranchSpec("slow", lambda: branch("slow", 0.03)),
            BranchSpec("fast", lambda: branch("fast", 0.0)),
        ),
    )
    result = ParallelStageExecutor(max_concurrency=2).execute(
        stage,
        correlation_id="product-1",
    )

    assert completion_order == ["fast", "slow"]
    assert [item.branch_id for item in result.branches] == ["slow", "fast"]
    assert result.require("slow") == "slow"
    assert result.succeeded is True


def test_parallel_stage_fails_closed_without_leaking_exception_text() -> None:
    def fail() -> str:
        raise RuntimeError("provider secret detail")

    result = ParallelStageExecutor(max_concurrency=2).execute(
        ParallelStage(
            stage_id="quality-gate",
            branches=(
                BranchSpec("required", fail),
                BranchSpec("optional", lambda: "ok", required=False),
            ),
        ),
        correlation_id="product-2",
    )

    with pytest.raises(ParallelStageError) as raised:
        result.require_success()
    assert "provider secret detail" not in str(raised.value)
    assert result.branch("required").error_type == "RuntimeError"


def test_parallel_scope_wraps_the_whole_fork_join_stage_once() -> None:
    scope_entries: list[str] = []

    @contextmanager
    def scope():
        scope_entries.append("enter")
        yield
        scope_entries.append("exit")

    executor = ParallelStageExecutor(max_concurrency=2, scope_factory=scope)
    executor.execute(
        ParallelStage(
            stage_id="scoped",
            branches=(
                BranchSpec("a", lambda: 1),
                BranchSpec("b", lambda: 2),
            ),
        )
    )

    assert scope_entries == ["enter", "exit"]
