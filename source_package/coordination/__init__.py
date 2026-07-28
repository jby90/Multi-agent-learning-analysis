"""Typed coordination primitives shared by agents and orchestrators."""

from coordination.artifacts import ArtifactEnvelope
from coordination.contracts import (
    LearnerProfileSnapshot,
    LearningContract,
    QualityPolicy,
)
from coordination.parallel import (
    BranchResult,
    BranchSpec,
    ParallelStage,
    ParallelStageError,
    ParallelStageExecutor,
    ParallelStageResult,
    bounded_llm_executor,
)

__all__ = [
    "ArtifactEnvelope",
    "BranchResult",
    "BranchSpec",
    "LearnerProfileSnapshot",
    "LearningContract",
    "ParallelStage",
    "ParallelStageError",
    "ParallelStageExecutor",
    "ParallelStageResult",
    "QualityPolicy",
    "bounded_llm_executor",
]
