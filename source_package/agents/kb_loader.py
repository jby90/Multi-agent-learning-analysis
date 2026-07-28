"""Load and validate the Markdown knowledge-base chunks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping

import yaml


Difficulty = Literal["basic", "applied", "advanced"]
DIFFICULTIES = ("basic", "applied", "advanced")
REQUIRED_FIELDS = (
    "chunk_id",
    "knowledge_point",
    "difficulty",
    "prerequisites",
    "learning_goal",
    "common_mistakes",
    "applicable_processes",
)
LIST_FIELDS = ("prerequisites", "common_mistakes", "applicable_processes")
TEACHING_FACT_FIELDS = (
    "schema_version",
    "card_id",
    "card_version",
    "title",
    "source",
    "facts",
)
TEACHING_FACT_SOURCE_FIELDS = (
    "type",
    "path",
    "locator",
    "version",
    "git_commit",
    "git_blob",
    "effective_scope",
)
TEACHING_FACT_ITEM_FIELDS = ("fact_id", "text")
_BLANK_LINE_RE = re.compile(r"\r?\n[^\S\r\n]*\r?\n+")
_SENTENCE_RE = re.compile(r".*?[。！？；]+|.+\Z", re.DOTALL)
_PERCENTAGE_RE = re.compile(r"\d+(?:\.\d+)?\s*[%％]")
_MARKDOWN_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_MARKDOWN_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")


@dataclass(frozen=True)
class TeachingFact:
    fact_id: str
    text: str


@dataclass(frozen=True)
class TeachingFactCard:
    schema_version: int
    card_id: str
    card_version: str
    title: str
    source: Mapping[str, str]
    facts: tuple[TeachingFact, ...]


@dataclass(frozen=True)
class EvidenceContextGroup:
    lead_ref: int
    member_refs: tuple[int, ...]


@dataclass(frozen=True)
class KnowledgeChunk:
    path: Path
    chunk_id: str
    knowledge_point: str
    difficulty: Difficulty
    prerequisites: tuple[str, ...]
    learning_goal: str
    common_mistakes: tuple[str, ...]
    applicable_processes: tuple[str, ...]
    body: str
    sentences: tuple[str, ...]
    metadata: Mapping[str, Any]
    teaching_fact_card: TeachingFactCard | None = None
    evidence_context_groups: tuple[EvidenceContextGroup, ...] = ()


@dataclass(frozen=True)
class ValidationIssue:
    path: Path
    field: str
    message: str


@dataclass(frozen=True)
class LoadResult:
    chunks: tuple[KnowledgeChunk, ...]
    issues: tuple[ValidationIssue, ...]


class KnowledgeBaseValidationError(ValueError):
    """Raised when production startup sees any invalid knowledge chunk."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        details = "; ".join(
            f"{issue.path.name}:{issue.field}: {issue.message}" for issue in issues
        )
        super().__init__(f"knowledge base validation failed: {details}")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _issue(path: Path, field: str, message: str) -> ValidationIssue:
    return ValidationIssue(path=path, field=field, message=message)


def _body_paragraphs(body: str) -> tuple[str, ...]:
    return tuple(
        stripped
        for paragraph in _BLANK_LINE_RE.split(body)
        if (stripped := paragraph.strip())
    )


def _paragraph_sentences(
    paragraph: str,
    reserved_sentences: frozenset[str],
) -> tuple[str, ...]:
    if paragraph in reserved_sentences:
        return (paragraph,)
    return tuple(
        sentence
        for match in _SENTENCE_RE.finditer(paragraph)
        if (sentence := match.group(0).strip())
    )


def _split_sentences(
    body: str,
    reserved_sentences: tuple[str, ...] = (),
) -> tuple[str, ...]:
    reserved = frozenset(reserved_sentences)
    return tuple(
        sentence
        for paragraph in _body_paragraphs(body)
        for sentence in _paragraph_sentences(paragraph, reserved)
    )


def _is_markdown_list_block(paragraph: str) -> bool:
    first_line = paragraph.splitlines()[0]
    return _MARKDOWN_LIST_ITEM_RE.match(first_line) is not None


def _is_markdown_heading(paragraph: str) -> bool:
    first_line = paragraph.splitlines()[0]
    return _MARKDOWN_HEADING_RE.match(first_line) is not None


