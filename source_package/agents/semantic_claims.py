"""Domain-owned claim plans and deterministic metric-language enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence
import unicodedata

from agents.domain_config import DomainConfig, SemanticInvariant
from agents.kb_loader import KnowledgeChunk


_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?|\n")
_MARKDOWN_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
_MARKDOWN_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_NEGATION_TERMS = (
    "错误",
    "不能",
    "不可",
    "禁止",
    "不应",
    "避免",
    "而不是",
    "≠",
)
_DIVISION_TERMS_RE = re.compile(r"(?:除以|相除|÷|/)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClaimPlanItem:
    """One invariant whose canonical claim is present in retrieved evidence."""

    invariant: SemanticInvariant
    evidence_quote: str


@dataclass(frozen=True, slots=True)
class SemanticRepair:
    invariant_id: str
    reason: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class SemanticValidation:
    text: str
    checked: int
    repaired: int
    failures: tuple[str, ...]
    repairs: tuple[SemanticRepair, ...]


def _public_claim_text(sentence: str) -> str:
    text = _MARKDOWN_HEADING_RE.sub("", sentence.strip())
    text = _MARKDOWN_LIST_ITEM_RE.sub("", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(text.split())


def build_claim_plan(
    domain_config: DomainConfig,
    knowledge_point: str,
    chunks: Sequence[KnowledgeChunk],
) -> tuple[ClaimPlanItem, ...]:
    """Select only invariants whose source quote is in the retrieved evidence."""

    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    plan: list[ClaimPlanItem] = []
    for invariant in domain_config.semantic_invariants:
        if knowledge_point not in invariant.knowledge_points:
            continue
        chunk = chunks_by_id.get(invariant.evidence_ref)
        if chunk is None:
            continue
        evidence_quote = next(
            (
                sentence
                for sentence in chunk.sentences
                if sentence == invariant.evidence_quote
                or _public_claim_text(sentence) == invariant.canonical_claim
            ),
            None,
        )
        if evidence_quote is None:
            continue
        plan.append(
            ClaimPlanItem(
                invariant=invariant,
                evidence_quote=evidence_quote,
            )
        )
    return tuple(plan)


def _term_pattern(terms: Sequence[str]) -> str:
    return "(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)
    ) + ")"


def _ratio_patterns(
    invariant: SemanticInvariant,
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    numerator = _term_pattern(invariant.numerator_terms)
    denominator = _term_pattern(invariant.denominator_terms)
    division = r"\s*(?:汇总)?\s*(?:除以|除|÷|/)\s*"
    reversed_formula = re.compile(
        denominator + division + numerator,
        re.IGNORECASE,
    )
    correct_formula = re.compile(
        numerator + division + denominator,
        re.IGNORECASE,
    )
    ambiguous_formula = re.compile(
        (
            rf"(?:(?:{numerator})\s*(?:与|和|、)\s*(?:{denominator})|"
            rf"(?:{denominator})\s*(?:与|和|、)\s*(?:{numerator}))"
            r"\s*(?:的)?\s*(?:比值|相除)"
            r"|(?:两者|二者)\s*(?:的)?\s*(?:比值|相除)"
        ),
        re.IGNORECASE,
    )
    return reversed_formula, correct_formula, ambiguous_formula


def _contains_trigger(sentence: str, invariant: SemanticInvariant) -> bool:
    folded = unicodedata.normalize("NFKC", sentence).casefold()
    return any(term.casefold() in folded for term in invariant.trigger_terms)


def _contains_negation(sentence: str) -> bool:
    return any(term in sentence for term in _NEGATION_TERMS)


def _replace_stored_formula(sentence: str, canonical_claim: str) -> str:
    prefix_match = re.match(r"^(\s*(?:[-*+]\s+)?)", sentence)
    prefix = prefix_match.group(1) if prefix_match is not None else ""
    newline = "\n" if sentence.endswith("\n") else ""
    return prefix + canonical_claim + newline


def enforce_claim_plan(
    text: str,
    plan: Sequence[ClaimPlanItem],
) -> SemanticValidation:
    """Repair known formula inversions/ambiguities, then verify none remain."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    current = text
    checked = 0
    repairs: list[SemanticRepair] = []
    failures: list[str] = []

    for item in plan:
        invariant = item.invariant
        transformed: list[str] = []
        for match in _SENTENCE_RE.finditer(current):
            sentence = match.group(0)
            if sentence == "\n" or not _contains_trigger(sentence, invariant):
                transformed.append(sentence)
                continue
            checked += 1
            if _contains_negation(sentence):
                transformed.append(sentence)
                continue

            updated = sentence
            reason: str | None = None
            if invariant.mode == "ratio":
                reversed_formula, _, ambiguous_formula = _ratio_patterns(invariant)
                display = str(invariant.display_expression)
                updated, reversed_count = reversed_formula.subn(display, updated)
                updated, ambiguous_count = ambiguous_formula.subn(display, updated)
                if reversed_count:
                    reason = "reversed_formula"
                elif ambiguous_count:
                    reason = "ambiguous_formula"
            else:
                if _DIVISION_TERMS_RE.search(
                    unicodedata.normalize("NFKC", updated)
                ):
                    updated = _replace_stored_formula(
                        updated,
                        invariant.canonical_claim,
                    )
                    reason = "formula_not_declared_by_domain"

            if updated != sentence:
                repairs.append(
                    SemanticRepair(
                        invariant_id=invariant.invariant_id,
                        reason=reason or "canonicalized",
                        before=sentence,
                        after=updated,
                    )
                )
            transformed.append(updated)
        current = "".join(transformed)

        for match in _SENTENCE_RE.finditer(current):
            sentence = match.group(0)
            if (
                sentence == "\n"
                or not _contains_trigger(sentence, invariant)
                or _contains_negation(sentence)
            ):
                continue
            if invariant.mode == "ratio":
                reversed_formula, _, ambiguous_formula = _ratio_patterns(invariant)
                if reversed_formula.search(sentence) is not None:
                    failures.append(f"{invariant.invariant_id}:reversed_formula")
                if ambiguous_formula.search(sentence) is not None:
                    failures.append(f"{invariant.invariant_id}:ambiguous_formula")
            elif _DIVISION_TERMS_RE.search(
                unicodedata.normalize("NFKC", sentence)
            ):
                failures.append(
                    f"{invariant.invariant_id}:formula_not_declared_by_domain"
                )

    return SemanticValidation(
        text=current,
        checked=checked,
        repaired=len(repairs),
        failures=tuple(dict.fromkeys(failures)),
        repairs=tuple(repairs),
    )
