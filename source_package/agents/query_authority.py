"""Structured task-template authority shared by generation and review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

import sqlglot
from sqlglot import exp


FAMILIES = frozenset({"Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"})
METRIC_COLUMNS = frozenset(
    {
        "plan_qty",
        "actual_qty",
        "complete_rate",
        "deviation_rate",
        "high_risk_rows",
    }
)
AUTHORITY_FIELDS = frozenset(
    {
        "source",
        "template_id",
        "family",
        "standard_stem",
        "output_columns",
        "metric_columns",
        "dimension_columns",
        "filter_columns",
        "group_by_columns",
        "time_column",
        "time_values",
    }
)
_MONTH_VALUE_RE = re.compile(r"20\d{2}-\d{2}")


@dataclass(frozen=True, slots=True)
class QueryAuthority:
    source: str
    template_id: str
    family: str
    standard_stem: str
    output_columns: tuple[str, ...]
    metric_columns: tuple[str, ...]
    dimension_columns: tuple[str, ...]
    filter_columns: tuple[str, ...]
    group_by_columns: tuple[str, ...]
    time_column: str | None
    time_values: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "template_id": self.template_id,
            "family": self.family,
            "standard_stem": self.standard_stem,
            "output_columns": list(self.output_columns),
            "metric_columns": list(self.metric_columns),
            "dimension_columns": list(self.dimension_columns),
            "filter_columns": list(self.filter_columns),
            "group_by_columns": list(self.group_by_columns),
            "time_column": self.time_column,
            "time_values": list(self.time_values),
        }


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"query_authority.{field} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"query_authority.{field} must be a string list")
    parsed = tuple(_non_empty_string(item, f"{field}[]") for item in value)
    if len(parsed) != len(set(parsed)):
        raise ValueError(f"query_authority.{field} must not contain duplicates")
    return parsed


def query_authority_from_mapping(value: Mapping[str, Any]) -> QueryAuthority:
    if set(value) != AUTHORITY_FIELDS:
        raise ValueError("query_authority fields do not match the approved shape")
    source = _non_empty_string(value["source"], "source")
    if source != "task_template":
        raise ValueError("query_authority.source must be task_template")
    family = _non_empty_string(value["family"], "family")
    if family not in FAMILIES:
        raise ValueError("query_authority.family is unsupported")
    time_column = value["time_column"]
    if time_column is not None:
        time_column = _non_empty_string(time_column, "time_column")
    authority = QueryAuthority(
        source=source,
        template_id=_non_empty_string(value["template_id"], "template_id"),
        family=family,
        standard_stem=_non_empty_string(value["standard_stem"], "standard_stem"),
        output_columns=_string_tuple(value["output_columns"], "output_columns"),
        metric_columns=_string_tuple(value["metric_columns"], "metric_columns"),
        dimension_columns=_string_tuple(
            value["dimension_columns"], "dimension_columns"
        ),
        filter_columns=_string_tuple(value["filter_columns"], "filter_columns"),
        group_by_columns=_string_tuple(
            value["group_by_columns"], "group_by_columns"
        ),
        time_column=time_column,
        time_values=_string_tuple(value["time_values"], "time_values"),
    )
    if set(authority.metric_columns) | set(authority.dimension_columns) != set(
        authority.output_columns
    ):
        raise ValueError("query_authority output columns are not fully classified")
    if not set(authority.metric_columns).issubset(METRIC_COLUMNS):
        raise ValueError("query_authority has unsupported metric columns")
    if authority.time_column is not None and authority.time_column not in set(
        authority.filter_columns
    ):
        raise ValueError("query_authority time_column must be a filter column")
    return authority


def build_query_authority(
    *,
    template_id: str,
    family: str,
    standard_stem: str,
    standard_sql: str,
) -> QueryAuthority:
    statement = sqlglot.parse_one(standard_sql, read="mysql")
    if not isinstance(statement, exp.Select):
        raise ValueError("task template standard_sql must be a SELECT")
    output_columns = tuple(name.casefold() for name in statement.named_selects)
    metric_columns = tuple(
        name for name in output_columns if name in METRIC_COLUMNS
    )
    dimension_columns = tuple(
        name for name in output_columns if name not in METRIC_COLUMNS
    )
    where = statement.args.get("where")
    group = statement.args.get("group")
    filter_columns = tuple(
        sorted(
            {
                column.name.casefold()
                for column in where.find_all(exp.Column)
            }
        )
        if isinstance(where, exp.Expression)
        else ()
    )
    group_by_columns = tuple(
        sorted(
            {
                column.name.casefold()
                for column in group.find_all(exp.Column)
            }
        )
        if isinstance(group, exp.Expression)
        else ()
    )
    time_column = next(
        (
            column
            for column in ("period_date", "batch_code")
            if column in filter_columns
        ),
        None,
    )
    return QueryAuthority(
        source="task_template",
        template_id=template_id,
        family=family,
        standard_stem=standard_stem,
        output_columns=output_columns,
        metric_columns=metric_columns,
        dimension_columns=dimension_columns,
        filter_columns=filter_columns,
        group_by_columns=group_by_columns,
        time_column=time_column,
        time_values=tuple(dict.fromkeys(_MONTH_VALUE_RE.findall(standard_stem))),
    )
