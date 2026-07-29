"""Derive immutable, domain-scoped misconception associations from KB chunks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from types import MappingProxyType

from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.knowledge_scope import CHUNK_DIRECTORY


@dataclass(frozen=True, slots=True)
class MisconceptionRelation:
    """One direction of a symmetric association supported by approved chunks."""

    target: str
    knowledge_point_support: int
    chunk_support: int
    support_points: tuple[str, ...]


@dataclass(slots=True)
class _RelationSupport:
    chunk_support: int
    support_points: set[str]


def build_relation_index(
    chunks: Sequence[KnowledgeChunk],
    allowed_ids: Sequence[str],
) -> Mapping[str, tuple[MisconceptionRelation, ...]]:
    """Build a read-only symmetric relation index limited to one domain."""

    domain_ids = tuple(dict.fromkeys(allowed_ids))
    order = {
        misconception: position
        for position, misconception in enumerate(domain_ids)
    }
    supports: dict[tuple[str, str], _RelationSupport] = {}
    point_order: dict[str, int] = {}

    for chunk in chunks:
        point_order.setdefault(chunk.knowledge_point, len(point_order))
        mistakes = tuple(
            sorted(
                {item for item in chunk.common_mistakes if item in order},
                key=order.__getitem__,
            )
        )
        for left, right in combinations(mistakes, 2):
            pair = (left, right)
            support = supports.setdefault(pair, _RelationSupport(0, set()))
            support.chunk_support += 1
            support.support_points.add(chunk.knowledge_point)

    related: dict[str, list[MisconceptionRelation]] = {
        misconception: [] for misconception in domain_ids
    }
    for (left, right), support in supports.items():
        support_points = tuple(
            sorted(support.support_points, key=point_order.__getitem__)
        )
        for source, target in ((left, right), (right, left)):
            related[source].append(
                MisconceptionRelation(
                    target=target,
                    knowledge_point_support=len(support_points),
                    chunk_support=support.chunk_support,
                    support_points=support_points,
                )
            )

    frozen = {
        source: tuple(
            sorted(
                relations,
                key=lambda item: (
                    -item.knowledge_point_support,
                    -item.chunk_support,
                    order[item.target],
                ),
            )
        )
        for source, relations in related.items()
    }
    return MappingProxyType(frozen)


@lru_cache(maxsize=8)
def default_relation_index(
    allowed_ids: tuple[str, ...],
) -> Mapping[str, tuple[MisconceptionRelation, ...]]:
    """Load the approved chunks once for each ordered domain vocabulary."""

    return build_relation_index(require_valid_chunks(CHUNK_DIRECTORY), allowed_ids)


def rank_related_targets(
    source: str,
    *,
    responsibility_scope: Sequence[str],
    probed: Sequence[str],
    relation_index: Mapping[str, Sequence[MisconceptionRelation]],
) -> tuple[str, ...]:
    """Return unprobed, responsibility-supported targets in priority order."""

    scope = frozenset(responsibility_scope)
    probed_targets = frozenset(probed)
    candidates = (
        relation
        for relation in relation_index.get(source, ())
        if relation.target not in probed_targets
        and any(point in scope for point in relation.support_points)
    )
    ranked = sorted(
        candidates,
        key=lambda relation: (
            -relation.knowledge_point_support,
            -relation.chunk_support,
        ),
    )
    return tuple(relation.target for relation in ranked)
