"""Deterministic responsibility scopes from approved KB prerequisite metadata."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Sequence

from agents.kb_loader import KnowledgeChunk, require_valid_chunks


CHUNK_DIRECTORY = Path(__file__).with_name("knowledge_base") / "chunks"


@lru_cache(maxsize=1)
def _approved_chunks() -> tuple[KnowledgeChunk, ...]:
    return require_valid_chunks(CHUNK_DIRECTORY)


def responsibility_scope(
    knowledge_point: str,
    difficulty: str | None = None,
    *,
    chunks: Sequence[KnowledgeChunk] | None = None,
) -> tuple[str, ...]:
    """Return the current point and only its direct prerequisite points."""

    if not isinstance(knowledge_point, str) or not knowledge_point.strip():
        raise ValueError("knowledge_point must be a non-empty string")
    point = knowledge_point.strip()
    catalog = tuple(chunks) if chunks is not None else _approved_chunks()
    candidates = tuple(
        chunk
        for chunk in catalog
        if chunk.knowledge_point == point
        and (difficulty is None or chunk.difficulty == difficulty)
    )
    if not candidates:
        return (point,)

    by_id = {chunk.chunk_id: chunk for chunk in catalog}
    scope = [point]
    for chunk in sorted(candidates, key=lambda item: item.chunk_id):
        for prerequisite_id in chunk.prerequisites:
            prerequisite = by_id.get(prerequisite_id)
            if (
                prerequisite is not None
                and prerequisite.knowledge_point not in scope
            ):
                scope.append(prerequisite.knowledge_point)
    return tuple(scope)


def prerequisite_scaffolds(
    knowledge_point: str,
    difficulty: str | None = None,
    *,
    chunks: Sequence[KnowledgeChunk] | None = None,
) -> tuple[dict[str, str], ...]:
    """Return auditable direct prerequisites outside the current point.

    Lower difficulty chunks of the same point remain part of the knowledge
    lineage, but R-03 only needs an explicit bridge when the advanced resource
    crosses into another knowledge point.  Each bridge is copied from approved
    chunk metadata so a guide cannot invent its own prerequisite description.
    """

    if not isinstance(knowledge_point, str) or not knowledge_point.strip():
        raise ValueError("knowledge_point must be a non-empty string")
    point = knowledge_point.strip()
    catalog = tuple(chunks) if chunks is not None else _approved_chunks()
    candidates = tuple(
        chunk
        for chunk in catalog
        if chunk.knowledge_point == point
        and (difficulty is None or chunk.difficulty == difficulty)
    )
    by_id = {chunk.chunk_id: chunk for chunk in catalog}
    seen_points: set[str] = set()
    scaffolds: list[dict[str, str]] = []
    for chunk in sorted(candidates, key=lambda item: item.chunk_id):
        for prerequisite_id in chunk.prerequisites:
            prerequisite = by_id.get(prerequisite_id)
            if (
                prerequisite is None
                or prerequisite.knowledge_point == point
                or prerequisite.knowledge_point in seen_points
            ):
                continue
            scaffolds.append(
                {
                    "knowledge_point": prerequisite.knowledge_point,
                    "chunk_id": prerequisite.chunk_id,
                    "learning_goal": prerequisite.learning_goal,
                }
            )
            seen_points.add(prerequisite.knowledge_point)
    return tuple(scaffolds)