def _evidence_context_groups(
    body: str,
    reserved_sentences: tuple[str, ...] = (),
) -> tuple[EvidenceContextGroup, ...]:
    paragraphs = _body_paragraphs(body)
    reserved = frozenset(reserved_sentences)
    paragraph_sentences = tuple(
        _paragraph_sentences(paragraph, reserved) for paragraph in paragraphs
    )
    paragraph_refs: list[tuple[int, ...]] = []
    next_ref = 1
    for sentences in paragraph_sentences:
        refs = tuple(range(next_ref, next_ref + len(sentences)))
        paragraph_refs.append(refs)
        next_ref += len(sentences)

    groups: list[EvidenceContextGroup] = []
    for index, paragraph in enumerate(paragraphs[:-1]):
        refs = paragraph_refs[index]
        if (
            not refs
            or _is_markdown_heading(paragraph)
            or _is_markdown_list_block(paragraph)
            or not paragraph_sentences[index][-1].endswith(("：", ":"))
            or not _is_markdown_list_block(paragraphs[index + 1])
        ):
            continue

        member_refs: list[int] = []
        member_index = index + 1
        while member_index < len(paragraphs) and _is_markdown_list_block(
            paragraphs[member_index]
        ):
            member_refs.extend(paragraph_refs[member_index])
            member_index += 1
        if member_refs:
            groups.append(
                EvidenceContextGroup(
                    lead_ref=refs[-1],
                    member_refs=tuple(member_refs),
                )
            )
    return tuple(groups)


def _declared_teaching_fact_texts(raw_card: Any) -> tuple[str, ...]:
    if not isinstance(raw_card, dict) or not isinstance(raw_card.get("facts"), list):
        return ()
    return tuple(
        raw_fact["text"]
        for raw_fact in raw_card["facts"]
        if isinstance(raw_fact, dict)
        and isinstance(raw_fact.get("text"), str)
        and bool(raw_fact["text"].strip())
    )


