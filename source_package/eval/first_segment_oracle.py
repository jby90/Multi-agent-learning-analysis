"""Grounded first-segment case loader and read-only CSV execution oracle."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
from typing import Any, Literal

import sqlglot
from sqlglot import exp

from agents.domain_config import DomainConfig
from agents.sandbox import validate_and_rewrite


Split = Literal["calibration", "holdout"]
Cell = str | None
CASE_FIELDS = frozenset(
    {"case_id", "section", "split", "question", "expected_intent", "answer_key", "sources"}
)
ANSWER_KEY_FIELDS = frozenset(
    {
        "table",
        "selected_columns",
        "filters",
        "order_by",
        "expected_sql",
        "expected_columns",
        "expected_rows",
    }
)
SOURCE_FIELDS = frozenset({"file", "sha256", "role"})
_CASE_ID_RE = re.compile(r"^FS-SEC(?P<section>\d{3})-(?P<ordinal>0[1-5])$")
_SECTION_RE = re.compile(r"^SEC-\d{3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTEGER_RE = re.compile(r"^-?\d+$")


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    file: str
    sha256: str
    role: str


@dataclass(frozen=True, slots=True)
class AnswerKey:
    table: str
    selected_columns: tuple[str, ...]
    filters: Mapping[str, str]
    order_by: tuple[str, ...]
    expected_sql: str
    expected_columns: tuple[str, ...]
    expected_rows: tuple[tuple[Cell, ...], ...]
    formula: None = None


@dataclass(frozen=True, slots=True)
class DomainSwapCase:
    case_id: str
    section: str
    split: Split
    question: str
    expected_intent: str
    answer_key: AnswerKey
    sources: tuple[SourceFingerprint, ...]


@dataclass(frozen=True, slots=True)
class ResultSet:
    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]
    executed_sql: str


def _mapping(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} fields must be exactly {sorted(fields)}")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ValueError(f"{field} must be {qualifier}")
    parsed = tuple(_string(item, f"{field}[]") for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} must not contain duplicates")
    return parsed


def _cell(value: Any, field: str) -> Cell:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} must be a string, number, or null")
    return str(value)


def _answer_key(value: Any, case_id: str) -> AnswerKey:
    raw = _mapping(value, ANSWER_KEY_FIELDS, f"{case_id}.answer_key")
    table = _string(raw["table"], f"{case_id}.answer_key.table")
    if not _IDENTIFIER_RE.fullmatch(table):
        raise ValueError(f"{case_id}.answer_key.table must be a SQL identifier")
    selected_columns = _strings(
        raw["selected_columns"], f"{case_id}.answer_key.selected_columns"
    )
    filters_raw = raw["filters"]
    if not isinstance(filters_raw, Mapping):
        raise ValueError(f"{case_id}.answer_key.filters must be an object")
    filters = MappingProxyType(
        {
            _string(key, f"{case_id}.answer_key.filters key"): _string(
                item, f"{case_id}.answer_key.filters.{key}"
            )
            for key, item in filters_raw.items()
        }
    )
    expected_columns = _strings(
        raw["expected_columns"], f"{case_id}.answer_key.expected_columns"
    )
    rows_raw = raw["expected_rows"]
    if not isinstance(rows_raw, (list, tuple)):
        raise ValueError(f"{case_id}.answer_key.expected_rows must be a list")
    rows: list[tuple[Cell, ...]] = []
    for row_index, row in enumerate(rows_raw):
        if not isinstance(row, (list, tuple)) or len(row) != len(expected_columns):
            raise ValueError(
                f"{case_id}.answer_key.expected_rows[{row_index}] width must match expected_columns"
            )
        rows.append(
            tuple(
                _cell(cell, f"{case_id}.answer_key.expected_rows[{row_index}][{index}]")
                for index, cell in enumerate(row)
            )
        )
    answer_key = AnswerKey(
        table=table,
        selected_columns=selected_columns,
        filters=filters,
        order_by=_strings(
            raw["order_by"], f"{case_id}.answer_key.order_by", allow_empty=True
        ),
        expected_sql=_string(raw["expected_sql"], f"{case_id}.answer_key.expected_sql"),
        expected_columns=expected_columns,
        expected_rows=tuple(rows),
    )
    _validate_answer_key_sql(answer_key, case_id)
    return answer_key


def _validate_answer_key_sql(answer_key: AnswerKey, case_id: str) -> None:
    try:
        statement = sqlglot.parse_one(answer_key.expected_sql, read="mysql")
    except (ValueError, sqlglot.errors.ParseError) as exc:
        raise ValueError(f"{case_id}.answer_key.expected_sql is invalid: {exc}") from exc
    if not isinstance(statement, exp.Select):
        raise ValueError(f"{case_id}.answer_key.expected_sql must be a SELECT")
    tables = tuple(table.name for table in statement.find_all(exp.Table))
    if tables != (answer_key.table,):
        raise ValueError(f"{case_id}.answer_key.table does not match expected_sql")
    for projection in statement.expressions:
        value = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(value, exp.Column):
            raise ValueError(
                f"{case_id}.answer_key.expected_sql must select stored fields only"
            )
    actual_outputs = tuple(name.casefold() for name in statement.named_selects)
    declared_outputs = tuple(name.casefold() for name in answer_key.selected_columns)
    if actual_outputs != declared_outputs:
        raise ValueError(
            f"{case_id}.answer_key.selected_columns do not match expected_sql"
        )
    where = statement.args.get("where")
    actual_filters = {
        column.name.casefold()
        for column in where.find_all(exp.Column)
    } if isinstance(where, exp.Expression) else set()
    declared_filters = {name.casefold() for name in answer_key.filters}
    if actual_filters != declared_filters:
        raise ValueError(f"{case_id}.answer_key.filters do not match expected_sql")
    order = statement.args.get("order")
    actual_order = (
        tuple(item.sql(dialect="mysql").casefold() for item in order.expressions)
        if isinstance(order, exp.Order)
        else ()
    )
    declared_order = tuple(item.casefold() for item in answer_key.order_by)
    if actual_order != declared_order:
        raise ValueError(f"{case_id}.answer_key.order_by does not match expected_sql")


def _sources(value: Any, case_id: str) -> tuple[SourceFingerprint, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{case_id}.sources must be a non-empty list")
    parsed: list[SourceFingerprint] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        raw = _mapping(item, SOURCE_FIELDS, f"{case_id}.sources[{index}]")
        name = _string(raw["file"], f"{case_id}.sources[{index}].file")
        path = Path(name)
        if path.is_absolute() or len(path.parts) != 1 or path.name != name:
            raise ValueError(f"{case_id}.sources[{index}].file must be one filename")
        sha256 = _string(raw["sha256"], f"{case_id}.sources[{index}].sha256")
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError(f"{case_id}.sources[{index}].sha256 must be lowercase SHA-256")
        if name in seen:
            raise ValueError(f"{case_id}.sources contains duplicate file {name}")
        seen.add(name)
        parsed.append(
            SourceFingerprint(
                file=name,
                sha256=sha256,
                role=_string(raw["role"], f"{case_id}.sources[{index}].role"),
            )
        )
    return tuple(parsed)


def _parse_case(value: Any, line_number: int) -> DomainSwapCase:
    raw = _mapping(value, CASE_FIELDS, f"case line {line_number}")
    case_id = _string(raw["case_id"], f"case line {line_number}.case_id")
    match = _CASE_ID_RE.fullmatch(case_id)
    if match is None:
        raise ValueError(f"invalid first-segment case ID: {case_id}")
    section = _string(raw["section"], f"{case_id}.section")
    if not _SECTION_RE.fullmatch(section) or section != f"SEC-{match.group('section')}":
        raise ValueError(f"{case_id}.section does not match its case ID")
    split = raw["split"]
    if split not in {"calibration", "holdout"}:
        raise ValueError(f"{case_id}.split must be calibration|holdout")
    return DomainSwapCase(
        case_id=case_id,
        section=section,
        split=split,
        question=_string(raw["question"], f"{case_id}.question"),
        expected_intent=_string(raw["expected_intent"], f"{case_id}.expected_intent"),
        answer_key=_answer_key(raw["answer_key"], case_id),
        sources=_sources(raw["sources"], case_id),
    )


def _verify_source_hashes(
    cases: Sequence[DomainSwapCase], raw_root: Path
) -> None:
    root = raw_root.resolve(strict=True)
    expected: dict[str, str] = {}
    for case in cases:
        for source in case.sources:
            previous = expected.setdefault(source.file, source.sha256)
            if previous != source.sha256:
                raise ValueError(f"conflicting source hashes for {source.file}")
    for name, expected_hash in expected.items():
        path = (root / name).resolve(strict=True)
        if path.parent != root:
            raise ValueError(f"source path escapes raw root: {name}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"source hash mismatch for {name}")


def load_first_segment_cases(
    path: str | Path,
    *,
    raw_root: str | Path | None = None,
) -> tuple[DomainSwapCase, ...]:
    """Load the frozen 10x5 matrix and optionally attest every raw source."""

    case_path = Path(path)
    try:
        lines = case_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read first-segment matrix {case_path}: {exc}") from exc
    cases: list[DomainSwapCase] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on case line {line_number}: {exc}") from exc
        cases.append(_parse_case(raw, line_number))

    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("first-segment case IDs must be unique")
    if len(cases) != 50:
        raise ValueError("first-segment matrix must contain exactly 50 cases")
    section_counts = Counter(case.section for case in cases)
    expected_sections = {f"SEC-{index:03d}" for index in range(1, 11)}
    if set(section_counts) != expected_sections or set(section_counts.values()) != {5}:
        raise ValueError("first-segment matrix must contain five cases in each SEC-001..010")
    calibration = Counter(
        case.section for case in cases if case.split == "calibration"
    )
    if set(calibration) != expected_sections or set(calibration.values()) != {2}:
        raise ValueError("each first-segment section must have two calibration cases")
    if raw_root is not None:
        _verify_source_hashes(cases, Path(raw_root))
    return tuple(cases)


def _sqlite_type(values: Sequence[str | None]) -> str:
    present = [value for value in values if value not in {None, ""}]
    if not present:
        return "TEXT"
    if all(_INTEGER_RE.fullmatch(value) for value in present):
        return "INTEGER"
    try:
        for value in present:
            Decimal(value)
    except InvalidOperation:
        return "TEXT"
    return "REAL"


def _coerce(value: str, sqlite_type: str) -> str | int | float | None:
    if value == "":
        return None
    if sqlite_type == "INTEGER":
        return int(value)
    if sqlite_type == "REAL":
        return float(value)
    return value


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"unsafe CSV identifier: {value}")
    return f'"{value}"'


def _normalize_cell(value: Any) -> Cell:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".15g")
    if isinstance(value, (str, bytes)):
        return value.decode("utf-8") if isinstance(value, bytes) else value
    raise ValueError(f"unsupported SQLite result cell: {type(value).__name__}")


class CsvOracle:
    """Execute sandbox-approved SQL over package-whitelisted raw CSV tables."""

    def __init__(self, raw_root: str | Path) -> None:
        self._raw_root = Path(raw_root).resolve(strict=True)
        self._connection: sqlite3.Connection | None = None
        self._loaded_package_hash: str | None = None

    def _connection_for(self, domain_config: DomainConfig) -> sqlite3.Connection:
        if (
            self._connection is not None
            and self._loaded_package_hash == domain_config.package_sha256
        ):
            return self._connection
        if self._connection is not None:
            self._connection.close()
        connection = sqlite3.connect(":memory:")
        try:
            for table, allowed_columns in domain_config.table_columns.items():
                if not table.startswith("dwr_mps_"):
                    raise ValueError(
                        f"CSV source is missing for non-raw package table {table}"
                    )
                source = (self._raw_root / f"{table}.csv").resolve()
                if source.parent != self._raw_root or not source.is_file():
                    raise ValueError(f"CSV source is missing: {source.name}")
                with source.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    headers = reader.fieldnames
                    if headers is None or len(headers) != len(set(item.casefold() for item in headers)):
                        raise ValueError(f"invalid or duplicate CSV headers in {source.name}")
                    if {item.casefold() for item in headers} != set(allowed_columns):
                        raise ValueError(f"CSV headers do not match package schema for {table}")
                    rows = list(reader)
                column_types = {
                    header: _sqlite_type([row[header] for row in rows]) for header in headers
                }
                columns_sql = ", ".join(
                    f"{_quote_identifier(header)} {column_types[header]}" for header in headers
                )
                connection.execute(
                    f"CREATE TABLE {_quote_identifier(table)} ({columns_sql})"
                )
                placeholders = ", ".join("?" for _ in headers)
                insert_sql = (
                    f"INSERT INTO {_quote_identifier(table)} "
                    f"VALUES ({placeholders})"
                )
                connection.executemany(
                    insert_sql,
                    [
                        tuple(_coerce(row[header], column_types[header]) for header in headers)
                        for row in rows
                    ],
                )
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._connection = connection
        self._loaded_package_hash = domain_config.package_sha256
        return connection

    def execute(self, sql: str, domain_config: DomainConfig) -> ResultSet:
        decision = validate_and_rewrite(sql, domain_config=domain_config)
        if not decision.allowed or decision.executed_sql is None:
            raise ValueError(
                f"sandbox rejected oracle SQL ({decision.rule_id}): {decision.reason}"
            )
        connection = self._connection_for(domain_config)
        try:
            cursor = connection.execute(decision.executed_sql)
            raw_rows = cursor.fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"CSV oracle SQL failed: {exc}") from exc
        columns = tuple(item[0] for item in (cursor.description or ()))
        rows = tuple(tuple(_normalize_cell(cell) for cell in row) for row in raw_rows)
        return ResultSet(
            columns=columns,
            rows=rows,
            executed_sql=decision.executed_sql,
        )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._loaded_package_hash = None

    def __enter__(self) -> "CsvOracle":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
