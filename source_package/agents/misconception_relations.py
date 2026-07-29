"""Derive immutable, domain-scoped misconception associations from KB chunks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from types import MappingProxyType
from typing import Literal

from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.knowledge_scope import CHUNK_DIRECTORY


@dataclass(frozen=True, slots=True)
class MisconceptionRelation:
    """One direction of a symmetric association supported by approved chunks."""

    target: str
    knowledge_point_support: int
    chunk_support: int
    support_points: tuple[str, ...]


class RelationIntegrityError(ValueError):
    """Raised when a domain relation snapshot cannot be trusted safely."""


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Internal deterministic switch for B0/B1 routing ablations."""

    mode: Literal["total_support", "marginal_support"] = "marginal_support"

    def __post_init__(self) -> None:
        if self.mode not in {"total_support", "marginal_support"}:
            raise ValueError(
                "routing policy must be total_support or marginal_support"
            )


@dataclass(frozen=True, slots=True)
class RelationRoute:
    """One eligible deterministic route and its approved support projection."""

    target: str
    route_support_points: tuple[str, ...]
    marginal_support_points: tuple[str, ...]
    knowledge_point_support: int
    chunk_support: int

    @property
    def marginal_gain(self) -> int:
        return len(self.marginal_support_points)


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


@lru_cache(maxsize=8)
def default_relation_support_points(
    allowed_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Derive the authoritative support vocabulary independently of the index."""

    domain_ids = frozenset(allowed_ids)
    points: list[str] = []
    for chunk in require_valid_chunks(CHUNK_DIRECTORY):
        related = {
            item for item in chunk.common_mistakes if item in domain_ids
        }
        if len(related) >= 2 and chunk.knowledge_point not in points:
            points.append(chunk.knowledge_point)
    return tuple(points)


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


def _validate_route_inputs(
    *,
    source: str,
    probed: Sequence[str],
    covered_relation_points: Sequence[str],
    relation_index: Mapping[str, Sequence[MisconceptionRelation]],
    allowed_ids: Sequence[str],
    allowed_support_points: Sequence[str],
    policy: RoutingPolicy,
) -> tuple[tuple[str, ...], frozenset[str], frozenset[str]]:
    if not isinstance(policy, RoutingPolicy):
        raise RelationIntegrityError("routing policy is invalid")
    domain_ids = tuple(dict.fromkeys(allowed_ids))
    if not domain_ids or source not in domain_ids:
        raise RelationIntegrityError("relation source is outside the active domain")
    if set(relation_index) != set(domain_ids):
        raise RelationIntegrityError("relation index domain keys are incomplete")
    allowed_points = frozenset(
        point
        for point in allowed_support_points
        if isinstance(point, str) and point.strip()
    )
    probed_set = frozenset(probed)
    if any(item not in domain_ids for item in probed_set):
        raise RelationIntegrityError("probed misconception is outside the active domain")
    covered = frozenset(covered_relation_points)
    if any(
        not isinstance(point, str)
        or not point.strip()
        or point not in allowed_points
        for point in covered
    ):
        raise RelationIntegrityError(
            "covered relation point is outside the active domain"
        )

    for relation_source, relations in relation_index.items():
        if not isinstance(relations, Sequence) or isinstance(relations, (str, bytes)):
            raise RelationIntegrityError("relation index row must be a sequence")
        seen_targets: set[str] = set()
        for relation in relations:
            if not isinstance(relation, MisconceptionRelation):
                raise RelationIntegrityError("relation index row is malformed")
            if (
                relation.target not in domain_ids
                or relation.target == relation_source
                or relation.target in seen_targets
            ):
                raise RelationIntegrityError("relation target is invalid")
            seen_targets.add(relation.target)
            if (
                isinstance(relation.knowledge_point_support, bool)
                or not isinstance(relation.knowledge_point_support, int)
                or relation.knowledge_point_support < 1
                or isinstance(relation.chunk_support, bool)
                or not isinstance(relation.chunk_support, int)
                or relation.chunk_support < 1
            ):
                raise RelationIntegrityError("relation support counts are invalid")
            points = relation.support_points
            if (
                not isinstance(points, tuple)
                or not points
                or len(set(points)) != len(points)
                or relation.knowledge_point_support != len(points)
                or any(
                    not isinstance(point, str)
                    or not point.strip()
                    or point not in allowed_points
                    for point in points
                )
            ):
                raise RelationIntegrityError("relation support points are invalid")
    return domain_ids, probed_set, covered


def rank_relation_routes(
    source: str,
    *,
    responsibility_scope: Sequence[str],
    probed: Sequence[str],
    covered_relation_points: Sequence[str],
    relation_index: Mapping[str, Sequence[MisconceptionRelation]],
    allowed_ids: Sequence[str],
    allowed_support_points: Sequence[str],
    policy: RoutingPolicy,
) -> tuple[RelationRoute, ...]:
    """Rank eligible routes using either frozen B0 or marginal B1 semantics."""

    domain_ids, probed_set, covered = _validate_route_inputs(
        source=source,
        probed=probed,
        covered_relation_points=covered_relation_points,
        relation_index=relation_index,
        allowed_ids=allowed_ids,
        allowed_support_points=allowed_support_points,
        policy=policy,
    )
    order = {item: index for index, item in enumerate(domain_ids)}
    scope = frozenset(responsibility_scope)
    routes: list[RelationRoute] = []
    for relation in relation_index[source]:
        if relation.target in probed_set:
            continue
        eligible = tuple(
            point for point in relation.support_points if point in scope
        )
        if not eligible:
            continue
        marginal = tuple(point for point in eligible if point not in covered)
        if policy.mode == "marginal_support" and not marginal:
            continue
        routes.append(
            RelationRoute(
                target=relation.target,
                route_support_points=eligible,
                marginal_support_points=marginal,
                knowledge_point_support=relation.knowledge_point_support,
                chunk_support=relation.chunk_support,
            )
        )
    if policy.mode == "marginal_support":
        key = lambda route: (
            -route.marginal_gain,
            -route.knowledge_point_support,
            -route.chunk_support,
            order[route.target],
        )
    else:
        key = lambda route: (
            -route.knowledge_point_support,
            -route.chunk_support,
            order[route.target],
        )
    return tuple(sorted(routes, key=key))


def select_relation_route(
    source: str,
    *,
    responsibility_scope: Sequence[str],
    probed: Sequence[str],
    covered_relation_points: Sequence[str],
    relation_index: Mapping[str, Sequence[MisconceptionRelation]],
    allowed_ids: Sequence[str],
    allowed_support_points: Sequence[str],
    policy: RoutingPolicy,
) -> RelationRoute | None:
    """Return the unique first route, or None for a legal no-route fallback."""

    routes = rank_relation_routes(
        source,
        responsibility_scope=responsibility_scope,
        probed=probed,
        covered_relation_points=covered_relation_points,
        relation_index=relation_index,
        allowed_ids=allowed_ids,
        allowed_support_points=allowed_support_points,
        policy=policy,
    )
    return routes[0] if routes else None