def _split_frontmatter(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("frontmatter must start with an independent --- line")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as error:
        raise ValueError("frontmatter must end with an independent --- line") from error
    return "\n".join(lines[1:closing]), "\n".join(lines[closing + 1 :]).strip()


def _mapping_shape_issues(
    path: Path,
    field: str,
    value: Any,
    required_fields: tuple[str, ...],
) -> list[ValidationIssue]:
    if not isinstance(value, dict):
        return [_issue(path, field, "type must be mapping")]
    issues = [
        _issue(path, f"{field}.{key}", "required field is missing")
        for key in required_fields
        if key not in value
    ]
    issues.extend(
        _issue(path, f"{field}.{key}", "unknown field is not allowed")
        for key in value
        if key not in required_fields
    )
    return issues


def _non_empty_string_issue(
    path: Path,
    field: str,
    value: Any,
) -> ValidationIssue | None:
    if not isinstance(value, str):
        return _issue(path, field, "type must be string")
    if not value.strip():
        return _issue(path, field, "string must not be empty")
    return None


def _parse_teaching_fact_card(
    path: Path,
    raw_card: Any,
    sentences: tuple[str, ...],
) -> tuple[TeachingFactCard | None, list[ValidationIssue]]:
    issues = _mapping_shape_issues(
        path,
        "teaching_facts",
        raw_card,
        TEACHING_FACT_FIELDS,
    )
    if not isinstance(raw_card, dict):
        return None, issues

    schema_version = raw_card.get("schema_version")
    if "schema_version" in raw_card:
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            issues.append(
                _issue(
                    path,
                    "teaching_facts.schema_version",
                    "type must be integer",
                )
            )
        elif schema_version != 1:
            issues.append(
                _issue(
                    path,
                    "teaching_facts.schema_version",
                    "value must equal 1",
                )
            )

    for field in ("card_id", "card_version", "title"):
        if field in raw_card:
            string_issue = _non_empty_string_issue(
                path,
                f"teaching_facts.{field}",
                raw_card[field],
            )
            if string_issue is not None:
                issues.append(string_issue)

    raw_source = raw_card.get("source")
    if "source" in raw_card:
        issues.extend(
            _mapping_shape_issues(
                path,
                "teaching_facts.source",
                raw_source,
                TEACHING_FACT_SOURCE_FIELDS,
            )
        )
        if isinstance(raw_source, dict):
            for field in TEACHING_FACT_SOURCE_FIELDS:
                if field not in raw_source:
                    continue
                string_issue = _non_empty_string_issue(
                    path,
                    f"teaching_facts.source.{field}",
                    raw_source[field],
                )
                if string_issue is not None:
                    issues.append(string_issue)

    raw_facts = raw_card.get("facts")
    parsed_facts: list[TeachingFact] = []
    if "facts" in raw_card:
        if not isinstance(raw_facts, list):
            issues.append(_issue(path, "teaching_facts.facts", "type must be list"))
        elif not raw_facts:
            issues.append(
                _issue(
                    path,
                    "teaching_facts.facts",
                    "must contain at least one fact",
                )
            )
        else:
            seen_fact_ids: set[str] = set()
            seen_fact_texts: set[str] = set()
            for index, raw_fact in enumerate(raw_facts):
                fact_field = f"teaching_facts.facts[{index}]"
                fact_issues = _mapping_shape_issues(
                    path,
                    fact_field,
                    raw_fact,
                    TEACHING_FACT_ITEM_FIELDS,
                )
                issues.extend(fact_issues)
                if not isinstance(raw_fact, dict):
                    continue

                fact_id = raw_fact.get("fact_id")
                text = raw_fact.get("text")
                if "fact_id" in raw_fact:
                    fact_id_issue = _non_empty_string_issue(
                        path,
                        f"{fact_field}.fact_id",
                        fact_id,
                    )
                    if fact_id_issue is not None:
                        issues.append(fact_id_issue)
                    elif fact_id in seen_fact_ids:
                        issues.append(
                            _issue(
                                path,
                                f"{fact_field}.fact_id",
                                f"duplicate fact_id: {fact_id}",
                            )
                        )
                    else:
                        seen_fact_ids.add(fact_id)

                if "text" in raw_fact:
                    text_issue = _non_empty_string_issue(
                        path,
                        f"{fact_field}.text",
                        text,
                    )
                    if text_issue is not None:
                        issues.append(text_issue)
                    elif text in seen_fact_texts:
                        issues.append(
                            _issue(
                                path,
                                f"{fact_field}.text",
                                "body sentence must not be shared by multiple facts",
                            )
                        )
                    else:
                        seen_fact_texts.add(text)
                        if _PERCENTAGE_RE.search(text) is None:
                            issues.append(
                                _issue(
                                    path,
                                    f"{fact_field}.text",
                                    "text must contain a percentage value",
                                )
                            )
                        match_count = sentences.count(text)
                        if match_count != 1:
                            issues.append(
                                _issue(
                                    path,
                                    f"{fact_field}.text",
                                    "text must appear as a complete body sentence "
                                    f"exactly once (found {match_count})",
                                )
                            )

                if (
                    isinstance(fact_id, str)
                    and fact_id.strip()
                    and isinstance(text, str)
                    and text.strip()
                ):
                    parsed_facts.append(TeachingFact(fact_id=fact_id, text=text))

    if issues:
        return None, issues

    assert isinstance(schema_version, int)
    assert isinstance(raw_source, dict)
    return (
        TeachingFactCard(
            schema_version=schema_version,
            card_id=raw_card["card_id"],
            card_version=raw_card["card_version"],
            title=raw_card["title"],
            source=MappingProxyType(dict(raw_source)),
            facts=tuple(parsed_facts),
        ),
        [],
    )


def _parse_chunk(path: Path) -> tuple[KnowledgeChunk | None, list[ValidationIssue]]:
    try:
        raw_frontmatter, body = _split_frontmatter(path)
        metadata = yaml.load(raw_frontmatter, Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        return None, [_issue(path, "frontmatter", str(error))]

    if not isinstance(metadata, dict):
        return None, [_issue(path, "frontmatter", "frontmatter must be a mapping")]

    issues: list[ValidationIssue] = []
    for field in REQUIRED_FIELDS:
        if field not in metadata:
            issues.append(_issue(path, field, "required field is missing"))

    for field in ("chunk_id", "knowledge_point", "learning_goal"):
        if field in metadata:
            value = metadata[field]
            if not isinstance(value, str):
                issues.append(_issue(path, field, "type must be string"))
            elif not value.strip():
                issues.append(_issue(path, field, "string must not be empty"))

    if "difficulty" in metadata:
        difficulty = metadata["difficulty"]
        if not isinstance(difficulty, str):
            issues.append(_issue(path, "difficulty", "type must be string"))
        elif difficulty not in DIFFICULTIES:
            issues.append(
                _issue(path, "difficulty", "value must be basic|applied|advanced")
            )

    for field in LIST_FIELDS:
        if field not in metadata:
            continue
        value = metadata[field]
        if not isinstance(value, list):
            issues.append(_issue(path, field, "type must be list"))
            continue
        for index, item in enumerate(value):
            if not isinstance(item, str):
                issues.append(
                    _issue(path, f"{field}[{index}]", "list item must be string")
                )

    if not body:
        issues.append(_issue(path, "body", "body must not be empty"))

    reserved_sentences = _declared_teaching_fact_texts(
        metadata.get("teaching_facts")
    )
    sentences = _split_sentences(body, reserved_sentences)
    evidence_context_groups = _evidence_context_groups(body, reserved_sentences)
    teaching_fact_card = None
    if "teaching_facts" in metadata:
        teaching_fact_card, teaching_fact_issues = _parse_teaching_fact_card(
            path,
            metadata["teaching_facts"],
            sentences,
        )
        issues.extend(teaching_fact_issues)

    if issues:
        return None, issues

    return (
        KnowledgeChunk(
            path=path,
            chunk_id=metadata["chunk_id"],
            knowledge_point=metadata["knowledge_point"],
            difficulty=metadata["difficulty"],
            prerequisites=tuple(metadata["prerequisites"]),
            learning_goal=metadata["learning_goal"],
            common_mistakes=tuple(metadata["common_mistakes"]),
            applicable_processes=tuple(metadata["applicable_processes"]),
            body=body,
            sentences=sentences,
            metadata=MappingProxyType(dict(metadata)),
            teaching_fact_card=teaching_fact_card,
            evidence_context_groups=evidence_context_groups,
        ),
        [],
    )


def load_chunks(directory: Path) -> LoadResult:
    """Load all ``*.md`` chunks and return accepted chunks plus all issues."""

    directory = Path(directory)
    if not directory.is_dir():
        issue = _issue(directory, "directory", "knowledge-base directory does not exist")
        return LoadResult(chunks=(), issues=(issue,))

    parsed: list[KnowledgeChunk] = []
    issues: list[ValidationIssue] = []
    for path in sorted(directory.glob("*.md"), key=lambda item: item.name):
        chunk, chunk_issues = _parse_chunk(path)
        issues.extend(chunk_issues)
        if chunk is not None:
            parsed.append(chunk)

    chunks_by_id: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    for chunk in parsed:
        chunks_by_id[chunk.chunk_id].append(chunk)

    duplicate_ids = {
        chunk_id for chunk_id, chunks in chunks_by_id.items() if len(chunks) > 1
    }
    for chunk_id in sorted(duplicate_ids):
        for chunk in chunks_by_id[chunk_id]:
            issues.append(
                _issue(chunk.path, "chunk_id", f"duplicate chunk_id: {chunk_id}")
            )

    cards_by_id: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    for chunk in parsed:
        if chunk.teaching_fact_card is not None:
            cards_by_id[chunk.teaching_fact_card.card_id].append(chunk)

    duplicate_card_ids = {
        card_id for card_id, chunks in cards_by_id.items() if len(chunks) > 1
    }
    for card_id in sorted(duplicate_card_ids):
        for chunk in cards_by_id[card_id]:
            issues.append(
                _issue(
                    chunk.path,
                    "teaching_facts.card_id",
                    f"duplicate card_id: {card_id}",
                )
            )

    active = [
        chunk
        for chunk in parsed
        if chunk.chunk_id not in duplicate_ids
        and (
            chunk.teaching_fact_card is None
            or chunk.teaching_fact_card.card_id not in duplicate_card_ids
        )
    ]
    while True:
        active_ids = {chunk.chunk_id for chunk in active}
        rejected: list[KnowledgeChunk] = []
        for chunk in active:
            missing = [
                (index, prerequisite)
                for index, prerequisite in enumerate(chunk.prerequisites)
                if prerequisite not in active_ids
            ]
            if missing:
                rejected.append(chunk)
                for index, prerequisite in missing:
                    issues.append(
                        _issue(
                            chunk.path,
                            f"prerequisites[{index}]",
                            f"missing prerequisite: {prerequisite}",
                        )
                    )
        if not rejected:
            break
        rejected_paths = {chunk.path for chunk in rejected}
        active = [chunk for chunk in active if chunk.path not in rejected_paths]

    return LoadResult(
        chunks=tuple(sorted(active, key=lambda chunk: chunk.chunk_id)),
        issues=tuple(
            sorted(issues, key=lambda item: (item.path.name, item.field, item.message))
        ),
    )


def require_valid_chunks(directory: Path) -> tuple[KnowledgeChunk, ...]:
    """Return chunks or reject startup if any file is invalid."""

    result = load_chunks(directory)
    if result.issues:
        raise KnowledgeBaseValidationError(result.issues)
    return result.chunks
