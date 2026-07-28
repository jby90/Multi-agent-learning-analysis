"""Bounded fork/join execution with deterministic result ordering."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Generic, Literal, TypeVar
from uuid import uuid4


T = TypeVar("T")
BranchStatus = Literal["succeeded", "failed"]
StageObserver = Callable[[str, Mapping[str, Any]], None]
ScopeFactory = Callable[[], AbstractContextManager[Any]]


def _required_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class BranchSpec(Generic[T]):
    branch_id: str
    task: Callable[[], T]
    required: bool = True

    def __post_init__(self) -> None:
        _required_identifier(self.branch_id, "branch_id")
        if not callable(self.task):
            raise ValueError("branch task must be callable")


@dataclass(frozen=True, slots=True)
class ParallelStage(Generic[T]):
    stage_id: str
    branches: tuple[BranchSpec[T], ...]

    def __post_init__(self) -> None:
        _required_identifier(self.stage_id, "stage_id")
        if not self.branches:
            raise ValueError("parallel stage must contain at least one branch")
        branch_ids = [branch.branch_id for branch in self.branches]
        if len(set(branch_ids)) != len(branch_ids):
            raise ValueError("parallel stage branch IDs must be unique")


@dataclass(frozen=True, slots=True)
class BranchResult(Generic[T]):
    branch_id: str
    required: bool
    status: BranchStatus
    elapsed_ms: int
    value: T | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ParallelStageResult(Generic[T]):
    stage_id: str
    correlation_id: str
    elapsed_ms: int
    max_concurrency: int
    branches: tuple[BranchResult[T], ...]

    @property
    def succeeded(self) -> bool:
        return all(
            branch.status == "succeeded"
            for branch in self.branches
            if branch.required
        )

    def branch(self, branch_id: str) -> BranchResult[T]:
        for result in self.branches:
            if result.branch_id == branch_id:
                return result
        raise KeyError(branch_id)

    def require(self, branch_id: str) -> T:
        result = self.branch(branch_id)
        if result.status != "succeeded":
            raise ParallelStageError(self)
        return result.value  # type: ignore[return-value]

    def require_success(self) -> "ParallelStageResult[T]":
        if not self.succeeded:
            raise ParallelStageError(self)
        return self

    def summary(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "correlation_id": self.correlation_id,
            "elapsed_ms": self.elapsed_ms,
            "max_concurrency": self.max_concurrency,
            "branches": [
                {
                    "branch_id": branch.branch_id,
                    "required": branch.required,
                    "status": branch.status,
                    "elapsed_ms": branch.elapsed_ms,
                    **(
                        {"error_type": branch.error_type}
                        if branch.error_type is not None
                        else {}
                    ),
                }
                for branch in self.branches
            ],
        }


class ParallelStageError(RuntimeError):
    """Fail closed without leaking provider exception text."""

    def __init__(self, result: ParallelStageResult[Any]) -> None:
        self.result = result
        failed = [
            branch.branch_id
            for branch in result.branches
            if branch.required and branch.status != "succeeded"
        ]
        super().__init__(
            f"parallel stage {result.stage_id} failed required branches: {failed}"
        )


class ParallelStageExecutor:
    """Execute declared branches with one bounded, reusable fork/join policy."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        scope_factory: ScopeFactory | None = None,
    ) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if scope_factory is not None and not callable(scope_factory):
            raise ValueError("scope_factory must be callable")
        self.max_concurrency = max_concurrency
        self._scope_factory = scope_factory

    def execute(
        self,
        stage: ParallelStage[T],
        *,
        correlation_id: str | None = None,
        observer: StageObserver | None = None,
    ) -> ParallelStageResult[T]:
        if not isinstance(stage, ParallelStage):
            raise ValueError("stage must be a ParallelStage")
        resolved_correlation = correlation_id or uuid4().hex
        _required_identifier(resolved_correlation, "correlation_id")
        concurrency = min(self.max_concurrency, len(stage.branches))
        started = perf_counter()
        self._notify(
            observer,
            "stage_started",
            stage_id=stage.stage_id,
            correlation_id=resolved_correlation,
            branch_ids=[branch.branch_id for branch in stage.branches],
            max_concurrency=concurrency,
        )

        def run_branch(branch: BranchSpec[T]) -> BranchResult[T]:
            branch_started = perf_counter()
            self._notify(
                observer,
                "branch_started",
                stage_id=stage.stage_id,
                correlation_id=resolved_correlation,
                branch_id=branch.branch_id,
                required=branch.required,
            )
            try:
                value = branch.task()
            except Exception as exc:
                result = BranchResult[T](
                    branch_id=branch.branch_id,
                    required=branch.required,
                    status="failed",
                    elapsed_ms=self._elapsed_ms(branch_started),
                    error_type=type(exc).__name__,
                )
                self._notify(
                    observer,
                    "branch_failed",
                    stage_id=stage.stage_id,
                    correlation_id=resolved_correlation,
                    branch_id=branch.branch_id,
                    required=branch.required,
                    elapsed_ms=result.elapsed_ms,
                    error_type=result.error_type,
                )
                return result
            result = BranchResult(
                branch_id=branch.branch_id,
                required=branch.required,
                status="succeeded",
                elapsed_ms=self._elapsed_ms(branch_started),
                value=value,
            )
            self._notify(
                observer,
                "branch_completed",
                stage_id=stage.stage_id,
                correlation_id=resolved_correlation,
                branch_id=branch.branch_id,
                required=branch.required,
                elapsed_ms=result.elapsed_ms,
            )
            return result

        scope = self._scope_factory() if self._scope_factory is not None else nullcontext()
        with scope:
            with ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix=f"stage-{stage.stage_id}",
            ) as pool:
                futures = [pool.submit(run_branch, branch) for branch in stage.branches]
                # Reading futures in declaration order makes aggregation reproducible even
                # when faster branches finish first.
                results = tuple(future.result() for future in futures)

        stage_result = ParallelStageResult(
            stage_id=stage.stage_id,
            correlation_id=resolved_correlation,
            elapsed_ms=self._elapsed_ms(started),
            max_concurrency=concurrency,
            branches=results,
        )
        self._notify(
            observer,
            "stage_completed",
            **stage_result.summary(),
            succeeded=stage_result.succeeded,
        )
        return stage_result

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round(max(0.0, perf_counter() - started) * 1000)

    @staticmethod
    def _notify(
        observer: StageObserver | None,
        event: str,
        **details: Any,
    ) -> None:
        if observer is not None:
            observer(event, details)


def bounded_llm_executor(
    llm_call: Callable[..., Any],
    *,
    max_concurrency: int = 2,
) -> ParallelStageExecutor:
    """Build the explicit production policy for a model boundary."""

    parallel_scope = getattr(llm_call, "parallel_scope", None)
    return ParallelStageExecutor(
        max_concurrency=max_concurrency,
        scope_factory=parallel_scope if callable(parallel_scope) else None,
    )
