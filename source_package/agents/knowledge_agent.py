"""RAG-backed teaching agent with mechanical quote verification."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any

from agents.kb_loader import DIFFICULTIES, KnowledgeChunk
from agents.knowledge_scope import responsibility_scope
from agents.retriever import Retriever
from orchestrator.llm import LLMResult, call_llm


MODEL = "qwen3-235b-a22b"
TEMPERATURE = 0.7
PROMPT_PATH = Path(__file__).with_name("prompts") / "knowledge.md"
PROFILE_IDS = frozenset({"planner_new", "craft_engineer", "line_leader"})
_SCAFFOLD_RE = re.compile(r"\[S\d+\]")
_M_ID_RE = re.compile(r"(?<![A-Za-z0-9])M-\d{2}(?!\d)")
_PERCENTAGE_RE = re.compile(r"\d+(?:\.\d+)?\s*[%％]")
_MARKDOWN_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
_MARKDOWN_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_EXAMPLE_PREFIX_RE = re.compile(
    r"^(?:数据示例|示例|例如|比如)(?:[（(:：]|$)|^以.+为例"
)

_NON_EMPTY_STRING = {"type": "string", "minLength": 1, "pattern": r".*\S.*"}
_FACT_SCHEMA = {
    "type": "object",
    "required": ["text", "kind", "chunk_id", "sentence_ref"],
    "properties": {
        "text": _NON_EMPTY_STRING,
        "kind": {"const": "fact"},
        "chunk_id": _NON_EMPTY_STRING,
        "sentence_ref": {"type": "array"},
    },
    "additionalProperties": False,
}
_SPECULATION_SCHEMA = {
    "type": "object",
    "required": ["text", "kind"],
    "properties": {
        "text": _NON_EMPTY_STRING,
        "kind": {"const": "speculation"},
    },
    "additionalProperties": False,
}
KNOWLEDGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [
        {
            "type": "object",
            "required": ["lecture_md", "claims", "coverage"],
            "properties": {
                "lecture_md": _NON_EMPTY_STRING,
                "claims": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"oneOf": [_FACT_SCHEMA, _SPECULATION_SCHEMA]},
                },
                "coverage": {
                    "type": "array",
                    "items": _NON_EMPTY_STRING,
                },
            },
            "additionalProperties": False,
        },
        {
            "type": "object",
            "required": ["lecture_md", "refuse_reason"],
            "properties": {
                "lecture_md": {"type": "null"},
                "refuse_reason": _NON_EMPTY_STRING,
            },
            "additionalProperties": False,
        },
    ],
}


class KnowledgeAgent:
    """Retrieve approved chunks, generate a lecture, and anchor every fact."""

    def __init__(
        self,
        trace_id: str,
        retriever: Retriever,
        llm_call: Callable[..., LLMResult] = call_llm,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        self._trace_id = trace_id
        self._retriever = retriever
        self._llm_call = llm_call
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def generate(
        self,
        *,
        knowledge_point: str,
        student_profile: Mapping[str, Any],
        learning_report_summary: str,
        keywords: Sequence[str] = (),
        difficulty: str | None = None,
    ) -> dict[str, Any]:
        self._validate_inputs(
            knowledge_point,
            student_profile,
            learning_report_summary,
            keywords,
            difficulty,
        )
        started = perf_counter()
        chunks = self._retriever.retrieve(knowledge_point, difficulty, keywords)
        resolved_difficulty = self._resolved_difficulty(difficulty, chunks)
        knowledge_point_match, knowledge_point_match_basis = self._match_audit(
            knowledge_point, chunks
        )
        user_message = self._user_message(
            knowledge_point,
            student_profile,
            learning_report_summary,
            chunks,
            knowledge_point_match,
        )
        llm_result = self._llm_call(
            model=MODEL,
            system=self._system_prompt,
            user=user_message,
            json_schema=KNOWLEDGE_OUTPUT_SCHEMA,
            temperature=TEMPERATURE,
        )
        if llm_result.data.get("lecture_md") is None:
            return self._refusal_draft(
                knowledge_point=knowledge_point,
                difficulty=resolved_difficulty,
                student_profile_ref=str(student_profile["profile_id"]),
                chunks=chunks,
                refuse_reason=str(llm_result.data["refuse_reason"]),
                refusal_origin="llm",
                knowledge_point_match=knowledge_point_match,
                knowledge_point_match_basis=knowledge_point_match_basis,
                llm_result=llm_result,
                started=started,
            )
        return self._success_draft(
            knowledge_point=knowledge_point,
            difficulty=resolved_difficulty,
            student_profile_ref=str(student_profile["profile_id"]),
            chunks=chunks,
            knowledge_point_match=knowledge_point_match,
            knowledge_point_match_basis=knowledge_point_match_basis,
            llm_result=llm_result,
            started=started,
        )

    @staticmethod
    def _resolved_difficulty(
        requested: str | None,
        chunks: Sequence[KnowledgeChunk],
    ) -> str | None:
        if requested in DIFFICULTIES:
            return requested
        retrieved = {chunk.difficulty for chunk in chunks}
        return next(iter(retrieved)) if len(retrieved) == 1 else None

    @staticmethod
    def _match_audit(
        knowledge_point: str,
        chunks: Sequence[KnowledgeChunk],
    ) -> tuple[bool, dict[str, list[str]]]:
        retrieved_points = sorted({chunk.knowledge_point for chunk in chunks})
        matched = [point for point in retrieved_points if point == knowledge_point]
        unmatched = [point for point in retrieved_points if point != knowledge_point]
        return bool(matched), {
            "matched_knowledge_points": matched,
            "unmatched_knowledge_points": unmatched,
        }

    @staticmethod
    def _validate_inputs(
        knowledge_point: str,
        student_profile: Mapping[str, Any],
        learning_report_summary: str,
        keywords: Sequence[str],
        difficulty: str | None,
    ) -> None:
        if not isinstance(knowledge_point, str) or not knowledge_point.strip():
            raise ValueError("knowledge_point must be a non-empty string")
        if not isinstance(student_profile, Mapping):
            raise ValueError("student_profile must be a mapping")
        if student_profile.get("profile_id") not in PROFILE_IDS:
            raise ValueError("student_profile.profile_id is not supported")
        if (
            not isinstance(learning_report_summary, str)
            or not learning_report_summary.strip()
        ):
            raise ValueError("learning_report_summary must be a non-empty string")
        if isinstance(keywords, str) or not isinstance(keywords, Sequence):
            raise ValueError("keywords must be a sequence of strings")
        if any(not isinstance(keyword, str) for keyword in keywords):
            raise ValueError("every keyword must be a string")
        if difficulty is not None and difficulty not in DIFFICULTIES:
            raise ValueError("difficulty must be basic|applied|advanced or None")

    @staticmethod
    def _user_message(
        knowledge_point: str,
        student_profile: Mapping[str, Any],
        learning_report_summary: str,
        chunks: Sequence[KnowledgeChunk],
        knowledge_point_match: bool,
    ) -> str:
        protect_card_numbers = any(
            chunk.teaching_fact_card is not None for chunk in chunks
        )
        data = {
            "knowledge_point": knowledge_point,
            "knowledge_point_match": knowledge_point_match,
            "student_profile": dict(student_profile),
            "learning_report_summary": learning_report_summary,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "knowledge_point": chunk.knowledge_point,
                    "difficulty": chunk.difficulty,
                    "prerequisites": list(chunk.prerequisites),
                    "learning_goal": chunk.learning_goal,
                    "applicable_processes": list(chunk.applicable_processes),
                    "body": "\n".join(
                        f"[S{index}] {sentence}"
                        for index, sentence in enumerate(chunk.sentences, start=1)
                        if index
                        not in KnowledgeAgent._request_held_out_sentence_refs(
                            chunk,
                            protect_card_numbers=protect_card_numbers,
                        )
                    ),
                }
                for chunk in chunks
            ],
        }
        return json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _request_held_out_sentence_refs(
        chunk: KnowledgeChunk,
        *,
        protect_card_numbers: bool,
    ) -> frozenset[int]:
        held_out = set(KnowledgeAgent._held_out_sentence_refs(chunk))
        if protect_card_numbers:
            held_out.update(
                index
                for index, sentence in enumerate(chunk.sentences, start=1)
                if _PERCENTAGE_RE.search(sentence)
            )
        return frozenset(held_out)

    @staticmethod
    def _held_out_sentence_refs(chunk: KnowledgeChunk) -> frozenset[int]:
        card = chunk.teaching_fact_card
        if card is None:
            return frozenset()
        fact_texts = {fact.text for fact in card.facts}
        return frozenset(
            index
            for index, sentence in enumerate(chunk.sentences, start=1)
            if sentence in fact_texts
        )

    @staticmethod
    def _evidence_sentence_refs(
        chunk: KnowledgeChunk,
        sentence_refs: Sequence[int],
        held_out_refs: frozenset[int],
    ) -> Sequence[int]:
        requested_refs = set(sentence_refs)
        matched_groups = tuple(
            group
            for group in chunk.evidence_context_groups
            if group.lead_ref in requested_refs
        )
        if not matched_groups:
            return sentence_refs

        expanded_refs = set(sentence_refs)
        for group in matched_groups:
            expanded_refs.update(
                reference
                for reference in group.member_refs
                if 1 <= reference <= len(chunk.sentences)
                and reference not in held_out_refs
            )
        return tuple(sorted(expanded_refs))

    @staticmethod
    def _public_claim_text(sentence: str) -> str:
        """Strip presentation-only Markdown from an approved source sentence."""

        text = _MARKDOWN_HEADING_RE.sub("", sentence.strip())
        text = _MARKDOWN_LIST_ITEM_RE.sub("", text)
        text = _MARKDOWN_LINK_RE.sub(r"\1", text)
        text = text.replace("**", "").replace("__", "").replace("`", "")
        return " ".join(text.split())

    @staticmethod
    def _grounding_chunks(
        knowledge_point: str,
        chunks: Sequence[KnowledgeChunk],
    ) -> tuple[tuple[KnowledgeChunk, ...], frozenset[str]]:
        """Return target chunks followed by their direct prerequisites only."""

        targets = tuple(
            chunk for chunk in chunks if chunk.knowledge_point == knowledge_point
        )
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        ordered = list(targets)
        seen = {chunk.chunk_id for chunk in targets}
        prerequisite_ids: set[str] = set()
        for target in targets:
            for prerequisite_id in target.prerequisites:
                prerequisite = chunks_by_id.get(prerequisite_id)
                if prerequisite is None:
                    continue
                prerequisite_ids.add(prerequisite_id)
                if prerequisite_id in seen:
                    continue
                ordered.append(prerequisite)
                seen.add(prerequisite_id)
        return tuple(ordered), frozenset(prerequisite_ids)

    @classmethod
    def _foundation_sentence_ref(
        cls,
        chunk: KnowledgeChunk,
        *,
        protect_card_numbers: bool = False,
    ) -> int | None:
        held_out_refs = cls._request_held_out_sentence_refs(
            chunk,
            protect_card_numbers=protect_card_numbers,
        )
        for reference, sentence in enumerate(chunk.sentences, start=1):
            raw_sentence = sentence.strip()
            public_text = cls._public_claim_text(sentence)
            if (
                reference not in held_out_refs
                and public_text
                and not _MARKDOWN_HEADING_RE.match(raw_sentence)
                and not _EXAMPLE_PREFIX_RE.match(public_text)
            ):
                return reference
        return None

    def _success_draft(
        self,
        *,
        knowledge_point: str,
        difficulty: str | None,
        student_profile_ref: str,
        chunks: Sequence[KnowledgeChunk],
        knowledge_point_match: bool,
        knowledge_point_match_basis: Mapping[str, list[str]],
        llm_result: LLMResult,
        started: float,
    ) -> dict[str, Any]:
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        protect_card_numbers = any(
            chunk.teaching_fact_card is not None for chunk in chunks
        )
        grounding_chunks, direct_prerequisite_ids = self._grounding_chunks(
            knowledge_point,
            chunks,
        )
        claims: list[dict[str, str]] = []
        claim_positions: dict[str, int] = {}
        evidence: list[dict[str, str]] = []
        evidence_seen: set[tuple[str, str, str]] = set()
        grounded_claims_by_chunk: dict[str, list[str]] = {}
        checked = 0
        passed = 0
        failed = 0
        failures: list[dict[str, Any]] = []

        def add_claim(text: str, kind: str) -> None:
            position = claim_positions.get(text)
            if position is None:
                claim_positions[text] = len(claims)
                claims.append({"text": text, "kind": kind})
                return
            if claims[position]["kind"] == "speculation" and kind == "fact":
                claims[position]["kind"] = "fact"

        def add_grounded_text(
            chunk: KnowledgeChunk,
            text: str,
            evidence_quotes: Sequence[str],
        ) -> str:
            if not text:
                return ""
            add_claim(text, "fact")
            chunk_claims = grounded_claims_by_chunk.setdefault(chunk.chunk_id, [])
            if text not in chunk_claims:
                chunk_claims.append(text)
            for quote in evidence_quotes:
                evidence_key = (chunk.chunk_id, quote, text)
                if evidence_key in evidence_seen:
                    continue
                evidence_seen.add(evidence_key)
                evidence.append(
                    {
                        "kind": "kb_chunk",
                        "ref": chunk.chunk_id,
                        "quote": quote,
                        "supports_claim": text,
                    }
                )
            return text

        def add_atom(chunk: KnowledgeChunk, reference: int) -> str:
            held_out_refs = self._request_held_out_sentence_refs(
                chunk,
                protect_card_numbers=protect_card_numbers,
            )
            text = self._public_claim_text(chunk.sentences[reference - 1])
            evidence_refs = self._evidence_sentence_refs(
                chunk,
                (reference,),
                held_out_refs,
            )
            return add_grounded_text(
                chunk,
                text,
                tuple(chunk.sentences[item - 1] for item in evidence_refs),
            )

        replacements: list[tuple[str, str]] = []
        for raw_claim in llm_result.data["claims"]:
            text = str(raw_claim["text"])
            if raw_claim["kind"] == "speculation":
                add_claim(text, "speculation")
                continue
            checked += 1
            chunk_id = str(raw_claim["chunk_id"])
            sentence_ref = raw_claim["sentence_ref"]
            chunk = chunks_by_id.get(chunk_id)
            held_out_refs = (
                self._request_held_out_sentence_refs(
                    chunk,
                    protect_card_numbers=protect_card_numbers,
                )
                if chunk is not None
                else frozenset()
            )
            valid_refs = (
                chunk is not None
                and isinstance(sentence_ref, list)
                and bool(sentence_ref)
                and all(
                    isinstance(reference, int)
                    and not isinstance(reference, bool)
                    and 1 <= reference <= len(chunk.sentences)
                    and reference not in held_out_refs
                    for reference in sentence_ref
                )
            )
            if valid_refs and chunk is not None:
                passed += 1
                atom_texts: list[str] = []
                for reference in sentence_ref:
                    atom_text = add_atom(chunk, reference)
                    if atom_text and atom_text not in atom_texts:
                        atom_texts.append(atom_text)
                replacements.append((text, "\n".join(atom_texts)))
            else:
                failed += 1
                replacements.append((text, ""))
                failures.append(
                    {
                        "claim_text": text,
                        "sentence_ref": sentence_ref,
                        "reason": "invalid_sentence_ref",
                    }
                )

        lecture_md, scaffold_leaks = _SCAFFOLD_RE.subn(
            "", llm_result.data["lecture_md"]
        )
        lecture_md, m_id_leaks = _M_ID_RE.subn("", lecture_md)
        for source_text, replacement in replacements:
            lecture_md = lecture_md.replace(source_text, replacement)

        for chunk in grounding_chunks:
            if grounded_claims_by_chunk.get(chunk.chunk_id):
                continue
            foundation_ref = self._foundation_sentence_ref(
                chunk,
                protect_card_numbers=protect_card_numbers,
            )
            if foundation_ref is not None:
                add_atom(chunk, foundation_ref)

        teaching_fact_cards: list[dict[str, Any]] = []
        teaching_fact_blocks: list[str] = []
        teaching_fact_texts: set[str] = set()
        for chunk in chunks:
            card = chunk.teaching_fact_card
            if card is None:
                continue
            teaching_fact_blocks.append(
                "\n\n".join((f"## {card.title}", *(fact.text for fact in card.facts)))
            )
            teaching_fact_cards.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "card_id": card.card_id,
                    "card_version": card.card_version,
                    "fact_ids": [fact.fact_id for fact in card.facts],
                }
            )
            for fact in card.facts:
                text = fact.text
                teaching_fact_texts.add(text)
                add_grounded_text(chunk, text, (text,))

        missing_prerequisite_facts: list[str] = []
        missing_other_facts: list[str] = []
        for claim in claims:
            if (
                claim["kind"] != "fact"
                or claim["text"] in lecture_md
                or claim["text"] in teaching_fact_texts
            ):
                continue
            source_chunk_ids = {
                chunk_id
                for chunk_id, texts in grounded_claims_by_chunk.items()
                if claim["text"] in texts
            }
            destination = (
                missing_prerequisite_facts
                if source_chunk_ids & direct_prerequisite_ids
                else missing_other_facts
            )
            destination.append(claim["text"])
        if missing_prerequisite_facts:
            prerequisite_block = "## 前置基础\n\n" + "\n\n".join(
                missing_prerequisite_facts
            )
            lecture_md = "\n\n".join(
                (prerequisite_block, lecture_md.lstrip())
            )
        if missing_other_facts:
            fact_block = "## 依据要点\n\n" + "\n\n".join(missing_other_facts)
            lecture_md = "\n\n".join((lecture_md.rstrip(), fact_block))
        if teaching_fact_blocks:
            lecture_md = "\n\n".join(
                (lecture_md.rstrip(), *teaching_fact_blocks)
            )

        coverage: list[str] = []
        ordered_coverage_chunks: list[KnowledgeChunk] = []
        seen_coverage_chunks: set[str] = set()
        for chunk in (*grounding_chunks, *chunks):
            if chunk.chunk_id in seen_coverage_chunks:
                continue
            ordered_coverage_chunks.append(chunk)
            seen_coverage_chunks.add(chunk.chunk_id)
        for chunk in ordered_coverage_chunks:
            grounded_texts = grounded_claims_by_chunk.get(chunk.chunk_id, [])
            if (
                any(text in lecture_md for text in grounded_texts)
                and chunk.knowledge_point not in coverage
            ):
                coverage.append(chunk.knowledge_point)
        content = {
            "event": "product_ready",
            "lecture_md": lecture_md,
            "knowledge_point": knowledge_point,
            "responsibility_scope": list(
                responsibility_scope(knowledge_point, difficulty)
            ),
            "knowledge_point_match": knowledge_point_match,
            "knowledge_point_match_basis": dict(knowledge_point_match_basis),
            "coverage": coverage,
            "retrieved_chunk_ids": [chunk.chunk_id for chunk in chunks],
            "quote_validation": {
                "checked": checked,
                "passed": passed,
                "failed": failed,
                "failures": failures,
                "scaffold_leaks": scaffold_leaks,
                "m_id_leaks": m_id_leaks,
            },
            "llm_latency_ms": llm_result.latency_ms,
        }
        if teaching_fact_cards:
            content["teaching_fact_cards"] = teaching_fact_cards
        if difficulty is not None:
            content["difficulty"] = difficulty
        total_latency = max(
            round((perf_counter() - started) * 1000),
            llm_result.latency_ms,
        )
        return {
            "trace_id": self._trace_id,
            "agent": "knowledge",
            "role": "produce",
            "payload": {"type": "lecture_note", "content": content},
            "evidence": evidence,
            "claims": claims,
            "student_profile_ref": student_profile_ref,
            "timestamp": self._clock().isoformat(),
            "model": llm_result.model,
            "latency_ms": total_latency,
            "token_usage": llm_result.token_usage.as_dict(),
        }

    def _refusal_draft(
        self,
        *,
        knowledge_point: str,
        difficulty: str | None,
        student_profile_ref: str,
        chunks: Sequence[KnowledgeChunk],
        refuse_reason: str,
        refusal_origin: str,
        knowledge_point_match: bool,
        knowledge_point_match_basis: Mapping[str, list[str]],
        llm_result: LLMResult,
        started: float,
    ) -> dict[str, Any]:
        content = {
            "event": "knowledge_refused",
            "lecture_md": None,
            "knowledge_point": knowledge_point,
            "responsibility_scope": list(
                responsibility_scope(knowledge_point, difficulty)
            ),
            "knowledge_point_match": knowledge_point_match,
            "knowledge_point_match_basis": dict(knowledge_point_match_basis),
            "retrieved_chunk_ids": [chunk.chunk_id for chunk in chunks],
            "refuse_reason": refuse_reason,
            "refusal_origin": refusal_origin,
            "llm_latency_ms": llm_result.latency_ms,
        }
        if difficulty is not None:
            content["difficulty"] = difficulty
        total_latency = max(
            round((perf_counter() - started) * 1000),
            llm_result.latency_ms,
        )
        return {
            "trace_id": self._trace_id,
            "agent": "knowledge",
            "role": "produce",
            "payload": {"type": "lecture_note", "content": content},
            "evidence": [],
            "claims": [],
            "student_profile_ref": student_profile_ref,
            "timestamp": self._clock().isoformat(),
            "model": llm_result.model,
            "latency_ms": total_latency,
            "token_usage": llm_result.token_usage.as_dict(),
        }
