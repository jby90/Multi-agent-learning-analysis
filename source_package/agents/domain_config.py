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
    "gateway_scope",
)
SCHEMA_FIELDS = frozenset({"tables", "prompt_ddl", "dictionary_rows"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


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
        gateway_scope=_deep_freeze(assets["gateway_scope"]),
        package_path=package,
        asset_paths=frozen_paths,
        package_sha256=_package_hash(package, paths),
    )


@lru_cache(maxsize=1)
def active_domain_config() -> DomainConfig:
    """Return the process-wide package selected before application startup."""

    return load_domain_config()
