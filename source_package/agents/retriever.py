"""Deterministic metadata-filtered BM25 retrieval."""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol, Sequence, runtime_checkable

from rank_bm25 import BM25Okapi

from agents.kb_loader import DIFFICULTIES, KnowledgeChunk


_WORD_RE = re.compile(r"[a-z0-9_]+")
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def tokenize(text: str) -> tuple[str, ...]:
    """Return stable ASCII words followed by Han unigrams and bigrams."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = _WORD_RE.findall(normalized)
    for match in _HAN_RE.finditer(normalized):
        segment = match.group(0)
        tokens.extend(segment)
        tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    return tuple(tokens)


@runtime_checkable
class Retriever(Protocol):
    def retrieve(
        self,
        knowledge_point: str,
        difficulty: str | None,
        keywords: Sequence[str],
    ) -> tuple[KnowledgeChunk, ...]: ...


class BM25Retriever:
    """Use metadata as the gate and BM25 only as a deterministic ranker."""

    def __init__(self, chunks: Sequence[KnowledgeChunk]) -> None:
        self._chunks = tuple(chunks)
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in self._chunks}

    @staticmethod
    def _index_text(chunk: KnowledgeChunk) -> str:
        return " ".join(
            (
                chunk.knowledge_point,
                chunk.learning_goal,
                *chunk.common_mistakes,
                *chunk.applicable_processes,
                chunk.body,
            )
        )

    def retrieve(
        self,
        knowledge_point: str,
        difficulty: str | None,
        keywords: Sequence[str],
    ) -> tuple[KnowledgeChunk, ...]:
        if not isinstance(knowledge_point, str) or not knowledge_point.strip():
            raise ValueError("knowledge_point must be a non-empty string")
        if difficulty is not None and difficulty not in DIFFICULTIES:
            raise ValueError("difficulty must be basic|applied|advanced or None")
        if isinstance(keywords, str) or not isinstance(keywords, Sequence):
            raise ValueError("keywords must be a sequence of strings")
        if any(not isinstance(keyword, str) for keyword in keywords):
            raise ValueError("every keyword must be a string")

        candidates = tuple(
            sorted(
                (
                    chunk
                    for chunk in self._chunks
                    if chunk.knowledge_point == knowledge_point
                    and (difficulty is None or chunk.difficulty == difficulty)
                ),
                key=lambda chunk: chunk.chunk_id,
            )
        )
        if not candidates:
            return ()

        corpus = [list(tokenize(self._index_text(chunk))) for chunk in candidates]
        query = list(tokenize(" ".join((knowledge_point, *keywords))))
        scores = BM25Okapi(corpus).get_scores(query)
        ranked = sorted(
            zip(scores, candidates),
            key=lambda item: (-float(item[0]), item[1].chunk_id),
        )
        targets = tuple(chunk for _, chunk in ranked[:3])
        expanded = list(targets)
        seen = {chunk.chunk_id for chunk in targets}
        for target in targets:
            for prerequisite_id in target.prerequisites:
                prerequisite = self._chunks_by_id.get(prerequisite_id)
                if prerequisite is None or prerequisite_id in seen:
                    continue
                expanded.append(prerequisite)
                seen.add(prerequisite_id)
        return tuple(expanded)
