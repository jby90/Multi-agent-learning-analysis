"""Validated, immutable domain configuration packages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOMAIN_ROOT = ROOT / "config" / "domains"
DOMAIN_CONFIG_ENV = "REF_DOMAIN_CONFIG"
DEFAULT_DOMAIN_ID = "production_progress"
MANIFEST_FIELDS = frozenset({"schema_version", "domain_id", "database", "assets"})
ASSET_KEYS = (
    "schema",
    "intents",
    "task_manifest",
    "task_templates",
    "counter_evidence",
    "semantic_invariants",
    "gateway_scope",
)
SCHEMA_FIELDS = frozenset({"tables", "prompt_ddl", "dictionary_rows"})
SEMANTIC_INVARIANT_ROOT_FIELDS = frozenset({"schema_version", "invariants"})
SEMANTIC_INVARIANT_FIELDS = frozenset(
    {
        "invariant_id",
        "metric",
        "mode",
        "knowledge_points",
        "trigger_terms",
        "canonical_expression",
        "display_expression",
        "canonical_claim",
        "evidence_ref",
        "evidence_quote",
        "numerator_terms",
        "denominator_terms",
    }
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class SemanticInvariant:
    """One domain-owned, machine-checkable metric invariant."""

    invariant_id: str
    metric: str
    mode: str
    knowledge_points: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    canonical_expression: str | None
    display_expression: str | None
    canonical_claim: str
    evidence_ref: str
    evidence_quote: str
    numerator_terms: tuple[str, ...]
    denominator_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DomainConfig:
    """One fully validated, read-only domain package."""

    domain_id: str
    schema_version: int
    database: str
    table_columns: Mapping[str, frozenset[str]]
    schema_ddl: str
    dictionary_rows: str
    intents: Mapping[str, Any]
    task_manifest: Mapping[str, Any]
    task_templates: Mapping[str, Any]
    counter_evidence: Mapping[str, Any]
    semantic_invariants: tuple[SemanticInvariant, ...]
    gateway_scope: Mapping[str, Any]
    package_path: Path
    asset_paths: Mapping[str, Path]
    package_sha256: str


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read domain asset {path}: {exc}") from exc


def _exact_mapping(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} fields must be exactly {sorted(fields)}")
    return value


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _identifier(value: Any, field: str) -> str:
    text = _non_empty_string(value, field)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise ValueError(f"{field} must be a SQL identifier")
    return text


def _string_tuple(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ValueError(f"{field} must be {qualifier}")
    parsed = tuple(_non_empty_string(item, field) for item in value)
    if len({item.casefold() for item in parsed}) != len(parsed):
        raise ValueError(f"{field} must not contain duplicates")
    return parsed


def _semantic_invariants(raw: Any) -> tuple[SemanticInvariant, ...]:
    root = _exact_mapping(
        raw,
        SEMANTIC_INVARIANT_ROOT_FIELDS,
        "semantic_invariants",
    )
    schema_version = root["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ValueError("semantic_invariants.schema_version must be 1")
    raw_items = root["invariants"]
    if not isinstance(raw_items, list):
        raise ValueError("semantic_invariants.invariants must be a list")

    parsed: list[SemanticInvariant] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        label = f"semantic_invariants.invariants[{index}]"
        item = _exact_mapping(raw_item, SEMANTIC_INVARIANT_FIELDS, label)
        invariant_id = _non_empty_string(item["invariant_id"], f"{label}.invariant_id")
        folded_id = invariant_id.casefold()
        if folded_id in seen_ids:
            raise ValueError("semantic_invariants invariant_id values must be unique")
        seen_ids.add(folded_id)
        metric = _identifier(item["metric"], f"{label}.metric")
        mode = _non_empty_string(item["mode"], f"{label}.mode")
        if mode not in {"ratio", "stored"}:
            raise ValueError(f"{label}.mode must be ratio|stored")
        knowledge_points = _string_tuple(
            item["knowledge_points"], f"{label}.knowledge_points"
        )
        trigger_terms = _string_tuple(
            item["trigger_terms"], f"{label}.trigger_terms"
        )
        numerator_terms = _string_tuple(
            item["numerator_terms"],
            f"{label}.numerator_terms",
            allow_empty=True,
        )
        denominator_terms = _string_tuple(
            item["denominator_terms"],
            f"{label}.denominator_terms",
            allow_empty=True,
        )
        canonical_expression = item["canonical_expression"]
        display_expression = item["display_expression"]
        if mode == "ratio":
            canonical_expression = _non_empty_string(
                canonical_expression, f"{label}.canonical_expression"
            )
            display_expression = _non_empty_string(
                display_expression, f"{label}.display_expression"
            )
            if not numerator_terms or not denominator_terms:
                raise ValueError(
                    f"{label} ratio semantic_invariants require numerator_terms "
                    "and denominator_terms"
                )
        else:
            if canonical_expression is not None or display_expression is not None:
                raise ValueError(
                    f"{label} stored semantic_invariants must not declare a formula"
                )
            if numerator_terms or denominator_terms:
                raise ValueError(
                    f"{label} stored semantic_invariants must use empty ratio terms"
                )

        parsed.append(
            SemanticInvariant(
                invariant_id=invariant_id,
                metric=metric,
                mode=mode,
                knowledge_points=knowledge_points,
                trigger_terms=trigger_terms,
                canonical_expression=canonical_expression,
                display_expression=display_expression,
                canonical_claim=_non_empty_string(
                    item["canonical_claim"], f"{label}.canonical_claim"
                ),
                evidence_ref=_non_empty_string(
                    item["evidence_ref"], f"{label}.evidence_ref"
                ),
                evidence_quote=_non_empty_string(
                    item["evidence_quote"], f"{label}.evidence_quote"
                ),
                numerator_terms=numerator_terms,
                denominator_terms=denominator_terms,
            )
        )
    return tuple(parsed)


def _resolve_package(value: str | Path | None) -> Path:
    selected: str | Path = value or os.environ.get(DOMAIN_CONFIG_ENV, DEFAULT_DOMAIN_ID)
    candidate = Path(selected)
    if (
        not candidate.is_absolute()
        and len(candidate.parts) == 1
        and _DOMAIN_ID_RE.fullmatch(str(selected))
    ):
        candidate = DOMAIN_ROOT / candidate
    elif not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"domain package does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError(f"domain package is not a directory: {resolved}")
    return resolved


def _asset_paths(package: Path, raw: Any) -> dict[str, Path]:
    if not isinstance(raw, dict) or tuple(raw) != ASSET_KEYS:
        raise ValueError(f"manifest assets must be ordered as {ASSET_KEYS}")
    parsed: dict[str, Path] = {}
    for key in ASSET_KEYS:
        name = _non_empty_string(raw[key], f"assets.{key}")
        relative = Path(name)
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name != name:
            raise ValueError(f"asset path for {key} must be one package-local filename")
        path = (package / relative).resolve()
        if path.parent != package:
            raise ValueError(f"asset path for {key} escapes the package")
        parsed[key] = path
    if len(set(parsed.values())) != len(parsed):
        raise ValueError("manifest assets must reference distinct files")
    return parsed


def _table_columns(raw: Any) -> Mapping[str, frozenset[str]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("schema tables must be a non-empty object")
    parsed: dict[str, frozenset[str]] = {}
    seen_tables: set[str] = set()
    for raw_table, raw_columns in raw.items():
        table = _identifier(raw_table, "table name")
        folded_table = table.casefold()
        if folded_table in seen_tables:
            raise ValueError(f"duplicate table name ignoring case: {table}")
        seen_tables.add(folded_table)
        if not isinstance(raw_columns, list) or not raw_columns:
            raise ValueError(f"table {table} columns must be a non-empty list")
        columns: set[str] = set()
        for raw_column in raw_columns:
            column = _identifier(raw_column, f"table {table} column")
            folded_column = column.casefold()
            if folded_column in columns:
                raise ValueError(
                    f"duplicate column name ignoring case in table {table}: {column}"
                )
            columns.add(folded_column)
        parsed[folded_table] = frozenset(columns)
    return MappingProxyType(parsed)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _package_hash(package: Path, paths: Mapping[str, Path]) -> str:
    digest = hashlib.sha256()
    ordered = {"manifest": package / "manifest.json", **dict(paths)}
    for key, path in sorted(ordered.items()):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read domain asset {path}: {exc}") from exc
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def load_domain_config(value: str | Path | None = None) -> DomainConfig:
    """Load one package without importing or mutating package content."""

    package = _resolve_package(value)
    manifest = _exact_mapping(
        _read_json(package / "manifest.json"), MANIFEST_FIELDS, "manifest"
    )
    schema_version = manifest["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < 1
    ):
        raise ValueError("schema_version must be a positive integer")
    domain_id = _non_empty_string(manifest["domain_id"], "domain_id")
    if not _DOMAIN_ID_RE.fullmatch(domain_id):
        raise ValueError("domain_id must match [a-z][a-z0-9_-]*")
    database = _identifier(manifest["database"], "database")
    paths = _asset_paths(package, manifest["assets"])
    assets = {key: _read_json(path) for key, path in paths.items()}
    schema = _exact_mapping(assets["schema"], SCHEMA_FIELDS, "schema")
    for key in ASSET_KEYS[1:]:
        if not isinstance(assets[key], dict):
            raise ValueError(f"{key} asset root must be an object")
    frozen_paths = MappingProxyType(dict(paths))
    return DomainConfig(
        domain_id=domain_id,
        schema_version=schema_version,
        database=database,
        table_columns=_table_columns(schema["tables"]),
        schema_ddl=_non_empty_string(schema["prompt_ddl"], "schema.prompt_ddl"),
        dictionary_rows=_non_empty_string(
            schema["dictionary_rows"], "schema.dictionary_rows"
        ),
        intents=_deep_freeze(assets["intents"]),
        task_manifest=_deep_freeze(assets["task_manifest"]),
        task_templates=_deep_freeze(assets["task_templates"]),
        counter_evidence=_deep_freeze(assets["counter_evidence"]),
        semantic_invariants=_semantic_invariants(assets["semantic_invariants"]),
        gateway_scope=_deep_freeze(assets["gateway_scope"]),
        package_path=package,
        asset_paths=frozen_paths,
        package_sha256=_package_hash(package, paths),
    )


@lru_cache(maxsize=1)
def active_domain_config() -> DomainConfig:
    """Return the process-wide package selected before application startup."""

    return load_domain_config()
